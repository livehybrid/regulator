"""Server-side correlation.

The property that matters: this is enrichment, and enrichment must never fail a
run. A load-test account frequently cannot read _audit or _introspection, and a
benchmark that refuses to report because it could not also ask the cluster's
opinion would be worse than useless.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import run_async
from regulator_agent.config import load_config
from regulator_agent.splunk import SplunkClient
from regulator_agent.sut import build_probes, correlate, marker_prefix_for

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk


@pytest.fixture
def splunkd():
    server = FakeSplunk(port=0, base_latency_ms=5.0, jitter_ms=1.0, dispatch_latency_ms=1.0)
    try:
        yield server
    finally:
        server.close()


def correlate_against(splunkd, env, **kwargs):
    config = load_config(env(REG_TARGET_URL=splunkd.base_url, REG_TARGET_VERIFY_TLS="0"))

    async def go():
        client = SplunkClient(config.target)
        await client.start()
        try:
            return await correlate(
                client, 1_756_800_000.0, 1_756_800_600.0, "reg:test-run:", **kwargs
            )
        finally:
            await client.close()

    return run_async(go())


# ------------------------------------------------------------------ probes


def test_the_marker_prefix_isolates_one_runs_searches():
    """Every dispatched search carries this as a Splunk comment.

    Matching on it is what separates this run's searches from everybody else's
    on a shared cluster.
    """
    assert marker_prefix_for("run-42") == "reg:run-42:"


def test_every_probe_searches_an_internal_index():
    """Correlation reads Splunk's own bookkeeping, never customer data."""
    for probe in build_probes("reg:x:"):
        assert probe.spl.startswith("search index=_") or "index=_" in probe.spl
        assert probe.description


def test_the_run_marker_is_used_to_select_our_own_searches():
    probes = {p.name: p for p in build_probes("reg:run-7:")}
    assert "reg:run-7:" in probes["our_searches"].spl
    # And the all-searches probe deliberately does not filter, so other traffic
    # on a shared cluster stays visible.
    assert "reg:run-7:" not in probes["all_searches"].spl


# ------------------------------------------------------------- behaviour


def test_correlation_returns_a_result_for_every_probe(splunkd, env):
    result = correlate_against(splunkd, env)
    expected = {p.name for p in build_probes("x")}
    assert set(result["probes"]) == expected
    assert result["window"]["earliest"] == 1_756_800_000.0
    assert result["marker_prefix"] == "reg:test-run:"


def test_the_self_load_is_recorded_rather_than_pretended_away(splunkd, env):
    """These searches run on the cluster under test. Small, but not zero."""
    result = correlate_against(splunkd, env)
    assert result["self_load"]["queries"] == len(build_probes("x"))
    assert "add a small" in result["self_load"]["note"]


def test_an_unreadable_index_becomes_a_note_not_a_failure(env):
    """The common case: a load-test account cannot read _audit."""
    server = FakeSplunk(port=0, base_latency_ms=5.0, reject_token="blocked-token")
    try:
        config = load_config(
            env(
                REG_TARGET_URL=server.base_url,
                REG_TARGET_TOKEN="blocked-token",
                REG_TARGET_VERIFY_TLS="0",
            )
        )

        async def go():
            client = SplunkClient(config.target)
            await client.start()
            try:
                return await correlate(client, 1.0, 2.0, "reg:x:")
            finally:
                await client.close()

        result = run_async(go())
        assert result["notes"], "an unreadable index must be reported"
        assert all(not p["available"] for p in result["probes"].values())
        # And it says so in words a person can act on.
        assert any("client-side measurement" in f for f in result["findings"])
    finally:
        server.close()


def test_an_unreachable_target_still_produces_a_result(env):
    config = load_config(
        env(REG_TARGET_URL="http://127.0.0.1:9", REG_TARGET_VERIFY_TLS="0")
    )

    async def go():
        client = SplunkClient(config.target, connect_timeout_s=0.4, read_timeout_s=1.0)
        await client.start()
        try:
            return await correlate(client, 1.0, 2.0, "reg:x:", timeout_s=3.0)
        finally:
            await client.close()

    result = run_async(go())
    assert result["notes"]
    assert result["findings"]


# -------------------------------------------------------------- findings


def _correlation(**probe_rows):
    probes = {}
    for name, rows in probe_rows.items():
        probes[name] = {"available": True, "reason": "", "description": "", "rows": rows}
    return {"probes": probes}


def test_findings_report_skipped_scheduled_searches():
    """The cost a load test imposes on everybody else, which is otherwise invisible."""
    from regulator_agent.sut import _findings

    findings = _findings(
        _correlation(scheduler=[{"status": "skipped", "reason": "concurrency", "count": "340"}])
    )
    assert any("340 scheduled search" in f for f in findings)
    assert any("somebody else's scheduled work" in f for f in findings)


def test_findings_call_out_a_cpu_bound_target():
    from regulator_agent.sut import _findings

    findings = _findings(_correlation(resource_usage=[{"max_user_cpu_pct": "96.4"}]))
    assert any("96%" in f for f in findings)
    assert any("saturated machine" in f for f in findings)


def test_a_target_below_the_cpu_ceiling_is_not_flagged():
    from regulator_agent.sut import _findings

    findings = _findings(_correlation(resource_usage=[{"max_user_cpu_pct": "40"}]))
    assert any("40%" in f for f in findings)
    assert not any("saturated" in f for f in findings)


def test_findings_report_the_clusters_own_count_of_our_searches():
    from regulator_agent.sut import _findings

    findings = _findings(
        _correlation(our_searches=[{"searches": "1200", "p95_run_time_s": "3.4"}])
    )
    assert any("1200" in f and "audit trail" in f for f in findings)
    # And says plainly that it is a floor, because audit records keep arriving
    # after a run and correlation happens immediately.
    assert any("floor rather than a total" in f for f in findings)


def test_a_partial_audit_count_is_not_reported_as_a_total():
    """The count would otherwise read as "the cluster lost our searches"."""
    from regulator_agent.sut import _findings

    findings = _findings(_correlation(our_searches=[{"searches": "0"}]))
    assert any("none of this run's searches yet" in f for f in findings)


def test_findings_separate_indexer_time_from_search_head_time():
    from regulator_agent.sut import _findings

    findings = _findings(_correlation(search_telemetry=[{"avg_indexer_ms": "820"}]))
    assert any("indexer-side" in f for f in findings)


def test_no_correlation_at_all_says_so_plainly():
    from regulator_agent.sut import _findings

    findings = _findings({"probes": {}})
    assert len(findings) == 1
    assert "client-side measurement" in findings[0]


# ---------------------------------- probes borrowed from cluster_health_tools


def test_the_audit_probe_carries_the_bucket_cache_accounting():
    probes = {p.name: p for p in build_probes("reg:run-7:")}
    spl = probes["our_searches"].spl
    for field in (
        "invocations_command_search_rawdata_bucketcache_miss",
        "duration_command_search_index_bucketcache_miss",
        "search_startup_time",
        "eliminated_buckets",
        "cold_path_s",
        "cache_miss_pct",
    ):
        assert field in spl, field
    assert '"reg:run-7:"' in spl


def test_the_borrowed_probes_read_splunks_own_metrics():
    probes = {p.name: p for p in build_probes("x")}
    assert "name=search_queue_metrics" in probes["queueing"].spl and "enqueue_seaches_count" in probes["queueing"].spl
    assert "DispatchManager" in probes["queueing_reasons"].spl and "by reason" in probes["queueing_reasons"].spl
    assert '"system total"' in probes["concurrency"].spl and "active_hist_searches" in probes["concurrency"].spl
    assert "group=searchscheduler" in probes["scheduler_lag"].spl and "max_lag" in probes["scheduler_lag"].spl
    assert "group=cachemgr_bucket" in probes["cache_buckets"].spl and "manual_evict" in probes["cache_buckets"].spl
    assert "action=download" in probes["cache_downloads"].spl and "elapsed_ms" in probes["cache_downloads"].spl
    for name in ("queueing", "queueing_reasons", "concurrency", "scheduler_lag", "cache_buckets", "cache_downloads"):
        assert probes[name].spl.startswith("search index=_internal")


def test_findings_price_the_cold_path_from_the_audit_trail():
    from regulator_agent.sut import _findings

    findings = _findings({"probes": {"our_searches": {"available": True, "rows": [{
        "searches": "12", "p95_run_time_s": "3.5", "cache_lookups": "400", "cold_path_s": "18.25", "cache_miss_pct": "12.5",
    }]}}})
    assert any("12.5% of this run's bucket-cache lookups missed" in f and "18.2s" in f for f in findings), findings
    warm = _findings({"probes": {"our_searches": {"available": True, "rows": [{
        "searches": "12", "cache_lookups": "400", "cold_path_s": "0", "cache_miss_pct": "0",
    }]}}})
    assert any("warm run by the cluster's own account" in f for f in warm)


def test_findings_tell_a_role_quota_from_the_instance_ceiling():
    from regulator_agent.sut import _findings

    ceiling = _findings({"probes": {
        "queueing": {"available": True, "rows": [{"enqueued": "40", "largest_queue_size": "9", "max_queued_s": "4.2"}]},
        "queueing_reasons": {"available": True, "rows": [{"reason": "The maximum number of concurrent historical searches on this instance has been reached.", "count": "40"}]},
    }})
    assert any("queued 40 search(es)" in f and "instance's concurrent-search ceiling" in f for f in ceiling), ceiling
    quota = _findings({"probes": {
        "queueing": {"available": True, "rows": [{"enqueued": "3", "largest_queue_size": "1", "max_queued_s": "1"}]},
        "queueing_reasons": {"available": True, "rows": [{"reason": "The maximum number of concurrent historical searches for this role has been reached.", "count": "3"}]},
    }})
    assert any("per-role quota" in f for f in quota), quota
