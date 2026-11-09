"""Forecasting which SmartStore buckets a run reads, and warming them first."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from conftest import run_async, tiny_scenario_dict
from regulator_agent import cachewarm
from regulator_agent.config import ConfigError, load_config
from regulator_agent.params import ParameterResolver
from regulator_agent.scenario import TimePolicy, parse_scenario
from regulator_agent.smartstore import Bucket, CacheState, IndexCache, cache_state
from regulator_agent.splunk import SplunkClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk

GB = 1_000_000_000


def scenario(**time_policy):
    doc = tiny_scenario_dict()
    if time_policy:
        doc["time_policy"] = time_policy
    return parse_scenario(doc)


def state_of(buckets, max_cache_size_mb=None):
    state = CacheState(available=True, buckets=list(buckets), max_cache_size_mb=max_cache_size_mb)
    for b in buckets:
        state.per_index.setdefault(b.index, IndexCache(index=b.index))
    return state


def bucket(n, index="main", status="remote", earliest=0.0, latest=0.0, server="idx1", size=GB):
    return Bucket(
        server=server, bid=f"bid|{index}~{n}~G|", index=index, status=status,
        size=size, earliest=earliest, latest=latest,
    )


# ------------------------------------------------------------ the pieces


@pytest.mark.parametrize(
    "spl, expected",
    [
        ("search index=main | stats count", ["main"]),
        ('| tstats count where index="web" by host', ["web"]),
        ("search index::fw_*", ["fw_*"]),
        ("search (index=a OR index=b) index!=c", ["a", "b"]),
        ('search index IN (a, "b", c*) | head 1', ["a", "b", "c*"]),
        ("search sourcetype=x", []),
        ("search myindex=foo", []),
    ],
)
def test_index_terms(spl, expected):
    assert cachewarm.index_terms(spl) == expected


def test_a_step_naming_no_index_uses_the_corpus_index():
    scn = parse_scenario({
        **tiny_scenario_dict(),
        "personas": [{
            "name": "u", "weight": 100, "think_time": {"dist": "fixed", "value_s": 0},
            "steps": [{"id": "s", "type": "search", "engine": "api", "spl": "`my_macro` | stats count"}],
        }],
    })
    assert cachewarm.step_indexes(scn, scn.steps[0]) == ["main"]


def test_a_rolling_span_covers_every_window_the_run_can_dispatch():
    policy = TimePolicy(mode="rolling", window_s=3600, jitter_s=300, align_s=60, offset_s=600)
    lo, hi = cachewarm.policy_span(policy, start=1_000_000.0, duration_s=900.0)
    assert hi == 1_000_000.0 + 900.0 - 600.0
    assert lo == 1_000_000.0 - 600.0 - 300.0 - 60.0 - 3600.0


def test_a_pinned_span_is_exact():
    policy = TimePolicy(mode="pinned", earliest_epoch=100.0, latest_epoch=200.0)
    assert cachewarm.policy_span(policy, start=5_000.0, duration_s=60.0) == (100.0, 200.0)


def test_merge_joins_overlaps_only():
    assert cachewarm.merge([(5, 8), (0, 2), (1, 3), (8, 9)]) == [(0, 3), (5, 9)]


# ------------------------------------------------------------ the forecast


def test_the_forecast_counts_only_buckets_the_run_reads():
    start = 10_000_000.0
    scn = scenario(mode="rolling", window="1h", jitter="0s", align="1m")
    state = state_of([
        bucket(1, status="local", earliest=start - 1800, latest=start),       # in
        bucket(2, status="remote", earliest=start - 7200, latest=start - 3000),  # in
        bucket(3, status="remote", earliest=start - 90000, latest=start - 86400),  # too old
        bucket(4, index="web", status="remote", earliest=start - 60, latest=start),  # not searched
    ], max_cache_size_mb=1024)

    fc = cachewarm.forecast(scn, state, start, duration_s=60.0)

    assert fc.available
    assert fc.buckets == 2
    assert fc.local == 1 and fc.remote == 1
    assert fc.bytes_total == 2 * GB and fc.bytes_remote == GB
    assert set(fc.per_index) == {"main"}
    assert fc.capacity_pct and fc.capacity_pct > 100
    assert any("larger than the cache" in note for note in fc.notes)


def test_a_replicated_bucket_is_counted_once_and_local_if_any_copy_is():
    start = 10_000_000.0
    scn = scenario(mode="rolling", window="1h", jitter="0s", align="1m")
    state = state_of([
        bucket(1, status="remote", earliest=start - 600, latest=start, server="idx1"),
        bucket(1, status="local", earliest=start - 600, latest=start, server="idx2"),
    ])
    fc = cachewarm.forecast(scn, state, start, duration_s=60.0)
    assert fc.buckets == 1 and fc.local == 1
    assert any("upper bound" in note for note in fc.notes)


def test_a_bucket_with_no_time_range_counts_as_touched():
    scn = scenario(mode="rolling", window="1h", jitter="0s", align="1m")
    state = state_of([Bucket(server="i", bid="bid|main~1~G|", index="main", status="remote", size=1)])
    fc = cachewarm.forecast(scn, state, 10_000_000.0, duration_s=60.0)
    assert fc.buckets == 1
    assert any("no time range" in note for note in fc.notes)


def test_no_cache_is_an_answer_not_an_error():
    fc = cachewarm.forecast(scenario(), CacheState(available=False, reason="not SmartStore"), 0.0, 1.0)
    assert not fc.available and fc.reason == "not SmartStore"


# ------------------------------------------------------------ planning


def test_chunks_that_are_already_local_are_not_searched():
    start = 10_000_000.0
    scn = scenario(mode="rolling", window="1h", jitter="0s", align="1m")
    all_local = state_of([bucket(1, status="local", earliest=start - 1800, latest=start)])
    fc = cachewarm.forecast(scn, all_local, start, duration_s=60.0)
    planned, skipped = cachewarm.warm_searches(scn, fc, "tsidx", 86400.0)
    assert planned == [] and skipped == 1


def test_the_shapes_of_the_warm_up_searches():
    start = 10_000_000.0
    scn = scenario(mode="rolling", window="1h", jitter="0s", align="1m")
    state = state_of([bucket(1, earliest=start - 1800, latest=start)])
    fc = cachewarm.forecast(scn, state, start, duration_s=60.0)

    (tsidx, window), = cachewarm.warm_searches(scn, fc, "tsidx", 86400.0)[0]
    assert tsidx.startswith('| tstats count where index="main"')
    assert window.earliest <= start - 3600 and window.latest >= start

    (full, _), = cachewarm.warm_searches(scn, fc, "full", 86400.0)[0]
    assert "len(_raw)" in full

    replay, _ = cachewarm.warm_searches(scn, fc, "replay", 86400.0, ParameterResolver(scn))
    spls = [spl for spl, _ in replay]
    assert "| tstats count where index=main" in spls
    assert any(spl.startswith("search index=main n=") and "{{" not in spl for spl in spls)

    with pytest.raises(ValueError):
        cachewarm.warm_searches(scn, fc, "nope", 86400.0)


def test_a_long_range_is_cut_into_chunks():
    start = 10_000_000.0
    scn = scenario(mode="rolling", window="3d", jitter="0s", align="1m")
    state = state_of([bucket(n, earliest=start - (n + 1) * 86400, latest=start - n * 86400) for n in range(3)])
    fc = cachewarm.forecast(scn, state, start, duration_s=60.0)
    planned, _ = cachewarm.warm_searches(scn, fc, "tsidx", 86400.0)
    assert len(planned) >= 3
    assert all(w.span_s <= 86400.0 for _, w in planned)


# ------------------------------------------------------------ against the fake


def test_warming_makes_the_buckets_the_run_reads_local(env):
    async def go():
        splunkd = FakeSplunk(
            port=0, base_latency_ms=2.0, jitter_ms=0.0, dispatch_latency_ms=1.0,
            smartstore_buckets=40, smartstore_local_pct=0, smartstore_bucket_span_s=3600.0,
        )
        try:
            config = load_config(env(REG_TARGET_URL=splunkd.base_url, REG_TARGET_VERIFY_TLS="0"))
            client = SplunkClient(config.target)
            await client.start()
            try:
                scn = scenario(mode="rolling", window="1h", jitter="5m", align="1m")
                before = cachewarm.forecast(scn, await cache_state(client), time.time(), 1.0)
                planned, _ = cachewarm.warm_searches(scn, before, "tsidx", 86400.0)
                result = await cachewarm.warm(client, planned, mode="tsidx", concurrency=2)
                after = cachewarm.forecast(scn, await cache_state(client), time.time(), 1.0)
                return before, result, after, splunkd.stats.buckets_localised
            finally:
                await client.close()
        finally:
            splunkd.close()

    before, result, after, localised = run_async(go())
    # main gets every other bucket; a 1h window with 5m jitter reaches the
    # newest two hourly buckets.
    assert before.buckets == 2 and before.local == 0
    assert result.searches >= 1 and result.failed == 0
    assert after.local == 2
    assert localised == 2  # nothing outside the run's range was fetched


def test_a_spent_budget_stops_starting_searches():
    class Client:
        calls = 0

        async def oneshot(self, spl, window, count=1):
            Client.calls += 1
            return [], 0

    from regulator_agent.timepolicy import TimeWindow

    planned = [("| makeresults", TimeWindow(0.0, 1.0))] * 5
    result = run_async(cachewarm.warm(Client(), planned, mode="tsidx", budget_s=0.0))
    assert result.searches == 0 and result.not_started == 5 and Client.calls == 0


# ------------------------------------------------------------ config


def test_warm_and_evict_together_is_refused(env):
    with pytest.raises(ConfigError):
        load_config(env(REG_WARM_CACHE="1", REG_EVICT_CACHE="1"))


def test_an_unknown_warm_mode_is_refused(env):
    with pytest.raises(ConfigError):
        load_config(env(REG_WARM_CACHE_MODE="hot"))


def test_warm_up_defaults(env):
    config = load_config(env())
    assert config.warm_cache is False
    assert config.warm_cache_mode == "tsidx"
    assert config.warm_cache_concurrency == 4


# ------------------------------------------------------------ the worker CLI


def _cli_env(monkeypatch, splunkd, scenario_dir, **extra):
    for key, value in {
        "REG_STANDALONE": "1",
        "REG_TARGET_URL": splunkd.base_url,
        "REG_TARGET_TOKEN": "t",
        "REG_TARGET_VERIFY_TLS": "0",
        "REG_SCENARIO": str(scenario_dir),
        "REG_POLL_INITIAL_MS": "10",
        "REG_POLL_MAX_MS": "20",
        "REG_SUT_SETTLE_S": "0",
        "REG_HARVEST_S": "0",
        **extra,
    }.items():
        monkeypatch.setenv(key, value)


def test_the_forecast_command_answers_without_running(monkeypatch, capsys, write_scenario):
    from regulator_agent.__main__ import main

    splunkd = FakeSplunk(
        port=0, base_latency_ms=2.0, smartstore_buckets=40, smartstore_local_pct=0,
        smartstore_bucket_span_s=3600.0,
    )
    try:
        _cli_env(monkeypatch, splunkd, write_scenario(tiny_scenario_dict()))
        assert main(["--cache-forecast"]) == 0
        payload = __import__("json").loads(capsys.readouterr().out)
        assert payload["available"] is True
        assert payload["buckets"] == 2 and payload["local"] == 0
        assert splunkd.stats.jobs_created == 0  # nothing was run
        assert splunkd.stats.buckets_localised == 0
    finally:
        splunkd.close()


def test_a_run_with_warm_up_starts_warm_and_says_so(monkeypatch, tmp_path, write_scenario):
    import json
    from regulator_agent.__main__ import main

    splunkd = FakeSplunk(
        port=0, base_latency_ms=2.0, smartstore_buckets=40, smartstore_local_pct=0,
        smartstore_bucket_span_s=3600.0,
    )
    try:
        summary = tmp_path / "summary.json"
        _cli_env(
            monkeypatch, splunkd, write_scenario(tiny_scenario_dict()),
            REG_WARM_CACHE="1", REG_SUMMARY_PATH=str(summary), REG_OUTPUT=str(tmp_path / "r.ndjson"),
        )
        main([])
        cache = json.loads(summary.read_text())["cache"]
        assert cache["warm"]["before"]["local"] == 0
        assert cache["warm"]["after"]["local"] == 2
        assert cache["forecast"]["local_pct"] == 100.0
    finally:
        splunkd.close()
