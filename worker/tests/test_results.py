"""Step records, emitters and the live aggregate."""

from __future__ import annotations

import io
import json

import pytest

from regulator_agent.results import (
    ERROR_SERVER,
    ERROR_TIMEOUT,
    NdjsonEmitter,
    RunStats,
    StepRecord,
    StepStats,
    summarise,
)


def record(step_id="one", ok=True, latency_ms=100.0, error_class=None, **kwargs):
    return StepRecord(
        run_id="r",
        slot=0,
        vu_id=1,
        iteration=0,
        persona="p",
        step_id=step_id,
        latency_ms=latency_ms,
        service_time_ms=latency_ms,
        ok=ok,
        error_class=error_class,
        **kwargs,
    )


def test_to_dict_includes_every_field():
    payload = record().to_dict()
    for key in ("run_id", "slot", "vu_id", "iteration", "persona", "step_id", "latency_ms",
                "service_time_ms", "co_corrected", "ok", "params"):
        assert key in payload


def test_events_per_second_appears_only_when_the_job_reported_both_numbers():
    """oneshot and export create no job artefact, so the number cannot exist."""
    assert "events_per_s" not in record().to_dict()
    with_stats = record(scan_count=10000, run_duration_s=2.0)
    assert with_stats.to_dict()["events_per_s"] == pytest.approx(5000.0)
    assert record(scan_count=10000).to_dict().get("events_per_s") is None


def test_ndjson_emitter_writes_one_parseable_object_per_line():
    stream = io.StringIO()
    emitter = NdjsonEmitter(stream=stream)
    emitter.emit(record(step_id="a"))
    emitter.emit(record(step_id="b"))
    lines = stream.getvalue().strip().split("\n")
    assert len(lines) == 2
    assert [json.loads(line)["step_id"] for line in lines] == ["a", "b"]


def test_ndjson_emitter_appends_to_a_file(tmp_path):
    path = tmp_path / "out.ndjson"
    emitter = NdjsonEmitter(path=str(path))
    emitter.emit(record())
    emitter.close()
    assert json.loads(path.read_text().strip())["step_id"] == "one"


def test_step_stats_accumulate():
    stats = StepStats(step_id="one", step_class="dense")
    for i in range(10):
        stats.add(record(latency_ms=100 + i, ok=(i % 5 != 0), error_class=ERROR_SERVER))
    summary = stats.summary()
    assert summary["executions"] == 10
    assert summary["errors"] == 2
    assert summary["error_rate_pct"] == pytest.approx(20.0)
    assert summary["errors_by_class"] == {ERROR_SERVER: 2}
    # Successes only. A target refusing work in the time of one POST would
    # otherwise drag the latency distribution down and make the numbers improve
    # as the cluster fails.
    assert summary["latency"]["count"] == 8
    assert summary["failure_latency"]["count"] == 2


def test_run_stats_keep_steps_separate():
    stats = RunStats(run_id="r")
    stats.record(record(step_id="a", latency_ms=10))
    stats.record(record(step_id="b", latency_ms=1000))
    snapshot = stats.snapshot()
    by_id = {s["step_id"]: s for s in snapshot["steps"]}
    assert by_id["a"]["latency"]["p50_ms"] < by_id["b"]["latency"]["p50_ms"]
    assert snapshot["executions"] == 2


def test_error_classes_are_counted_separately():
    stats = RunStats(run_id="r")
    stats.record(record(ok=False, error_class=ERROR_TIMEOUT))
    stats.record(record(ok=False, error_class=ERROR_SERVER))
    stats.record(record(ok=False, error_class=ERROR_TIMEOUT))
    assert stats.errors_by_class == {ERROR_TIMEOUT: 2, ERROR_SERVER: 1}
    assert stats.error_rate_pct == pytest.approx(100.0)


def test_in_flight_tracking_never_goes_negative():
    stats = RunStats(run_id="r")
    stats.enter()
    stats.enter()
    assert stats.peak_in_flight == 2
    stats.leave()
    stats.leave()
    stats.leave()
    assert stats.in_flight == 0
    assert stats.peak_in_flight == 2


def test_snapshot_has_the_documented_shape():
    stats = RunStats(run_id="r", slot=3)
    stats.record(record())
    snapshot = stats.snapshot()
    for key in ("run_id", "slot", "elapsed_s", "executions", "errors", "error_rate_pct",
                "in_flight", "peak_in_flight", "throughput_per_s", "latency", "generator",
                "steps"):
        assert key in snapshot
    assert snapshot["slot"] == 3
    assert "max_drift_ms" in snapshot["generator"]


def test_summarise_agrees_with_an_equivalent_run_stats():
    records = [record(latency_ms=10 * i, ok=(i != 3)) for i in range(1, 11)]
    offline = summarise(records)
    live = RunStats(run_id="offline")
    for r in records:
        live.record(r)
    assert offline["executions"] == live.snapshot()["executions"]
    assert offline["latency"]["p95_ms"] == live.snapshot()["latency"]["p95_ms"]


def test_drift_is_tracked_for_the_generator_health_check():
    stats = RunStats(run_id="r")
    stats.record(record(late_by_ms=50.0))
    stats.record(record(late_by_ms=400.0))
    assert stats.max_drift_ms == 400.0
    assert stats.snapshot()["generator"]["max_drift_ms"] == 400.0


def test_queueing_and_unknown_sids_are_counted_on_the_step_they_happened_to():
    """The run totals say how many; the step says where."""
    stats = RunStats(run_id="r")
    stats.record(record(step_id="busy", queued_ms=120.0, unknown_sid_count=2))
    stats.record(record(step_id="busy", queued_ms=80.0))
    stats.record(record(step_id="busy"))
    stats.record(record(step_id="quiet"))
    steps = {s["step_id"]: s for s in stats.snapshot()["steps"]}

    assert steps["busy"]["queued"] == 2
    assert steps["busy"]["queued_p95_ms"] == pytest.approx(120.0, rel=0.05)
    assert steps["busy"]["unknown_sid"] == {"searches": 1, "responses": 2}
    assert steps["quiet"]["queued"] == 0 and steps["quiet"]["queued_p95_ms"] is None
    assert steps["quiet"]["unknown_sid"] == {"searches": 0, "responses": 0}
    # The per-step counts add up to the run's.
    assert sum(s["queued"] for s in steps.values()) == stats.snapshot()["queueing"]["searches_queued"]
    assert sum(s["unknown_sid"]["responses"] for s in steps.values()) == stats.snapshot()["unknown_sid"]["responses"]
    # And the queued histogram travels, so a fleet's p95 is over the merged whole.
    assert "queued" in stats.steps["busy"].histograms()


# ---------------------------------------------------------------- the ledger


def test_the_ledger_keeps_the_searches_with_a_problem_and_the_slowest_that_worked():
    from regulator_agent.results import LEDGER_SLOWEST

    stats = RunStats(run_id="r")
    stats.record(record(ok=False, error_class="timeout", error_detail="read timed out", sid="s-timeout"))
    stats.record(record(ok=False, error_class="cancelled", sid="s-cut", latency_ms=200_000.0))
    stats.record(record(sid="s-partial", partial=True))
    for n in range(LEDGER_SLOWEST + 10):
        stats.record(record(sid=f"s-{n}", latency_ms=float(n)))

    ledger = stats.ledger.to_dict()
    assert [(e["sid"], e["outcome"]) for e in ledger["problems"]] == [
        ("s-timeout", "failed"), ("s-cut", "unfinished"), ("s-partial", "partial"),
    ]
    assert ledger["problems"][0]["error_detail"] == "read timed out"
    slowest = [e["sid"] for e in ledger["slowest"]]
    assert len(slowest) == LEDGER_SLOWEST
    assert slowest[0] == f"s-{LEDGER_SLOWEST + 9}" and "s-0" not in slowest
    # The live snapshot does not carry it: a heartbeat a second has no use for it.
    assert "searches" not in stats.snapshot()


def test_the_ledger_is_bounded_per_kind_and_counts_what_it_left_out():
    from regulator_agent.results import LEDGER_PER_KIND

    stats = RunStats(run_id="r")
    for n in range(LEDGER_PER_KIND + 7):
        stats.record(record(ok=False, error_class="server", sid=f"s-{n}"))
    stats.record(record(ok=False, error_class="auth", sid="s-auth"))
    ledger = stats.ledger.to_dict()
    # One flood does not crowd out the other kind of failure.
    assert len(ledger["problems"]) == LEDGER_PER_KIND + 1
    assert ledger["problems"][-1]["sid"] == "s-auth"
    assert ledger["dropped"] == {"failed:server": 7}


def test_a_followed_search_is_updated_with_how_it_ended():
    stats = RunStats(run_id="r")
    cut = record(ok=False, error_class="cancelled", sid="s-cut")
    gone = record(ok=False, error_class="cancelled", sid="s-gone")
    stats.record(cut)
    stats.record(gone)
    cut.dispatch_state, cut.scan_count, cut.done_progress = "DONE", 1234, 1.0
    stats.record_after_end(212.5, True, record=cut)
    gone.dispatch_state, gone.done_progress = "RUNNING", 0.4
    stats.record_after_end(None, False, record=gone)

    first, second = stats.ledger.to_dict()["problems"]
    assert first["outcome"] == "finished_after_end"
    assert first["run_duration_s"] == 212.5 and first["scan_count"] == 1234
    assert first["after_end"] == {
        "finished": True, "run_duration_s": 212.5, "dispatch_state": "DONE", "done_progress": 1.0, "scan_count": 1234,
    }
    assert second["outcome"] == "still_running"
    assert second["after_end"]["done_progress"] == 0.4
    assert stats.finished_after_end == 1 and stats.still_running_at_end == 1


def test_a_followed_search_that_failed_on_the_target_is_not_called_still_running():
    stats = RunStats(run_id="r")
    died = record(ok=False, error_class="cancelled", sid="s-died")
    stats.record(died)
    died.dispatch_state, died.messages = "FAILED", ["FATAL: search process did not exit cleanly"]
    stats.record_after_end(None, False, record=died)
    entry = stats.ledger.to_dict()["problems"][0]
    assert entry["outcome"] == "failed_after_end"
    assert entry["messages"] == ["FATAL: search process did not exit cleanly"]


def test_a_jobs_performance_table_is_cut_down_and_kept_out_of_the_record():
    from regulator_agent.results import LEDGER_PHASES, compact_performance, ledger_entry

    table = {f"command.c{n}": {"duration_secs": n, "invocations": 1, "input_count": 0, "output_count": n} for n in range(30)}
    table["dispatch.stream.remote.idx1"] = {"duration_secs": "4.5", "invocations": "2", "output_count": "900"}
    table["dispatch.stream.remote.idx2"] = {"duration_secs": 9.0}
    table["not a row"] = "ignored"

    compact = compact_performance(table)
    assert [row["name"] for row in compact["phases"]][:2] == ["command.c29", "command.c28"]
    assert len(compact["phases"]) == LEDGER_PHASES and compact["phases_dropped"] == 30 - LEDGER_PHASES
    assert compact["indexers"] == [
        {"name": "idx2", "seconds": 9.0},
        {"name": "idx1", "seconds": 4.5, "invocations": 2, "output": 900},
    ]
    assert compact_performance({}) is None and compact_performance(None) is None

    rec = record(sid="s", performance=table)
    assert "performance" not in rec.to_dict()
    assert rec.performance is table  # and to_dict left the record as it was
    assert ledger_entry(rec)["performance"]["phases"][0]["name"] == "command.c29"


def test_ledgers_from_a_fleet_merge_under_the_same_caps():
    from regulator_agent.results import LEDGER_SLOWEST, merge_ledgers

    def doc(slot):
        return {
            "problems": [{"sid": f"p{slot}", "outcome": "failed", "started_at": 100.0 - slot}],
            "slowest": [{"sid": f"w{slot}-{n}", "service_time_ms": slot * 1000.0 + n} for n in range(LEDGER_SLOWEST)],
            "dropped": {"failed:server": slot},
        }

    merged = merge_ledgers([doc(1), doc(2), None, "not a ledger"])
    assert [e["sid"] for e in merged["problems"]] == ["p2", "p1"]  # by start time
    assert len(merged["slowest"]) == LEDGER_SLOWEST
    assert all(e["sid"].startswith("w2-") for e in merged["slowest"])
    assert merged["dropped"] == {"failed:server": 3}
