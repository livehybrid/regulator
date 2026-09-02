"""Time-window resolution.

Small module, outsized job: the time range is the biggest lever on how much
work a search does, and the main defence against measuring a cache instead of a
cluster.
"""

from __future__ import annotations

import pytest

from regulator_agent.params import DrawContext
from regulator_agent.scenario import TimePolicy
from regulator_agent.timepolicy import TimeWindow, resolve_window

NOW = 1_756_800_000.0  # a fixed epoch, so nothing here depends on wall clock


def ctx(vu=1, iteration=1, step="s", seed=7):
    return DrawContext(scenario_seed=seed, vu_id=vu, iteration=iteration, step_id=step)


def test_a_pinned_policy_returns_exactly_what_was_asked_for():
    policy = TimePolicy(mode="pinned", earliest_epoch=1000.0, latest_epoch=2000.0)
    window = resolve_window(policy, ctx(), now=NOW)
    assert (window.earliest, window.latest) == (1000.0, 2000.0)
    assert window.span_s == 1000.0


def test_a_pinned_policy_missing_a_bound_is_an_error():
    with pytest.raises(ValueError):
        resolve_window(TimePolicy(mode="pinned", earliest_epoch=1000.0), ctx(), now=NOW)


def test_the_span_always_equals_the_window():
    policy = TimePolicy(mode="rolling", window_s=3600, jitter_s=900, align_s=60)
    for i in range(50):
        window = resolve_window(policy, ctx(iteration=i), now=NOW)
        assert window.span_s == pytest.approx(3600, abs=0.001)


def test_both_bounds_are_aligned():
    policy = TimePolicy(mode="rolling", window_s=3600, jitter_s=900, align_s=60)
    for i in range(50):
        window = resolve_window(policy, ctx(iteration=i), now=NOW)
        assert window.latest % 60 == 0
        assert window.earliest % 60 == 0


def test_the_window_never_ends_in_the_future():
    policy = TimePolicy(mode="rolling", window_s=3600, jitter_s=900, align_s=60)
    for i in range(50):
        assert resolve_window(policy, ctx(iteration=i), now=NOW).latest <= NOW


def test_zero_jitter_gives_one_window_for_every_draw():
    policy = TimePolicy(mode="rolling", window_s=3600, jitter_s=0, align_s=60)
    windows = {
        (resolve_window(policy, ctx(vu=v, iteration=i), now=NOW).earliest)
        for v in range(5)
        for i in range(5)
    }
    assert len(windows) == 1


def test_jitter_moves_the_working_set():
    """Different iterations must read different buckets.

    Without this the host page cache answers the second iteration from RAM and
    the cluster looks faster than it is.
    """
    policy = TimePolicy(mode="rolling", window_s=3600, jitter_s=1800, align_s=60)
    starts = {resolve_window(policy, ctx(iteration=i), now=NOW).earliest for i in range(40)}
    assert len(starts) > 15


def test_the_same_draw_context_always_gives_the_same_window():
    """Reproducibility again: run two must ask the same questions as run one."""
    policy = TimePolicy(mode="rolling", window_s=3600, jitter_s=1800, align_s=60)
    first = resolve_window(policy, ctx(vu=4, iteration=9), now=NOW)
    for _ in range(20):
        again = resolve_window(policy, ctx(vu=4, iteration=9), now=NOW)
        assert (again.earliest, again.latest) == (first.earliest, first.latest)


def test_as_args_emits_the_fields_splunkd_expects():
    window = TimeWindow(earliest=1000.4, latest=2000.6)
    args = window.as_args()
    assert set(args) == {"earliest_time", "latest_time"}
    assert args == {"earliest_time": "1000", "latest_time": "2001"}


def test_describe_is_human_readable():
    assert "h)" in TimeWindow(earliest=0, latest=7200).describe()
