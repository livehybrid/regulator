"""Latency histograms that can be merged across workers.

Why not just collect the numbers and take a percentile at the end? Because a
fleet of twenty workers each running four hundred virtual users for twenty
minutes produces tens of millions of samples, and shipping them all to a
control plane every five seconds is not a live chart, it is a denial of service
against your own database.

Why not have each worker report its own p95 and average those? Because
averaging percentiles is meaningless. Two workers reporting p95 of 4 s each do
not make a fleet p95 of 4 s: the true value depends on the whole distribution,
and the error is not small or in a predictable direction.

The answer both problems share is a histogram. Each worker keeps a bounded,
fixed-size sketch, ships it on every heartbeat, and the control plane sums the
bucket counts before computing a single percentile over the merged whole. The
merge is exact addition, so a fleet percentile is as correct as a single
worker's.

**Implementation.** A log-linear (HdrHistogram-shaped) bucketing with 64
sub-buckets per octave, giving a worst-case relative error of 1/64, about 1.6%,
reported at the bucket midpoint so the practical error is nearer 0.8%. Values
are microseconds as integers. Written by hand rather than pulling in a
dependency for three reasons: the wire format stays plain JSON that a human can
read in a heartbeat body, merging is a dictionary sum rather than a library
call, and the worker image keeps one fewer transitive dependency.

The exact count, sum, minimum and maximum are tracked separately and are not
approximations, so the mean and the extremes are always exact even though the
percentiles are bucketed.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

# 2^6 = 64 sub-buckets per octave. Raising this improves precision and costs
# proportionally more buckets. 64 keeps a full millisecond-to-ten-minute range
# in roughly 1400 buckets, which is a few tens of kilobytes of JSON at worst
# and usually far less, because a real latency distribution only populates a
# narrow band of them.
SUB_BITS = 6
SUB = 1 << SUB_BITS

SCHEMA_VERSION = 1


def bucket_index(value_us: int) -> int:
    """Bucket for a non-negative integer number of microseconds."""
    if value_us < 0:
        raise ValueError("a latency cannot be negative")
    if value_us < SUB:
        # Linear region: every value below 64 microseconds is its own bucket,
        # so sub-millisecond timings are exact. Not because anyone cares about
        # a 40 microsecond search, but because the same class is used for
        # dispatch overhead, where small values are real.
        return value_us
    exponent = value_us.bit_length() - 1
    shift = exponent - SUB_BITS
    mantissa = value_us >> shift
    return SUB + (shift * SUB) + (mantissa - SUB)


def bucket_lower_bound(index: int) -> int:
    """Smallest value that lands in this bucket."""
    if index < SUB:
        return index
    offset = index - SUB
    shift, mantissa_offset = divmod(offset, SUB)
    return (SUB + mantissa_offset) << shift


def bucket_width(index: int) -> int:
    if index < SUB:
        return 1
    return 1 << ((index - SUB) // SUB)


def bucket_midpoint(index: int) -> float:
    return bucket_lower_bound(index) + (bucket_width(index) - 1) / 2.0


class LatencyHistogram:
    """A mergeable, serialisable latency sketch in microseconds."""

    __slots__ = ("_buckets", "_count", "_sum", "_min", "_max")

    def __init__(self) -> None:
        self._buckets: Dict[int, int] = {}
        self._count = 0
        self._sum = 0
        self._min: Optional[int] = None
        self._max: Optional[int] = None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_us(self, value_us: float, count: int = 1) -> None:
        if count <= 0:
            return
        value = int(round(max(0.0, float(value_us))))
        index = bucket_index(value)
        self._buckets[index] = self._buckets.get(index, 0) + count
        self._count += count
        self._sum += value * count
        if self._min is None or value < self._min:
            self._min = value
        if self._max is None or value > self._max:
            self._max = value

    def record_ms(self, value_ms: float, count: int = 1) -> None:
        self.record_us(value_ms * 1000.0, count=count)

    def record_s(self, value_s: float, count: int = 1) -> None:
        self.record_us(value_s * 1_000_000.0, count=count)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def count(self) -> int:
        return self._count

    @property
    def total_us(self) -> int:
        return self._sum

    @property
    def mean_ms(self) -> float:
        return (self._sum / self._count) / 1000.0 if self._count else 0.0

    @property
    def min_ms(self) -> float:
        return (self._min or 0) / 1000.0

    @property
    def max_ms(self) -> float:
        return (self._max or 0) / 1000.0

    def percentile_us(self, p: float) -> float:
        """The value at percentile ``p``, expressed 0 to 100.

        Returns 0.0 for an empty histogram rather than raising. A step that has
        not run yet is a normal state during a ramp, and making every caller
        guard against it would be noise.
        """
        if self._count == 0:
            return 0.0
        if p <= 0:
            return float(self._min or 0)
        if p >= 100:
            return float(self._max or 0)
        target = (p / 100.0) * self._count
        seen = 0
        for index in sorted(self._buckets):
            seen += self._buckets[index]
            if seen >= target:
                # Clamp to the observed extremes: bucket midpoints can sit
                # outside [min, max] for a histogram with very few samples, and
                # reporting a p99 above the largest value ever seen is the kind
                # of small lie that destroys trust in a whole report.
                value = bucket_midpoint(index)
                if self._min is not None:
                    value = max(value, float(self._min))
                if self._max is not None:
                    value = min(value, float(self._max))
                return value
        return float(self._max or 0)

    def percentile_ms(self, p: float) -> float:
        return self.percentile_us(p) / 1000.0

    def summary(self) -> Dict[str, float]:
        """The standard reporting shape, all in milliseconds."""
        return {
            "count": float(self._count),
            "mean_ms": round(self.mean_ms, 3),
            "min_ms": round(self.min_ms, 3),
            "p50_ms": round(self.percentile_ms(50), 3),
            "p90_ms": round(self.percentile_ms(90), 3),
            "p95_ms": round(self.percentile_ms(95), 3),
            "p99_ms": round(self.percentile_ms(99), 3),
            "max_ms": round(self.max_ms, 3),
        }

    # ------------------------------------------------------------------
    # Merging and serialisation
    # ------------------------------------------------------------------

    def merge(self, other: "LatencyHistogram") -> "LatencyHistogram":
        """Add another histogram into this one, in place."""
        for index, count in other._buckets.items():
            self._buckets[index] = self._buckets.get(index, 0) + count
        self._count += other._count
        self._sum += other._sum
        if other._min is not None:
            self._min = other._min if self._min is None else min(self._min, other._min)
        if other._max is not None:
            self._max = other._max if self._max is None else max(self._max, other._max)
        return self

    def to_dict(self) -> Dict[str, Any]:
        """A compact, human-readable wire form.

        Bucket keys are strings because that is what JSON gives back anyway,
        and pretending otherwise leads to a round-trip that is not a round trip.
        """
        return {
            "v": SCHEMA_VERSION,
            "unit": "us",
            "sub_bits": SUB_BITS,
            "count": self._count,
            "sum": self._sum,
            "min": self._min,
            "max": self._max,
            "buckets": {str(k): v for k, v in sorted(self._buckets.items())},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LatencyHistogram":
        if raw.get("v") != SCHEMA_VERSION:
            raise ValueError(
                f"histogram schema version {raw.get('v')!r} is not {SCHEMA_VERSION}"
            )
        if int(raw.get("sub_bits", SUB_BITS)) != SUB_BITS:
            # Merging histograms with different bucket layouts would silently
            # produce nonsense, so refuse rather than approximate.
            raise ValueError(
                f"histogram sub_bits {raw.get('sub_bits')!r} does not match this build's "
                f"{SUB_BITS}: these histograms cannot be merged"
            )
        hist = cls()
        hist._buckets = {int(k): int(v) for k, v in (raw.get("buckets") or {}).items()}
        hist._count = int(raw.get("count", 0))
        hist._sum = int(raw.get("sum", 0))
        hist._min = None if raw.get("min") is None else int(raw["min"])
        hist._max = None if raw.get("max") is None else int(raw["max"])
        return hist

    def copy(self) -> "LatencyHistogram":
        clone = LatencyHistogram()
        clone.merge(self)
        return clone

    def reset(self) -> None:
        """Empty the histogram, keeping the object identity.

        Used for interval reporting: a heartbeat can carry either the
        cumulative histogram or the delta since the last heartbeat. Regulator
        ships the delta, because a cumulative histogram makes a live chart
        monotonically smoother and hides exactly the moment things got worse.
        """
        self._buckets.clear()
        self._count = 0
        self._sum = 0
        self._min = None
        self._max = None

    def __len__(self) -> int:
        return self._count

    def __repr__(self) -> str:
        if not self._count:
            return "LatencyHistogram(empty)"
        return (
            f"LatencyHistogram(count={self._count}, p50={self.percentile_ms(50):.1f}ms, "
            f"p95={self.percentile_ms(95):.1f}ms, max={self.max_ms:.1f}ms)"
        )


def merge_all(histograms: Iterable[LatencyHistogram]) -> LatencyHistogram:
    """Merge many histograms into a new one."""
    merged = LatencyHistogram()
    for hist in histograms:
        merged.merge(hist)
    return merged


def merge_dicts(payloads: Iterable[Mapping[str, Any]]) -> LatencyHistogram:
    """Merge serialised histograms straight off the wire."""
    return merge_all(LatencyHistogram.from_dict(p) for p in payloads)
