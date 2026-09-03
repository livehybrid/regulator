"""The API engine: dispatch a search over REST, poll it, read the answer.

This is the engine that does the measuring. Everything else in Regulator exists
to decide what it should run and when, or to write down what it found.

Two things it does that a naive load generator does not, and both of them are
the difference between a number and an explanation.

**It records the server's half as well as its own.** Every completed job hands
back ``runDuration``, ``scanCount``, ``eventCount`` and ``resultCount``, and
those four fields are what let a slower run be attributed. Client latency up
with scan count flat means contention or queueing. Client latency up with scan
count up means the workload changed and the comparison was never valid. Without
the server half you cannot tell those apart, and almost every argument about a
benchmark is really an argument about which of the two happened.

**It measures dispatch and queueing separately from run time.** A search head
under admission-control pressure takes longer to *accept* a search well before
it takes longer to *run* one, so ``dispatch_ms`` and ``queued_ms`` move first.
They are the leading indicator that a cluster is at its ceiling, and they are
invisible to any tool that only times the whole call.

Timing responsibilities, restated because getting this wrong is silent: this
engine fills ``service_time_ms`` and everything below it. The scheduler fills
``latency_ms`` and ``late_by_ms``, because only the scheduler knows when the
work was *supposed* to start. Nothing here touches those two fields.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from ..params import DrawContext, ParameterResolver
from ..results import (
    ERROR_AUTH,
    ERROR_CANCELLED,
    ERROR_CLIENT,
    ERROR_PARSE,
    ERROR_QUOTA,
    ERROR_SEARCH_FAILED,
    ERROR_SERVER,
    ERROR_TIMEOUT,
    StepRecord,
)
from ..scenario import PLACEHOLDER_RE, Scenario
from ..splunk import (
    SplunkAuthError,
    SplunkClient,
    SplunkError,
    SplunkHttpError,
    SplunkTimeout,
)
from ..timepolicy import TimeWindow
from .base import StepContext, TargetCapabilities

ADVICE = "advice: "


def _quote_spl(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# What a placeholder becomes when a scenario is linted against the live parser.
# It has to be a legal bare token in every position a parameter can appear
# (field value, index name, comparison operand), otherwise the parser rejects
# the substitution rather than the search and the lint reports a phantom fault.
LINT_PLACEHOLDER = "regulator_placeholder"

# How far back a parameter resolver looks. A day is enough to find real values
# in any corpus worth testing against, and short enough that the resolver does
# not become an expensive search in its own right against a large index.
RESOLVER_WINDOW_S = 86400.0

# The default cap on how many values a resolver binds. Hundreds is plenty: the
# point is variety across iterations, and a set of five hundred source IPs
# already makes a cache hit vanishingly unlikely.
DEFAULT_RESOLVER_LIMIT = 500

# Substrings in a splunkd error body that mean "you have hit a limit", not
# "your search is wrong". They land in the quota bucket so a run that degrades
# because it crossed the concurrency ceiling reads differently from one that
# degrades because the cluster got slow.
_QUOTA_MARKERS = (
    "quota",
    "maximum number of concurrent",
    "concurrent searches",
    "search limit",
    "disk usage quota",
)


def spl_hash(spl: str) -> str:
    """A stable identity for a search, ignoring its cache-busting comment.

    This matters more than it looks. Every iteration appends a unique marker
    comment so Splunk cannot serve the result from its dispatch cache, which
    means the literal search string is different every single time. Hash that
    and every execution is its own group of one, and no aggregation by search
    is possible: no p95 per search, no "which search got slower", nothing. So
    the marker is cut off before hashing and the same logical search collapses
    back to one identity across the whole run.

    The cut is at the *opening* backticks of the last Splunk comment. Finding
    the closing pair with ``rfind`` and cutting there would be the obvious bug:
    it leaves the marker text in place and changes nothing.
    """
    text = spl or ""
    close = text.rfind("```")
    if close != -1:
        opener = text.rfind("```", 0, close)
        text = text[: opener if opener != -1 else close]
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class ApiEngine:
    """Executes search steps against splunkd's REST API."""

    name = "api"

    def __init__(self, config: Config, client: Optional[SplunkClient] = None) -> None:
        self._config = config
        self._client = client or SplunkClient(
            config.target,
            connect_timeout_s=config.connect_timeout_s,
            read_timeout_s=config.read_timeout_s,
            http2=config.http2,
            max_connections=config.max_in_flight,
        )
        self._closed = False

    @property
    def client(self) -> SplunkClient:
        return self._client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        await self._client.start()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.close()

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------
    async def probe(self) -> TargetCapabilities:
        """Ask the target what it is, so the report can be read in context."""
        info = await self._client.server_info()
        roles = info.get("server_roles") or []
        if isinstance(roles, str):
            roles = [roles]

        caps = TargetCapabilities(
            version=str(info.get("version", "") or ""),
            build=str(info.get("build", "") or ""),
            server_name=str(info.get("serverName", "") or ""),
            # numberOfVirtualCores is what Splunk itself uses for the search
            # concurrency formula on a hyper-threaded box, so prefer it and only
            # fall back to the physical count when it is absent.
            cpu_count=_int_or_none(info.get("numberOfVirtualCores"))
            or _int_or_none(info.get("numberOfCores"))
            or 0,
            server_roles=[str(role) for role in roles],
            auth_method=self._client.auth_method,
        )

        try:
            limits = await self._client.search_limits()
        except SplunkError as exc:
            limits = {}
            caps.notes.append(f"search limits could not be read: {exc}")

        if limits:
            caps.base_max_searches = _int_or_none(limits.get("base_max_searches"))
            caps.max_searches_per_cpu = _int_or_none(limits.get("max_searches_per_cpu"))
            caps.max_searches_perc = _int_or_none(limits.get("max_searches_perc"))
        else:
            caps.notes.append(
                "limits.conf [search] was not readable, so the concurrency ceiling "
                "cannot be drawn on the report: grant the account list_settings, or "
                "read the ceiling off the target by hand"
            )

        if not caps.cpu_count:
            caps.notes.append(
                "server/info reported no core count, so the concurrency ceiling "
                "cannot be computed"
            )

        role_set = {role.lower() for role in caps.server_roles}
        if "indexer" in role_set and "search_head" in role_set:
            caps.notes.append(
                "the target is a single instance acting as both indexer and search "
                "head, so nothing here exercises distributed search: bundle "
                "replication, the map phase across peers and the search-head "
                "concurrency split are all out of scope for this run"
            )
        elif "indexer" in role_set and "search_head" not in role_set:
            caps.notes.append(
                "the target reports itself as an indexer with no search-head role: "
                "searches dispatched here will not exercise a real search tier"
            )

        return caps

    # ------------------------------------------------------------------
    # Online lint
    # ------------------------------------------------------------------
    async def validate(self, scenario: Scenario) -> List[str]:
        """Parse every search against the target and confirm the data exists.

        Never raises. A connection problem during validation is itself a
        reportable problem, and turning it into a traceback would lose the other
        problems found alongside it, which is the whole point of collecting them
        into a list.
        """
        problems: List[str] = []
        # The same SPL frequently appears in several personas. Parsing is cheap
        # but it is a round trip to a search head, so do each distinct search
        # once.
        parsed: Dict[str, Tuple[bool, str]] = {}

        # Placeholders must be filled with values of the right *type*, not a
        # sentinel string. Substituting a word into `| makeresults count={{n}}`
        # produces SPL that Splunk rightly rejects, so the lint would report a
        # syntax error in a search that is perfectly valid at run time. Drawing
        # from the real generators means what gets parsed is what will actually
        # be dispatched.
        #
        # Dynamic parameters are the exception: their values come from a search
        # against the target that has not run yet, so they are bound to one
        # representative string purely so the SPL is complete. That is honest,
        # because a choice_from_search value is always a field value and always
        # arrives as a string.
        probe_resolver = ParameterResolver(scenario)
        for name in probe_resolver.dynamic_parameters:
            probe_resolver.bind(name, [LINT_PLACEHOLDER])

        for persona in scenario.personas:
            for step in persona.steps:
                if step.type != "search" or not step.spl or step.dispatch == "saved":
                    continue
                draw = DrawContext(
                    scenario_seed=scenario.seed,
                    vu_id=0,
                    iteration=0,
                    step_id=step.id,
                )
                try:
                    probe_spl, _ = probe_resolver.render(step.spl, draw)
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    problems.append(
                        f"persona {persona.name} step {step.id}: could not resolve its "
                        f"parameters: {exc}"
                    )
                    continue
                if probe_spl not in parsed:
                    try:
                        parsed[probe_spl] = await self._client.parse_spl(probe_spl)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - reported, not raised
                        parsed[probe_spl] = (False, f"could not parse against the target: {exc}")
                ok, message = parsed[probe_spl]
                if not ok:
                    problems.append(f"persona {persona.name} step {step.id}: {message}")

        problems.extend(await self._check_index(scenario.corpus.index, "corpus.index"))
        if scenario.corpus.metric_index:
            problems.extend(
                await self._check_index(scenario.corpus.metric_index, "corpus.metric_index")
            )
        problems.extend(await self._check_sourcetypes(scenario))
        problems.extend(await self._check_saved_dispatch(scenario))

        problems.extend(await self._check_dispatch())
        return problems

    async def _check_sourcetypes(self, scenario: Scenario) -> List[str]:
        """Confirm each declared sourcetype has events in the scenario's window.

        The index existing is necessary and not sufficient: an index that
        holds none of the sourcetypes a scenario searches returns nothing in
        milliseconds and reads as a magnificent cluster. One tstats over the
        accelerated metadata answers it for every sourcetype at once.
        """
        wanted = [st for st in scenario.corpus.sourcetypes if st]
        if not wanted:
            return []
        window_s = max(scenario.time_policy.window_s or 0.0, 3600.0)
        now = time.time()
        window = TimeWindow(earliest=now - window_s * 2, latest=now)
        indexes = [scenario.corpus.index] + (
            [scenario.corpus.metric_index] if scenario.corpus.metric_index else []
        )
        index_clause = " OR ".join(f"index={_quote_spl(i)}" for i in indexes if i)
        spl = f"| tstats count where ({index_clause}) by sourcetype"
        try:
            rows, _ = await self._client.oneshot(spl, window, count=500)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - inconclusive, say so
            return [
                f"{ADVICE}could not census the sourcetypes ({exc}); the corpus was not "
                "confirmed before the run"
            ]
        present = {str(row.get("sourcetype", "")) for row in rows}
        missing = [st for st in wanted if st not in present]
        if not missing:
            return []
        span_h = window.span_s / 3600.0
        if scenario.corpus.metric_index:
            metric_missing = await self._check_metrics(scenario, missing, window)
            missing = [st for st in missing if st not in metric_missing]
            if not missing:
                return []
        return [
            f"corpus.sourcetypes: no events for {', '.join(repr(m) for m in missing)} in "
            f"the last {span_h:.0f}h of {', '.join(indexes)}. Every search against them "
            "would return nothing in milliseconds and the report would look like a very "
            "fast cluster. Fill with Stoker first, or fix the scenario's corpus"
        ]

    async def _check_metrics(self, scenario: Scenario, wanted: List[str], window: TimeWindow) -> List[str]:
        """Metric sourcetypes do not appear in an event tstats: ask mcatalog."""
        spl = (
            f"| mcatalog values(metric_name) as metric_name where "
            f"index={_quote_spl(scenario.corpus.metric_index or '')} by sourcetype"
        )
        try:
            rows, _ = await self._client.oneshot(spl, window, count=500)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return []
        present = {str(row.get("sourcetype", "")) for row in rows}
        return [st for st in wanted if st in present]

    async def _check_saved_dispatch(self, scenario: Scenario) -> List[str]:
        """A step dispatched by name needs the saved search to exist on the target."""
        problems: List[str] = []
        seen: set[tuple[str, str]] = set()
        for persona in scenario.personas:
            for step in persona.steps:
                if step.dispatch != "saved" or not step.saved:
                    continue
                key = (step.app or "", step.saved)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    entries = await self._client.saved_searches(app=step.app)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    problems.append(
                        f"persona {persona.name} step {step.id}: could not list saved "
                        f"searches in app {step.app!r} ({exc})"
                    )
                    continue
                names = {str(entry.get("name")) for entry in entries}
                if step.saved not in names:
                    problems.append(
                        f"persona {persona.name} step {step.id}: no saved search named "
                        f"{step.saved!r} in app {step.app!r} on the target, so dispatch: saved "
                        "cannot run it. Use dispatch: spl to run the copy in the scenario"
                    )
        return problems

    async def _check_index(self, name: str, where: str) -> List[str]:
        try:
            exists = await self._client.index_exists(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            # Inconclusive rather than negative: plenty of load-test accounts
            # cannot read /services/data/indexes yet can search the index
            # perfectly well. Say so rather than pretending either way.
            return [
                f"{where}: could not confirm index {name!r} exists ({exc}). The account "
                "may lack the capability to list indexes; check by hand before trusting "
                "this run"
            ]
        if not exists:
            return [
                f"{where}: index {name!r} does not exist on the target or is not visible "
                "to this account. Every search would return nothing in milliseconds and "
                "the report would look like a very fast cluster"
            ]
        return []

    async def _check_dispatch(self) -> List[str]:
        """Prove the account can actually run a search, not merely parse one.

        Parsing needs no search capability at all, so a scenario can lint clean
        against an account that cannot dispatch anything. One trivial oneshot
        settles it before the run starts rather than after the first thousand
        identical failures.
        """
        now = time.time()
        window = TimeWindow(earliest=now - 60.0, latest=now)
        try:
            await self._client.oneshot("| makeresults count=1", window, count=1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return [
                "the target rejected a trivial '| makeresults count=1' dispatch, so no "
                f"search in this scenario can run: {exc}"
            ]
        return []

    # ------------------------------------------------------------------
    # Parameter resolvers
    # ------------------------------------------------------------------
    async def resolve_parameters(self, resolver: ParameterResolver) -> None:
        """Run each ``choice_from_search`` and bind its values.

        Failures here are deliberately fatal. ``ParameterResolver.bind`` refuses
        an empty value set, and letting that propagate is the point: a scenario
        that draws source IPs from an index which turned out to be empty would
        otherwise run to completion, search for nothing, and report excellent
        latency. A load test that measures nothing while looking healthy is the
        worst outcome available, so it fails at start-up instead.
        """
        specs = resolver.dynamic_parameters
        if not specs:
            return

        now = time.time()
        window = TimeWindow(earliest=now - RESOLVER_WINDOW_S, latest=now)

        for name, spec in specs.items():
            limit = _int_or_none(spec.get("limit", DEFAULT_RESOLVER_LIMIT)) or DEFAULT_RESOLVER_LIMIT
            field = str(spec.get("field", ""))
            rows, _ = await self._client.oneshot(str(spec.get("spl", "")), window, count=limit)

            values: List[Any] = []
            seen: set[str] = set()
            for row in rows:
                value = row.get(field)
                if value is None or value == "":
                    continue
                key = str(value)
                if key in seen:
                    continue
                seen.add(key)
                values.append(value)
                if len(values) >= limit:
                    break

            # No empty-list guard here on purpose: bind raises ParameterError
            # with a much better message than anything this loop could invent.
            resolver.bind(name, values)

    # ------------------------------------------------------------------
    # The hot path
    # ------------------------------------------------------------------
    async def execute(self, ctx: StepContext) -> StepRecord:
        """Execute one step. Returns a record for every outcome bar a dead target."""
        record = ctx.blank_record()
        record.spl_hash = spl_hash(
            ctx.spl_template or ctx.spl or f"saved:{ctx.step.app}/{ctx.step.saved}"
        )
        started = time.perf_counter()

        try:
            if ctx.step.exec_mode in ("oneshot", "export"):
                await self._execute_oneshot(ctx, record)
            else:
                await self._execute_job(ctx, record)
        except asyncio.CancelledError:
            # Never swallowed: the scheduler cancels virtual users to end a run
            # or to abort one, and an engine that catches this turns a clean
            # shutdown into a hang. The record is still completed so the
            # scheduler can file the abandoned search with the time it had
            # accrued, and the job is still deleted below so the target is not
            # left running work nobody is waiting for.
            record.ok = False
            record.error_class = ERROR_CANCELLED
            record.error_detail = "the run ended while this search was still in flight"
            record.service_time_ms = (time.perf_counter() - started) * 1000.0
            ctx.outcome.append(record)
            await self._tidy(record)
            raise
        except SplunkAuthError as exc:
            record.ok = False
            record.error_class = ERROR_AUTH
            record.error_detail = str(exc)
            # The one failure that is re-raised. Credentials do not fix
            # themselves, so continuing would produce thousands of identical
            # meaningless records and, on a real stack, an account lockout.
            record.service_time_ms = (time.perf_counter() - started) * 1000.0
            await self._tidy(record)
            raise
        except SplunkTimeout as exc:
            record.ok = False
            record.error_class = ERROR_TIMEOUT
            record.error_detail = str(exc)
        except SplunkHttpError as exc:
            record.ok = False
            record.http_status = exc.status
            record.error_class = _classify_http(exc)
            record.error_detail = str(exc)
        except SplunkError as exc:
            record.ok = False
            record.error_class = ERROR_CLIENT
            record.error_detail = str(exc)
        except Exception as exc:  # noqa: BLE001 - a bug here must not stop the run
            record.ok = False
            record.error_class = ERROR_CLIENT
            record.error_detail = f"{type(exc).__name__}: {exc}"

        # Service time is stamped before the tidy-up, because deleting the job
        # artefact is Regulator's housekeeping and not part of what the
        # simulated user waited for.
        record.service_time_ms = (time.perf_counter() - started) * 1000.0
        await self._tidy(record)
        return record

    async def _tidy(self, record: StepRecord) -> None:
        """Delete the job artefact, whatever happened to the poll.

        Keyed on ``record.sid`` rather than a local variable, because the sid
        exists from the moment the dispatch returns and a poll that then times
        out or fails must still clean up: a search head at its ceiling that
        makes two hundred polls time out would otherwise keep two hundred
        searches running with no client waiting, so the applied load quietly
        exceeded the configured load at exactly the moment the target could
        least afford it. A failed delete leaks one artefact and never changes
        the outcome of the measurement.
        """
        if not record.sid or not self._config.delete_jobs:
            return
        try:
            await asyncio.shield(self._client.delete_job(record.sid))
        except Exception:  # noqa: BLE001
            pass

    async def _execute_oneshot(self, ctx: StepContext, record: StepRecord) -> None:
        """The cheap path: one blocking call, no job artefact, no server half.

        ``export`` shares this path in Phase 0. Regulator has no streaming
        reader yet, so an export step is dispatched as a oneshot: it measures
        the same single round trip, and lint already tells the operator that
        neither mode can report runDuration or scanCount.
        """
        count = ctx.step.result_count if ctx.step.result_count > 0 else 100
        rows, byte_count = await self._client.oneshot(ctx.spl, ctx.window, count=count)
        # 204 with no body is how splunkd answers a oneshot with no results.
        record.http_status = 200 if byte_count else 204
        record.result_bytes = byte_count
        record.result_count = len(rows)
        # scan_count, event_count, run_duration_s and dispatch_state stay None:
        # there is no job record to read them from, which is precisely why lint
        # warns about these exec modes.

    async def _execute_job(self, ctx: StepContext, record: StepRecord) -> None:
        """The measured path: dispatch, poll to completion, read results."""
        # Time to first result is measured from the moment the user asked,
        # which includes the dispatch. Starting the clock after the POST
        # returned understated TTFR by exactly the number that climbs first
        # under admission pressure, which is the number it exists to show.
        started = time.perf_counter()
        if ctx.step.dispatch == "saved" and ctx.step.saved:
            sid, dispatch_ms, status = await self._client.dispatch_saved(
                ctx.step.saved,
                ctx.step.app or self._config.target.app,
                ctx.window,
                owner=self._config.target.owner,
            )
        else:
            sid, dispatch_ms, status = await self._client.create_job(
                ctx.spl,
                ctx.window,
                exec_mode=ctx.step.exec_mode,
                # A server-side net under the client-side timeout: if this worker
                # dies or gives up, splunkd cancels the job itself rather than
                # running it to completion for nobody.
                auto_cancel_s=int(self._config.read_timeout_s) + 60,
            )
        record.sid = sid
        record.dispatch_ms = dispatch_ms
        record.http_status = status

        interval_s = max(0.001, self._config.poll_initial_ms / 1000.0)
        max_interval_s = max(interval_s, self._config.poll_max_ms / 1000.0)
        last_observed = time.perf_counter()
        queued_ms = 0.0

        while True:
            # Sleep first, then poll. A poll fired immediately after the
            # dispatch returns is guaranteed to say QUEUED or PARSING, so it
            # costs the search head a round trip to tell us something we already
            # know, multiplied by every virtual user in the run.
            await asyncio.sleep(interval_s)
            job = await self._client.job_status(sid)
            record.poll_count += 1
            now = time.perf_counter()

            if job.dispatch_state == "QUEUED":
                # Attribute the interval just elapsed to the state we found at
                # the end of it. Queue time is the number that says the target
                # is admission-limited rather than slow, so it is worth
                # recording even approximately.
                queued_ms += (now - last_observed) * 1000.0
            last_observed = now

            if record.ttfr_ms is None and (
                (job.result_preview_count or 0) > 0 or job.is_done
            ):
                # Time to first result: what a dashboard user perceives as the
                # page coming alive, which is often far earlier than completion.
                record.ttfr_ms = (now - started) * 1000.0

            if job.is_terminal:
                record.queued_ms = queued_ms
                record.dispatch_state = job.dispatch_state
                record.is_finalized = job.is_finalized
                record.run_duration_s = job.run_duration_s
                record.scan_count = job.scan_count
                record.event_count = job.event_count
                record.result_count = job.result_count
                if job.messages:
                    record.messages = list(job.messages)

                if job.is_failed:
                    record.ok = False
                    record.error_class = ERROR_SEARCH_FAILED
                    record.error_detail = (
                        "; ".join(job.messages) or "the job ended FAILED with no message"
                    )
                    return

                # DONE is not the same as complete. A peer that dropped out,
                # an early finalization or a truncation limit all produce a
                # fast, small, successful-looking job that did less than it
                # was asked. splunkd says so in the messages, with HTTP 200.
                record.partial = bool(job.is_finalized) or _looks_partial(job.messages)

                if ctx.step.result_count > 0:
                    fetch_started = time.perf_counter()
                    if ctx.step.want_preview:
                        rows, byte_count = await self._client.fetch_preview(
                            sid, ctx.step.result_count
                        )
                    else:
                        rows, byte_count = await self._client.fetch_results(
                            sid, ctx.step.result_count
                        )
                    record.results_fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                    record.result_bytes = byte_count
                    if record.result_count is None:
                        record.result_count = len(rows)
                return

            # Back off gently. Polling hard is a real cost on the target: a
            # thousand virtual users at 100 ms is ten thousand REST calls a
            # second into the same splunkd the test is trying to measure.
            interval_s = min(interval_s * 1.5, max_interval_s)


# Fragments of splunkd job messages that mean the job finished without doing
# all of its work. Lower-cased substring matches, because the exact wording
# has drifted between releases while the vocabulary has not.
_PARTIAL_MARKERS = (
    "might be incomplete",
    "may be incomplete",
    "results are incomplete",
    "unable to distribute",
    "peer",
    "truncat",
    "was finalized",
    "auto-finalized",
    "exceeded",
    "limit",
)


def _looks_partial(messages: List[str]) -> bool:
    for message in messages or []:
        lowered = message.lower()
        if lowered.startswith("info:") or lowered.startswith("debug:"):
            continue
        if any(marker in lowered for marker in _PARTIAL_MARKERS):
            return True
    return False


def _classify_http(exc: SplunkHttpError) -> str:
    """Put an HTTP failure in the right bucket of the error taxonomy."""
    body = (exc.body or "").lower()
    if exc.status == 429 or any(marker in body for marker in _QUOTA_MARKERS):
        # A quota breach is a capacity finding, not a bug in the scenario. It
        # is the answer a saturation test is looking for, so it must not be
        # lumped in with client errors.
        return ERROR_QUOTA
    if exc.status == 400:
        return ERROR_PARSE
    if exc.status >= 500:
        return ERROR_SERVER
    return ERROR_CLIENT
