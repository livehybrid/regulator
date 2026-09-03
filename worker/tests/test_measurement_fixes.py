"""Regression tests for the second adversarial review.

Each test is named for the defect it pins. They are here rather than spread
across the per-module files so the list of what was wrong, and what would be
wrong again, stays in one place.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from conftest import FakeEngine, run_async, tiny_scenario_dict
from regulator_agent.compare import GateError, compare_runs, parse_gate
from regulator_agent.config import load_config
from regulator_agent.engines.base import StepContext
from regulator_agent.histogram import LatencyHistogram
from regulator_agent.params import ParameterResolver
from regulator_agent.results import ERROR_CANCELLED, RunStats, StepRecord, StepStats
from regulator_agent.scenario import Step, is_advice, lint, parse_scenario
from regulator_agent.scheduler import Scheduler
from regulator_agent.timepolicy import TimeWindow


# ------------------------------------------------------------- histograms


def test_an_empty_histogram_reports_none_not_zero():
    """A step whose searches all failed passed every latency gate as -100%."""
    empty = LatencyHistogram().summary()
    assert empty["count"] == 0.0
    assert empty["p95_ms"] is None
    assert empty["mean_ms"] is None
    filled = LatencyHistogram()
    filled.record_ms(120.0)
    assert filled.summary()["p95_ms"] == pytest.approx(120.0, rel=0.05)


def test_a_gate_on_a_step_with_no_successes_fails_rather_than_passing():
    candidate = _summary(step_p95=None, step_successes=0, step_errors=50)
    baseline = _summary(step_p95=2000.0)
    result = compare_runs(candidate, baseline, gates=["p95[one] <= baseline + 25%"])
    assert result.ok is False
    assert "no successful searches" in result.gates[0].detail


def _summary(step_p95, step_successes=100, step_errors=0, scan_total=100000, valid=True, **extra):
    latency = {
        "count": float(step_successes),
        "mean_ms": step_p95,
        "min_ms": step_p95,
        "p50_ms": step_p95,
        "p90_ms": step_p95,
        "p95_ms": step_p95,
        "p99_ms": step_p95,
        "max_ms": step_p95,
    }
    summary = {
        "scenario": "tiny",
        "valid": valid,
        "scenario_seed": 7,
        "effective_seed": 7,
        "peak_virtual_users": 2,
        "co_corrected": False,
        "load_model": "closed",
        "target_url": "https://x",
        "stats": {
            "executions": step_successes + step_errors,
            "errors": step_errors,
            "error_rate_pct": 0.0,
            "throughput_per_s": 1.0,
            "latency": latency,
            "queueing": {"searches_queued": 0},
            "steps": [
                {
                    "step_id": "one",
                    "class": "dense",
                    "executions": step_successes + step_errors,
                    "successes": step_successes,
                    "errors": step_errors,
                    "latency": latency,
                    "scan_count_total": scan_total,
                }
            ],
        },
    }
    summary.update(extra)
    return summary


# ----------------------------------------------------------------- gates


def test_a_relative_gate_must_name_its_unit():
    """'baseline + 200' meant 200 percent while '200' meant 200 ms."""
    with pytest.raises(GateError):
        parse_gate("p95 <= baseline + 200")
    assert parse_gate("p95 <= baseline + 200ms").relative_abs == 200.0
    assert parse_gate("p95 <= baseline + 15%").relative_pct == 15.0
    assert parse_gate("p95 <= 200").absolute == 200.0


def test_a_comparison_with_no_gates_is_report_only_not_a_pass():
    result = compare_runs(_summary(1000.0), _summary(500.0), gates=[])
    assert "REPORT ONLY" in result.explain()


def test_scanned_more_is_per_search_not_per_run():
    """A faster cluster fits more iterations in and scans more in total."""
    faster = _summary(500.0, step_successes=200, scan_total=200_000)
    slower = _summary(1000.0, step_successes=100, scan_total=100_000)
    result = compare_runs(faster, slower, gates=["p95 <= baseline + 10%"])
    assert result.steps[0].scanned_more is False
    heavier = _summary(1000.0, step_successes=100, scan_total=300_000)
    result = compare_runs(heavier, slower, gates=["p95 <= baseline + 10%"])
    assert result.steps[0].scanned_more is True


def test_too_few_samples_is_warned_about():
    result = compare_runs(_summary(1000.0, step_successes=5), _summary(900.0), gates=["valid"])
    assert any("fewer than" in warning for warning in result.warnings)
    assert "under 20 samples" in result.explain()


def test_different_digests_and_load_models_are_called_out():
    candidate = _summary(1000.0, scenario_digest="aaaa", load_model="open")
    baseline = _summary(1000.0, scenario_digest="bbbb", load_model="closed")
    result = compare_runs(candidate, baseline, gates=["valid"])
    joined = " ".join(result.warnings)
    assert "digests" in joined
    assert "load models" in joined


def test_an_override_seed_counts_as_a_different_workload():
    candidate = _summary(1000.0, effective_seed=1)
    baseline = _summary(1000.0, effective_seed=2)
    result = compare_runs(candidate, baseline, gates=["valid"])
    assert any("different seeds" in warning for warning in result.warnings)


# ---------------------------------------------------------------- results


def _record(ok=True, **fields) -> StepRecord:
    record = StepRecord(run_id="r", slot=0, vu_id=1, iteration=0, persona="p", step_id="s")
    record.ok = ok
    record.service_time_ms = 100.0
    record.latency_ms = 100.0
    for key, value in fields.items():
        setattr(record, key, value)
    return record


def test_failed_jobs_do_not_feed_the_scan_totals():
    stats = StepStats(step_id="s", step_class="dense")
    stats.add(_record(ok=True, scan_count=1000, run_duration_s=1.0))
    stats.add(_record(ok=False, scan_count=5_000_000, run_duration_s=30.0))
    summary = stats.summary()
    assert summary["scan_count_total"] == 1000
    assert summary["scan_count_per_search"] == 1000.0
    assert summary["successes"] == 1


def test_queued_pct_is_over_the_searches_that_could_have_queued():
    stats = RunStats(run_id="r")
    stats.record(_record(ok=True, sid="a", queued_ms=500.0))
    stats.record(_record(ok=True, sid="b"))
    # Failures and oneshots cannot report queueing, so they are not counted.
    stats.record(_record(ok=False))
    stats.record(_record(ok=True, sid=None))
    snap = stats.snapshot()
    assert snap["queueing"]["job_executions"] == 2
    assert snap["queueing"]["queued_pct"] == 50.0


def test_partial_and_abandoned_are_counted():
    stats = RunStats(run_id="r")
    stats.record(_record(ok=True, sid="a", partial=True))
    stats.record(_record(ok=False, error_class=ERROR_CANCELLED))
    snap = stats.snapshot()
    assert snap["partial"] == 1
    assert snap["abandoned"] == 1


# --------------------------------------------------------------- scheduler


def build(doc, env, **overrides):
    scenario = parse_scenario(doc)
    config = load_config(env(**overrides))
    return scenario, config, ParameterResolver(scenario, seed=config.seed)


def test_an_arrival_rate_override_scales_an_open_ramp(env):
    """REG_ARRIVAL_RATE_PER_MIN was silently ignored when the scenario had a ramp."""
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {
        "model": "open",
        "arrival_rate_per_min": 600,
        "ramp": [{"to": 600, "over_s": 10}],
        "duration": "1s",
    }
    scenario, config, resolver = build(doc, env, REG_ARRIVAL_RATE_PER_MIN="2400")
    scheduler = Scheduler(scenario=scenario, config=config, engine=FakeEngine(), resolver=resolver)
    assert scheduler.ramp.final_target == 2400.0


def test_an_open_ramp_from_zero_does_not_invalidate_a_healthy_worker(env):
    """The zero-rate leg held next_arrival at t0 and reported it as loop lag."""
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {
        "model": "open",
        "arrival_rate_per_min": 6000,
        "ramp": [{"hold_s": 0.4}, {"to": 6000, "over_s": 0.2}],
        "duration": "1s",
    }
    doc["abort_if"] = {"error_rate_pct": 50, "p95_ms": 60000, "generator_drift_ms": 200}
    scenario, config, resolver = build(doc, env)
    scheduler = Scheduler(scenario=scenario, config=config, engine=FakeEngine(latency=0.001), resolver=resolver)
    summary = run_async(scheduler.run())
    assert summary.valid, summary.invalid_reason
    assert summary.stats["executions"] > 0


def test_the_override_seed_reaches_every_draw_and_the_summary(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 1, "duration": "0.3s"}
    scenario, config, resolver = build(doc, env, REG_SEED="4242")
    scheduler = Scheduler(scenario=scenario, config=config, engine=FakeEngine(latency=0.005), resolver=resolver)
    summary = run_async(scheduler.run())
    assert summary.scenario_seed == 7
    assert summary.effective_seed == 4242
    assert summary.configured_load["model"] == "closed"
    assert summary.configured_load["virtual_users"] == 1


def test_poisson_arrivals_are_the_default_and_reproducible(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "open", "arrival_rate_per_min": 3000, "duration": "0.5s"}
    scenario, config, resolver = build(doc, env)
    assert scenario.load.arrivals == "poisson"
    a = FakeEngine(latency=0.001)
    run_async(Scheduler(scenario=scenario, config=config, engine=a, resolver=resolver).run())
    b = FakeEngine(latency=0.001)
    run_async(Scheduler(scenario=scenario, config=config, engine=b, resolver=resolver).run())
    # The intended starts are a seeded sequence, so both runs planned the same
    # arrivals (allowing for the run ending at a slightly different count).
    n = min(len(a.contexts), len(b.contexts), 10)
    assert n > 3
    assert [c.iteration for c in a.contexts[:n]] == [c.iteration for c in b.contexts[:n]]


class AbandoningEngine(FakeEngine):
    """Parks a record on the context when cancelled, as the API engine does."""

    async def execute(self, ctx: StepContext) -> StepRecord:
        try:
            return await super().execute(ctx)
        except asyncio.CancelledError:
            record = ctx.blank_record()
            record.ok = False
            record.error_class = ERROR_CANCELLED
            record.service_time_ms = 50.0
            ctx.outcome.append(record)
            raise


def test_work_in_flight_at_the_end_is_recorded_as_abandoned_not_dropped(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 2, "duration": "0.2s"}
    scenario, config, resolver = build(doc, env, REG_DRAIN_BUDGET_S="1")
    engine = AbandoningEngine(latency=5.0)  # every search outlives the run
    scheduler = Scheduler(scenario=scenario, config=config, engine=engine, resolver=resolver)
    summary = run_async(scheduler.run())
    assert summary.stats["abandoned"] == 2
    assert summary.stats["errors"] == 2
    assert summary.stats["errors_by_class"] == {ERROR_CANCELLED: 2}


def test_ramp_down_retires_users_after_their_iteration_not_during(env):
    """Cancelling mid-iteration dropped the slowest samples of every ramp-down."""
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {
        "model": "closed",
        "virtual_users": 3,
        "ramp": [{"to": 3, "over_s": 0.05}, {"to": 1, "over_s": 0.2}],
        "duration": "0.8s",
    }
    scenario, config, resolver = build(doc, env)
    engine = AbandoningEngine(latency=0.15)
    scheduler = Scheduler(scenario=scenario, config=config, engine=engine, resolver=resolver)
    summary = run_async(scheduler.run())
    # Nothing was cancelled during the ramp down: the only cancellations
    # possible are at the very end, inside the drain.
    assert summary.stats["errors_by_class"].get(ERROR_CANCELLED, 0) <= 1


# --------------------------------------------------------------- scenarios


def test_think_time_declared_with_the_wrong_parameter_is_a_lint_error():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["think_time"] = {"dist": "fixed", "median_s": 30}
    problems = [line for line in lint(parse_scenario(doc)) if not is_advice(line)]
    assert any("value_s" in line for line in problems)


def test_jitter_smaller_than_align_is_a_lint_error():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["time_policy"] = {"mode": "rolling", "window": "1h", "jitter": "30s", "align": "1m"}
    problems = [line for line in lint(parse_scenario(doc)) if not is_advice(line)]
    assert any("smaller than align" in line for line in problems)


def test_a_run_id_default_carries_the_start_time(env):
    """Two default runs never emit identical cache-busting markers."""
    first = load_config(env())
    assert first.run_id.startswith("local-")
    assert first.run_id != "local"
