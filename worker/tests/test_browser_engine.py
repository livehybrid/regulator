"""The browser engine, against a fake Playwright.

No real Chromium here. The behaviours worth protecting are decisions the engine
makes (reuse a context, log in once, capture the sids the page fires, treat a
missing panel as a result rather than a crash) and every one of those is
testable without paying 300 MB and a page render for it. A real browser check
belongs in the smoke, where it exercises the packaging rather than the logic.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from conftest import run_async
from regulator_agent.config import load_config
from regulator_agent.engines.base import StepContext
from regulator_agent.engines.browser import BrowserEngine, BrowserUnavailable
from regulator_agent.results import ERROR_AUTH, ERROR_CLIENT, ERROR_TIMEOUT
from regulator_agent.scenario import Step
from regulator_agent.timepolicy import TimeWindow


# --------------------------------------------------------------- the fake
#
# Only the surface the engine actually touches. Anything beyond that is a
# detail of Playwright rather than of Regulator, and pinning it here would make
# the suite fail on an upgrade that changed nothing we care about.


class FakeResponse:
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.url = url
        self.headers = headers or {}


class FakePage:
    def __init__(self, owner: "FakeContext") -> None:
        self.owner = owner
        self.url = ""
        self.filled: Dict[str, str] = {}
        self.clicks: List[str] = []
        self.goto_urls: List[str] = []
        self._listeners: Dict[str, List[Any]] = {}
        self.panels_found = 3
        self.panel_timeout = False
        self.responses_to_emit: List[FakeResponse] = []
        self.page_errors = 0
        self.timings = {
            "ttfb_ms": 42.0,
            "dom_content_loaded_ms": 180.0,
            "load_ms": 640.0,
            "lcp_ms": 500.0,
        }
        self.login_fails = False

    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self._listeners.get(event, []).remove(handler)

    def _emit(self) -> None:
        for response in self.responses_to_emit:
            for handler in self._listeners.get("response", []):
                handler(response)
        for _ in range(self.page_errors):
            for handler in self._listeners.get("pageerror", []):
                handler(RuntimeError("boom"))

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.goto_urls.append(url)
        self.url = url
        if "/account/login" in url and self.login_fails:
            return
        if "/account/login" not in url:
            self._emit()

    async def fill(self, selector: str, value: str) -> None:
        self.filled[selector] = value

    async def click(self, selector: str) -> None:
        self.clicks.append(selector)
        if not self.login_fails:
            self.url = f"{self.owner.web_url}/en-GB/app/search/home"

    async def wait_for_load_state(self, *_a: Any, **_k: Any) -> None:
        return None

    async def wait_for_selector(self, *_a: Any, **_k: Any) -> None:
        if self.panel_timeout:
            raise TimeoutError("no panel appeared")

    async def eval_on_selector_all(self, *_a: Any, **_k: Any) -> int:
        return self.panels_found

    async def evaluate(self, *_a: Any, **_k: Any) -> Dict[str, Any]:
        return dict(self.timings)


class FakeContext:
    def __init__(self, web_url: str) -> None:
        self.web_url = web_url
        self.pages: List[FakePage] = []
        self.closed = False

    async def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, web_url: str) -> None:
        self.web_url = web_url
        self.contexts: List[FakeContext] = []
        self.launch_args: List[str] = []
        self.closed = False

    async def new_context(self, **_kwargs: Any) -> FakeContext:
        context = FakeContext(self.web_url)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    async def launch(self, **kwargs: Any) -> FakeBrowser:
        self._browser.launch_args = list(kwargs.get("args") or [])
        return self._browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeFactory:
    """Stands in for playwright.async_api.async_playwright()."""

    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser
        self.playwright = FakePlaywright(browser)

    def __call__(self) -> "FakeFactory":
        return self

    async def start(self) -> FakePlaywright:
        return self.playwright


# -------------------------------------------------------------- fixtures


WEB_URL = "https://splunk.example:8000"


def make_engine(env, browser: FakeBrowser, **overrides) -> BrowserEngine:
    config = load_config(
        env(
            REG_TARGET_WEB_URL=WEB_URL,
            REG_TARGET_TOKEN=None,
            REG_TARGET_USERNAME="loadtest",
            REG_TARGET_PASSWORD="pw",
            REG_TARGET_VERIFY_TLS="0",
            **overrides,
        )
    )
    return BrowserEngine(config, playwright_factory=FakeFactory(browser))


def dashboard_step(step_id="open-dashboard", wait_for="first_result") -> Step:
    return Step(
        id=step_id,
        type="dashboard",
        engine="browser",
        app="regulator_sut",
        dashboard="triage_overview",
        wait_for=wait_for,
    )


def context_for(step: Step, vu_id: int = 1, iteration: int = 0) -> StepContext:
    return StepContext(
        run_id="btest",
        slot=0,
        vu_id=vu_id,
        iteration=iteration,
        persona="analyst",
        step=step,
        window=TimeWindow(earliest=1_756_800_000.0, latest=1_756_886_400.0),
        spl="",
        marker=f"reg:btest:vu{vu_id}:i{iteration}:{step.id}",
    )


def drive(engine: BrowserEngine, contexts: List[StepContext]):
    async def go():
        await engine.start()
        try:
            return [await engine.execute(c) for c in contexts]
        finally:
            await engine.close()

    return run_async(go())


# ----------------------------------------------------------------- tests


def test_a_dashboard_load_records_what_the_user_experienced(env):
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)
    step = dashboard_step()

    records = drive(engine, [context_for(step)])
    record = records[0]

    assert record.ok is True
    assert record.step_type == "dashboard"
    assert record.first_panel_ms is not None
    # Perceived responsiveness is the first panel, so time-to-first-result
    # mirrors it rather than being left empty for a browser step.
    assert record.ttfr_ms == record.first_panel_ms
    assert record.panels == 3
    assert record.ttfb_ms == 42.0
    assert record.lcp_ms == 500.0
    assert record.service_time_ms > 0
    # The scheduler owns these, exactly as for the API engine.
    assert record.latency_ms == 0.0


def test_the_context_is_reused_across_a_virtual_users_iterations(env):
    """The whole reason the engine is shaped this way.

    A fresh context re-downloads the Splunk Web bundle every time, which
    measures something no returning user experiences.
    """
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)
    step = dashboard_step()

    records = drive(engine, [context_for(step, iteration=i) for i in range(4)])

    assert len(browser.contexts) == 1
    assert len(browser.contexts[0].pages) == 1
    # Only the first iteration is a cold bundle load, and it is labelled.
    assert records[0].params["first_visit"] is True
    assert all(r.params["first_visit"] is False for r in records[1:])


def test_each_virtual_user_gets_its_own_context(env):
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)
    step = dashboard_step()

    drive(engine, [context_for(step, vu_id=vu) for vu in (1, 2, 3)])
    assert len(browser.contexts) == 3


def test_login_happens_once_per_context_not_once_per_iteration(env):
    """A real user logs in once a day, not once a dashboard.

    Doing it per iteration would measure the login endpoint and add a load no
    real population generates.
    """
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)
    step = dashboard_step()

    drive(engine, [context_for(step, iteration=i) for i in range(5)])

    page = browser.contexts[0].pages[0]
    logins = [u for u in page.goto_urls if "/account/login" in u]
    assert len(logins) == 1
    assert page.filled["input[name='username']"] == "loadtest"
    assert page.clicks


def test_rejected_credentials_are_fatal_rather_than_repeated(env):
    """Otherwise a bad password produces thousands of identical failures."""
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)

    async def go():
        await engine.start()
        browser.contexts_to_fail = True
        try:
            context = await engine._context_for(1)
            context.page.login_fails = True
            with pytest.raises(PermissionError):
                await engine._login(context.page)
        finally:
            await engine.close()

    run_async(go())


def test_the_dashboard_url_pins_the_step_time_window(env):
    """The browser must ask the same question of the same data as the API engine.

    Without this the page uses whatever time range it was saved with, and the
    two channels are not comparable.
    """
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)
    step = dashboard_step()

    drive(engine, [context_for(step)])

    page = browser.contexts[0].pages[0]
    dashboard_urls = [u for u in page.goto_urls if "/app/" in u]
    assert dashboard_urls
    url = dashboard_urls[0]
    assert "earliest=1756800000" in url
    assert "latest=1756886400" in url
    assert "regulator_sut/triage_overview" in url
    # The marker reaches the access log and _audit, so a page load can be
    # reconciled against the target's own record of it.
    assert "_reg=reg%3Abtest" in url or "_reg=reg:btest" in url


def test_search_xhrs_are_captured_with_their_sids(env):
    """What makes a browser number joinable to a server-side one."""
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)
    step = dashboard_step()

    async def go():
        await engine.start()
        try:
            entry = await engine._context_for(1)
            entry.page.responses_to_emit = [
                FakeResponse(f"{WEB_URL}/en-GB/splunkd/__raw/services/search/v2/jobs/SID-111"),
                FakeResponse(f"{WEB_URL}/en-GB/splunkd/__raw/services/search/v2/jobs/SID-222"),
                FakeResponse(f"{WEB_URL}/static/app/search/bundle.js", {"content-length": "9000"}),
            ]
            return await engine.execute(context_for(step))
        finally:
            await engine.close()

    record = run_async(go())
    assert record.sids == ["SID-111", "SID-222"]
    assert record.xhr_count == 2
    assert record.static_bytes == 9000


def test_a_namespaced_search_xhr_is_also_recognised(env):
    """Splunk Web uses servicesNS as well as services, depending on the app."""
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)

    async def go():
        await engine.start()
        try:
            entry = await engine._context_for(1)
            entry.page.responses_to_emit = [
                FakeResponse(
                    f"{WEB_URL}/en-GB/splunkd/__raw/servicesNS/admin/search/search/jobs/SID-NS"
                )
            ]
            return await engine.execute(context_for(dashboard_step()))
        finally:
            await engine.close()

    assert run_async(go()).sids == ["SID-NS"]


def test_a_dashboard_that_never_paints_is_a_result_not_a_crash(env):
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)

    async def go():
        await engine.start()
        try:
            entry = await engine._context_for(1)
            entry.page.panel_timeout = True
            return await engine.execute(context_for(dashboard_step()))
        finally:
            await engine.close()

    record = run_async(go())
    assert record.first_panel_ms is None
    assert record.panels == 0
    # Still a completed measurement: the page loaded, nothing rendered.
    assert record.service_time_ms > 0


def test_javascript_errors_fail_the_step_and_are_counted(env):
    """A dashboard that throws is broken even if it renders quickly."""
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)

    async def go():
        await engine.start()
        try:
            entry = await engine._context_for(1)
            entry.page.page_errors = 2
            return await engine.execute(context_for(dashboard_step()))
        finally:
            await engine.close()

    record = run_async(go())
    assert record.ok is False
    assert record.js_errors == 2
    assert record.error_class == ERROR_CLIENT


def test_chromium_is_launched_with_the_shared_memory_workaround(env):
    """Chromium's default /dev/shm in a container is too small for a real page.

    Without this the tab crashes and it looks like a flaky test.
    """
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)
    drive(engine, [])
    assert "--disable-dev-shm-usage" in browser.launch_args


def test_closing_is_safe_twice_and_closes_every_context(env):
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)

    async def go():
        await engine.start()
        for vu in (1, 2):
            await engine._context_for(vu)
        await engine.close()
        await engine.close()

    run_async(go())
    assert all(c.closed for c in browser.contexts)
    assert browser.closed


def test_a_target_without_a_web_url_is_refused_with_an_explanation(env):
    config = load_config(env(REG_TARGET_WEB_URL=None))
    engine = BrowserEngine(config, playwright_factory=FakeFactory(FakeBrowser(WEB_URL)))

    async def go():
        with pytest.raises(BrowserUnavailable) as excinfo:
            await engine.start()
        assert "REG_TARGET_WEB_URL" in str(excinfo.value)

    run_async(go())


def test_a_bearer_token_alone_cannot_drive_splunk_web(env):
    """A token authenticates the REST API but not Splunk Web's session.

    SAML cannot be driven headlessly at all, so a browser identity has to be a
    native account even when every real user is federated.
    """
    browser = FakeBrowser(WEB_URL)
    config = load_config(
        env(REG_TARGET_WEB_URL=WEB_URL, REG_TARGET_TOKEN="just-a-token")
    )
    engine = BrowserEngine(config, playwright_factory=FakeFactory(browser))

    async def go():
        await engine.start()
        try:
            entry = await engine._context_for(1)
            with pytest.raises(BrowserUnavailable) as excinfo:
                await engine._login(entry.page)
            assert "REG_TARGET_USERNAME" in str(excinfo.value)
        finally:
            await engine.close()

    run_async(go())


@pytest.mark.parametrize(
    "exc, expected",
    [
        (TimeoutError("timed out"), ERROR_TIMEOUT),
        (PermissionError("credentials rejected"), ERROR_AUTH),
        (RuntimeError("something else"), ERROR_CLIENT),
    ],
)
def test_failures_are_classified(exc, expected):
    assert BrowserEngine._classify(exc) == expected


def test_all_panels_waits_for_the_page_to_settle(env):
    """first_result and all_panels are different questions.

    A dashboard can paint one panel instantly and take a minute to finish, and
    the scenario chooses which of those it is measuring.
    """
    browser = FakeBrowser(WEB_URL)
    engine = make_engine(env, browser)
    settled: List[str] = []

    async def go():
        await engine.start()
        try:
            entry = await engine._context_for(1)
            original = entry.page.wait_for_load_state

            async def spy(*args, **kwargs):
                settled.append(args[0] if args else kwargs.get("state", ""))
                return await original(*args, **kwargs)

            entry.page.wait_for_load_state = spy
            await engine.execute(context_for(dashboard_step(wait_for="all_panels")))
        finally:
            await engine.close()

    run_async(go())
    assert "networkidle" in settled
