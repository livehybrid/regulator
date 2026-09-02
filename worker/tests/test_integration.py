"""End to end against a real socket.

Everything else in the suite is a unit test against a fake. This file starts
the fake splunkd from ``tools/``, points the real :class:`SplunkClient` and the
real :class:`ApiEngine` at it over HTTP, and drives a whole scheduler run
through it. It is the test that would catch a broken URL, a mis-parsed job
status, a form field with the wrong name, or a poll loop that never terminates:
the entire class of bug a mock cannot see because the mock was written from the
same misunderstanding as the code.

The fake's latency knobs are turned right down, so the whole file runs in a few
seconds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import run_async, tiny_scenario_dict
from regulator_agent.config import load_config
from regulator_agent.engines.api import ApiEngine
from regulator_agent.engines.base import StepContext
from regulator_agent.params import ParameterResolver
from regulator_agent.results import ERROR_SEARCH_FAILED, ERROR_SERVER
from regulator_agent.scenario import Step, parse_scenario
from regulator_agent.scheduler import Scheduler
from regulator_agent.splunk import SplunkAuthError, SplunkClient
from regulator_agent.timepolicy import TimeWindow

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk


@pytest.fixture
def splunkd():
    """A fast fake splunkd. Latencies are tiny so the suite stays quick."""
    server = FakeSplunk(
        port=0,
        base_latency_ms=15.0,
        jitter_ms=5.0,
        dispatch_latency_ms=2.0,
        seed=1234,
    )
    try:
        yield server
    finally:
        server.close()


def make_config(splunkd, env, **overrides):
    """Point a config at the fake, polling fast.

    The production default of 250 ms between polls is right against a real
    cluster (polling hard is itself load on the target) but here it would
    dominate every measurement and hide short-lived states such as QUEUED,
    which is one of the things these tests exist to observe.
    """
    defaults = {"REG_POLL_INITIAL_MS": "10", "REG_POLL_MAX_MS": "50"}
    defaults.update(overrides)
    return load_config(
        env(REG_TARGET_URL=splunkd.base_url, REG_TARGET_VERIFY_TLS="0", **defaults)
    )


def window():
    return TimeWindow(earliest=1_756_800_000.0, latest=1_756_886_400.0)


# --------------------------------------------------------------- the client


def test_the_client_walks_a_whole_job_lifecycle(splunkd, env):
    async def scenario():
        client = SplunkClient(make_config(splunkd, env).target)
        await client.start()
        try:
            info = await client.server_info()
            assert info["version"] == "10.4.0"

            limits = await client.search_limits()
            assert limits["base_max_searches"] == "6"

            sid, dispatch_ms, status = await client.create_job(
                "search index=main | stats count", window()
            )
            assert sid
            assert dispatch_ms >= 0
            assert status in (200, 201)

            # Poll to completion.
            for _ in range(200):
                job = await client.job_status(sid)
                if job.is_done or job.is_failed:
                    break
                await _pause()
            else:  # pragma: no cover - only on a pathological failure
                pytest.fail("the job never finished")

            assert job.dispatch_state == "DONE"
            assert job.run_duration_s is not None and job.run_duration_s > 0
            assert job.scan_count and job.scan_count > 0
            assert job.result_count is not None

            rows, byte_count = await client.fetch_results(sid, count=5)
            assert rows and byte_count > 0

            await client.delete_job(sid)
            # Deleting twice must not raise: a job can age out via its TTL, and
            # that is not an error the run should care about.
            await client.delete_job(sid)
        finally:
            await client.close()
            await client.close()  # must be safe twice

    run_async(scenario())


async def _pause():
    import asyncio

    await asyncio.sleep(0.01)


def test_a_bad_token_is_fatal_rather_than_retried_forever(splunkd, env):
    async def scenario():
        server = FakeSplunk(port=0, reject_token="rejected-token", base_latency_ms=5.0)
        try:
            config = load_config(
                env(
                    REG_TARGET_URL=server.base_url,
                    REG_TARGET_TOKEN="rejected-token",
                    REG_TARGET_VERIFY_TLS="0",
                )
            )
            client = SplunkClient(config.target)
            await client.start()
            try:
                with pytest.raises(SplunkAuthError):
                    await client.create_job("search index=main", window())
            finally:
                await client.close()
        finally:
            server.close()

    run_async(scenario())


def test_username_and_password_authentication_works(splunkd, env):
    async def scenario():
        config = load_config(
            env(
                REG_TARGET_URL=splunkd.base_url,
                REG_TARGET_TOKEN=None,
                REG_TARGET_USERNAME="loadtest",
                REG_TARGET_PASSWORD="changeme",
                REG_TARGET_VERIFY_TLS="0",
            )
        )
        client = SplunkClient(config.target)
        await client.start()
        try:
            sid, _, _ = await client.create_job("search index=main | head 1", window())
            assert sid
        finally:
            await client.close()

    run_async(scenario())


# --------------------------------------------------------------- the engine


def search_step(step_id="probe", spl="search index=main | stats count", **kwargs):
    return Step(id=step_id, type="search", engine="api", spl=spl, **kwargs)


def context_for(step, spl=None):
    return StepContext(
        run_id="itest",
        slot=0,
        vu_id=1,
        iteration=0,
        persona="p",
        step=step,
        window=window(),
        spl=spl or step.spl,
        marker="reg:itest:vu1:i0:probe",
    )


def test_the_engine_records_both_halves_of_the_measurement(splunkd, env):
    """Client-side timing and the server's own accounting, in one record."""

    async def scenario():
        engine = ApiEngine(make_config(splunkd, env))
        await engine.start()
        try:
            record = await engine.execute(context_for(search_step(result_count=5)))
        finally:
            await engine.close()
        return record

    record = run_async(scenario())

    assert record.ok is True
    assert record.sid
    assert record.dispatch_ms is not None and record.dispatch_ms >= 0
    assert record.service_time_ms > 0
    assert record.ttfr_ms is not None
    assert record.poll_count >= 1
    assert record.run_duration_s is not None
    assert record.scan_count and record.scan_count > 0
    assert record.result_bytes and record.result_bytes > 0
    assert record.dispatch_state == "DONE"
    # The scheduler owns these two, so the engine must leave them alone.
    assert record.latency_ms == 0.0
    assert record.co_corrected is False


def test_a_failed_search_is_a_result_not_an_exception(env):
    """A load test that stops at the first error measures nothing useful."""

    async def scenario():
        server = FakeSplunk(port=0, base_latency_ms=10.0, fail_rate=1.0, seed=7)
        try:
            engine = ApiEngine(make_config(server, env))
            await engine.start()
            try:
                return await engine.execute(context_for(search_step()))
            finally:
                await engine.close()
        finally:
            server.close()

    record = run_async(scenario())
    assert record.ok is False
    assert record.error_class == ERROR_SEARCH_FAILED
    assert record.service_time_ms > 0


def test_the_probe_computes_the_concurrency_ceiling(splunkd, env):
    async def scenario():
        engine = ApiEngine(make_config(splunkd, env))
        await engine.start()
        try:
            return await engine.probe()
        finally:
            await engine.close()

    caps = run_async(scenario())
    assert caps.version == "10.4.0"
    assert caps.cpu_count == 8
    # base_max_searches + (max_searches_per_cpu * cores) = 6 + 8.
    assert caps.max_hist_searches == 14


def test_online_validation_catches_broken_spl(env):
    async def scenario():
        server = FakeSplunk(port=0, base_latency_ms=5.0, strict_spl=True)
        try:
            engine = ApiEngine(make_config(server, env))
            await engine.start()
            try:
                doc = tiny_scenario_dict()
                doc["personas"][0]["steps"][0]["spl"] = "search SYNTAX_ERROR bad"
                return await engine.validate(parse_scenario(doc))
            finally:
                await engine.close()
        finally:
            server.close()

    problems = run_async(scenario())
    assert any("one" in p for p in problems), problems


def test_dynamic_parameters_are_resolved_from_the_target(splunkd, env):
    async def scenario():
        engine = ApiEngine(make_config(splunkd, env))
        await engine.start()
        try:
            doc = tiny_scenario_dict()
            doc["parameters"] = {
                "host": {
                    "type": "choice_from_search",
                    "spl": "search index=main | stats count by host",
                    "field": "host",
                    "limit": 10,
                }
            }
            doc["personas"][0]["steps"][0]["spl"] = 'search index=main host="{{host}}"'
            scenario_obj = parse_scenario(doc)
            resolver = ParameterResolver(scenario_obj)
            await engine.resolve_parameters(resolver)
            return resolver
        finally:
            await engine.close()

    resolver = run_async(scenario())
    from regulator_agent.params import DrawContext

    value = resolver.value("host", DrawContext(7, 1, 1, "one"))
    assert isinstance(value, str) and value


def test_the_same_logical_search_hashes_the_same_despite_cache_busting(splunkd, env):
    """Without this, every iteration looks like a unique search.

    Aggregating results by search would then be impossible, which is most of
    the value of the report.
    """

    async def scenario():
        engine = ApiEngine(make_config(splunkd, env))
        await engine.start()
        try:
            step = search_step()
            a = await engine.execute(
                context_for(step, spl="search index=main | stats count ```reg:a:vu1:i0:x```")
            )
            b = await engine.execute(
                context_for(step, spl="search index=main | stats count ```reg:a:vu1:i9:x```")
            )
            c = await engine.execute(
                context_for(step, spl="search index=other | stats count ```reg:a:vu1:i0:x```")
            )
            return a, b, c
        finally:
            await engine.close()

    a, b, c = run_async(scenario())
    assert a.spl_hash == b.spl_hash
    assert a.spl_hash != c.spl_hash


def test_jobs_are_deleted_unless_told_otherwise(splunkd, env):
    async def scenario(delete: str):
        engine = ApiEngine(make_config(splunkd, env, REG_DELETE_JOBS=delete))
        await engine.start()
        try:
            await engine.execute(context_for(search_step()))
        finally:
            await engine.close()

    before = splunkd.stats.jobs_deleted
    run_async(scenario("1"))
    assert splunkd.stats.jobs_deleted == before + 1

    middle = splunkd.stats.jobs_deleted
    run_async(scenario("0"))
    assert splunkd.stats.jobs_deleted == middle


# ------------------------------------------------------------ a whole run


def test_a_whole_scheduler_run_against_a_real_socket(splunkd, env):
    """The full path: scenario, parameters, scheduler, engine, HTTP, records."""
    doc = tiny_scenario_dict()
    doc["load"] = {"model": "closed", "virtual_users": 4, "duration": "2s"}
    scenario_obj = parse_scenario(doc)
    config = make_config(splunkd, env)
    resolver = ParameterResolver(scenario_obj)
    engine = ApiEngine(config)

    records = []

    class Sink:
        def emit(self, record):
            records.append(record)

    async def go():
        await engine.start()
        try:
            caps = await engine.probe()
            scheduler = Scheduler(
                scenario=scenario_obj,
                config=config,
                engine=engine,
                resolver=resolver,
                emitters=[Sink()],
                capabilities=caps,
            )
            return await scheduler.run()
        finally:
            await engine.close()

    summary = run_async(go())

    assert summary.outcome == "completed"
    assert summary.valid is True
    # Deliberately not a throughput assertion. This suite has to pass on a
    # heavily loaded CI box, and asserting a rate would make it a flake
    # detector for the runner rather than a correctness test for Regulator.
    assert summary.stats["executions"] >= 4
    assert records
    assert all(r.sid for r in records)
    assert all(r.ok for r in records)
    assert summary.stats["latency"]["p95_ms"] > 0
    # Both steps of the persona ran, and the cache-busting marker reached the
    # target: the fake records every search string it was sent.
    assert {r.step_id for r in records} == {"one", "two"}
    assert any("reg:" in s for s in splunkd.stats.searches)


def test_concurrency_queueing_is_visible_when_the_target_has_a_ceiling(env):
    """Cross the target's concurrency limit and the queue must show up.

    This is the behaviour the whole tool exists to measure: the moment load
    crosses max_hist_searches, searches start queueing rather than running.
    """
    server = FakeSplunk(
        port=0,
        # Eight virtual users against two concurrency slots, each search taking
        # 800 ms, so the queue is many hundreds of milliseconds deep for the
        # whole run. A shallower queue can form and clear between two polls,
        # which is real but invisible, and would make this test a coin flip on
        # a loaded machine.
        base_latency_ms=800.0,
        jitter_ms=10.0,
        dispatch_latency_ms=1.0,
        max_concurrent=2,
        seed=99,
    )
    try:
        doc = tiny_scenario_dict()
        doc["personas"][0]["steps"] = [doc["personas"][0]["steps"][0]]
        doc["load"] = {"model": "closed", "virtual_users": 8, "duration": "4s"}
        scenario_obj = parse_scenario(doc)
        config = make_config(server, env)
        engine = ApiEngine(config)
        records = []

        class Sink:
            def emit(self, record):
                records.append(record)

        async def go():
            await engine.start()
            try:
                scheduler = Scheduler(
                    scenario=scenario_obj,
                    config=config,
                    engine=engine,
                    resolver=ParameterResolver(scenario_obj),
                    emitters=[Sink()],
                )
                return await scheduler.run()
            finally:
                await engine.close()

        summary = run_async(go())

        assert summary.stats["executions"] > 0
        assert server.stats.peak_concurrent <= 2
        assert server.stats.queued_total > 0
        # The generator saw the queue too, which is the number that ends up on
        # the chart next to the ceiling line.
        assert any((r.queued_ms or 0) > 0 for r in records)
    finally:
        server.close()
