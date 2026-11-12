"""Where the limit was: one reading of what a run already collected."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from regulator_server import bottleneck  # noqa: E402

RUN_S = 600.0


def stats(**over):
    base = {
        "executions": 1000, "errors": 0, "error_rate_pct": 0.0, "errors_by_class": {},
        "peak_in_flight": 8, "target": {"max_hist_searches": 22},
        "queueing": {"job_executions": 1000, "searches_queued": 0, "queued_pct": 0.0, "queued_ms": {"p95_ms": 0}},
        "generator": {"max_loop_lag_ms": 12.0},
    }
    base.update(over)
    return base


def host(name, role, avg=20.0, hot_s=0, peak=None, **more):
    return {
        "host": name, "role": role,
        "cpu": {"avg_pct": avg, "peak_pct": peak if peak is not None else avg + 5, "before_pct": 5.0, "hot_s": hot_s},
        "mem": {"peak_pct": 50.0}, "load": {"peak": 0.4}, **more,
    }


def summary(hosts=None, sut=None, **over):
    out = {"valid": True, "sut": sut or {}}
    if hosts is not None:
        out["resources"] = {
            "available": True, "hosts": hosts,
            "window": {"run_started_at": 1000.0, "run_ended_at": 1000.0 + RUN_S},
        }
    out.update(over)
    return out


def audit(searches=1000, avg_run_time_s=2.0, cold_path_s=0.0, miss_pct=0.0):
    return {"probes": {"our_searches": {"available": True, "rows": [{
        "searches": searches, "avg_run_time_s": avg_run_time_s, "cache_lookups": 5000,
        "cold_path_s": cold_path_s, "cache_miss_pct": miss_pct,
    }]}}}


QUIET = [host("sh1", "search head"), host("idx1", "indexer"), host("idx2", "indexer")]


def kinds(verdict):
    return [f["kind"] for f in verdict["factors"] if f["named"]]


def test_a_run_that_reached_nothing_says_so_and_says_what_it_checked():
    verdict = bottleneck.assess(stats(), summary(QUIET, audit()))
    assert verdict["strength"] == "none" and kinds(verdict) == []
    assert verdict["headline"].startswith("No limit was reached")
    fine = " ".join(verdict["fine"])
    assert "The load generator kept to its schedule" in fine
    assert "Admission: 0.0% of searches waited" in fine
    assert "Indexers: the busiest averaged 20% CPU" in fine
    assert "Search head: the busiest averaged 20% CPU" in fine
    assert "Bucket cache: 0.0% of lookups missed" in fine
    assert verdict["not_checked"] == []


def test_indexers_pinned_on_cpu_are_named_with_the_host_and_the_time():
    hosts = [host("sh1", "search head", avg=35.0), host("idx1", "indexer", avg=96.0, hot_s=540, peak=99.0),
             host("idx2", "indexer", avg=94.0, hot_s=480)]
    verdict = bottleneck.assess(stats(), summary(hosts, audit()))
    assert verdict["headline"] == "CPU on the indexers" and verdict["strength"] == "strong"
    top = verdict["factors"][0]
    assert top["kind"] == "indexer_cpu" and top["score"] == 1.0
    said = " ".join(top["evidence"])
    assert "idx1 averaged 96% CPU during the run and spent 90% of it at 90% or more (peak 99%)" in said
    assert "2 of 2 indexers were at 90% or more" in said
    assert "It was at 5% in the three minutes before the run" in said
    assert "search_head_cpu" not in kinds(verdict)


def test_one_hot_indexer_among_several_is_called_one_host_not_the_tier():
    hosts = [host("idx1", "indexer", avg=95.0, hot_s=500), host("idx2", "indexer"), host("idx3", "indexer")]
    verdict = bottleneck.assess(stats(), summary(hosts))
    assert "One host, not the tier" in " ".join(verdict["factors"][0]["evidence"])


def test_queueing_is_admission_and_a_role_quota_is_told_from_the_ceiling():
    queued = stats(
        peak_in_flight=22,
        queueing={"job_executions": 1000, "queued_pct": 31.0, "queued_ms": {"p95_ms": 14000}},
    )
    verdict = bottleneck.assess(queued, summary(QUIET))
    assert verdict["headline"].startswith("Admission: the search head's concurrent-search ceiling")
    said = " ".join(verdict["factors"][0]["evidence"])
    assert "31.0% of searches waited to be admitted before they ran, waiting 14.0 s at the 95th percentile" in said
    assert "peaked at 22 against a ceiling of 22" in said

    reasons = {"probes": {"queueing_reasons": {"available": True, "rows": [
        {"reason": "The maximum number of concurrent historical searches for this user based on their role quota has been reached"}]}}}
    verdict = bottleneck.assess(queued, summary(QUIET, reasons))
    assert verdict["factors"][0]["kind"] == "admission_role"
    assert "srchJobsQuota" in verdict["factors"][0]["advice"]


def test_a_generator_that_could_not_keep_time_outranks_everything_else():
    hosts = [host("idx1", "indexer", avg=97.0, hot_s=590)]
    bad = stats(generator={"max_loop_lag_ms": 2600.0}, arrivals_shed=40)
    verdict = bottleneck.assess(bad, summary(hosts, valid=False, invalid_reason="the generator's own scheduling loop ran 2600ms late"))
    assert verdict["headline"] == "The load generator, not the target"
    assert kinds(verdict)[0] == "generator" and "indexer_cpu" in kinds(verdict)
    assert "40 scheduled arrivals" in " ".join(verdict["factors"][0]["evidence"])


def test_a_cold_cache_is_named_when_waiting_on_it_was_a_real_share_of_the_search_time():
    sut = audit(searches=500, avg_run_time_s=4.0, cold_path_s=900.0, miss_pct=38.0)
    sut["probes"]["our_searches_by_step"] = {"available": True, "rows": [
        {"step": "triage.overview", "cold_path_s": 700.0}, {"step": "other", "cold_path_s": 200.0}]}
    verdict = bottleneck.assess(stats(), summary(QUIET, sut, cache={"delta": {"provenance": "cold"}}))
    assert verdict["factors"][0]["kind"] == "cache" and verdict["strength"] == "strong"
    said = " ".join(verdict["factors"][0]["evidence"])
    assert "38.0% of bucket-cache lookups missed" in said and "45% of this run's search run time (900 s of 2000 s)" in said
    assert "Step triage.overview paid the most: 700 s" in said and "calls this run cold" in said


def test_two_limits_close_together_are_both_in_the_headline():
    hosts = [host("idx1", "indexer", avg=96.0, hot_s=560, io={"wait_ms": 90.0, "before_wait_ms": 2.0, "read_mb_s": 400.0})]
    verdict = bottleneck.assess(stats(), summary(hosts))
    assert verdict["headline"] == "CPU on the indexers, with disk on the indexers"


def test_a_disk_that_was_slow_before_the_run_counts_for_less():
    slow = [host("idx1", "indexer", io={"wait_ms": 60.0, "before_wait_ms": 2.0, "read_mb_s": 300.0})]
    always = [host("idx1", "indexer", io={"wait_ms": 60.0, "before_wait_ms": 55.0, "read_mb_s": 300.0})]
    loaded = bottleneck.assess(stats(), summary(slow))["factors"][0]
    idle = bottleneck.assess(stats(), summary(always))["factors"][0]
    assert loaded["kind"] == idle["kind"] == "disk"
    assert loaded["named"] is True and idle["named"] is False
    assert abs(idle["score"] - loaded["score"] / 2) <= 0.01


def test_memory_and_errors_are_named_past_their_lines():
    hosts = [host("sh1", "search head", mem={"peak_pct": 96.0, "swap_used_mb": 800.0})]
    verdict = bottleneck.assess(stats(), summary(hosts))
    assert "memory" in kinds(verdict) and "paging" in " ".join(verdict["factors"][0]["evidence"])

    failing = stats(errors=250, error_rate_pct=25.0, errors_by_class={"timeout": 200, "server": 50})
    verdict = bottleneck.assess(failing, summary(QUIET))
    assert verdict["headline"] == "Searches failed rather than slowed"
    assert "25.0% of searches failed (200 timeout, 50 server)" in verdict["factors"][0]["evidence"][0]


def test_what_could_not_be_checked_is_not_reported_as_fine():
    verdict = bottleneck.assess(stats(), {"valid": True, "resources": {"available": False, "reason": "HTTP 403"}})
    assert verdict["headline"] == "No limit was found in what could be checked"
    assert any("Host CPU, memory and disk: HTTP 403" in line for line in verdict["not_checked"])
    assert not any("Indexers" in line for line in verdict["fine"])

    only_sh = bottleneck.assess(stats(), summary([host("sh1", "search head")], audit()))
    assert any("The indexers: none of them reported" in line for line in only_sh["not_checked"])

    old = bottleneck.assess(stats(), {"valid": True})
    assert any("finished before they were collected" in line for line in old["not_checked"])
    assert bottleneck.assess(None, None) is None


def test_the_split_of_search_time_is_given_as_context():
    sut = audit()
    sut["probes"]["search_telemetry"] = {"available": True, "rows": [{"avg_indexer_ms": 900, "avg_reduce_ms": 100}]}
    verdict = bottleneck.assess(stats(), summary(QUIET, sut))
    assert "90% of a search's time was the map phase on the indexers (900 ms)" in verdict["context"]
