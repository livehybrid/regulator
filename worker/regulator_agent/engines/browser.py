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
_SID_IN_BODY = re.compile(r'"sid"\s*:\s*"([^"]+)"')
_SID_IN_PATH = re.compile(r"/jobs/([^/?]+)")

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
        began = time.perf_counter()

        try:
            entry = await self._context_for(ctx.vu_id)
            first_visit = entry.iterations == 0
            entry.iterations += 1

            sids: List[str] = []
            xhr_count = 0
            static_bytes = 0
            js_errors = 0

            page = entry.page

            def on_response(response: Any) -> None:
                nonlocal xhr_count, static_bytes
                url = response.url
                if _SEARCH_XHR.search(url):
                    xhr_count += 1
                    found = _SID_IN_PATH.search(url)
                    if found and found.group(1) not in sids:
                        sids.append(found.group(1))
                elif any(url.endswith(ext) for ext in (".js", ".css", ".woff2", ".png", ".svg")):
                    try:
                        static_bytes += int(response.headers.get("content-length", 0) or 0)
                    except (TypeError, ValueError):
                        pass

            def on_page_error(_error: Any) -> None:
                nonlocal js_errors
                js_errors += 1

            page.on("response", on_response)
            page.on("pageerror", on_page_error)

            try:
                if not entry.logged_in:
                    await self._login(page)
                    entry.logged_in = True

                url = self._dashboard_url(ctx)
                await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_STEP_TIMEOUT_S * 1000)

                first_panel_ms, panels = await self._wait_for_panels(page, ctx)
                timings = await self._page_timings(page)
                timings.apply(record)

                record.first_panel_ms = first_panel_ms
                record.ttfr_ms = first_panel_ms
                record.panels = panels
                record.xhr_count = xhr_count
                record.static_bytes = static_bytes or None
                record.js_errors = js_errors
                record.sids = list(sids) or None
                record.params = {**record.params, "first_visit": first_visit}

                # Join back to the server's own accounting for whichever search
                # the page ran. This is what makes a browser number and an API
                # number comparable rather than merely adjacent.
                if sids:
                    await self._attach_job_stats(record, sids[0])

                record.ok = js_errors == 0
                if js_errors:
                    record.error_class = ERROR_CLIENT
                    record.error_detail = f"{js_errors} JavaScript error(s) on the page"
            finally:
                page.remove_listener("response", on_response)
                page.remove_listener("pageerror", on_page_error)

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

        record.service_time_ms = (time.perf_counter() - began) * 1000.0
        return record

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
        await page.fill("input[name='username']", target.username)
        await page.fill("input[name='password']", target.password)
        await page.click("input[type='submit'], button[type='submit']")
        await page.wait_for_load_state("domcontentloaded")
        if "/account/login" in page.url:
            raise PermissionError("Splunk Web rejected the load-test credentials")

    def _dashboard_url(self, ctx: StepContext) -> str:
        """Build the dashboard URL, pinning the time range to the step's window.

        The same absolute epochs the API engine would dispatch with, so the two
        channels are asking the same question of the same data rather than
        whatever the dashboard's saved default happens to be.
        """
        target = self._config.target
        query = urlencode(
            {
                "earliest": f"{ctx.window.earliest:.0f}",
                "latest": f"{ctx.window.latest:.0f}",
                # Lands in the access log and in _audit, so a page load can be
                # reconciled against the target's own record of it.
                "_reg": ctx.marker,
            }
        )
        return f"{target.web_url}/en-GB/app/{ctx.step.app}/{ctx.step.dashboard}?{query}"

    async def _wait_for_panels(self, page: Any, ctx: StepContext) -> Tuple[Optional[float], int]:
        """Wait for what the operator asked for, and time it.

        ``first_result`` is the number that maps to perceived responsiveness:
        when did anything appear. ``all_panels`` is when the page settled. They
        are different questions and a dashboard can be good at one and bad at
        the other, so the scenario chooses.
        """
        began = time.perf_counter()
        selector = ", ".join(PANEL_READY_SELECTORS)
        try:
            await page.wait_for_selector(
                selector, timeout=DEFAULT_STEP_TIMEOUT_S * 1000, state="visible"
            )
        except Exception:  # noqa: BLE001 - no panel is a result, not a crash
            return None, 0

        first_panel_ms = (time.perf_counter() - began) * 1000.0

        if ctx.step.wait_for == "all_panels":
            try:
                await page.wait_for_load_state("networkidle", timeout=DEFAULT_STEP_TIMEOUT_S * 1000)
            except Exception:  # noqa: BLE001 - a busy dashboard may never idle
                log.debug("the page never reached network idle", exc_info=True)

        try:
            panels = await page.eval_on_selector_all(selector, "els => els.length")
        except Exception:  # noqa: BLE001
            panels = 0
        return first_panel_ms, int(panels or 0)

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

    async def _attach_job_stats(self, record: StepRecord, sid: str) -> None:
        """The server's half, for a search the page ran rather than we did."""
        try:
            client = await self._ensure_client()
            job = await client.job_status(sid)
        except Exception:  # noqa: BLE001 - the job may already have aged out, and
            # enrichment failing must never turn a good page measurement into a
            # failed step.
            return
        record.sid = sid
        record.run_duration_s = job.run_duration_s
        record.scan_count = job.scan_count
        record.event_count = job.event_count
        record.result_count = job.result_count
        record.dispatch_state = job.dispatch_state

    @staticmethod
    def _classify(exc: Exception) -> str:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if isinstance(exc, PermissionError) or "credential" in text or "unauthor" in text:
            return ERROR_AUTH
        if "timeout" in name or "timeout" in text:
            return ERROR_TIMEOUT
        if "5" == text[:1] and "server" in text:
            return ERROR_SERVER
        return ERROR_CLIENT
