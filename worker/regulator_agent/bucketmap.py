"""A target's buckets laid out by index and time, for the bucket map.

The cache forecast reduces every bucket to a one-line summary. This module
keeps the shape instead: for each index, how many bytes of buckets fall in
each slice of time, split by how much of each bucket is on local disk. That
is what lets an operator see at a glance which parts of which indexes are
warm, and where a scenario's searches will land.

Aggregated here, on the server side, on purpose: a large estate has tens of
thousands of buckets and a browser should receive a few thousand cells, not
one row per bucket.

**States.** With only the cache manager's reading a bucket is ``local`` or
``remote``. Where ``dbinspect`` was also read (see ``cachewarm.inspect_buckets``)
a local bucket is refined to ``full``, ``index_only`` or ``partial``, and a
bucket evicted to a stub counts as ``remote``. A local bucket dbinspect had no
row for stays ``local``: known to be local, detail not read.

**A bucket spanning several slices** contributes its bytes to each in
proportion to the overlap, so a row's bytes sum to the index's total. The
bucket count of a cell is the number of buckets overlapping it, so counts do
not sum along a row.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .cachewarm import FULL, INDEX_ONLY, PARTIAL, STUB, Residency
from .smartstore import Bucket, CacheState

REMOTE = "remote"
LOCAL = "local"
# Least to most ready for a search. ``local`` is last: local, detail unknown.
STATE_ORDER = (REMOTE, PARTIAL, INDEX_ONLY, FULL, LOCAL)

MIN_BINS, MAX_BINS = 10, 240


def _state(bid: str, held: Sequence[Bucket], residency: Optional[Residency]) -> str:
    if not any(b.is_local for b in held):
        return REMOTE
    entry = residency.per_bid.get(bid) if residency is not None and residency.available else None
    if entry is None:
        return LOCAL
    return REMOTE if entry.cls == STUB else entry.cls


def group_by_id(
    buckets: Iterable[Bucket], indexes: Optional[Sequence[str]] = None
) -> Dict[str, List[Bucket]]:
    """Bucket copies by bucket id, optionally only for the named indexes."""
    wanted = {i.lower() for i in indexes} if indexes else None
    copies: Dict[str, List[Bucket]] = {}
    for bucket in buckets:
        if wanted is not None and bucket.index.lower() not in wanted:
            continue
        copies.setdefault(bucket.bid, []).append(bucket)
    return copies


def build(
    state: CacheState,
    *,
    indexes: Optional[Sequence[str]] = None,
    earliest: Optional[float] = None,
    latest: Optional[float] = None,
    bins: int = 120,
    residency: Optional[Residency] = None,
    max_indexes: int = 50,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The map: per index, per time slice, bytes by state."""
    now = time.time() if now is None else now
    out: Dict[str, Any] = {
        "available": False, "reason": "", "detail": "cache", "states": [],
        "earliest": None, "latest": None, "bin_s": None, "bins": 0,
        "indexes": [], "notes": [],
    }
    if not state.available:
        out["reason"] = state.reason or "the SmartStore cache could not be read"
        return out
    copies = group_by_id(state.buckets, indexes)
    if not copies:
        out["reason"] = (
            "the cache manager listed no buckets for " + ", ".join(indexes)
            if indexes else "the cache manager listed no buckets"
        )
        return out

    dated = [h[0] for h in copies.values() if h[0].earliest is not None and h[0].latest is not None]
    lo = earliest if earliest is not None else min((b.earliest for b in dated), default=now - 86400)
    hi = latest if latest is not None else max((b.latest for b in dated), default=now)
    if hi <= lo:
        hi = lo + 1.0
    bins = max(MIN_BINS, min(MAX_BINS, int(bins)))
    bin_s = (hi - lo) / bins

    rows: Dict[str, Dict[str, Any]] = {}
    seen_states = set()
    for bid, held in copies.items():
        first = held[0]
        size = max(b.size for b in held)
        bucket_state = _state(bid, held, residency)
        row = rows.setdefault(first.index, {
            "index": first.index, "buckets": 0, "bytes": 0, "undated": 0, "outside": 0,
            "by_state": {}, "_cells": {},
        })
        row["buckets"] += 1
        row["bytes"] += size
        tally = row["by_state"].setdefault(bucket_state, {"buckets": 0, "bytes": 0})
        tally["buckets"] += 1
        tally["bytes"] += size
        seen_states.add(bucket_state)
        if first.earliest is None or first.latest is None:
            row["undated"] += 1
            continue
        b_lo, b_hi = first.earliest, max(first.latest, first.earliest + 1.0)
        if b_hi <= lo or b_lo >= hi:
            row["outside"] += 1
            continue
        start = max(0, int((max(b_lo, lo) - lo) // bin_s))
        end = min(bins - 1, int((min(b_hi, hi) - lo - 1e-9) // bin_s))
        span = b_hi - b_lo
        for n in range(start, end + 1):
            c_lo, c_hi = lo + n * bin_s, lo + (n + 1) * bin_s
            overlap = max(0.0, min(b_hi, c_hi) - max(b_lo, c_lo))
            cell = row["_cells"].setdefault(n, {"buckets": 0, "bytes": {}})
            cell["buckets"] += 1
            cell["bytes"][bucket_state] = cell["bytes"].get(bucket_state, 0.0) + size * overlap / span

    ordered = sorted(rows.values(), key=lambda r: (-r["bytes"], r["index"]))
    if len(ordered) > max_indexes:
        out["notes"].append(
            f"showing the {max_indexes} largest of {len(ordered)} indexes; name indexes to see others"
        )
        ordered = ordered[:max_indexes]
    for row in ordered:
        cells = row.pop("_cells")
        row["cells"] = [
            {"bin": n, "buckets": c["buckets"], "bytes": {k: int(v) for k, v in c["bytes"].items()}}
            for n, c in sorted(cells.items())
        ]
        local_bytes = sum(v["bytes"] for k, v in row["by_state"].items() if k != REMOTE)
        row["local_pct"] = round(100.0 * local_bytes / row["bytes"], 2) if row["bytes"] else 0.0

    inspected = residency is not None and residency.available
    out.update(
        available=True,
        detail="disk" if inspected else "cache",
        states=[s for s in STATE_ORDER if s in seen_states],
        earliest=int(lo), latest=int(hi), bin_s=bin_s, bins=bins,
        indexes=ordered,
    )
    if residency is not None and not residency.available:
        out["notes"].append(f"on-disk detail not read: {residency.reason}")
    if inspected and residency.unmatched:
        out["notes"].append(
            f"{residency.unmatched} bucket(s) had no dbinspect row and are shown as the cache "
            "manager reported them"
        )
    return out


def overlay(
    step_spans: Iterable[Any], index_names: Iterable[str], earliest: float, latest: float
) -> List[Dict[str, Any]]:
    """The ranges a scenario's searches read, per index on the map.

    ``step_spans`` are the forecast's ``StepSpan`` objects, whose indexes may
    be wildcards; each is matched against the map's index names and the
    matching ranges merged and clipped to the map.
    """
    import fnmatch

    from .cachewarm import merge

    out: List[Dict[str, Any]] = []
    for name in index_names:
        spans: List[Tuple[float, float]] = []
        for span in step_spans:
            if any(fnmatch.fnmatchcase(name.lower(), p) for p in span.indexes):
                spans.append((span.earliest, span.latest))
        for lo, hi in merge(spans):
            if hi <= earliest or lo >= latest:
                continue
            out.append({
                "index": name, "earliest": int(lo), "latest": int(hi),
                "clipped": lo < earliest or hi > latest,
            })
    return out


class _Span:
    """The shape :func:`overlay` reads: lower-cased index patterns and a range."""

    __slots__ = ("indexes", "earliest", "latest")

    def __init__(self, indexes: Sequence[str], earliest: float, latest: float) -> None:
        self.indexes = [i.lower() for i in indexes]
        self.earliest = float(earliest)
        self.latest = float(latest)


def _matching(state: CacheState, patterns: Iterable[str]) -> List[str]:
    """Index names the cache manager lists that match any of ``patterns``."""
    import fnmatch

    wanted = [p.lower() for p in patterns]
    return sorted(
        name for name in {b.index for b in state.buckets}
        if any(fnmatch.fnmatchcase(name.lower(), p) for p in wanted)
    )


def _extent(states: Sequence[CacheState], names: Sequence[str]) -> Optional[Tuple[float, float]]:
    """The earliest and latest time any dated bucket of ``names`` covers."""
    wanted = {n.lower() for n in names}
    los, his = [], []
    for state in states:
        for bucket in state.buckets:
            if bucket.index.lower() in wanted and bucket.earliest is not None and bucket.latest is not None:
                los.append(bucket.earliest)
                his.append(bucket.latest)
    return (min(los), max(his)) if los else None


def plan_map(
    state: CacheState,
    plan: Sequence[Any],
    *,
    bins: int = 60,
    max_indexes: int = 20,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The map around a warm-up plan, with the plan's lines drawn over it.

    ``plan`` is a list of ``cachewarm.WarmTarget``. The rows are the indexes
    the plan names (wildcards matched against the cache manager's list) and
    the time axis is the plan's own span with a margin either side, so what
    sits just outside a line is visible too.
    """
    names = _matching(state, [t.index for t in plan]) if state.available else []
    lo = min(t.earliest for t in plan)
    hi = max(t.latest for t in plan)
    margin = max(60.0, 0.1 * (hi - lo))
    document = build(
        state, indexes=names or [t.index for t in plan], earliest=lo - margin, latest=hi + margin,
        bins=bins, max_indexes=max_indexes, now=now,
    )
    if document["available"]:
        document["overlay"] = {
            "plan": True,
            "spans": overlay(
                [_Span([t.index], t.earliest, t.latest) for t in plan],
                [row["index"] for row in document["indexes"]],
                document["earliest"], document["latest"],
            ),
            "notes": [],
        }
        unmatched = sorted({
            t.index for t in plan if not _matching(state, [t.index])
        })
        if unmatched:
            document["overlay"]["notes"].append(
                "no bucket in the cache manager matches " + ", ".join(unmatched)
                + ": a non-SmartStore index, or one this account cannot see"
            )
    return document


def run_pair(
    before: CacheState,
    after: CacheState,
    forecast: Optional[Dict[str, Any]] = None,
    *,
    bins: int = 60,
    max_indexes: int = 10,
) -> Dict[str, Any]:
    """The map before and after a run, on the same rows and the same axis.

    Built from the two cache manager readings every run already takes, so it
    costs the target nothing more: no extra listing and no dbinspect. That
    also means the states are local or remote only. A forecast read with
    dbinspect at the start describes the before side alone, and refining one
    side and not the other would make the pair disagree about buckets that
    did not change.

    The rows are the indexes the run's forecast says it reads, the largest
    first; without a forecast, the indexes whose local bucket count changed;
    failing that, the largest. The axis spans what the run reads, clipped to
    the buckets that exist.
    """
    out: Dict[str, Any] = {"available": False, "reason": "", "source": None, "notes": []}
    if not before.available or not after.available:
        out["reason"] = before.reason or after.reason or "the SmartStore cache could not be read"
        return out

    forecast = forecast or {}
    per_index = (forecast.get("per_index") or {}) if forecast.get("available") else {}
    spans: List[_Span] = []
    if per_index:
        ranked = sorted(per_index.items(), key=lambda kv: (-(kv[1].get("bytes_total") or 0), kv[0]))
        names = [name for name, _ in ranked[:max_indexes]]
        for name in names:
            for lo, hi in per_index[name].get("spans") or []:
                spans.append(_Span([name], lo, hi))
        source = "forecast"
    else:
        b_idx, a_idx = before.per_index, after.per_index

        def local(side: Dict[str, Any], name: str) -> int:
            return side[name].local_buckets if name in side else 0

        def size(name: str) -> int:
            return max(a_idx[name].total_bytes if name in a_idx else 0, b_idx[name].total_bytes if name in b_idx else 0)

        changed = [name for name in set(b_idx) | set(a_idx) if local(b_idx, name) != local(a_idx, name)]
        if changed:
            names, source = sorted(changed, key=lambda n: (-size(n), n))[:max_indexes], "changed"
        else:
            names = sorted(set(b_idx) | set(a_idx), key=lambda n: (-size(n), n))[:max_indexes]
            source = "largest"
    if len(per_index) > max_indexes:
        out["notes"].append(
            f"showing the {max_indexes} largest of the {len(per_index)} indexes the run reads"
        )

    extent = _extent([before, after], names)
    if extent is None:
        lo = hi = None
    else:
        lo, hi = extent
        if spans:
            lo = max(lo, min(s.earliest for s in spans))
            hi = min(hi, max(s.latest for s in spans))
            if hi <= lo:
                lo, hi = extent
    pair = {
        side: build(state, indexes=names or None, earliest=lo, latest=hi, bins=bins, max_indexes=max_indexes)
        for side, state in (("before", before), ("after", after))
    }
    if not pair["before"]["available"] and not pair["after"]["available"]:
        out["reason"] = pair["before"]["reason"]
        return out
    base = pair["before"] if pair["before"]["available"] else pair["after"]
    out.update(available=True, source=source, before=pair["before"], after=pair["after"])
    out["overlay"] = overlay(
        spans, [row["index"] for row in base["indexes"]], base["earliest"], base["latest"]
    ) if spans else []
    return out
