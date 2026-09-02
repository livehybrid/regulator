#!/usr/bin/env python3
"""A fake splunkd: an HTTP test double for the search load generator.

CI has no real Splunk, so this module emulates enough of the splunkd REST API
that the whole generator code path (auth, dispatch, poll, read job stats, fetch
results, cancel) runs for real over a socket. It also simulates
concurrency-dependent slowdown and admission queueing, so a test can assert the
generator notices degradation once it pushes past the target's search ceiling.

Standard library only. Import it, or run it:

    from tools.fake_splunk import FakeSplunk

    with FakeSplunk(port=0, base_latency_ms=50) as splunkd:
        print(splunkd.base_url)

    $ python tools/fake_splunk.py --port 8089 --max-concurrent 6

Deliberate deviations from real splunkd, all of them to keep the double useful:

* ``/services/server/info`` needs no authentication, because our capability
  probe calls it before a token is proven good.
* ``/services/auth/login`` needs no authentication either. Real splunkd does not
  require one there and cannot: it is the endpoint that mints the credential.
  Every other endpoint (bar the ``/__stats`` and ``/__reset`` test hooks)
  demands an ``Authorization`` header.
* ``scanCount`` and ``eventCount`` hold their final, search-derived values for
  the whole life of a job rather than ticking up. Only ``resultPreviewCount``
  grows with progress, which is what the generator watches.
* Job state is derived lazily from timestamps on every poll, so tens of
  thousands of jobs cost nothing. A single background thread pumps the
  admission queue, rather than one thread per job.
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

__all__ = ["FakeSplunk", "FakeSplunkConfig", "main"]

# Both the current and the deprecated jobs endpoints, most specific first.
_JOB_PREFIXES = ("/services/search/v2/jobs", "/services/search/jobs")
_PARSER_PATHS = ("/services/search/v2/parser", "/services/search/parser")

# Endpoints that answer without an Authorization header.
_UNAUTHENTICATED = (
    "/services/server/info",
    "/services/auth/login",
    "/__stats",
    "/__reset",
)

# Paths kept out of the requests_by_path map, so that reading the counters does
# not change them.
_INTROSPECTION = ("/__stats", "/__reset")

_UNAUTHENTICATED_BODY = {
    "messages": [{"type": "WARN", "text": "call not properly authenticated"}]
}
_UNKNOWN_SID_BODY = {"messages": [{"type": "FATAL", "text": "Unknown sid."}]}
_BAD_SPL_BODY = {"messages": [{"type": "ERROR", "text": "Unknown search command"}]}

# Sentinel meaning "answer 204 with no body at all".
_NO_CONTENT = object()

_RESULT_FIELDS = [
    {"name": "_time"},
    {"name": "host"},
    {"name": "sourcetype"},
    {"name": "count"},
]


@dataclass
class FakeSplunkConfig:
    """Everything that shapes how the double behaves."""

    host: str = "127.0.0.1"
    port: int = 0
    password: str = "changeme"
    reject_token: Optional[str] = None
    strict_spl: bool = False
    max_concurrent: int = 0
    base_latency_ms: float = 200.0
    jitter_ms: float = 100.0
    per_concurrent_penalty_ms: float = 0.0
    dispatch_latency_ms: float = 20.0
    seed: Optional[int] = None
    scan_count_base: int = 100000
    fail_rate: float = 0.0
    log_requests: bool = False
    # Indexes that /services/data/indexes/<name> reports as existing. Anything
    # else 404s, so a scenario naming an index that is not there is caught by
    # the online lint exactly as it would be against a real cluster.
    indexes: Tuple[str, ...] = (
        "main",
        "_internal",
        "_audit",
        "_introspection",
        "stoker_metrics",
    )


@dataclass
class _Stats:
    jobs_created: int = 0
    jobs_done: int = 0
    jobs_deleted: int = 0
    jobs_cancelled: int = 0
    peak_concurrent: int = 0
    current_concurrent: int = 0
    queued_total: int = 0
    auth_failures: int = 0
    v1_dispatches: int = 0
    oneshots: int = 0
    requests_by_path: Dict[str, int] = field(default_factory=dict)
    searches: List[str] = field(default_factory=list)


@dataclass
class _Job:
    """One dispatched search. State is a function of these timestamps."""

    sid: str
    spl: str
    earliest: str
    latest: str
    exec_mode: str
    adhoc_search_level: str
    auto_cancel: str
    timeout: str
    api_version: str
    created_wall: float
    created_at: float
    duration_s: float
    parse_s: float
    scan_count: int
    event_count: int
    result_count: int
    will_fail: bool
    internal: bool = False
    started_at: Optional[float] = None
    finalized: bool = False
    paused_at: Optional[float] = None
    paused_total: float = 0.0
    retired: bool = False


class FakeSplunk:
    """A running fake splunkd, bound to a real socket on 127.0.0.1 by default.

    The server starts as soon as the object is built, so ``server.port`` is
    readable straight away and a test never has to guess a port or race the
    bind. Use it as a context manager, or call ``close()``.
    """

    def __init__(self, config: Optional[FakeSplunkConfig] = None, **overrides: Any) -> None:
        self.config = config or FakeSplunkConfig()
        for key, value in overrides.items():
            if not hasattr(self.config, key):
                raise TypeError(f"unknown option: {key!r}")
            setattr(self.config, key, value)

        self._lock = threading.RLock()
        self._rng = random.Random(self.config.seed)
        self._jobs: Dict[str, _Job] = {}
        self._active: List[_Job] = []
        self._pending: List[_Job] = []
        self.stats = _Stats()

        self.port: int = 0
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._serve_thread: Optional[threading.Thread] = None
        self._timer_thread: Optional[threading.Thread] = None
        self._timer_stop = threading.Event()
        self._closed = False
        self.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> "FakeSplunk":
        if self._httpd is not None:
            return self
        httpd = _FakeSplunkHTTPServer((self.config.host, self.config.port), _Handler)
        httpd.fake = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._serve_thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="fake-splunkd",
            daemon=True,
        )
        self._serve_thread.start()
        self._timer_thread = threading.Thread(
            target=self._timer_loop, name="fake-splunkd-timer", daemon=True
        )
        self._timer_thread.start()
        return self

    def close(self) -> None:
        """Stop the server threads and release the socket. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._timer_stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        for thread in (self._serve_thread, self._timer_thread):
            if thread is not None:
                thread.join(timeout=5.0)
        self._httpd = None

    def __enter__(self) -> "FakeSplunk":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def base_url(self) -> str:
        host = self.config.host
        if ":" in host:  # an IPv6 literal needs brackets in a URL
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def _timer_loop(self) -> None:
        # One thread, not one per job: it only nudges the admission queue so
        # that finished jobs free their slot even when nothing is polling.
        while not self._timer_stop.wait(0.005):
            try:
                self.pump()
            except Exception:  # pragma: no cover - the timer must never die
                pass

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------
    def pump(self, now: Optional[float] = None) -> None:
        with self._lock:
            self._pump_locked(now if now is not None else time.monotonic())

    def _pump_locked(self, now: float) -> None:
        """Retire finished jobs, admit queued ones, refresh the gauges."""
        still_active: List[_Job] = []
        for job in self._active:
            if self._is_complete(job, now):
                if not job.retired:
                    job.retired = True
                    if not job.internal and not job.will_fail:
                        self.stats.jobs_done += 1
            else:
                still_active.append(job)
        self._active = still_active

        limit = self.config.max_concurrent
        while self._pending and (limit <= 0 or len(self._active) < limit):
            job = self._pending.pop(0)
            job.started_at = now
            self._active.append(job)

        count = len(self._active)
        self.stats.current_concurrent = count
        if count > self.stats.peak_concurrent:
            self.stats.peak_concurrent = count

    def _elapsed(self, job: _Job, now: float) -> float:
        if job.started_at is None:
            return 0.0
        elapsed = now - job.started_at - job.paused_total
        if job.paused_at is not None:
            elapsed -= now - job.paused_at
        return max(0.0, elapsed)

    def _is_complete(self, job: _Job, now: float) -> bool:
        if job.started_at is None or job.paused_at is not None:
            return False
        return job.finalized or self._elapsed(job, now) >= job.duration_s

    def _job_state(self, job: _Job, now: float) -> Tuple[str, float, float]:
        """Return (dispatchState, doneProgress, runDuration seconds)."""
        if job.started_at is None:
            return "QUEUED", 0.0, 0.0
        elapsed = self._elapsed(job, now)
        if job.finalized or elapsed >= job.duration_s:
            # A finalised job has its duration trimmed to the moment it was
            # finalised, so runDuration stops climbing here either way.
            state = "FAILED" if (job.will_fail and not job.finalized) else "DONE"
            return state, 1.0, min(elapsed, job.duration_s)
        progress = elapsed / job.duration_s if job.duration_s > 0 else 1.0
        if job.paused_at is not None:
            return "PAUSED", progress, elapsed
        if elapsed < job.parse_s:
            return "PARSING", progress, elapsed
        return "RUNNING", progress, elapsed

    def _derive(self, spl: str) -> Tuple[int, int, int, bool]:
        """Work out (scanCount, eventCount, resultCount, accelerated) from SPL."""
        lowered = spl.lower()
        accelerated = "tstats" in lowered or "mstats" in lowered
        if accelerated:
            # An accelerated search touches summaries, not raw events.
            scan_count = 1000 + int(self._rng.random() * 200)
            event_count = scan_count
        else:
            scan_count = self.config.scan_count_base
            if "earliest=-7d" in lowered:
                scan_count *= 10
            event_count = max(1, scan_count // 2)
        result_count = min(scan_count, 1 + len(spl) % 500)
        return scan_count, event_count, result_count, accelerated

    def _create_job(
        self,
        spl: str,
        form: Dict[str, List[str]],
        api_version: str,
        internal: bool = False,
    ) -> _Job:
        sid = str(uuid.uuid4())
        with self._lock:
            now = time.monotonic()
            self._pump_locked(now)
            # The job itself counts, hence the +1: dispatching into an empty
            # target gives a penalty term of zero.
            at_dispatch = len(self._active) + 1
            scan_count, event_count, result_count, accelerated = self._derive(spl)
            jitter = self._rng.uniform(0.0, max(0.0, self.config.jitter_ms))
            will_fail = self._rng.random() < self.config.fail_rate

            duration_ms = (
                self.config.base_latency_ms
                + jitter
                + self.config.per_concurrent_penalty_ms * (at_dispatch - 1)
            )
            if accelerated:
                duration_ms /= 10.0
            duration_s = max(0.0, duration_ms) / 1000.0

            job = _Job(
                sid=sid,
                spl=spl,
                earliest=_first(form, "earliest_time", ""),
                latest=_first(form, "latest_time", ""),
                exec_mode=_first(form, "exec_mode", "normal"),
                adhoc_search_level=_first(form, "adhoc_search_level", "smart"),
                auto_cancel=_first(form, "auto_cancel", "0"),
                timeout=_first(form, "timeout", "86400"),
                api_version=api_version,
                created_wall=time.time(),
                created_at=now,
                duration_s=duration_s,
                parse_s=duration_s * 0.1,
                scan_count=scan_count,
                event_count=event_count,
                result_count=result_count,
                will_fail=will_fail,
                internal=internal,
            )
            self._jobs[sid] = job
            if not internal:
                self.stats.jobs_created += 1
                self.stats.searches.append(spl)
                if api_version == "v1":
                    self.stats.v1_dispatches += 1

            limit = self.config.max_concurrent
            if limit <= 0 or len(self._active) < limit:
                job.started_at = now
                self._active.append(job)
            else:
                self._pending.append(job)
                self.stats.queued_total += 1
            self._pump_locked(now)
        return job

    def _remove_job(self, job: _Job) -> None:
        with self._lock:
            self._jobs.pop(job.sid, None)
            self._active = [other for other in self._active if other.sid != job.sid]
            self._pending = [other for other in self._pending if other.sid != job.sid]
            self._pump_locked(time.monotonic())

    def reset(self) -> None:
        """Drop every job and zero every counter."""
        with self._lock:
            self._jobs.clear()
            self._active.clear()
            self._pending.clear()
            self.stats = _Stats()
            self._rng = random.Random(self.config.seed)

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------
    def _job_content(self, job: _Job, now: float) -> Dict[str, Any]:
        state, progress, run_duration = self._job_state(job, now)
        failed = state == "FAILED"
        done = state in ("DONE", "FAILED")
        result_count = 0 if failed else job.result_count
        if done:
            preview_count = result_count
        elif state in ("RUNNING", "PAUSED"):
            preview_count = int(result_count * progress)
        else:
            preview_count = 0
        messages: List[Dict[str, str]] = []
        if failed:
            messages.append(
                {"type": "FATAL", "text": "Search process did not exit cleanly."}
            )
        return {
            "sid": job.sid,
            "dispatchState": state,
            "isDone": done,
            "isFailed": failed,
            "isFinalized": job.finalized,
            "isPaused": state == "PAUSED",
            "isZombie": False,
            "doneProgress": round(progress, 6),
            "runDuration": f"{run_duration:.3f}",
            "scanCount": str(job.scan_count),
            "eventCount": str(job.event_count),
            "resultCount": str(result_count),
            "resultPreviewCount": str(preview_count),
            "earliestTime": job.earliest or _iso(job.created_wall - 900),
            "latestTime": job.latest or _iso(job.created_wall),
            "eventSearch": _event_search(job.spl),
            "searchProviders": ["fake-splunk"],
            "performance": {},
            "ttl": job.timeout or "86400",
            "messages": messages,
            "request": {
                "search": job.spl,
                "earliest_time": job.earliest,
                "latest_time": job.latest,
                "exec_mode": job.exec_mode,
                "adhoc_search_level": job.adhoc_search_level,
                "auto_cancel": job.auto_cancel,
            },
        }

    def _job_entry(self, job: _Job, now: float) -> Dict[str, Any]:
        prefix = _JOB_PREFIXES[0] if job.api_version == "v2" else _JOB_PREFIXES[1]
        return {
            "links": {"alternate": f"{prefix}/{job.sid}"},
            "generator": {},
            "entry": [
                {
                    "name": job.sid,
                    "id": f"{self.base_url}{prefix}/{job.sid}",
                    "updated": _iso(time.time()),
                    "links": {
                        "alternate": f"{prefix}/{job.sid}",
                        "results": f"{prefix}/{job.sid}/results",
                        "results_preview": f"{prefix}/{job.sid}/results_preview",
                        "control": f"{prefix}/{job.sid}/control",
                    },
                    "author": "admin",
                    "acl": {"app": "search", "owner": "admin", "sharing": "global"},
                    "content": self._job_content(job, now),
                }
            ],
            "paging": {"total": 1, "perPage": 1, "offset": 0},
        }

    def _rows(self, job: _Job, count: int, offset: int) -> List[Dict[str, str]]:
        rows = []
        for index in range(offset, offset + count):
            rows.append(
                {
                    "_time": _iso(job.created_wall - index * 60),
                    "host": "web-%02d" % (index % 8 + 1),
                    "sourcetype": "access_combined",
                    "count": str(1 + (index * 7 + len(job.spl)) % 997),
                }
            )
        return rows

    def _results_payload(
        self, job: _Job, count: int, offset: int, preview: bool, available: int
    ) -> Dict[str, Any]:
        # count of 0 means "everything", matching splunkd.
        wanted = available if count == 0 else min(count, available)
        wanted = max(0, min(wanted, max(0, available - offset)))
        return {
            "preview": preview,
            "init_offset": offset,
            "messages": [],
            "fields": list(_RESULT_FIELDS),
            "results": self._rows(job, wanted, offset),
        }

    def stats_payload(self) -> Dict[str, Any]:
        with self._lock:
            self._pump_locked(time.monotonic())
            stats = self.stats
            return {
                "jobs_created": stats.jobs_created,
                "jobs_done": stats.jobs_done,
                "jobs_deleted": stats.jobs_deleted,
                "jobs_cancelled": stats.jobs_cancelled,
                "peak_concurrent": stats.peak_concurrent,
                "current_concurrent": stats.current_concurrent,
                "queued_total": stats.queued_total,
                "requests_by_path": dict(stats.requests_by_path),
                "auth_failures": stats.auth_failures,
                "v1_dispatches": stats.v1_dispatches,
                "oneshots": stats.oneshots,
                "searches": list(stats.searches),
            }

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def record_request(self, method: str, normalised_path: str) -> None:
        if normalised_path in _INTROSPECTION:
            return
        key = f"{method} {normalised_path}"
        with self._lock:
            self.stats.requests_by_path[key] = (
                self.stats.requests_by_path.get(key, 0) + 1
            )

    def note_auth_failure(self) -> None:
        with self._lock:
            self.stats.auth_failures += 1

    def route(
        self,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        form: Dict[str, List[str]],
    ) -> Tuple[int, Any]:
        if path == "/__stats" and method == "GET":
            return 200, self.stats_payload()
        if path == "/__reset" and method in ("POST", "GET"):
            self.reset()
            return 200, {"messages": []}
        if path == "/services/server/info" and method == "GET":
            return 200, _server_info()
        if path == "/services/auth/login" and method == "POST":
            return self._login(form)
        if path == "/services/configs/conf-limits/search" and method == "GET":
            return 200, _conf_limits()
        if path.startswith("/services/data/indexes") and method == "GET":
            return self._index(path)
        if path in _PARSER_PATHS and method in ("POST", "GET"):
            source = form if form else query
            return self._parse(_first(source, "q", ""))

        matched = _match_job_path(path)
        if matched is not None:
            prefix, sid, tail = matched
            api_version = "v2" if prefix == _JOB_PREFIXES[0] else "v1"
            if sid is None:
                if method == "POST":
                    return self._dispatch(form, api_version)
                if method == "GET":
                    return 200, self._job_listing()
                return 405, {"messages": [{"type": "ERROR", "text": "Method Not Allowed"}]}
            return self._job_endpoint(method, sid, tail, query, form)

        return 404, {"messages": [{"type": "ERROR", "text": f"Not found: {path}"}]}

    def _index(self, path: str) -> Tuple[int, Any]:
        """Answer an index existence check.

        Regulator's online lint calls this before a run, because a search
        against an index that does not exist (or that the load-test account
        cannot read) returns nothing in milliseconds and makes the report look
        like a gloriously fast cluster. Getting a 404 here is a much better
        outcome than a green benchmark measuring nothing.

        Anything not in ``config.indexes`` is a 404, so the negative case stays
        testable.
        """
        tail = path[len("/services/data/indexes"):].strip("/")
        if not tail:
            return 200, {
                "entry": [{"name": name, "content": {"disabled": "0"}} for name in self.config.indexes]
            }
        name = unquote(tail)
        if name in self.config.indexes:
            return 200, {
                "entry": [
                    {
                        "name": name,
                        "content": {
                            "disabled": "0",
                            "datatype": "metric" if "metric" in name else "event",
                            "totalEventCount": "123456",
                            "currentDBSizeMB": "512",
                        },
                    }
                ]
            }
        return 404, {
            "messages": [{"type": "ERROR", "text": f"Index '{name}' does not exist"}]
        }

    def _login(self, form: Dict[str, List[str]]) -> Tuple[int, Any]:
        password = _first(form, "password", "")
        if password != self.config.password:
            self.note_auth_failure()
            return 401, _UNAUTHENTICATED_BODY
        return 200, {"sessionKey": str(uuid.uuid4()), "message": "", "code": ""}

    def _parse(self, spl: str) -> Tuple[int, Any]:
        # The parser is always strict: it is what the scenario linter leans on.
        if not _parses_as_spl(spl):
            return 400, {
                "messages": [
                    {
                        "type": "ERROR",
                        "text": f"Error in 'search' command: cannot parse: {spl[:120]}",
                    }
                ]
            }
        return 200, {"messages": []}

    def _dispatch(
        self, form: Dict[str, List[str]], api_version: str
    ) -> Tuple[int, Any]:
        spl = _first(form, "search", "")
        if self.config.strict_spl and not _looks_dispatchable(spl):
            return 400, _BAD_SPL_BODY
        if not spl.strip():
            return 400, _BAD_SPL_BODY

        # splunkd takes a moment to hand back a sid, so dispatch_ms is never zero.
        _sleep_ms(self.config.dispatch_latency_ms)

        exec_mode = _first(form, "exec_mode", "normal")
        if exec_mode == "oneshot":
            return self._oneshot(spl, form, api_version)

        job = self._create_job(spl, form, api_version)
        return 201, {"sid": job.sid}

    def _oneshot(
        self, spl: str, form: Dict[str, List[str]], api_version: str
    ) -> Tuple[int, Any]:
        # A oneshot never becomes a visible job: it blocks and hands back rows.
        with self._lock:
            self.stats.oneshots += 1
        job = self._create_job(spl, form, api_version, internal=True)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            with self._lock:
                self._pump_locked(time.monotonic())
                if self._is_complete(job, time.monotonic()):
                    break
            time.sleep(0.002)
        count = _int_of(_first(form, "count", "100"), 100)
        available = 0 if job.will_fail else job.result_count
        payload = self._results_payload(job, count, 0, False, available)
        self._remove_job(job)
        return 200, payload

    def _job_listing(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            jobs = [job for job in self._jobs.values() if not job.internal]
            entries = [
                {"name": job.sid, "content": self._job_content(job, now)} for job in jobs
            ]
        return {"links": {}, "entry": entries, "paging": {"total": len(entries)}}

    def _job_endpoint(
        self,
        method: str,
        sid: str,
        tail: str,
        query: Dict[str, List[str]],
        form: Dict[str, List[str]],
    ) -> Tuple[int, Any]:
        now = time.monotonic()
        with self._lock:
            self._pump_locked(now)
            job = self._jobs.get(sid)
        if job is None or job.internal:
            return 404, _UNKNOWN_SID_BODY

        if tail == "" and method == "GET":
            return 200, self._job_entry(job, time.monotonic())

        if tail == "" and method == "DELETE":
            self._remove_job(job)
            with self._lock:
                self.stats.jobs_deleted += 1
            return 200, {"messages": []}

        if tail in ("results", "results_preview") and method == "GET":
            return self._results(job, tail == "results_preview", query)

        if tail == "control" and method == "POST":
            return self._control(job, _first(form, "action", ""))

        return 404, {
            "messages": [{"type": "ERROR", "text": f"Unknown job endpoint: {tail}"}]
        }

    def _results(
        self, job: _Job, preview: bool, query: Dict[str, List[str]]
    ) -> Tuple[int, Any]:
        now = time.monotonic()
        state, progress, _ = self._job_state(job, now)
        count = _int_of(_first(query, "count", "100"), 100)
        offset = _int_of(_first(query, "offset", "0"), 0)

        if preview:
            # Previews land as soon as the search is actually running.
            if state in ("QUEUED", "PARSING"):
                return 204, _NO_CONTENT
            available = 0 if job.will_fail else int(job.result_count * progress)
            if state in ("DONE", "FAILED"):
                available = 0 if job.will_fail else job.result_count
            return 200, self._results_payload(job, count, offset, True, available)

        if state not in ("DONE", "FAILED"):
            # Real splunkd answers 204 when the results are not ready yet.
            return 204, _NO_CONTENT
        available = 0 if job.will_fail else job.result_count
        return 200, self._results_payload(job, count, offset, False, available)

    def _control(self, job: _Job, action: str) -> Tuple[int, Any]:
        now = time.monotonic()
        if action == "cancel":
            self._remove_job(job)
            with self._lock:
                self.stats.jobs_cancelled += 1
            return 200, {"messages": []}
        if action == "finalize":
            with self._lock:
                job.finalized = True
                if job.started_at is None:
                    job.started_at = now
                job.will_fail = False
                job.duration_s = max(0.0, self._elapsed(job, now))
                self._pump_locked(now)
            return 200, {"messages": []}
        if action == "pause":
            with self._lock:
                if job.started_at is not None and job.paused_at is None:
                    job.paused_at = now
            return 200, {"messages": []}
        if action in ("unpause", "resume"):
            with self._lock:
                if job.paused_at is not None:
                    job.paused_total += now - job.paused_at
                    job.paused_at = None
            return 200, {"messages": []}
        return 400, {
            "messages": [{"type": "ERROR", "text": f"Unknown action: {action}"}]
        }

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def check_auth(self, path: str, header: Optional[str]) -> bool:
        if path in _UNAUTHENTICATED:
            return True
        if not header:
            return False
        parts = header.split(None, 1)
        if len(parts) != 2:
            return False
        scheme, token = parts[0], parts[1].strip()
        if scheme not in ("Bearer", "Splunk"):
            return False
        if not token:
            return False
        rejected = self.config.reject_token
        if rejected and token == rejected:
            return False
        return True


class _FakeSplunkHTTPServer(ThreadingHTTPServer):
    """Thread per request, with a backlog deep enough for a load test."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 512
    block_on_close = False
    fake: FakeSplunk


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Splunkd/10.4.0"
    sys_version = ""

    # TCP_NODELAY, and it is not a micro-optimisation.
    #
    # BaseHTTPRequestHandler writes the headers and the body as two separate
    # unbuffered writes. With Nagle's algorithm on, the second write waits for
    # an acknowledgement of the first, and the client's delayed-ACK timer holds
    # that back for around 40 ms. Every single request then costs tens of
    # milliseconds of pure stall, on loopback, with no work happening at all.
    #
    # For a test double used by a *latency measuring tool* that is not merely
    # slow, it is actively misleading: the simulated latency knobs stop being
    # the thing the tests are measuring. Measured here at roughly 174 ms per
    # request before this line and under 2 ms after it.
    disable_nagle_algorithm = True

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        fake = getattr(self.server, "fake", None)
        if fake is not None and fake.config.log_requests:
            print("fake-splunkd %s - %s" % (self.address_string(), fmt % args))

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send(self, status: int, payload: Any) -> None:
        try:
            if payload is _NO_CONTENT or status == 204:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=UTF-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            self.close_connection = True

    # -- verbs ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def _handle(self, method: str) -> None:
        fake: FakeSplunk = self.server.fake  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        query = parse_qs(parsed.query, keep_blank_values=True)
        raw_body = self._read_body()
        form: Dict[str, List[str]] = {}
        if raw_body:
            form = parse_qs(raw_body.decode("utf-8", "replace"), keep_blank_values=True)

        fake.record_request(method, _normalise_path(path))

        if not fake.check_auth(path, self.headers.get("Authorization")):
            fake.note_auth_failure()
            self._send(401, _UNAUTHENTICATED_BODY)
            return

        try:
            status, payload = fake.route(method, path, query, form)
        except Exception as exc:  # pragma: no cover - defensive
            self._send(500, {"messages": [{"type": "FATAL", "text": str(exc)}]})
            return
        self._send(status, payload)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _first(mapping: Dict[str, List[str]], key: str, default: str) -> str:
    values = mapping.get(key)
    if not values:
        return default
    return values[0]


def _int_of(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _sleep_ms(milliseconds: float) -> None:
    if milliseconds and milliseconds > 0:
        time.sleep(milliseconds / 1000.0)


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(timestamp)) + ".000+00:00"


def _event_search(spl: str) -> str:
    body = spl.strip()
    if body.lower().startswith("search "):
        body = body[7:]
    return body.split("|", 1)[0].strip()


def _looks_dispatchable(spl: str) -> bool:
    """A dispatched search must start with a command, as splunkd insists."""
    candidate = spl.lstrip()
    return candidate.startswith(("search ", "search\n", "search\t", "|"))


def _parses_as_spl(spl: str) -> bool:
    """Looser than dispatch: the parser also takes a bare search term."""
    candidate = spl.strip()
    if not candidate:
        return False
    if "SYNTAX_ERROR" in candidate:
        return False
    if _looks_dispatchable(candidate):
        return True
    head = candidate[0]
    return head.isalnum() or head in ("_", "*", '"')


def _match_job_path(path: str) -> Optional[Tuple[str, Optional[str], str]]:
    """Split a jobs path into (prefix, sid or None, trailing segment)."""
    for prefix in _JOB_PREFIXES:
        if path == prefix:
            return prefix, None, ""
        if path.startswith(prefix + "/"):
            rest = path[len(prefix) + 1 :]
            parts = rest.split("/")
            return prefix, parts[0], "/".join(parts[1:])
    return None


def _normalise_path(path: str) -> str:
    """Collapse the sid out of a path so requests_by_path stays small."""
    matched = _match_job_path(path)
    if matched is None:
        return path
    prefix, sid, tail = matched
    if sid is None:
        return prefix
    return f"{prefix}/<sid>" + (f"/{tail}" if tail else "")


def _server_info() -> Dict[str, Any]:
    return {
        "links": {},
        "generator": {},
        "entry": [
            {
                "name": "server-info",
                "content": {
                    "version": "10.4.0",
                    "build": "f798d4d49089",
                    "serverName": "fake-splunk",
                    "numberOfCores": 8,
                    "numberOfVirtualCores": 8,
                    "physicalMemoryMB": 16384,
                    "server_roles": ["indexer", "search_head"],
                },
            }
        ],
        "paging": {"total": 1},
    }


def _conf_limits() -> Dict[str, Any]:
    return {
        "links": {},
        "generator": {},
        "entry": [
            {
                "name": "search",
                "content": {
                    "base_max_searches": "6",
                    "max_searches_per_cpu": "1",
                    "max_rt_search_multiplier": "1",
                    "max_searches_perc": "50",
                },
            }
        ],
        "paging": {"total": 1},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A fake splunkd for testing the search load generator."
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8089, help="bind port, 0 to pick one")
    parser.add_argument("--password", default="changeme", help="password /services/auth/login accepts")
    parser.add_argument("--reject-token", default=None, help="token that always 401s")
    parser.add_argument("--strict-spl", action="store_true", help="reject SPL that does not start with a command")
    parser.add_argument("--max-concurrent", type=int, default=0, help="concurrent job ceiling, 0 for unlimited")
    parser.add_argument("--base-latency-ms", type=float, default=200.0, help="baseline simulated run duration")
    parser.add_argument("--jitter-ms", type=float, default=100.0, help="upper bound of the uniform jitter")
    parser.add_argument("--per-concurrent-penalty-ms", type=float, default=0.0, help="added per already-running job")
    parser.add_argument("--dispatch-latency-ms", type=float, default=20.0, help="how long a dispatch POST blocks")
    parser.add_argument("--seed", type=int, default=None, help="seed for the jitter and failure RNG")
    parser.add_argument("--scan-count-base", type=int, default=100000, help="scanCount for a raw search")
    parser.add_argument("--fail-rate", type=float, default=0.0, help="fraction of jobs that end FAILED")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = FakeSplunkConfig(
        host=args.host,
        port=args.port,
        password=args.password,
        reject_token=args.reject_token,
        strict_spl=args.strict_spl,
        max_concurrent=args.max_concurrent,
        base_latency_ms=args.base_latency_ms,
        jitter_ms=args.jitter_ms,
        per_concurrent_penalty_ms=args.per_concurrent_penalty_ms,
        dispatch_latency_ms=args.dispatch_latency_ms,
        seed=args.seed,
        scan_count_base=args.scan_count_base,
        fail_rate=args.fail_rate,
        log_requests=args.verbose,
    )
    splunkd = FakeSplunk(config)
    print(f"fake splunkd listening on {splunkd.base_url}", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("shutting down", flush=True)
    finally:
        splunkd.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
