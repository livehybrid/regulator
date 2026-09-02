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
