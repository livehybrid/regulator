"""The browser engine against real Chromium.

Everything in test_browser_engine.py runs against a fake Playwright, which
proves the engine's decisions. This proves the engine actually works: real
Chromium, a real page, real XHRs to a real fake splunkd, real jobs with real
sids joined back to real job statistics.

Skipped cleanly when Playwright's browser binaries are not installed, because
they are a few hundred megabytes and the base image deliberately does not carry
them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import run_async
from regulator_agent.config import load_config
from regulator_agent.engines.base import StepContext
from regulator_agent.engines.browser import BrowserEngine
from regulator_agent.scenario import Step
from regulator_agent.timepolicy import TimeWindow

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

playwright = pytest.importorskip("playwright.async_api", reason="playwright is not installed")
fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
fake_web = pytest.importorskip("tools.fake_web", reason="the fake Splunk Web is not present")


def _chromium_available() -> bool:
    """Playwright is installed, but are the browser binaries?"""
    import asyncio

    async def check():
        try:
            async with playwright.async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                await browser.close()
            return True
        except Exception:  # noqa: BLE001 - any failure means "not usable here"
            return False

    try:
        return asyncio.run(check())
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="chromium is not installed (run: playwright install chromium)",
)


@pytest.fixture
def splunk_stack():
    """A fake splunkd and a fake Splunk Web in front of it, as one instance would be."""
    splunkd = fake_splunk.FakeSplunk(
        port=0, base_latency_ms=40.0, jitter_ms=5.0, dispatch_latency_ms=2.0, seed=3
    )
    web = fake_web.FakeSplunkWeb(port=0, splunkd=splunkd.base_url, panels=2)
    try:
        yield splunkd, web
    finally:
        web.close()
        splunkd.close()


def make_engine(env, splunkd, web) -> BrowserEngine:
    config = load_config(
        env(
            REG_TARGET_URL=splunkd.base_url,
            REG_TARGET_WEB_URL=web.base_url,
            REG_TARGET_TOKEN=None,
            REG_TARGET_USERNAME="loadtest",
            REG_TARGET_PASSWORD="changeme",
            REG_TARGET_VERIFY_TLS="0",
        )
    )
    return BrowserEngine(config)


def dashboard_context(vu_id=1, iteration=0, wait_for="first_result") -> StepContext:
    step = Step(
        id="open-dashboard",
        type="dashboard",
        engine="browser",
        app="regulator_sut",
        dashboard="triage_overview",
        wait_for=wait_for,
    )
    return StepContext(
        run_id="live",
        slot=0,
        vu_id=vu_id,
        iteration=iteration,
        persona="analyst",
        step=step,
        window=TimeWindow(earliest=1_756_800_000.0, latest=1_756_886_400.0),
        spl="",
        marker=f"reg:live:vu{vu_id}:i{iteration}:open-dashboard",
    )


def dashboards_served(web, name="triage_overview") -> int:
    """Count loads of one dashboard.

    Not web.stats.dashboards_served: a successful login redirects to
    /en-GB/app/search/home, which is legitimately a dashboard path and would
    otherwise be counted as an extra load of the one under test.
    """
    return sum(
        count for path, count in web.stats.paths.items()
        if path.startswith("GET ") and path.endswith("/" + name)
    )


def test_a_real_browser_loads_a_dashboard_and_captures_its_searches(env, splunk_stack):
    """The whole point, end to end.

    Chromium logs in, loads a page, the page runs real searches through the raw
    proxy, and the engine captures their sids and joins them back to the job
    statistics splunkd reports. That join is what makes a browser number and an
    API number comparable rather than merely adjacent.
    """
    splunkd, web = splunk_stack
    engine = make_engine(env, splunkd, web)

    async def go():
        await engine.start()
        try:
            return await engine.execute(dashboard_context())
        finally:
            await engine.close()

    record = run_async(go())

    assert record.ok is True, record.error_detail
    assert record.first_panel_ms and record.first_panel_ms > 0
    assert record.panels >= 1
    # Real Navigation Timing from a real page.
    assert record.ttfb_ms is not None
    assert record.dom_content_loaded_ms is not None
    # Real searches, captured from the wire.
    assert record.sids, "no search XHR was captured from the page"
    assert record.xhr_count >= 1
    # And joined back to what splunkd says it did.
    assert record.dispatch_state == "DONE"
    assert record.scan_count is not None
    assert web.stats.logins == 1
    assert dashboards_served(web) == 1


def test_the_bundle_is_only_paid_for_once_per_virtual_user(env, splunk_stack):
    """The persistent-context decision, measured rather than asserted.

    A returning user has Splunk Web's assets cached, so the second iteration
    must not re-log-in or re-fetch the page shell.
    """
    splunkd, web = splunk_stack
    engine = make_engine(env, splunkd, web)

    async def go():
        await engine.start()
        try:
            return [await engine.execute(dashboard_context(iteration=i)) for i in range(3)]
        finally:
            await engine.close()

    records = run_async(go())

    assert all(r.ok for r in records), [r.error_detail for r in records]
    assert web.stats.logins == 1
    assert dashboards_served(web) == 3
    assert records[0].params["first_visit"] is True
    assert records[1].params["first_visit"] is False


def test_a_page_that_throws_is_reported_as_a_failed_step(env):
    """A dashboard that renders fast and throws is broken, not fast."""
    splunkd = fake_splunk.FakeSplunk(port=0, base_latency_ms=20.0, seed=5)
    web = fake_web.FakeSplunkWeb(
        port=0,
        splunkd=splunkd.base_url,
        panels=1,
        extra_script="setTimeout(() => { throw new Error('panel blew up'); }, 10);",
    )
    try:
        engine = make_engine(_env_factory(splunkd, web), splunkd, web)

        async def go():
            await engine.start()
            try:
                return await engine.execute(dashboard_context())
            finally:
                await engine.close()

        record = run_async(go())
        assert record.js_errors >= 1
        assert record.ok is False
    finally:
        web.close()
        splunkd.close()


def _env_factory(splunkd, web):
    """A minimal stand-in for the shared env fixture, for tests building their own stack."""

    def _env(**overrides):
        base = {
            "REG_STANDALONE": "1",
            "REG_SCENARIO": "smoke",
            "REG_TARGET_URL": splunkd.base_url,
            "REG_TARGET_WEB_URL": web.base_url,
            "REG_TARGET_USERNAME": "loadtest",
            "REG_TARGET_PASSWORD": "changeme",
            "REG_TARGET_VERIFY_TLS": "0",
        }
        for key, value in overrides.items():
            if value is None:
                base.pop(key, None)
            else:
                base[key] = value
        return base

    return _env


def test_wrong_credentials_are_rejected_by_the_real_login_form(env, splunk_stack):
    splunkd, web = splunk_stack
    config = load_config(
        env(
            REG_TARGET_URL=splunkd.base_url,
            REG_TARGET_WEB_URL=web.base_url,
            REG_TARGET_TOKEN=None,
            REG_TARGET_USERNAME="loadtest",
            REG_TARGET_PASSWORD="definitely-wrong",
            REG_TARGET_VERIFY_TLS="0",
        )
    )
    engine = BrowserEngine(config)

    async def go():
        await engine.start()
        try:
            with pytest.raises(PermissionError):
                await engine.execute(dashboard_context())
        finally:
            await engine.close()

    run_async(go())
