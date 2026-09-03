"""The browser engine: what a Splunk user actually experiences.

The API engine answers the search-tier question. This one answers a different
question that REST cannot: when an analyst opens a dashboard, how long until
they see data. That number includes things the search tier knows nothing about,
the JavaScript bundle, the panel layout, the browser's own rendering, and it is
the number the analyst would quote at you.

Four decisions in here are load-bearing.

**One persistent context per virtual user, reused across iterations.** A fresh
browser context re-downloads and re-parses the whole Splunk Web bundle every
time, which inflates page timings against any real returning user and can make
the search head's static-asset path look like a bottleneck it is not. Splunk
cache-busts its assets on the build number, so a returning user's browser has
them cached essentially forever. Persisting the context is what makes the
measurement resemble the thing being measured. The first iteration of each
virtual user is still recorded separately as ``first_visit``, because a cold
bundle load is a real event, just not the common one.

**Login once per context, not per iteration.** Splunk Web's login is a form
post plus a CSRF cookie, and a real user does it once a day. Doing it per
iteration would measure the login endpoint, which nobody cares about, and would
add a load to the search head that no real population generates.

**Every search the page fires is captured with its sid.** The page issues XHRs
to splunkd's search endpoints; intercepting them means a browser step can be
joined back to exactly the same server-side job statistics the API engine reads.
Without that the two channels produce unrelated numbers and there is no way to
say whether a slow dashboard was a slow search or a slow render.

**The XHR shape is discovered, not assumed.** Splunk Web's internal request
pattern differs between classic Simple XML and Dashboard Studio, and between
versions, and it is not documented anywhere. So this engine matches on a broad
URL pattern and extracts what it finds, rather than encoding a brittle
expectation of a particular release's internals.

Cost, stated plainly: a Chromium context is roughly 150 to 300 MB. A realistic
large test is a big API cohort for search-tier load alongside a small browser
cohort for the experience measurement, not a thousand browsers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from ..config import Config
from ..results import (
    ERROR_AUTH,
    ERROR_CLIENT,
    ERROR_SERVER,
    ERROR_TIMEOUT,
    StepRecord,
)
from ..scenario import Scenario
from ..splunk import SplunkClient
from .base import StepContext, TargetCapabilities

log = logging.getLogger("regulator.browser")

# Splunk Web serves its search XHRs under a raw-proxy path to splunkd. Both the
# classic and the Studio data layers go through it, which is why matching here
# is deliberately broad rather than pinned to one framework's exact URL.
_SEARCH_XHR = re.compile(r"/splunkd/__raw/services(NS/[^/]+/[^/]+)?/search/(v2/)?jobs")
_SID_IN_PATH = re.compile(r"/jobs/([^/?]+)")
_IS_DONE_TRUE = re.compile(r'"isDone"\s*:\s*(true|"1"|1)\b')

# How many of a page's jobs to read back for the server-side join.
MAX_JOBS_TO_JOIN = 8

# A dashboard that has not painted anything after this long is not going to.
DEFAULT_STEP_TIMEOUT_S = 120.0

# Selectors that mean "a panel has data on screen". Splunk's markup differs
# between the classic dashboard framework and Studio, so both are tried and the
# first to appear wins. Kept as data rather than logic so it can be corrected
# against a real deployment without touching the engine.
PANEL_READY_SELECTORS = (
    "[data-test='visualization']",
    "[data-test-viz-type]",
    ".dashboard-element .panel-body .viz-chart",
    ".splunk-view .highcharts-container",
    "table.table-chrome tbody tr",
    ".dashboard-panel .panel-body table tbody tr",
)


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed in this image.

    The browser engine is an optional extra: its dependency is a few hundred
    megabytes of browser binaries, so the base worker image does not carry it.
    This is raised with instructions rather than an ImportError traceback,
    because the person who hits it is an operator, not the author.
    """


@dataclass
class _Context:
    """One virtual user's browser, kept alive across their iterations."""

    context: Any
    page: Any
    logged_in: bool = False
    iterations: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class PageTimings:
    """What the browser itself reports, straight from the Navigation Timing API."""

    ttfb_ms: Optional[float] = None
    dom_content_loaded_ms: Optional[float] = None
    load_ms: Optional[float] = None
    lcp_ms: Optional[float] = None

    def apply(self, record: StepRecord) -> None:
        record.ttfb_ms = self.ttfb_ms
        record.dom_content_loaded_ms = self.dom_content_loaded_ms
        record.load_ms = self.load_ms
        record.lcp_ms = self.lcp_ms


# Collected in the page rather than inferred from the outside. responseStart
# minus requestStart is the server's think time; the rest is the browser's.
_TIMING_SCRIPT = """
() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const lcp = performance.getEntriesByType('largest-contentful-paint').slice(-1)[0];
  if (!nav) return {};
  return {
    ttfb_ms: nav.responseStart - nav.requestStart,
    dom_content_loaded_ms: nav.domContentLoadedEventEnd - nav.startTime,
    load_ms: nav.loadEventEnd > 0 ? nav.loadEventEnd - nav.startTime : null,
    lcp_ms: lcp ? lcp.startTime : null,
  };
}
"""


class BrowserEngine:
    """Drives real Chromium against Splunk Web."""

    name = "browser"

    def __init__(self, config: Config, playwright_factory: Any = None) -> None:
        self._config = config
        self._factory = playwright_factory
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: Dict[int, _Context] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._client_started = False
        # A splunkd client alongside the browser, so a captured sid can be
        # turned into the same server-side job statistics the API engine reads.
        self._client = SplunkClient(
            config.target,
            connect_timeout_s=config.connect_timeout_s,
            read_timeout_s=config.read_timeout_s,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._config.target.web_url is None:
            raise BrowserUnavailable(
                "the browser engine needs REG_TARGET_WEB_URL (Splunk Web, normally port "
                "8000). The management URI on 8089 serves no user interface"
            )

        factory = self._factory
        if factory is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - exercised by operators
                raise BrowserUnavailable(
                    "playwright is not installed in this image. The browser engine needs "
                    "a few hundred megabytes of browser binaries, so it ships as a "
                    "separate image: use ghcr.io/livehybrid/regulator-worker:browser, or "
                    "install it with 'pip install -r worker/requirements-browser.txt && "
                    "playwright install chromium'"
                ) from exc
            factory = async_playwright

        self._playwright = await factory().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                # Chromium's default shared-memory segment is 64 MB in most
                # containers, which is not enough for a real page and produces
                # tab crashes that look like flaky tests. The Kubernetes
                # manifests mount a memory-backed /dev/shm instead; this is the
                # belt to that pair of braces.
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        # The splunkd client is NOT started here. It is only needed to turn a
        # captured sid into job statistics and to validate that a dashboard
        # exists, both of which are enrichment rather than the measurement. A
        # browser-only run against a target whose management port is firewalled
        # should still produce page timings rather than refusing to start.

    @property
    def client(self) -> SplunkClient:
        """The splunkd client, for the cache and correlation code around a run.

        The same attribute the API engine exposes, because the entrypoint reads
        cache state and correlates through ``engine.client`` for whichever
        engine the scenario chose. Without this every browser run died with an
        AttributeError after lint and before the first page load, which no test
        caught because every browser test drove the engine directly rather than
        through the entrypoint. The client starts itself on first use.
        """
        self._client_started = True
        return self._client

    async def _ensure_client(self) -> SplunkClient:
        """Start the splunkd client on first use, not at launch.

        Enrichment must never be a precondition. A browser run whose target has
        a firewalled management port should still report what the user saw.
        """
        if not self._client_started:
            await self._client.start()
            self._client_started = True
        return self._client

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for entry in list(self._contexts.values()):
            try:
                await entry.context.close()
            except Exception:  # noqa: BLE001 - closing must not mask a result
                log.debug("closing a browser context failed", exc_info=True)
        self._contexts.clear()
        for closer in (self._browser, self._playwright):
            if closer is None:
                continue
            try:
                await (closer.close() if hasattr(closer, "close") else closer.stop())
            except Exception:  # noqa: BLE001
                log.debug("closing playwright failed", exc_info=True)
        self._browser = None
        self._playwright = None
        if self._client_started:
            await self._client.close()

    # ------------------------------------------------------------------
    # Probe and validation
    # ------------------------------------------------------------------

    async def probe(self) -> TargetCapabilities:
        """Reuses the management API: Splunk Web has nothing equivalent to ask."""
        from .api import ApiEngine

        engine = ApiEngine(self._config, client=await self._ensure_client())
        caps = await engine.probe()
        caps.notes.append(
            "browser engine: a Chromium context costs roughly 150 to 300 MB, so size "
            "the browser cohort by memory rather than by how many users you wish to "
            "simulate"
        )
        return caps

    async def validate(self, scenario: Scenario) -> List[str]:
        """Check the dashboards a scenario names actually exist.

        Cheaper and far more reliable through the management API than by
        driving a browser at each of them: a missing dashboard renders as a
        perfectly fast error page, which would otherwise be measured as a very
        quick load.
        """
        problems: List[str] = []
        seen = set()
        for persona in scenario.personas:
            for step in persona.steps:
                if step.type != "dashboard" or not step.dashboard:
                    continue
                key = (step.app, step.dashboard)
                if key in seen:
                    continue
                seen.add(key)
                path = f"/servicesNS/-/{step.app}/data/ui/views/{step.dashboard}"
                try:
                    entries = await (await self._ensure_client()).entries(path, "dashboard")
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    problems.append(
                        f"persona {persona.name} step {step.id}: could not confirm the "
                        f"dashboard {step.dashboard!r} exists ({exc})"
                    )
                    continue
                if not entries:
                    problems.append(
                        f"persona {persona.name} step {step.id}: no dashboard named "
                        f"{step.dashboard!r} in app {step.app!r}. A missing dashboard "
                        "renders as a fast error page, which would be measured as a "
                        "very quick load"
                    )
        return problems

    async def resolve_parameters(self, resolver: Any) -> None:
        from .api import ApiEngine

        client = await self._ensure_client()
        await ApiEngine(self._config, client=client).resolve_parameters(resolver)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, ctx: StepContext) -> StepRecord:
        record = ctx.blank_record()
        record.spl_hash = hashlib.sha256(
            f"{ctx.step.app}/{ctx.step.dashboard}".encode("utf-8")
        ).hexdigest()[:16]

        try:
            entry = await self._context_for(ctx.vu_id)
            page = entry.page

            # Login is outside the measured window. A real user logs in once
            # a day; measuring it inside the first iteration's service time
            # made iteration zero of every virtual user the slowest of the run
            # for a reason that had nothing to do with the dashboard.
            if not entry.logged_in:
                await self._login(page)
                entry.logged_in = True

            first_visit = entry.iterations == 0
            entry.iterations += 1
            record.params = {**record.params, "first_visit": first_visit}

            await self._load_dashboard(ctx, entry, record)
        except asyncio.CancelledError:
            raise
        except BrowserUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - an ordinary failure is a result
            record.ok = False
            record.error_class = self._classify(exc)
            record.error_detail = str(exc)[:500]
            if record.error_class == ERROR_AUTH:
                # Credentials being rejected is unrecoverable and would
                # otherwise produce thousands of identical failures.
                raise
        return record

    async def _load_dashboard(self, ctx: StepContext, entry: _Context, record: StepRecord) -> None:
        """One dashboard load, timed from the request to the data on screen."""
        page = entry.page
        sids: List[str] = []
        done_sids: set[str] = set()
        xhr_count = 0
        static_bytes = 0
        js_errors = 0
        pending_bodies: List[asyncio.Task[None]] = []

        async def read_status(response: Any, sid: str) -> None:
            # The page polls each job it started; the poll bodies say when a
            # job is done. Reading them means "a panel has data" can be
            # required to coincide with "its search finished", which is what
            # keeps a wrapper Studio renders before any data arrives from
            # counting as a painted panel.
            try:
                body = await response.text()
            except Exception:  # noqa: BLE001
                return
            if _IS_DONE_TRUE.search(body):
                done_sids.add(sid)

        def on_response(response: Any) -> None:
            nonlocal xhr_count, static_bytes
            url = response.url
            if _SEARCH_XHR.search(url):
                xhr_count += 1
                found = _SID_IN_PATH.search(url)
                if found:
                    sid = found.group(1)
                    if sid not in sids:
                        sids.append(sid)
                    if response.request.method == "GET":
                        pending_bodies.append(asyncio.ensure_future(read_status(response, sid)))
            elif any(ext in url for ext in (".js", ".css", ".woff2", ".png", ".svg")):
                try:
                    static_bytes += int(response.headers.get("content-length", 0) or 0)
                except (TypeError, ValueError):
                    pass

        def on_page_error(_error: Any) -> None:
            nonlocal js_errors
            js_errors += 1

        page.on("response", on_response)
        page.on("pageerror", on_page_error)
        began = time.perf_counter()
        try:
            url = self._dashboard_url(ctx)
            await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_STEP_TIMEOUT_S * 1000)

            if "/account/login" in page.url:
                # Splunk Web's session aged out mid-run (server.conf
                # sessionTimeout, an hour by default). Log in again once and
                # retry, rather than measuring a login page as a very fast
                # dashboard for the rest of the soak.
                entry.logged_in = False
                await self._login(page)
                entry.logged_in = True
                began = time.perf_counter()
                await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_STEP_TIMEOUT_S * 1000)
                record.params = {**record.params, "relogin": True}

            first_panel_ms, panels, settled_ms = await self._wait_for_panels(
                page, ctx, began, sids, done_sids
            )
            timings = await self._page_timings(page)
            timings.apply(record)

            record.first_panel_ms = first_panel_ms
            record.ttfr_ms = first_panel_ms
            record.panels = panels
            record.xhr_count = xhr_count
            record.static_bytes = static_bytes or None
            record.js_errors = js_errors
            record.sids = list(sids) or None
            if settled_ms is not None:
                record.params = {**record.params, "settled_ms": settled_ms}

            if first_panel_ms is None:
                # Nothing painted inside the timeout. That is the failure a
                # dashboard has, and filing it as a 120 second success hid
                # expired sessions, missing views and errored panels alike.
                record.ok = False
                record.error_class = ERROR_TIMEOUT
                record.error_detail = (
                    f"no panel showed data within {DEFAULT_STEP_TIMEOUT_S:.0f}s "
                    f"({xhr_count} search requests seen, {js_errors} JavaScript errors)"
                )
            else:
                # JavaScript errors are recorded, not fatal: Splunk Web throws
                # a benign one or two on most pages (telemetry beacons, a
                # third-party app's bundle), and failing every step on them
                # aborted runs that were measuring perfectly well.
                record.ok = True

            # Join back to the server's own accounting. The slowest job the
            # page ran is the one that decided when it settled, so that is
            # the one whose statistics describe the load.
            if sids:
                await self._attach_job_stats(record, sids, ctx)
        finally:
            page.remove_listener("response", on_response)
            page.remove_listener("pageerror", on_page_error)
            for task in pending_bodies:
                if not task.done():
                    task.cancel()
        record.service_time_ms = (time.perf_counter() - began) * 1000.0

    # ------------------------------------------------------------------

    async def _context_for(self, vu_id: int) -> _Context:
        """One browser context per virtual user, created once and kept.

        The whole point: a returning user has the bundle cached, so measuring a
        fresh context every iteration measures something nobody experiences.
        """
        entry = self._contexts.get(vu_id)
        if entry is not None:
            return entry
        async with self._lock:
            entry = self._contexts.get(vu_id)
            if entry is not None:
                return entry
            if self._browser is None:
                raise BrowserUnavailable("the browser engine was not started")
            context = await self._browser.new_context(
                viewport={"width": 1600, "height": 1000},
                ignore_https_errors=not self._config.target.verify_tls,
            )
            page = await context.new_page()
            entry = _Context(context=context, page=page)
            self._contexts[vu_id] = entry
            return entry

    async def _login(self, page: Any) -> None:
        """The native Splunk Web login form.

        Native auth only, deliberately. SAML cannot be driven headlessly at all
        (the password never reaches Splunk in an assertion), so a load-test
        identity has to be a local account even when every real user is
        federated.
        """
        target = self._config.target
        if not (target.username and target.password):
            raise BrowserUnavailable(
                "the browser engine needs REG_TARGET_USERNAME and REG_TARGET_PASSWORD. "
                "A bearer token authenticates the REST API but not Splunk Web's session, "
                "and SAML cannot be driven headlessly at all"
            )
        await page.goto(
            f"{target.web_url}/en-GB/account/login",
            wait_until="domcontentloaded",
            timeout=DEFAULT_STEP_TIMEOUT_S * 1000,
        )
        await page.locator("input[name='username']").first.fill(target.username)
        await page.locator("input[name='password']").first.fill(target.password)
        # Wait for the URL to leave the login page rather than for the load
        # state of the document that is still the login form. Under load the
        # form post takes long enough that reading page.url straight after
        # the click reported a rejected login that was merely slow.
        await page.locator("input[type='submit'], button[type='submit'], button").first.click()
        try:
            await page.wait_for_url(
                lambda url: "/account/login" not in url,
                timeout=DEFAULT_STEP_TIMEOUT_S * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - still on the login page
            raise PermissionError(
                f"Splunk Web did not leave the login page: {str(exc)[:120]}"
            ) from exc
        if "/account/login" in page.url:
            raise PermissionError("Splunk Web rejected the load-test credentials")

    def _dashboard_url(self, ctx: StepContext) -> str:
        """Build the dashboard URL, pinning the time range to the step's window.

        The same absolute epochs the API engine would dispatch with, so the two
        channels are asking the same question of the same data rather than
        whatever the dashboard's saved default happens to be.
        """
        target = self._config.target
        earliest = ctx.window.earliest_rel or f"{ctx.window.earliest:.0f}"
        latest = ctx.window.latest_rel or f"{ctx.window.latest:.0f}"
        params: Dict[str, str] = {
            # A dashboard whose time input has no token reads these.
            "earliest": earliest,
            "latest": latest,
            # Lands in the web access log, so a page load can be reconciled
            # against the target's own record of it.
            "_reg": ctx.marker,
        }
        if ctx.step.time_token:
            # A named time input reads form.<token>.earliest and ignores the
            # bare parameters, which is how every browser step ran the
            # dashboard's saved default for a while.
            params[f"form.{ctx.step.time_token}.earliest"] = earliest
            params[f"form.{ctx.step.time_token}.latest"] = latest
        for name, value in ctx.params.items():
            if name in ("first_visit", "relogin", "settled_ms"):
                continue
            params[f"form.{name}"] = str(value)
        return f"{target.web_url}/en-GB/app/{ctx.step.app}/{ctx.step.dashboard}?{urlencode(params)}"

    async def _wait_for_panels(
        self,
        page: Any,
        ctx: StepContext,
        began: float,
        sids: List[str],
        done_sids: set[str],
    ) -> Tuple[Optional[float], int, Optional[float]]:
        """Wait for what the operator asked for, and time it from the request.

        ``first_result`` is the number that maps to perceived responsiveness:
        when did anything appear. ``all_panels`` is when the page settled.
        They are different questions and a dashboard can be good at one and
        bad at the other, so the scenario chooses and both are recorded.

        "Anything appeared" means a data selector is visible AND, when the
        page has started searches, at least one of them has finished. Studio
        renders its visualisation wrappers before any data arrives, so the
        selector alone fired at layout time and measured React mounting.
        """
        selector = ", ".join(PANEL_READY_SELECTORS)
        deadline = began + DEFAULT_STEP_TIMEOUT_S
        first_panel_ms: Optional[float] = None
        panels = 0

        while time.perf_counter() < deadline:
            try:
                visible = await page.eval_on_selector_all(
                    selector,
                    "els => els.filter(e => e.offsetParent !== null || e.getClientRects().length).length",
                )
            except Exception:  # noqa: BLE001 - a navigation mid-query
                visible = 0
            searches_seen = bool(sids)
            if visible and (not searches_seen or done_sids):
                first_panel_ms = (time.perf_counter() - began) * 1000.0
                panels = int(visible or 0)
                break
            await asyncio.sleep(0.1)

        if first_panel_ms is None:
            return None, 0, None

        settled_ms: Optional[float] = None
        if ctx.step.wait_for == "all_panels":
            # Settled means every search the page started has finished. The
            # network never goes idle on a real Splunk page (it polls its own
            # health and messages), so networkidle was a 120 s timeout that
            # got swallowed and added to service time without being recorded.
            while time.perf_counter() < deadline:
                if sids and all(sid in done_sids for sid in sids):
                    break
                if not sids:
                    break
                await asyncio.sleep(0.2)
            settled_ms = (time.perf_counter() - began) * 1000.0
            try:
                panels = int(await page.eval_on_selector_all(selector, "els => els.length") or 0)
            except Exception:  # noqa: BLE001
                pass
        return first_panel_ms, panels, settled_ms

    async def _page_timings(self, page: Any) -> PageTimings:
        try:
            raw = await page.evaluate(_TIMING_SCRIPT)
        except Exception:  # noqa: BLE001 - timings are a bonus, not the result
            return PageTimings()
        if not isinstance(raw, dict):
            return PageTimings()
        return PageTimings(
            ttfb_ms=raw.get("ttfb_ms"),
            dom_content_loaded_ms=raw.get("dom_content_loaded_ms"),
            load_ms=raw.get("load_ms"),
            lcp_ms=raw.get("lcp_ms"),
        )

    async def _attach_job_stats(self, record: StepRecord, sids: List[str], ctx: StepContext) -> None:
        """The server's half, for the searches the page ran rather than we did.

        Reads up to a handful of the page's jobs and keeps the slowest, since
        that is the one that decided when the page settled. Also checks the
        job's own time range against the window the URL asked for, which is
        the only proof available that the dashboard honoured the pin.
        """
        try:
            client = await self._ensure_client()
        except Exception as exc:  # noqa: BLE001 - enrichment never fails a page measurement
            record.params = {**record.params, "job_stats": f"unavailable: {str(exc)[:120]}"}
            return
        slowest = None
        pinned: Optional[bool] = None
        for sid in sids[:MAX_JOBS_TO_JOIN]:
            try:
                job = await client.job_status(sid)
            except Exception:  # noqa: BLE001 - the job may already have aged out
                continue
            if slowest is None or (job.run_duration_s or 0) > (slowest.run_duration_s or 0):
                slowest = job
            if pinned is None and job.earliest_time and not ctx.window.is_relative:
                pinned = _within(job.earliest_time, ctx.window.earliest, 120.0)
        if slowest is None:
            record.params = {**record.params, "job_stats": "unavailable"}
            return
        record.sid = slowest.sid
        record.run_duration_s = slowest.run_duration_s
        record.scan_count = slowest.scan_count
        record.event_count = slowest.event_count
        record.result_count = slowest.result_count
        record.dispatch_state = slowest.dispatch_state
        if pinned is not None:
            record.params = {**record.params, "time_pinned": pinned}
            if not pinned:
                record.partial = True
                record.messages = [
                    "the dashboard's searches did not use the time range the URL asked for: "
                    "set time_token to the dashboard's time input token"
                ]

    @staticmethod
    def _classify(exc: Exception) -> str:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if isinstance(exc, PermissionError):
            return ERROR_AUTH
        if "timeout" in name or "timeout" in text:
            return ERROR_TIMEOUT
        if re.search(r"\b5\d\d\b", text) or "net::err_" in text:
            return ERROR_SERVER
        return ERROR_CLIENT


def _within(iso: str, epoch: float, tolerance_s: float) -> bool:
    """Whether an ISO-8601 time from splunkd is within tolerance of an epoch."""
    import datetime as _dt

    try:
        text = iso.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return abs(moment.timestamp() - epoch) <= tolerance_s
