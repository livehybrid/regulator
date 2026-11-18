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
    # Every probe plus the index access check that runs before them.
    assert result["self_load"]["queries"] == len(build_probes("x")) + 1
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


# ------------------------------------------------- probe accuracy (SPL shape)


def test_no_probe_single_quotes_a_field_inside_stats():
    """stats takes 'a.b' as a field literally named with quotes.

    Every aggregate over such a name came back empty, so resource_usage listed
    hosts with no CPU figures. Single quotes mean "field" only in eval/where.
    """
    import re

    from regulator_agent.targetguard import _CPU_SPL

    pattern = re.compile(r"\b(?:avg|max|min|sum|perc\d+|count)\(\s*'")
    for probe in build_probes("reg:x:", "reg_x_"):
        assert not pattern.search(probe.spl), probe.name
    assert not pattern.search(_CPU_SPL)


def test_our_searches_match_by_search_id_as_well_as_the_marker():
    probes = {p.name: p for p in build_probes("reg:run-7:", "reg_run_7_")}
    spl = probes["our_searches"].spl
    assert 'search_id="*reg_run_7_*"' in spl
    assert '"reg:run-7:"' in spl


def test_the_sid_prefix_is_safe_in_a_search_id_and_an_spl_operand():
    from regulator_agent.params import sid_prefix_for

    assert sid_prefix_for("r12-nightly run") == "reg_r12_nightly_run_"
    assert sid_prefix_for('x" | delete') == "reg_x____delete_"


def test_an_aggregate_over_no_events_reports_no_rows():
    """stats with no by clause returns one row of zeros over nothing."""
    from regulator_agent.sut import _drop_empty_aggregate

    probes = {p.name: p for p in build_probes("reg:x:")}
    ours = probes["our_searches"]
    assert _drop_empty_aggregate(ours, [{"searches": "0", "cache_lookups": "0"}]) == []
    rows = [{"searches": "3"}]
    assert _drop_empty_aggregate(ours, rows) == rows
    # A grouped probe is left alone: no events means no rows already.
    resources = probes["resource_usage"]
    assert _drop_empty_aggregate(resources, [{"host": "sh1"}]) == [{"host": "sh1"}]


class _StubClient:
    """Answers the access check, then every probe with one zero row."""

    def __init__(self, eventcount):
        self.eventcount = eventcount
        self.searches = []

    async def oneshot(self, spl, window, count=50):
        self.searches.append(spl)
        if spl.startswith("| eventcount"):
            if isinstance(self.eventcount, Exception):
                raise self.eventcount
            return self.eventcount, None
        return [{"searches": "0", "events": "0"}], None


def test_a_probe_on_an_unreadable_index_is_skipped_and_says_why():
    client = _StubClient([{"index": "_internal", "events": "1000"}])
    result = run_async(correlate(client, 1.0, 2.0, "reg:x:", settle_s=0, sid_prefix="reg_x_"))
    ours = result["probes"]["our_searches"]
    assert not ours["available"]
    assert "cannot read _audit" in ours["reason"]
    assert not any("index=_audit" in s for s in client.searches if not s.startswith("| eventcount"))
    assert result["probes"]["queueing"]["available"]
    assert result["readable_indexes"] == ["_internal"]


def test_a_failed_access_check_runs_every_probe():
    from regulator_agent.splunk import SplunkError

    client = _StubClient(SplunkError("eventcount is not allowed"))
    result = run_async(correlate(client, 1.0, 2.0, "reg:x:", settle_s=0))
    assert result["readable_indexes"] is None
    assert all(p["available"] for p in result["probes"].values())
    # And an aggregate over nothing is "no matching events", not zeros.
    assert result["probes"]["our_searches"]["rows"] == []
    assert any("none of this run's searches yet" in f for f in result["findings"])


# ------------------------------------------------- attribution by step (#18)


def test_the_per_step_probe_reads_the_step_from_the_run_marker():
    """The marker is the only place a dispatched search says which step it is."""
    import re

    from regulator_agent.params import cache_bust_marker
    from regulator_agent.sut import UNATTRIBUTED_STEP

    probes = {p.name: p for p in build_probes(marker_prefix_for("r7-v1.2"), "reg_r7_v1_2_")}
    spl = probes["our_searches_by_step"].spl
    assert spl.startswith("search index=_audit action=search info=completed")
    # The same selection as our_searches, so the two agree on what "ours" is.
    assert '(search_id="*reg_r7_v1_2_*" OR "reg:r7-v1.2:")' in spl
    assert " by step " in spl and "cold_path_pct" in spl and "eliminated_buckets" in spl
    # A record whose search string lost the marker is counted, not dropped.
    assert UNATTRIBUTED_STEP in spl

    pattern = re.search(r'rex field=search "([^"]+)"', spl).group(1)
    assert "\\\\" not in pattern  # nothing SPL's own unescaping could change
    extract = re.compile(pattern.replace("(?<reg_step>", "(?P<step>"))
    audited = "search index=main error | stats count ```" + cache_bust_marker("r7-v1.2", 3, 12, "triage.overview") + "```"
    assert extract.search(audited).group("step") == "triage.overview"
    # A run whose label differs only where this one has a dot is not ours.
    other = "search index=main ```" + cache_bust_marker("r7-v1x2", 3, 12, "triage.overview") + "```"
    assert extract.search(other) is None


def test_findings_name_the_step_the_cold_path_cost_most():
    from regulator_agent.sut import UNATTRIBUTED_STEP, _findings

    findings = _findings(_correlation(our_searches_by_step=[
        {"step": "triage.overview", "searches": "600", "run_time_s": "300", "cold_path_s": "123", "cache_misses": "212"},
        {"step": "triage.detail", "searches": "600", "run_time_s": "200", "cold_path_s": "4", "cache_misses": "9"},
        # More waiting than any step, but no step to blame it on.
        {"step": UNATTRIBUTED_STEP, "searches": "5", "run_time_s": "900", "cold_path_s": "800", "cache_misses": "999"},
    ]))
    line = next(f for f in findings if f.startswith("step "))
    assert "step triage.overview spent 41% of its run time waiting on 212 bucket-cache miss(es)" in line
    assert "123.0s of 300.0s" in line


def test_a_warm_run_names_no_costly_step():
    from regulator_agent.sut import _findings

    findings = _findings(_correlation(our_searches_by_step=[
        {"step": "triage.overview", "searches": "600", "run_time_s": "300", "cold_path_s": "0", "cache_misses": "0"},
    ]))
    assert not any(f.startswith("step ") for f in findings)


# ---------------------------------------- split by search head cluster member (#18)


def test_the_member_probe_splits_the_same_records_by_step_and_host():
    probes = {p.name: p for p in build_probes(marker_prefix_for("r7"), "reg_r7_")}
    by_step, by_member = probes["our_searches_by_step"].spl, probes["our_searches_by_member"].spl
    # The same selection and the same figures as the split by step, so a
    # step's rows across members add up to its row there.
    assert by_member.split(" | ")[0] == by_step.split(" | ")[0]
    stats_by_step = next(part for part in by_step.split(" | ") if part.startswith("stats "))
    stats_by_member = next(part for part in by_member.split(" | ") if part.startswith("stats "))
    assert stats_by_member == stats_by_step.replace(" by step", " by step, member")
    # The member is the host that wrote the audit record; none is still counted.
    assert '| eval member=coalesce(host, "(member not recorded)")' in by_member
    # Steps by members passes the usual 50 rows.
    assert probes["our_searches_by_member"].max_rows > 50


def test_the_member_lookup_matches_each_sid_exactly():
    from regulator_agent.sut import member_lookup_spl

    spl = member_lookup_spl(["reg_r7_0_ab12cd_1", "reg_r7_0_ab12cd_2"])
    # Quoted as _audit writes it and bare, and never a wildcard: *_1* would
    # also find _10 to _19.
    assert "search_id IN (\"reg_r7_0_ab12cd_1\", \"'reg_r7_0_ab12cd_1'\", " in spl
    assert "*" not in spl
    assert "stats latest(host) as member" in spl and " by sid" in spl


class _AuditStub:
    """A target whose audit trail places each sid on a member, and records what it was asked."""

    def __init__(self, members, fail_lookups=False):
        self.members = members
        self.fail_lookups = fail_lookups
        self.searches = []

    async def oneshot(self, spl, window, count=50):
        import re

        self.searches.append((spl, count))
        if "eventcount" in spl:
            return [{"index": i, "events": "10"} for i in ("_audit", "_internal", "_introspection")], None
        if "search_id IN" in spl:
            if self.fail_lookups:
                from regulator_agent.splunk import SplunkError

                raise SplunkError("lookup refused")
            # Each sid is asked for bare and in single quotes; take the quoted.
            asked = set(re.findall(r"'([^']+)'", spl.split("search_id IN", 1)[1].split(") ")[0]))
            # As _audit has them: quoted.
            return [{"sid": f"'{sid}'", "member": self.members[sid], "members": "1"} for sid in asked if sid in self.members], None
        return [], None


def _correlate_with(stub, issue_sids):
    return run_async(correlate(stub, 1.0, 2.0, "reg:r7:", settle_s=0, sid_prefix="reg_r7_", issue_sids=issue_sids))


def test_issues_are_split_by_the_member_the_audit_trail_names():
    from regulator_agent.sut import NO_AUDIT_RECORD

    stub = _AuditStub({"s1": "sh1", "s2": "sh2", "s3": "sh1", "s4": "sh2"})
    issue_sids = {
        "failed": [["s1", "a", "timeout"], ["s2", "a", "quota"], ["s9", "a", "server"]],
        "partial": [["s3", "b"]],
        "queued": [["s4", "b"], ["s3", "b"]],
        "dropped": {"queued": 12},
        "no_sid": {"failed": 2},
    }
    result = _correlate_with(stub, issue_sids)
    split = result["issues_by_member"]
    assert split["available"] and split["members"] == ["sh1", "sh2"]
    rows = {(r["step"], r["member"]): r for r in split["rows"]}
    assert rows[("a", "sh1")]["failed"] == 1 and rows[("a", "sh1")]["errors_by_class"] == {"timeout": 1}
    assert rows[("a", "sh2")]["errors_by_class"] == {"quota": 1}
    # A sid the audit trail does not hold yet is still counted, under its own row.
    assert rows[("a", NO_AUDIT_RECORD)]["errors_by_class"] == {"server": 1}
    assert rows[("b", "sh1")]["partial"] == 1 and rows[("b", "sh1")]["queued"] == 1
    assert rows[("b", "sh2")]["queued"] == 1
    # What the worker could not keep, and could not name a member for, is carried.
    assert split["dropped"] == {"queued": 12} and split["no_sid"] == {"failed": 2}
    # One lookup for five distinct sids, and it is counted as load.
    lookups = [s for s in stub.searches if "search_id IN" in s[0]]
    assert len(lookups) == 1
    assert result["self_load"]["queries"] == len(build_probes("x")) + 1 + 1


def test_the_member_lookup_runs_in_batches_and_drops_unsafe_sids():
    from regulator_agent.sut import MEMBER_LOOKUP_BATCH

    sids = [f"reg_r7_0_aa_{n}" for n in range(MEMBER_LOOKUP_BATCH + 1)]
    stub = _AuditStub({sid: "sh1" for sid in sids})
    issue_sids = {"failed": [[sid, "a", "server"] for sid in sids] + [['x" | delete', "a", "server"]]}
    result = _correlate_with(stub, issue_sids)
    lookups = [s for s in stub.searches if "search_id IN" in s[0]]
    assert len(lookups) == 2
    assert not any("delete" in spl for spl, _ in lookups)
    rows = {r["member"]: r for r in result["issues_by_member"]["rows"]}
    assert rows["sh1"]["failed"] == MEMBER_LOOKUP_BATCH + 1


def test_a_failed_member_lookup_is_a_note_not_a_failure():
    stub = _AuditStub({}, fail_lookups=True)
    result = _correlate_with(stub, {"failed": [["s1", "a", "server"]]})
    assert result["issues_by_member"] == {"available": False, "reason": "lookup refused"}
    assert any(n.startswith("issues_by_member:") for n in result["notes"])


def test_no_issue_sids_means_no_lookup():
    stub = _AuditStub({})
    result = _correlate_with(stub, {"failed": [], "partial": [], "queued": [], "dropped": {}, "no_sid": {"failed": 3}})
    assert "issues_by_member" not in result
    assert not any("search_id IN" in s[0] for s in stub.searches)


def test_findings_name_a_member_with_most_of_an_issue():
    from regulator_agent.sut import NO_AUDIT_RECORD, _findings

    def split(rows, members):
        return {"probes": {}, "issues_by_member": {"available": True, "rows": rows, "members": members, "dropped": {}}}

    concentrated = split(
        [
            {"step": "a", "member": "sh2", "failed": 0, "partial": 8, "queued": 0},
            {"step": "b", "member": "sh2", "failed": 0, "partial": 1, "queued": 0},
            {"step": "a", "member": "sh1", "failed": 0, "partial": 1, "queued": 0},
            # Not placed on a member, so not counted for or against one.
            {"step": "a", "member": NO_AUDIT_RECORD, "failed": 0, "partial": 50, "queued": 0},
        ],
        ["sh1", "sh2"],
    )
    line = next(f for f in _findings(concentrated) if "member" in f)
    assert line.startswith("search head cluster member sh2 had 9 of the 10 partial results")

    spread = split(
        [{"step": "a", "member": m, "failed": 0, "partial": 5, "queued": 0} for m in ("sh1", "sh2", "sh3")],
        ["sh1", "sh2", "sh3"],
    )
    assert not any("cluster member" in f for f in _findings(spread))
    # One member is a standalone search head, or a sticky balancer: nothing to compare.
    single = split([{"step": "a", "member": "sh1", "failed": 9, "partial": 0, "queued": 0}], ["sh1"])
    assert not any("cluster member" in f for f in _findings(single))


def test_the_probes_leave_their_own_audit_records_out():
    """Each probe names the run marker, so it matched the next probe's selection.

    Seen on the contract cluster: a probe's own audit record turned up in the
    split by member as a step called 'coalesce(step, ...)', which Splunk had
    extracted as step=... from the logged search text.
    """
    from regulator_agent.sut import CORRELATION_TAG

    for probe in build_probes(marker_prefix_for("r7"), "reg_r7_"):
        if '"reg:r7:"' in probe.spl:
            # The selection excludes the tag, and in doing so carries it, so
            # each probe's own record is left out by every probe after it.
            assert f'NOT "{CORRELATION_TAG}"' in probe.spl, probe.name
    # The step comes from the marker only, never from a field Splunk extracted
    # from the logged search text: that one is called step too.
    by_step = next(p for p in build_probes(marker_prefix_for("r7"), "reg_r7_") if p.name == "our_searches_by_step")
    assert "coalesce(reg_step, " in by_step.spl and "coalesce(step, " not in by_step.spl
