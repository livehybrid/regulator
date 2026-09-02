"""Comparing runs, and the gate language.

The tests that matter most here are the refusals. A comparison that produces a
confident percentage from an invalid run, or silently compares a warm
SmartStore run against a cold one, is worse than no comparison at all, because
somebody will merge on the strength of it.
"""

from __future__ import annotations

import copy

import pytest

from regulator_agent.compare import (
    GateError,
    compare_runs,
    metric_value,
    parse_gate,
)


def summary(
    p95=1000.0,
    p50=400.0,
    throughput=12.0,
    error_rate=0.0,
    queued=0,
    valid=True,
    scenario="search-classes",
    seed=4242,
    vus=50,
    provenance="warm",
    co_corrected=True,
    steps=None,
):
    return {
        "scenario": scenario,
        "scenario_seed": seed,
        "outcome": "completed",
        "valid": valid,
        "invalid_reason": None if valid else "the generator fell behind its own schedule",
        "co_corrected": co_corrected,
        "peak_virtual_users": vus,
        "target_url": "https://splunk.example:8089",
        "cache": {"delta": {"provenance": provenance, "buckets_downloaded": 0}},
        "stats": {
            "executions": 1000,
            "error_rate_pct": error_rate,
            "throughput_per_s": throughput,
            "latency": {
                "count": 1000,
                "mean_ms": p50,
                "min_ms": 10.0,
                "p50_ms": p50,
                "p90_ms": p95 * 0.9,
                "p95_ms": p95,
                "p99_ms": p95 * 1.2,
                "max_ms": p95 * 2,
            },
            "queueing": {"searches_queued": queued, "queued_pct": queued / 10.0},
            "generator": {"max_drift_ms": 5.0, "drift_p95_ms": 3.0},
            "steps": steps
            if steps is not None
            else [
                {
                    "step_id": "dense-web-status",
                    "class": "dense",
                    "executions": 500,
                    "errors": 0,
                    "error_rate_pct": 0.0,
                    "latency": {"p50_ms": 300.0, "p95_ms": 800.0},
                    "dispatch": {"p95_ms": 40.0},
                    "ttfr": {"p95_ms": 500.0},
                    "scan_count_total": 1_000_000,
                },
                {
                    "step_id": "rare-bucket-policy",
                    "class": "rare",
                    "executions": 500,
                    "errors": 0,
                    "error_rate_pct": 0.0,
                    "latency": {"p50_ms": 900.0, "p95_ms": 2000.0},
                    "dispatch": {"p95_ms": 60.0},
                    "ttfr": {"p95_ms": 1500.0},
                    "scan_count_total": 5_000_000,
                },
            ],
        },
    }


# ------------------------------------------------------------ gate parsing


@pytest.mark.parametrize(
    "expression, metric, op, pct, absolute",
    [
        ("p95 <= baseline + 15%", "p95", "<=", 15.0, None),
        ("p95<=baseline+15%", "p95", "<=", 15.0, None),
        ("throughput >= baseline - 10%", "throughput", ">=", -10.0, None),
        ("p99 <= 5000ms", "p99", "<=", None, 5000.0),
        ("p99 <= 5s", "p99", "<=", None, 5000.0),
        ("error_rate <= 2%", "error_rate", "<=", None, 2.0),
        ("queued == 0", "queued", "==", None, 0.0),
    ],
)
def test_gates_parse(expression, metric, op, pct, absolute):
    gate = parse_gate(expression)
    assert gate.metric == metric
    assert gate.op == op
    assert gate.relative_pct == pct
    assert gate.absolute == absolute


def test_a_bare_metric_is_a_truthiness_gate():
    gate = parse_gate("valid")
    assert gate.boolean is True
    assert gate.metric == "valid"


def test_a_per_step_gate_names_its_step():
    gate = parse_gate("p95[rare-bucket-policy] <= baseline + 25%")
    assert gate.step == "rare-bucket-policy"
    assert gate.metric == "p95"


def test_an_absolute_baseline_offset_parses():
    gate = parse_gate("p95 <= baseline + 200ms")
    assert gate.relative_abs == 200.0


@pytest.mark.parametrize("expression", ["", "p95 <=", "p95 <= soon", "!!!"])
def test_nonsense_gates_are_fatal(expression):
    with pytest.raises(GateError):
        parse_gate(expression)


# ---------------------------------------------------------- metric lookup


def test_metrics_are_read_from_the_run():
    run = summary(p95=1234.0, throughput=9.5, error_rate=1.5, queued=7)
    assert metric_value(run, "p95") == 1234.0
    assert metric_value(run, "throughput") == 9.5
    assert metric_value(run, "error_rate") == 1.5
    assert metric_value(run, "queued") == 7
    assert metric_value(run, "valid") == 1.0


def test_per_step_metrics_are_read_from_the_named_step():
    run = summary()
    assert metric_value(run, "p95", "rare-bucket-policy") == 2000.0
    assert metric_value(run, "dispatch_p95", "rare-bucket-policy") == 60.0
    assert metric_value(run, "p95", "no-such-step") is None


# -------------------------------------------------------------- refusals


def test_an_invalid_candidate_is_refused_rather_than_compared():
    """The most important test in the module.

    An invalid run measured the load generator rather than Splunk, so any
    percentage derived from it is a confident number about the wrong system.
    """
    result = compare_runs(summary(valid=False), summary(), gates=["p95 <= baseline + 15%"])
    assert result.ok is False
    assert result.blocked
    assert "invalid" in result.blocked
    assert "generator fell behind" in result.blocked
    # And no gate was evaluated, so nothing can be quoted out of context.
    assert result.gates == []


def test_an_invalid_baseline_is_refused_too():
    result = compare_runs(summary(), summary(valid=False), gates=["p95 <= baseline + 15%"])
    assert result.blocked and "baseline" in result.blocked


def test_an_invalid_run_can_be_compared_when_explicitly_allowed():
    result = compare_runs(
        summary(valid=False), summary(), gates=["p95 <= baseline + 15%"], allow_invalid=True
    )
    assert result.blocked is None


# -------------------------------------------------------------- warnings


def test_a_cache_provenance_difference_is_warned_about():
    """On SmartStore this can be an order of magnitude."""
    result = compare_runs(
        summary(provenance="cold"), summary(provenance="warm"), gates=["valid"]
    )
    assert any("provenance" in w for w in result.warnings)
    assert any("order of magnitude" in w for w in result.warnings)


def test_a_different_seed_is_warned_about():
    """A different seed means a different workload, not a different cluster."""
    result = compare_runs(summary(seed=1), summary(seed=2), gates=["valid"])
    assert any("seed" in w for w in result.warnings)


def test_a_different_scenario_is_warned_about():
    result = compare_runs(summary(scenario="a"), summary(scenario="b"), gates=["valid"])
    assert any("different scenarios" in w for w in result.warnings)


def test_different_concurrency_is_warned_about():
    result = compare_runs(summary(vus=50), summary(vus=100), gates=["valid"])
    assert any("concurrency" in w for w in result.warnings)


def test_a_coordinated_omission_mismatch_is_warned_about():
    result = compare_runs(
        summary(co_corrected=True), summary(co_corrected=False), gates=["valid"]
    )
    assert any("coordinated-omission" in w for w in result.warnings)


def test_identical_workloads_produce_no_warnings():
    result = compare_runs(summary(), summary(), gates=["valid"])
    assert result.warnings == []


# ----------------------------------------------------------------- gates


def test_a_regression_inside_the_allowance_passes():
    result = compare_runs(
        summary(p95=1100.0), summary(p95=1000.0), gates=["p95 <= baseline + 15%"]
    )
    assert result.ok is True
    assert result.gates[0].passed is True
    assert "+10.0%" in result.gates[0].detail


def test_a_regression_beyond_the_allowance_fails():
    result = compare_runs(
        summary(p95=1400.0), summary(p95=1000.0), gates=["p95 <= baseline + 15%"]
    )
    assert result.ok is False
    assert result.failed_gates
    assert "worse" in result.failed_gates[0].detail


def test_an_absolute_ceiling_needs_no_baseline():
    assert compare_runs(summary(p95=900.0), None, gates=["p95 <= 1000ms"]).ok is True
    assert compare_runs(summary(p95=1100.0), None, gates=["p95 <= 1000ms"]).ok is False


def test_a_relative_gate_without_a_baseline_fails_with_an_explanation():
    result = compare_runs(summary(), None, gates=["p95 <= baseline + 15%"])
    assert result.ok is False
    assert "no baseline was given" in result.gates[0].detail


def test_throughput_going_down_is_the_regression_direction():
    """Where a bigger number is better, worse means down."""
    result = compare_runs(
        summary(throughput=8.0), summary(throughput=10.0), gates=["throughput >= baseline - 10%"]
    )
    assert result.ok is False


def test_a_queueing_gate():
    assert compare_runs(summary(queued=0), None, gates=["queued == 0"]).ok is True
    assert compare_runs(summary(queued=5), None, gates=["queued == 0"]).ok is False


def test_a_validity_gate():
    assert compare_runs(summary(valid=True), None, gates=["valid"]).ok is True


def test_a_per_step_gate_judges_only_that_step():
    """One slow rare search should not be hidden by a fast average."""
    result = compare_runs(
        summary(steps=[{
            "step_id": "rare-bucket-policy", "class": "rare", "executions": 10, "errors": 0,
            "error_rate_pct": 0.0, "latency": {"p95_ms": 5000.0}, "scan_count_total": 100,
        }]),
        summary(steps=[{
            "step_id": "rare-bucket-policy", "class": "rare", "executions": 10, "errors": 0,
            "error_rate_pct": 0.0, "latency": {"p95_ms": 1000.0}, "scan_count_total": 100,
        }]),
        gates=["p95[rare-bucket-policy] <= baseline + 25%"],
    )
    assert result.ok is False
    assert result.gates[0].step == "rare-bucket-policy"


def test_a_gate_naming_a_metric_the_run_lacks_fails_loudly():
    result = compare_runs(summary(), None, gates=["p95[no-such-step] <= 100ms"])
    assert result.ok is False
    assert "no 'p95'" in result.gates[0].detail


# ------------------------------------------------------------ step deltas


def test_step_deltas_are_computed_and_sorted_worst_first():
    candidate = summary()
    baseline = copy.deepcopy(summary())
    baseline["stats"]["steps"][1]["latency"]["p95_ms"] = 1000.0  # rare was faster

    result = compare_runs(candidate, baseline, gates=["valid"])
    rare = next(s for s in result.steps if s.step_id == "rare-bucket-policy")
    assert rare.delta_ms == 1000.0
    assert rare.delta_pct == pytest.approx(100.0)

    report = result.explain()
    assert "rare-bucket-policy" in report
    assert "+100.0%" in report


def test_scanning_more_is_distinguished_from_getting_slower():
    """The distinction that settles most arguments about a benchmark.

    Latency up with scan count flat is contention. Latency up with scan count up
    means the workload changed and the comparison was never valid.
    """
    candidate = summary()
    candidate["stats"]["steps"][0]["scan_count_total"] = 5_000_000
    baseline = summary()  # 1,000,000

    result = compare_runs(candidate, baseline, gates=["valid"])
    dense = next(s for s in result.steps if s.step_id == "dense-web-status")
    assert dense.scanned_more is True

    other = next(s for s in result.steps if s.step_id == "rare-bucket-policy")
    assert other.scanned_more is False


# ---------------------------------------------------------------- report


def test_the_explanation_reads_like_a_report():
    result = compare_runs(
        summary(p95=1400.0), summary(p95=1000.0),
        gates=["p95 <= baseline + 15%", "error_rate <= 2%"],
    )
    text = result.explain()
    assert text.startswith("FAIL")
    assert "1/2 gates met" in text
    assert "p95 <= baseline + 15%" in text


def test_a_blocked_comparison_explains_itself_and_nothing_else():
    text = compare_runs(summary(valid=False), summary(), gates=["valid"]).explain()
    assert text.startswith("COMPARISON BLOCKED")
    assert "gates met" not in text


def test_the_result_round_trips_as_json():
    import json

    result = compare_runs(summary(), summary(), gates=["p95 <= baseline + 15%"])
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["ok"] is True
    assert payload["summary"]["candidate"]["scenario"] == "search-classes"
    assert "scanned_more" in payload["steps"][0]
