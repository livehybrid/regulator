"""The rolling interval behind the time series, and merging intervals across workers."""

from __future__ import annotations

from regulator_agent.histogram import LatencyHistogram
from regulator_agent.results import IntervalStats, RunStats, StepRecord, merge_intervals


def _record(latency_ms: float, ok: bool = True, queued_ms: float = 0.0, step: str = "s") -> StepRecord:
    return StepRecord(
        run_id="r", slot=0, vu_id=1, iteration=1, persona="p", step_id=step, step_class="dense", step_type="search",
        engine="api", started_at=1000.0, intended_start=1000.0, latency_ms=latency_ms, service_time_ms=latency_ms,
        ok=ok, error_class=None if ok else "timeout", sid="sid" if ok else None, queued_ms=queued_ms,
    )


def test_an_interval_is_reset_on_every_take():
    stats = RunStats(run_id="r")
    for value in (10.0, 20.0, 30.0):
        stats.record(_record(value))
    stats.record(_record(5.0, ok=False))
    stats.record_loop_lag(7.0)
    first = stats.take_interval(with_buckets=True)
    assert first["executions"] == 4 and first["errors"] == 1
    assert first["error_rate_pct"] == 25.0
    assert first["p50_ms"] is not None and first["p99_ms"] is not None
    assert first["loop_lag_p95_ms"] is not None
    assert set(first["buckets"]) == {"latency", "failure_latency", "queued", "loop_lag"}
    # The cumulative figures are untouched; the interval starts again.
    assert stats.executions == 4
    second = stats.take_interval()
    assert second["executions"] == 0 and second["p95_ms"] is None and "buckets" not in second


def test_intervals_merge_with_percentiles_over_the_merged_buckets():
    a, b = IntervalStats(), IntervalStats()
    for value in (10.0, 12.0, 14.0):
        a.add(_record(value))
    for value in (100.0, 120.0, 140.0, 160.0):
        b.add(_record(value, queued_ms=50.0))
    merged = merge_intervals([a.summary(with_buckets=True), b.summary(with_buckets=True)])
    assert merged["executions"] == 7 and merged["queued"] == 4
    # Neither worker's p95 alone: the merged distribution's.
    expected = LatencyHistogram()
    for value in (10.0, 12.0, 14.0, 100.0, 120.0, 140.0, 160.0):
        expected.record_ms(value)
    assert abs(merged["p95_ms"] - expected.percentile_ms(95)) < 1e-6
    assert merged["queued_p95_ms"] is not None
    assert "percentiles_note" not in merged


def test_intervals_without_buckets_fall_back_to_the_busiest_worker_and_say_so():
    a, b = IntervalStats(), IntervalStats()
    a.add(_record(10.0))
    for value in (100.0, 120.0):
        b.add(_record(value))
    merged = merge_intervals([a.summary(), b.summary()])
    assert merged["executions"] == 3
    assert merged["p95_ms"] == b.summary()["p95_ms"]
    assert merged["percentiles_note"] == "busiest worker's"


def test_merging_nothing_is_an_empty_interval():
    merged = merge_intervals([])
    assert merged["executions"] == 0 and merged["throughput_per_s"] == 0.0


def test_records_are_stamped_with_the_cache_epoch_and_split_cold_from_warm():
    stats = RunStats(run_id="r")
    stats.cold_window_s = 30.0
    before = _record(10.0)
    stats.record(before)
    assert before.cache_epoch == 0 and before.since_evict_s is None
    stats.set_epoch(1, 1000.0)
    cold = _record(50.0)
    cold.started_at = 1010.0
    stats.record(cold)
    warm = _record(20.0)
    warm.started_at = 1100.0
    stats.record(warm)
    assert cold.cache_epoch == 1 and cold.since_evict_s == 10.0
    assert warm.cache_epoch == 1 and warm.since_evict_s == 100.0
    step = stats.steps["s"]
    assert len(step.latency_cold) == 1 and len(step.latency_warm) == 1
    summary = step.summary()
    assert summary["cold"]["count"] == 1 and summary["warm"]["count"] == 1
    assert stats.epochs_summary() == [{"epoch": 1, "executions": 2, "latency": stats.epoch_latency[1].summary()}]
    # An older epoch number never rewinds the clock.
    stats.set_epoch(1, 2000.0)
    assert stats.epoch_started_at == 1000.0
    assert "latency_cold" in stats.histograms()["steps"]["s"] and "1" in stats.histograms()["epochs"]
