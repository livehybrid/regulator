"""Launching runs and watching them."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi import Request

from ..adapters import load_named_scenario
from ..audit import record as audit_record
from ..config import get_settings
from ..db import get_session
from ..models import Run, Target, is_terminal
from ..runner import RunRejected, manager
from ..schemas import RunCreate, RunOut

log = logging.getLogger("regulator.server.runs")

router = APIRouter(prefix="/api", tags=["runs"])


def _headline(stats: Optional[Dict[str, Any]], summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The handful of figures the list view shows, not the whole document."""
    stats = stats or {}
    summary = summary or {}
    return {
        "executions": stats.get("executions"),
        "errors": stats.get("errors"),
        "error_rate_pct": stats.get("error_rate_pct"),
        "throughput_per_s": stats.get("throughput_per_s"),
        "p95_ms": (stats.get("latency") or {}).get("p95_ms"),
        "searches_queued": (stats.get("queueing") or {}).get("searches_queued"),
        "valid": summary.get("valid"),
        "invalid_reason": summary.get("invalid_reason"),
        "outcome": summary.get("outcome"),
        "cache_provenance": ((summary.get("cache") or {}).get("delta") or {}).get("provenance"),
        "load_model": summary.get("load_model"),
    }


def _serialise(run: Run, target_name: Optional[str] = None, full: bool = True) -> Dict[str, Any]:
    """One run as the UI wants it.

    While a run is in flight the live aggregate comes from the manager rather
    than the database, because the publisher only writes every second and a
    hundred-millisecond-old number looks stuck to somebody watching.
    """
    live = manager.live_stats(run.id) if not is_terminal(run.state) else None
    base = {
        "id": run.id,
        "label": run.label,
        "target_id": run.target_id,
        "target_name": target_name,
        "scenario": run.scenario,
        "state": run.state,
        "virtual_users": run.virtual_users,
        "duration_s": run.duration_s,
        "evict_cache": run.evict_cache,
        "evict_every_s": run.evict_every_s,
        "cold_window_s": run.cold_window_s,
        "seed": run.seed,
        "scenario_digest": run.scenario_digest,
        "fleet": run.fleet or "inprocess",
        "workers": run.workers or 1,
        "fleet_state": run.fleet_state,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "error": run.error,
    }
    if full:
        from ..samples import markers_for

        base["stats"] = live or run.stats_json
        base["summary"] = run.summary_json
        base["markers"] = markers_for(run)
    else:
        # The list used to ship every run's complete summary, per-step
        # aggregates and correlation rows included, to draw six columns.
        base["headline"] = _headline(live or run.stats_json, run.summary_json)
    return base


@router.get("/runs")
def get_runs(
    session: Session = Depends(get_session), limit: int = 50, offset: int = 0
) -> List[Dict[str, Any]]:
    rows = session.execute(
        select(Run, Target.name)
        .join(Target, Target.id == Run.target_id, isouter=True)
        .order_by(Run.id.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 500)))
    ).all()
    return [_serialise(run, name, full=False) for run, name in rows]


@router.get("/runs/sparklines")
def get_sparklines(ids: str = "", session: Session = Depends(get_session)) -> Dict[str, List[Optional[float]]]:
    """Interval p95 series, thinned, for the runs list. ``ids`` is comma separated."""
    from ..samples import sparkline_for

    out: Dict[str, List[Optional[float]]] = {}
    for raw in ids.split(","):
        raw = raw.strip()
        if raw.isdigit():
            out[raw] = sparkline_for(session, int(raw))
    return out


@router.get("/runs/{run_id}/samples")
def get_run_samples(
    run_id: int,
    since: Optional[float] = None,
    slots: bool = False,
    points: int = 600,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """The run's time series: aggregate rows, or one row per worker slot.

    ``since`` (a sample's ``at``) fetches only newer rows, so a live page
    appends rather than reloads. Rows are thinned to ``points`` server side.
    """
    from ..samples import markers_for, samples_for

    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    rows = samples_for(session, run_id, since=since, slots=slots, points=max(10, min(points, 5000)))
    markers = markers_for(run)
    for epoch in manager.epochs_for(run_id):
        markers.append({"at": epoch["requested_at"], "kind": "evict", "label": f"E{epoch['epoch']}", "duration_s": epoch.get("duration_s")})
    return {
        "run_id": run_id,
        "state": run.state,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "t0": run.t0,
        "samples": rows,
        "markers": markers,
    }


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    row = session.execute(
        select(Run, Target.name)
        .join(Target, Target.id == Run.target_id, isouter=True)
        .where(Run.id == run_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    run, name = row
    return _serialise(run, name)


@router.post("/runs", status_code=201)
def create_run(
    body: RunCreate, request: Request, session: Session = Depends(get_session)
) -> Dict[str, Any]:
    try:
        body.check()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    target = session.get(Target, body.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"no target with id {body.target_id}")

    try:
        scenario = load_named_scenario(body.scenario)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if body.evict_cache and not body.evict_cache_indexes and not body.evict_all_indexes:
        if not scenario.corpus.index:
            raise HTTPException(
                status_code=422,
                detail=(
                    "evict_cache needs at least one index, or evict_all_indexes: the "
                    "scenario declares no corpus index to default to"
                ),
            )

    run = Run(
        label=body.label,
        target_id=body.target_id,
        scenario=body.scenario,
        virtual_users=body.virtual_users,
        duration_s=body.duration_s,
        arrival_rate_per_min=body.arrival_rate_per_min,
        pacing_s=body.pacing_s,
        seed=body.seed,
        fleet=body.fleet or get_settings().fleet.default_fleet,
        workers=body.workers,
        evict_cache=body.evict_cache,
        evict_cache_indexes=(
            ",".join(body.evict_cache_indexes)
            if body.evict_cache_indexes
            else ("*" if body.evict_all_indexes else None)
        ),
        evict_every_s=body.evict_every_s,
        cold_window_s=(body.cold_window_s or (body.evict_every_s / 2.0 if body.evict_every_s else None)),
        state="pending",
    )
    session.add(run)
    session.flush()
    run_id = run.id
    session.commit()
    audit_record(
        "run_started",
        request=request,
        target_id=body.target_id,
        detail=f"run {run_id}: {body.scenario}" + (f" ({body.label})" if body.label else "")
        + (" with cache eviction" if body.evict_cache else ""),
    )

    from ..telemetry import telemetry

    telemetry.lifecycle(
        "run_created", run, target.name if target is not None else None,
        virtual_users=body.virtual_users, duration_s=body.duration_s, arrival_rate_per_min=body.arrival_rate_per_min,
        workers=body.workers, evict_cache=body.evict_cache, seed=body.seed,
    )
    try:
        manager.start(run_id)
    except RunRejected as exc:
        run.state = "failed"
        run.error = str(exc)
        session.add(run)
        session.commit()
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - a launch failure is recorded, not lost
        log.exception("could not start run %s", run_id)
        run.state = "failed"
        run.error = str(exc)
        session.add(run)
        session.commit()
        raise HTTPException(status_code=500, detail=str(exc))

    session.refresh(run)
    return _serialise(run, target.name)


@router.post("/runs/{run_id}/stop")
def stop_run(
    run_id: int, request: Request, session: Session = Depends(get_session)
) -> Dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    if is_terminal(run.state):
        return _serialise(run)
    audit_record("run_stopped", request=request, target_id=run.target_id, detail=f"run {run_id}")
    if not manager.request_stop(run_id):
        # Not in flight in this process. Re-read before deciding: the run
        # thread may have finished between our SELECT and its own commit, and
        # stamping "stopped" over a completed run gave a valid summary a false
        # error banner.
        session.expire(run)
        run = session.get(Run, run_id)
        if run is not None and not is_terminal(run.state):
            run.state = "stopped"
            run.error = run.error or "the run was not in flight when a stop was requested"
            run.ended_at = run.ended_at or time.time()
            session.add(run)
    return _serialise(run) if run is not None else {}


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(run_id: int, session: Session = Depends(get_session)) -> Response:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    if not is_terminal(run.state):
        raise HTTPException(status_code=409, detail="stop the run before deleting it")
    session.delete(run)
    return Response(status_code=204)
