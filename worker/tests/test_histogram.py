"""Histogram accuracy and, above all, exact mergeability.

The fleet aggregation design rests entirely on one property: merging N workers'
histograms and then computing a percentile gives the same answer as recording
every sample into one histogram. If that is not true, a fleet report is a
guess. It is tested hard below.
"""

from __future__ import annotations

import random

import pytest

from regulator_agent.histogram import (
    SUB,
    LatencyHistogram,
    bucket_index,
    bucket_lower_bound,
    bucket_width,
    merge_all,
    merge_dicts,
)


def exact_percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    import math

    rank = max(0, min(len(ordered) - 1, math.ceil(p / 100.0 * len(ordered)) - 1))
    return ordered[rank]


# --------------------------------------------------------------- bucketing


def test_bucket_indices_are_monotonic():
    previous = -1
    for value in list(range(0, 5000)) + [10 ** n for n in range(4, 10)]:
        index = bucket_index(value)
        assert index >= previous
        previous = index


@pytest.mark.parametrize(
    "value", [0, 1, 62, 63, 64, 65, 127, 128, 129, 1000, 65535, 10 ** 6, 10 ** 9]
)
def test_a_value_lands_inside_its_own_bucket(value):
    index = bucket_index(value)
    low = bucket_lower_bound(index)
    assert low <= value < low + bucket_width(index)


def test_the_linear_region_is_exact():
    for value in range(SUB):
        assert bucket_index(value) == value
        assert bucket_width(value) == 1


def test_relative_error_stays_within_the_documented_bound():
    """Worst case is one part in 64, about 1.6%, before midpoint reporting."""
    for value in [100, 999, 12345, 987654, 5_000_000]:
        index = bucket_index(value)
        assert bucket_width(index) / value <= 1.0 / SUB * 1.05


def test_negative_values_are_rejected():
    with pytest.raises(ValueError):
        bucket_index(-1)


# --------------------------------------------------------------- accuracy


def test_percentiles_are_within_two_percent_of_the_truth():
    rng = random.Random(11)
    values_ms = [rng.lognormvariate(3.0, 0.8) for _ in range(10000)]
    hist = LatencyHistogram()
    for value in values_ms:
        hist.record_ms(value)

    for p in (50, 90, 95, 99):
        estimate = hist.percentile_ms(p)
        truth = exact_percentile(values_ms, p)
        assert abs(estimate - truth) / truth < 0.02, (p, estimate, truth)


def test_count_sum_mean_min_and_max_are_exact_not_bucketed():
    values_ms = [1.0, 2.5, 100.0, 3333.0]
    hist = LatencyHistogram()
    for value in values_ms:
        hist.record_ms(value)
    assert hist.count == 4
    assert hist.min_ms == pytest.approx(1.0, abs=0.001)
    assert hist.max_ms == pytest.approx(3333.0, abs=0.001)
    assert hist.mean_ms == pytest.approx(sum(values_ms) / 4, rel=1e-6)


def test_a_percentile_never_escapes_the_observed_range():
    """A p99 above the largest value ever seen destroys trust in a report."""
    hist = LatencyHistogram()
    hist.record_ms(1234.5)
    for p in (0, 1, 50, 95, 99, 100):
        assert hist.min_ms <= hist.percentile_ms(p) <= hist.max_ms


def test_an_empty_histogram_reports_zero_rather_than_raising():
    """A step that has not run yet during a ramp is a normal state."""
    hist = LatencyHistogram()
    assert hist.percentile_ms(95) == 0.0
    assert hist.mean_ms == 0.0
    assert hist.summary()["count"] == 0


def test_recording_in_different_units_agrees():
    a, b, c = LatencyHistogram(), LatencyHistogram(), LatencyHistogram()
    a.record_us(1_500_000)
    b.record_ms(1500)
    c.record_s(1.5)
    assert a.to_dict() == b.to_dict() == c.to_dict()


# ---------------------------------------------------------------- merging


def test_merging_is_exact():
    """The property the whole fleet aggregation rests on."""
    rng = random.Random(5)
    parts = [[rng.lognormvariate(4.0, 1.0) for _ in range(2000)] for _ in range(6)]

    per_worker = []
    for values in parts:
        hist = LatencyHistogram()
        for value in values:
            hist.record_ms(value)
        per_worker.append(hist)

    combined = LatencyHistogram()
    for values in parts:
        for value in values:
            combined.record_ms(value)

    merged = merge_all(per_worker)
    assert merged.count == combined.count
    assert merged.total_us == combined.total_us
    for p in (50, 90, 95, 99, 99.9):
        assert merged.percentile_us(p) == combined.percentile_us(p)


def test_averaging_percentiles_would_have_been_wrong():
    """Demonstrates why merging exists at all.

    One worker sees fast work and one sees slow work. The mean of their p95s is
    nowhere near the p95 of the population, which is exactly the mistake a
    naive fleet report makes.
    """
    fast, slow = LatencyHistogram(), LatencyHistogram()
    for _ in range(1000):
        fast.record_ms(10)
    for _ in range(1000):
        slow.record_ms(1000)

    merged = merge_all([fast, slow])
    naive_average = (fast.percentile_ms(95) + slow.percentile_ms(95)) / 2
    assert merged.percentile_ms(95) == pytest.approx(1000, rel=0.02)
    assert abs(naive_average - merged.percentile_ms(95)) > 400


def test_round_trip_through_the_wire_form_is_exact():
    rng = random.Random(3)
    hist = LatencyHistogram()
    for _ in range(500):
        hist.record_ms(rng.uniform(0.1, 5000))

    restored = LatencyHistogram.from_dict(hist.to_dict())
    assert restored.count == hist.count
    assert restored.total_us == hist.total_us
    assert restored.min_ms == hist.min_ms
    assert restored.max_ms == hist.max_ms
    assert restored.summary() == hist.summary()


def test_merge_dicts_works_straight_off_the_wire():
    a, b = LatencyHistogram(), LatencyHistogram()
    a.record_ms(10)
    b.record_ms(20)
    merged = merge_dicts([a.to_dict(), b.to_dict()])
    assert merged.count == 2


def test_a_mismatched_layout_is_refused_rather_than_approximated():
    payload = LatencyHistogram().to_dict()
    payload["sub_bits"] = 5
    with pytest.raises(ValueError) as excinfo:
        LatencyHistogram.from_dict(payload)
    assert "cannot be merged" in str(excinfo.value)


def test_a_wrong_schema_version_is_refused():
    payload = LatencyHistogram().to_dict()
    payload["v"] = 99
    with pytest.raises(ValueError):
        LatencyHistogram.from_dict(payload)


def test_reset_empties_but_keeps_the_object():
    hist = LatencyHistogram()
    hist.record_ms(5)
    hist.reset()
    assert hist.count == 0
    assert hist.percentile_ms(50) == 0.0


def test_summary_has_the_reporting_shape():
    hist = LatencyHistogram()
    for value in range(1, 101):
        hist.record_ms(value)
    summary = hist.summary()
    assert set(summary) == {
        "count", "mean_ms", "min_ms", "p50_ms", "p90_ms", "p95_ms", "p99_ms", "max_ms"
    }
    assert summary["p50_ms"] < summary["p95_ms"] < summary["max_ms"]
