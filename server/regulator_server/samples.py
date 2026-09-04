"""The time series behind the graphs and the HEC samples.

One sample every few seconds per run: the cumulative figures at that instant
and what happened in the interval since the previous sample. The same
document is stored as a row for the run page and handed to the telemetry
emitter, so the chart in the browser and the chart in Splunk are drawn from
one series.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select

from .models import Run, RunSample, TargetSample

INTERVAL_KEYS = (
    "since", "seconds", "executions", "errors", "error_rate_pct", "errors_by_class", "queued",
    "job_executions", "queued_pct", "scan_count", "throughput_per_s", "p50_ms", "p95_ms", "p99_ms",
    "max_ms", "queued_p95_ms", "loop_lag_p95_ms", "in_flight", "percentiles_note",
)


def build_sample(interval: Dict[str, Any], snapshot: Dict[str, Any], at: Optional[float] = None, slot: Optional[int] = None) -> Dict[str, Any]:
    """One sample document from an interval and the cumulative snapshot."""
    at = at or time.time()
    latency = snapshot.get("latency") or {}
    queueing = snapshot.get("queueing") or {}
    generator = snapshot.get("generator") or {}
    return {
        "at": round(at, 3),
        "elapsed_s": round(float(snapshot.get("elapsed_s") or 0.0), 2),
        "slot": slot,
        "executions": int(snapshot.get("executions") or 0),
        "errors": int(snapshot.get("errors") or 0),
        "in_flight": int(snapshot.get("in_flight") or interval.get("in_flight") or 0),
        "searches_queued": int(queueing.get("searches_queued") or 0),
        "throughput_per_s": float(snapshot.get("throughput_per_s") or 0.0),
        "interval": {key: interval.get(key) for key in INTERVAL_KEYS if key in interval},
        "cum": {
            "p50_ms": latency.get("p50_ms"),
            "p95_ms": latency.get("p95_ms"),
            "p99_ms": latency.get("p99_ms"),
            "error_rate_pct": snapshot.get("error_rate_pct"),
            "queued_pct": queueing.get("queued_pct"),
            "loop_lag_p95_ms": generator.get("loop_lag_p95_ms"),
            "peak_in_flight": snapshot.get("peak_in_flight"),
        },
    }


def store_sample(session, run_id: int, sample: Dict[str, Any]) -> RunSample:
    row = RunSample(
        run_id=run_id,
        slot=sample.get("slot"),
        at=float(sample["at"]),
        elapsed_s=float(sample.get("elapsed_s") or 0.0),
        executions=int(sample.get("executions") or 0),
        errors=int(sample.get("errors") or 0),
        in_flight=int(sample.get("in_flight") or 0),
        searches_queued=int(sample.get("searches_queued") or 0),
        throughput_per_s=float(sample.get("throughput_per_s") or 0.0),
        interval_json=sample.get("interval") or {},
        cum_json=sample.get("cum") or {},
    )
    session.add(row)
    return row


def store_cache_sample(session, target_id: Optional[int], run_id: Optional[int], kind: str, state: Dict[str, Any], detail: Optional[Dict[str, Any]] = None) -> Optional[TargetSample]:
    """A cache reading, from a CacheState document, when the cache exists."""
    if not target_id or not isinstance(state, dict) or not state.get("available"):
        return None
    row = TargetSample(
        target_id=target_id,
        run_id=run_id,
        at=time.time(),
        kind=kind,
        local_buckets=int(state.get("local_buckets") or 0),
        total_buckets=int(state.get("total_buckets") or 0),
        local_pct=float(state.get("local_pct") or 0.0),
        fill_pct=float(state.get("fill_pct") or 0.0),
        local_bytes=int(state.get("local_bytes") or 0),
        detail_json=detail or None,
    )
    session.add(row)
    return row


def samples_for(session, run_id: int, since: Optional[float] = None, slots: bool = False, points: int = 600) -> List[Dict[str, Any]]:
    """The run's series, aggregate rows by default, downsampled to ``points``."""
    query = select(RunSample).where(RunSample.run_id == run_id)
    query = query.where(RunSample.slot.isnot(None)) if slots else query.where(RunSample.slot.is_(None))
    if since is not None:
        query = query.where(RunSample.at > float(since))
    rows = list(session.scalars(query.order_by(RunSample.at)))
    if points and len(rows) > points:
        step = len(rows) / float(points)
        rows = [rows[int(index * step)] for index in range(points)] + [rows[-1]]
    return [row.to_dict() for row in rows]


def sparkline_for(session, run_id: int, points: int = 40) -> List[Optional[float]]:
    """Interval p95 values, evenly thinned, for a runs-list sparkline."""
    rows = list(session.scalars(
        select(RunSample).where(RunSample.run_id == run_id, RunSample.slot.is_(None)).order_by(RunSample.at)
    ))
    if len(rows) > points:
        step = len(rows) / float(points)
        rows = [rows[int(index * step)] for index in range(points)]
    return [((row.interval_json or {}).get("p95_ms")) for row in rows]


def cache_samples_for(session, target_id: int, since: Optional[float] = None, limit: int = 2000) -> List[Dict[str, Any]]:
    query = select(TargetSample).where(TargetSample.target_id == target_id)
    if since is not None:
        query = query.where(TargetSample.at > float(since))
    rows = list(session.scalars(query.order_by(TargetSample.at.desc()).limit(limit)))
    rows.reverse()
    return [row.to_dict() for row in rows]


def markers_for(run: Run) -> List[Dict[str, Any]]:
    """Instants worth drawing on a run's charts, from what the run row knows."""
    markers: List[Dict[str, Any]] = []
    if run.t0:
        markers.append({"at": run.t0, "kind": "release", "label": "T0"})
    summary = run.summary_json or {}
    cache = summary.get("cache") or {}
    for epoch in cache.get("epochs") or []:
        if epoch.get("requested_at"):
            markers.append({"at": epoch["requested_at"], "kind": "evict", "label": f"E{epoch.get('epoch', '')}", "duration_s": epoch.get("duration_s")})
    for worker in summary.get("workers") or []:
        if worker.get("outcome") == "lost" and worker.get("lost_at"):
            markers.append({"at": worker["lost_at"], "kind": "lost", "label": f"slot {worker.get('slot')} lost"})
    if run.ended_at and run.state == "stopped":
        markers.append({"at": run.ended_at, "kind": "stop", "label": "stop"})
    return markers


def cache_facts(state: Dict[str, Any]) -> Dict[str, Any]:
    """The few numbers of a cache reading worth an event of their own."""
    if not isinstance(state, dict):
        return {}
    return {
        key: state.get(key)
        for key in ("available", "local_buckets", "total_buckets", "local_pct", "fill_pct", "local_bytes", "eviction_policy")
        if key in state
    }


def thin(values: Sequence[Any], points: int) -> List[Any]:
    if points <= 0 or len(values) <= points:
        return list(values)
    step = len(values) / float(points)
    return [values[int(index * step)] for index in range(points)]
