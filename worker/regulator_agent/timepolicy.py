"""Choosing the time range for each dispatched search.

This is a small module doing an outsized job. The time range is the single
biggest lever on how much work a search does, and it is also the main defence
against measuring a cache rather than a cluster.

Three things it has to get right.

**Move the working set.** A search over the same window twice reads the same
buckets, and the second read comes from the host's page cache at RAM speed. The
number that comes back is real, it is just not the number you wanted. Jitter
slides the window so successive iterations touch different buckets.

**Stay reproducible.** The jitter is drawn from the same derived-seed machinery
as every other parameter, so iteration 7 of virtual user 3 asks about the same
window it asked about in yesterday's run.

**Align, so the ranges are legible.** Splunk snaps and reports time ranges
constantly, and an unaligned range like ``earliest=1756813271`` makes two
otherwise identical runs look different in the audit trail. Aligning to a
minute by default keeps ranges comparable without materially changing the work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .params import DrawContext, rng_for
from .scenario import TimePolicy


@dataclass(frozen=True)
class TimeWindow:
    """A concrete, absolute search time range.

    Absolute epochs rather than relative modifiers like ``-24h`` on purpose.
    A relative range is re-evaluated by Splunk at dispatch, so two workers
    dispatching "the last 24 hours" a second apart are running measurably
    different searches, and a re-run months later covers entirely different
    data. Pinning the epochs at dispatch time makes the search that was
    actually run recoverable from the record.
    """

    earliest: float
    latest: float

    @property
    def span_s(self) -> float:
        return self.latest - self.earliest

    def as_args(self) -> dict[str, str]:
        """The form fields splunkd wants on a job dispatch."""
        return {
            "earliest_time": f"{self.earliest:.0f}",
            "latest_time": f"{self.latest:.0f}",
        }

    def describe(self) -> str:
        return f"{self.earliest:.0f}-{self.latest:.0f} ({self.span_s / 3600.0:.2f}h)"


def resolve_window(
    policy: TimePolicy,
    ctx: DrawContext,
    now: Optional[float] = None,
) -> TimeWindow:
    """Turn a policy plus a draw context into concrete epochs.

    ``pinned`` returns exactly what the scenario asked for, every time. That is
    the reproducible-but-cached case, and lint warns about it, because on a
    second run Splunk will hand back the artefact it kept from the first.

    ``rolling`` walks backwards from now by a jittered offset. The jitter is
    drawn once per (virtual user, iteration, step) and applied to the *end* of
    the window, so the whole range slides rather than stretching: the span stays
    constant, which is what keeps two iterations comparable in cost.
    """
    if policy.mode == "pinned":
        if policy.earliest_epoch is None or policy.latest_epoch is None:
            raise ValueError(
                "a pinned time policy needs both earliest_epoch and latest_epoch"
            )
        return TimeWindow(earliest=policy.earliest_epoch, latest=policy.latest_epoch)

    wall = time.time() if now is None else now

    offset = 0.0
    if policy.jitter_s > 0:
        rng = rng_for("timewindow", ctx.scenario_seed, ctx.vu_id, ctx.iteration, ctx.step_id)
        offset = rng.uniform(0.0, policy.jitter_s)

    latest = wall - offset
    if policy.align_s > 0:
        # Floor rather than round: a window whose end is in the future covers
        # data that does not exist yet, which costs nothing but makes the
        # reported span misleading.
        latest = (latest // policy.align_s) * policy.align_s

    # Derived from the aligned end, and NOT aligned again. Flooring both ends
    # independently made the span longer than the configured window whenever
    # the window was not a whole number of alignment units: `window: 90s` with
    # the default `align: 1m` produced a 120 second span, scanning 33% more
    # data than asked for, silently, on every step. Worse, `window: 30m` and
    # `window: 45m` with `align: 1h` both collapsed to the same hour and did
    # identical work while reading as different tests.
    earliest = latest - policy.window_s

    return TimeWindow(earliest=earliest, latest=latest)
