"""An async splunkd REST client, shaped by what a load generator does to it.

A search generator is an unusual REST client. It opens thousands of short
requests to exactly one splunkd, most of them job polls of a few hundred bytes,
and it does so from hundreds of coroutines at once. Three consequences drive
every decision in this module.

**One client per process, never one per virtual user.** httpx keeps a
connection pool on the client, so sharing it is what gives keep-alive and
multiplexing. A client per virtual user pays a TCP and TLS handshake per
search, and a handshake against splunkd over TLS is comfortably more expensive
than the poll it is carrying. Measure that and you have benchmarked OpenSSL on
the load generator, not Splunk.

**A session key expires, so the re-login has to be concurrency safe.** Splunk's
default session lifetime is about an hour, which a long soak test will cross.
When it does, every request in flight gets a 401 at roughly the same instant.
The naive fix (re-login on 401) then fires five hundred logins into an
authentication path that is one of the most expensive endpoints splunkd has,
which is a self-inflicted denial of service in the middle of a measurement. The
lock plus generation counter below turns that storm into a single login. Bearer
tokens do not expire this way, which is why they are the recommended
credential, and there a 401 is fatal rather than retryable.

**Failures are data, not exceptions to be logged and swallowed.** This module
raises precise, typed errors and the engine above it turns them into error
classes on a step record. The one thing it must never do is retry silently:
a hidden retry inflates the load on the target and deflates the latency being
reported, which corrupts both halves of the measurement at once.
"""

from __future__ import annotations

import asyncio
import json
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .config import TargetConfig
from .timepolicy import TimeWindow

# Endpoints that are never namespaced. Splunk exposes these outside any app
# context and asking for them under servicesNS is at best pointless and at
# worst a 404 on an older release.
_LOGIN_PATH = "/services/auth/login"
_SERVER_INFO_PATH = "/services/server/info"
_LIMITS_PATH = "/services/configs/conf-limits/search"
_INDEXES_PATH = "/services/data/indexes"


class SplunkError(Exception):
    """Anything that went wrong talking to splunkd."""


class SplunkAuthError(SplunkError):
    """Credentials were rejected: a 401 or a 403.

    Unrecoverable by design. A load test that keeps hammering a target with a
    credential it has already refused produces thousands of identical records
    that say nothing, and on a real stack it will trip the account lockout that
    then blocks the operator from logging in to investigate.
    """


class SplunkHttpError(SplunkError):
    """A non-2xx that is not an authentication failure."""

    def __init__(self, status: int, body: str, context: str = "") -> None:
        self.status = status
        self.body = body
        where = f"{context}: " if context else ""
        # splunkd error bodies are frequently several kilobytes of XML or a
        # stack of messages. Truncate for the exception string, keep the whole
        # thing on ``.body`` for anyone who wants it.
        detail = " ".join(body.split())[:500]
        super().__init__(f"{where}splunkd returned HTTP {status}: {detail}")


class SplunkTimeout(SplunkError):
    """The request exceeded its connect or read timeout.

    A distinct type because a timeout is a completely different diagnosis from
    a 503: the target accepted the work and did not finish it in time, which is
    the signature of a saturated search head rather than a rejected request.
    """


@dataclass
class JobStatus:
    """One poll of a search job's REST record.

    Everything numeric is optional because splunkd populates these fields
    progressively: a job that is still QUEUED has no ``runDuration`` and a job
    that failed to start may never report a ``scanCount``. ``None`` means "not
    reported", which is different from zero and must stay different, otherwise
    an aggregate of scan counts quietly under-reports.
    """

    sid: str
    dispatch_state: str = ""
    is_done: bool = False
    is_failed: bool = False
    is_finalized: bool = False
    done_progress: float = 0.0
    run_duration_s: Optional[float] = None
    scan_count: Optional[int] = None
    event_count: Optional[int] = None
    result_count: Optional[int] = None
    result_preview_count: Optional[int] = None
    messages: List[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.is_done or self.is_failed


def _as_float(value: Any) -> Optional[float]:
    """Coerce a splunkd field to a float, or ``None`` if it will not go.

    splunkd hands back most numerics as JSON *strings* (``"scanCount": "1024"``),
    and which fields do that has changed between releases. Coercing defensively
    in one helper is much safer than sprinkling ``int(...)`` at the call sites
    and discovering on a different Splunk version that one of them throws in
    the middle of a run.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    return None if number is None else int(number)


def _as_bool(value: Any) -> bool:
    """splunkd's booleans arrive as ``true``, ``"1"``, ``"0"`` or ``"false"``.

    Note that ``bool("0")`` is ``True`` in Python, so the obvious one-liner is
    wrong in exactly the case that matters: a job that is not done would read
    as done and the poll loop would exit early with no results.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    lowered = str(value).strip().lower()
    return lowered in ("1", "true", "yes", "on", "t")


def _payload_messages(payload: Any) -> List[str]:
    """Pull the human-readable messages out of a splunkd JSON body."""
    messages: List[str] = []
    if not isinstance(payload, dict):
        return messages
    for message in payload.get("messages") or []:
        if isinstance(message, dict):
            text = str(message.get("text", "")).strip()
            kind = str(message.get("type", "")).strip()
            if text:
                messages.append(f"{kind}: {text}" if kind else text)
        elif message:
            messages.append(str(message))
    return messages


def _describe(response: httpx.Response) -> str:
    """The most useful one-line description of a failed response we can build."""
    body = response.text or ""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return " ".join(body.split())[:500]
    messages = _payload_messages(payload)
    if messages:
        return "; ".join(messages)[:500]
    return " ".join(body.split())[:500]


def _first_content(payload: Any) -> Dict[str, Any]:
    """``entry[0].content`` from an Atom-shaped JSON response, or an empty dict."""
    if not isinstance(payload, dict):
        return {}
    entries = payload.get("entry")
    if not isinstance(entries, list) or not entries:
        return {}
    content = entries[0].get("content") if isinstance(entries[0], dict) else None
    return content if isinstance(content, dict) else {}


class SplunkClient:
    """An async splunkd client, safe to share across every virtual user."""

    def __init__(
        self,
        target: TargetConfig,
        *,
        connect_timeout_s: float = 10.0,
        read_timeout_s: float = 300.0,
        http2: bool = False,
        max_connections: int = 512,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._target = target
        self._base_url = target.url.rstrip("/")
        self._transport = transport
        self._http2 = http2
        self._max_connections = max(1, int(max_connections))

        self._timeout = httpx.Timeout(
            connect=connect_timeout_s,
            read=read_timeout_s,
            write=connect_timeout_s,
            pool=connect_timeout_s,
        )

        self._client: Optional[httpx.AsyncClient] = None
        self._closed = False

        # Session-mode state. ``_auth_generation`` is what makes the re-login
        # safe under concurrency: a coroutine that gets a 401 asks for a refresh
        # quoting the generation it used, and the refresh is a no-op if someone
        # else has already moved the generation on. Five hundred simultaneous
        # 401s therefore produce one login and four hundred and ninety-nine
        # immediate returns.
        self._session_key: Optional[str] = None
        self._auth_generation = 0
        self._auth_lock = asyncio.Lock()

        # Namespaced paths. Derived from api_version rather than by string
        # surgery on target.jobs_path, because that property is the plain
        # /services form and rewriting it into servicesNS is exactly the kind of
        # incidental string manipulation that breaks the day someone adds a v3.
        version_suffix = "search/v2" if target.api_version == "v2" else "search"
        if target.owner == "nobody" and target.app == "search":
            # The common case: keep the plain path so what Regulator dispatched
            # is byte-identical to what an operator would type into a browser
            # when they go looking for the job.
            self._jobs_path = target.jobs_path
            self._parser_path = f"/services/{version_suffix}/parser"
        else:
            ns = f"/servicesNS/{quote(target.owner, safe='')}/{quote(target.app, safe='')}"
            self._jobs_path = f"{ns}/{version_suffix}/jobs"
            self._parser_path = f"{ns}/{version_suffix}/parser"

        if not target.verify_tls:
            # Once, here, rather than per request. A load test issuing tens of
            # thousands of requests against a self-signed lab stack would
            # otherwise print tens of thousands of identical warnings, which
            # buries the one line that actually mattered. urllib3 is not a
            # dependency of this worker but it is often present transitively, so
            # the import is guarded.
            warnings.filterwarnings("ignore", message="Unverified HTTPS request")
            try:  # pragma: no cover - depends on what else is installed
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def target(self) -> TargetConfig:
        return self._target

    @property
    def auth_method(self) -> str:
        return "token" if self._target.token else "session"

    @property
    def jobs_path(self) -> str:
        return self._jobs_path

    async def start(self) -> None:
        """Build the connection pool and, in session mode, log in once.

        Logging in eagerly rather than lazily is deliberate: it means a wrong
        password fails at start-up, in a single obvious place, instead of on the
        first virtual user's first search where it looks like a step failure.
        """
        if self._closed:
            raise SplunkError("this SplunkClient has been closed and cannot be reused")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                verify=self._target.verify_tls,
                timeout=self._timeout,
                http2=self._http2,
                transport=self._transport,
                limits=httpx.Limits(
                    max_connections=self._max_connections,
                    max_keepalive_connections=self._max_connections,
                ),
                # Redirects off on purpose. splunkd redirects an unauthenticated
                # request to a login page, and following that turns a crisp 401
                # into a 200 full of HTML that then fails to parse as JSON.
                follow_redirects=False,
            )
        if not self._target.token and self._session_key is None:
            await self._refresh_session(self._auth_generation)

    async def close(self) -> None:
        """Release the pool. Safe to call twice."""
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> "SplunkClient":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    async def _refresh_session(self, seen_generation: int) -> None:
        """Log in, unless another coroutine already did it for us."""
        async with self._auth_lock:
            if self._auth_generation != seen_generation:
                # Someone else refreshed while we were queued on the lock. Their
                # key is as good as one we would mint now, so take it.
                return
            client = self._require_client()
            try:
                response = await client.post(
                    _LOGIN_PATH,
                    data={
                        "username": self._target.username or "",
                        "password": self._target.password or "",
                    },
                    params={"output_mode": "json"},
                )
            except httpx.TimeoutException as exc:
                raise SplunkTimeout(f"timed out logging in to {self._base_url}") from exc
            except httpx.HTTPError as exc:
                raise SplunkError(f"could not reach {self._base_url}{_LOGIN_PATH}: {exc}") from exc

            if response.status_code in (401, 403):
                raise SplunkAuthError(
                    f"login rejected for user {self._target.username!r}: "
                    f"{_describe(response)}"
                )
            if response.status_code >= 400:
                raise SplunkHttpError(response.status_code, response.text, context="login")

            try:
                key = str((response.json() or {}).get("sessionKey") or "").strip()
            except ValueError as exc:
                raise SplunkError(
                    f"login returned a body that is not JSON: {response.text[:200]!r}"
                ) from exc
            if not key:
                raise SplunkAuthError("login succeeded but returned no sessionKey")

            self._session_key = key
            self._auth_generation += 1

    def _auth_header(self) -> Dict[str, str]:
        if self._target.token:
            return {"Authorization": f"Bearer {self._target.token}"}
        if self._session_key:
            return {"Authorization": f"Splunk {self._session_key}"}
        return {}

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise SplunkError("SplunkClient.start() has not been called")
        return self._client

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        context: str = "",
        tolerate: Tuple[int, ...] = (),
    ) -> httpx.Response:
        """One request, with at most one re-login, and no other retries.

        The absence of a general retry is a feature. Retrying a dispatch that
        timed out means the target is now running two copies of a search that
        was already too slow, and the record would report the second attempt's
        latency as though it were the first's.

        ``tolerate`` names statuses the caller wants handed back untouched. It
        exists for exactly one case: a 403 on an optional endpoint, where "you
        may not read this" is an answer rather than a failure.
        """
        if self._client is None:
            await self.start()
        client = self._require_client()

        query: Dict[str, Any] = {"output_mode": "json"}
        if params:
            query.update({k: v for k, v in params.items() if v is not None})

        retried = False
        while True:
            generation = self._auth_generation
            try:
                response = await client.request(
                    method,
                    path,
                    params=query,
                    data=data,
                    headers=self._auth_header(),
                )
            except httpx.TimeoutException as exc:
                raise SplunkTimeout(
                    f"{context or method + ' ' + path} timed out against {self._base_url}"
                ) from exc
            except httpx.HTTPError as exc:
                raise SplunkError(
                    f"{context or method + ' ' + path} failed against {self._base_url}: {exc}"
                ) from exc

            if response.status_code not in (401, 403) or response.status_code in tolerate:
                return response

            if self._target.token:
                # A bearer token does not expire on the hour, so a 401 here
                # means the token is wrong, revoked or lacks the capability.
                # Retrying would generate noise forever without ever succeeding.
                raise SplunkAuthError(
                    f"{context or path}: bearer token rejected with HTTP "
                    f"{response.status_code}: {_describe(response)}"
                )
            if response.status_code == 403 or retried:
                raise SplunkAuthError(
                    f"{context or path}: rejected with HTTP {response.status_code} "
                    f"after re-authenticating: {_describe(response)}"
                )

            # Session mode, first 401: assume the key aged out and refresh once.
            retried = True
            await self._refresh_session(generation)

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Tuple[Any, httpx.Response]:
        response = await self._request(
            method, path, params=params, data=data, context=context
        )
        if response.status_code >= 400:
            raise SplunkHttpError(response.status_code, response.text, context=context or path)
        if not response.content:
            return None, response
        try:
            return response.json(), response
        except ValueError as exc:
            raise SplunkError(
                f"{context or path}: expected JSON, got {response.text[:200]!r}"
            ) from exc

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    async def server_info(self) -> Dict[str, Any]:
        """The content of ``/services/server/info``: version, build, cores, roles."""
        payload, _ = await self._json("GET", _SERVER_INFO_PATH, context="server info")
        return _first_content(payload)

    async def entries(self, path: str, context: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Every ``entry`` from a collection endpoint, as name plus content.

        Used by the target report to describe a cluster nobody has benchmarked
        before. Everything it asks for is optional, so an absent endpoint or a
        capability this account lacks comes back as an empty list rather than
        an exception: a load-test account with no admin rights should still be
        able to say what it can see.

        503 is in the tolerated set, which looks wrong and is not. Splunk
        answers 503 for an endpoint that is *not enabled on this node*, for
        example ``/services/shcluster/member/info`` on an instance that is not
        in a search head cluster. That is a statement about configuration, not
        about load, and it is the normal answer for most of what this method
        asks about.
        """
        try:
            payload, _ = await self._json(
                "GET", path, params={"count": "0", **(params or {})}, context=context
            )
        except SplunkHttpError as exc:
            if exc.status in (401, 403, 404, 503):
                return []
            raise
        entries = (payload or {}).get("entry") or []
        return [
            {"name": entry.get("name"), **(entry.get("content") or {})}
            for entry in entries
            if isinstance(entry, dict)
        ]

    async def post_action(self, path: str, context: str = "") -> Dict[str, Any]:
        """POST to an EAI action endpoint that takes no body.

        Splunk's admin endpoints expose custom actions this way, and they are
        POST-only: a GET answers "All custom actions of this endpoint require
        POST". Used for SmartStore cache eviction.
        """
        payload, _ = await self._json("POST", path, data={}, context=context or path)
        return payload or {}

    async def search_limits(self) -> Dict[str, Any]:
        """``limits.conf [search]`` as splunkd sees it, or ``{}`` if it is not readable.

        This feeds the concurrency ceiling line on the report's chart, which is
        a nicety rather than a requirement. Plenty of sensible load-test
        accounts have no capability to read configuration, and refusing to run
        because of that would be an own goal: the run is still valid, the chart
        just loses one annotation.
        """
        response = await self._request(
            "GET", _LIMITS_PATH, context="search limits", tolerate=(403,)
        )
        if response.status_code == 403:
            return {}
        if response.status_code >= 400:
            raise SplunkHttpError(response.status_code, response.text, context="search limits")
        try:
            return _first_content(response.json())
        except ValueError:
            return {}

    async def parse_spl(self, spl: str) -> Tuple[bool, str]:
        """Ask splunkd to parse a search without running it.

        This is the cheapest possible online lint. It catches a renamed macro, a
        command that only exists on the developer's laptop, or a typo, before a
        twenty-minute run produces twenty minutes of identical failures.
        """
        if self._target.api_version == "v2":
            response = await self._request(
                "POST", self._parser_path, data={"q": spl}, context="parse"
            )
        else:
            # The v1 parser is a GET with the search in the query string.
            response = await self._request(
                "GET", self._parser_path, params={"q": spl, "parse_only": "true"},
                context="parse",
            )
        if response.status_code < 400:
            return True, ""
        return False, _describe(response) or f"parser returned HTTP {response.status_code}"

    async def index_exists(self, name: str) -> bool:
        """True when the index is present and visible to this account.

        A missing index is the single most common reason a scenario runs
        perfectly and measures nothing: every search returns zero events in
        milliseconds and the report looks like a spectacularly fast cluster.
        """
        response = await self._request(
            "GET",
            f"{_INDEXES_PATH}/{quote(name, safe='')}",
            context=f"index {name}",
        )
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise SplunkHttpError(
                response.status_code, response.text, context=f"index {name}"
            )
        return True

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    async def create_job(
        self,
        spl: str,
        window: TimeWindow,
        *,
        exec_mode: str = "normal",
        adhoc_search_level: Optional[str] = None,
        auto_cancel_s: Optional[int] = None,
    ) -> Tuple[str, float, int]:
        """Dispatch a search, returning ``(sid, dispatch_ms, http_status)``.

        ``dispatch_ms`` is measured around the POST and nothing else. It is one
        of the most diagnostic numbers Regulator records: when a search head is
        under pressure the time to *accept* a search climbs long before the time
        to run one does, so a rising dispatch time with flat run durations is
        the earliest visible sign of admission-control pressure.
        """
        form: Dict[str, Any] = {"search": spl, "exec_mode": exec_mode}
        form.update(window.as_args())
        if adhoc_search_level:
            form["adhoc_search_level"] = adhoc_search_level
        if auto_cancel_s:
            # Insurance against a runaway search outliving the run that started
            # it. splunkd cancels the job itself if nobody touches it.
            form["auto_cancel"] = str(int(auto_cancel_s))

        started = time.perf_counter()
        response = await self._request(
            "POST", self._jobs_path, data=form, context="dispatch"
        )
        dispatch_ms = (time.perf_counter() - started) * 1000.0

        if response.status_code >= 400:
            raise SplunkHttpError(response.status_code, response.text, context="dispatch")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SplunkError(
                f"dispatch returned a body that is not JSON: {response.text[:200]!r}"
            ) from exc

        sid = ""
        if isinstance(payload, dict):
            sid = str(payload.get("sid") or "").strip()
            if not sid:
                # Some versions answer the dispatch with the whole job entry.
                sid = str(_first_content(payload).get("sid") or "").strip()
        if not sid:
            raise SplunkError(f"dispatch returned no sid: {response.text[:200]!r}")
        return sid, dispatch_ms, response.status_code

    async def job_status(self, sid: str) -> JobStatus:
        """One poll of a job's REST record."""
        payload, _ = await self._json(
            "GET", f"{self._jobs_path}/{quote(sid, safe='')}", context=f"job {sid}"
        )
        content = _first_content(payload)
        return JobStatus(
            sid=str(content.get("sid") or sid),
            dispatch_state=str(content.get("dispatchState") or ""),
            is_done=_as_bool(content.get("isDone")),
            is_failed=_as_bool(content.get("isFailed")),
            is_finalized=_as_bool(content.get("isFinalized")),
            done_progress=_as_float(content.get("doneProgress")) or 0.0,
            run_duration_s=_as_float(content.get("runDuration")),
            scan_count=_as_int(content.get("scanCount")),
            event_count=_as_int(content.get("eventCount")),
            result_count=_as_int(content.get("resultCount")),
            result_preview_count=_as_int(content.get("resultPreviewCount")),
            messages=_payload_messages(content),
        )

    async def _fetch(
        self, sid: str, count: int, endpoint: str
    ) -> Tuple[List[Dict[str, Any]], int]:
        response = await self._request(
            "GET",
            f"{self._jobs_path}/{quote(sid, safe='')}/{endpoint}",
            params={"count": str(int(count))},
            context=f"job {sid} {endpoint}",
        )
        if response.status_code == 204 or not response.content:
            # Not an error: splunkd answers 204 while the results are not ready.
            # A preview asked for during PARSING lands here on every run, so
            # raising would turn a normal condition into a flood of failures.
            return [], 0
        if response.status_code >= 400:
            raise SplunkHttpError(
                response.status_code, response.text, context=f"job {sid} {endpoint}"
            )
        byte_count = len(response.content)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SplunkError(
                f"job {sid} {endpoint}: expected JSON, got {response.text[:200]!r}"
            ) from exc
        rows = payload.get("results") if isinstance(payload, dict) else None
        return (list(rows) if isinstance(rows, list) else []), byte_count

    async def fetch_results(self, sid: str, count: int) -> Tuple[List[Dict[str, Any]], int]:
        """Final results, plus the byte length of the response body.

        The byte count is recorded because payload size is part of what a
        dashboard costs: a panel returning fifty thousand rows is expensive on
        the search head, on the wire and in the browser, and only the third of
        those shows up in run duration.
        """
        return await self._fetch(sid, count, "results")

    async def fetch_preview(self, sid: str, count: int) -> Tuple[List[Dict[str, Any]], int]:
        """Preview results, which is what a real dashboard panel reads."""
        return await self._fetch(sid, count, "results_preview")

    async def delete_job(self, sid: str) -> None:
        """Delete a job artefact. A 404 is success, not failure.

        The job may already have aged out on its own TTL, or another cleanup
        may have got there first. Treating that as an error would make the
        tidy-up path noisier than the thing it is tidying.
        """
        response = await self._request(
            "DELETE", f"{self._jobs_path}/{quote(sid, safe='')}", context=f"delete job {sid}"
        )
        if response.status_code == 404:
            return
        if response.status_code >= 400:
            raise SplunkHttpError(
                response.status_code, response.text, context=f"delete job {sid}"
            )

    async def oneshot(
        self, spl: str, window: TimeWindow, count: int = 100
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Dispatch and read in one blocking call, returning ``(rows, bytes)``.

        Used for the small internal searches (capability probes, parameter
        resolvers) where there is nothing to measure and a job artefact would
        only be litter. Scenario steps should generally *not* use it, because it
        produces no job record and therefore no scan count or run duration.
        """
        form: Dict[str, Any] = {
            "search": spl,
            "exec_mode": "oneshot",
            "count": str(int(count)),
        }
        form.update(window.as_args())
        response = await self._request(
            "POST", self._jobs_path, data=form, context="oneshot"
        )
        if response.status_code == 204 or not response.content:
            return [], 0
        if response.status_code >= 400:
            raise SplunkHttpError(response.status_code, response.text, context="oneshot")
        byte_count = len(response.content)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SplunkError(
                f"oneshot: expected JSON, got {response.text[:200]!r}"
            ) from exc
        rows = payload.get("results") if isinstance(payload, dict) else None
        return (list(rows) if isinstance(rows, list) else []), byte_count
