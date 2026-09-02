"""Launching runs and watching them."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import list_scenarios, load_named_scenario, scenario_summary
from ..db import get_session
from ..models import Run, Target, is_terminal
from ..runner import RunRejected, manager
from ..schemas import RunCreate, RunOut, ScenarioOut

log = logging.getLogger("regulator.server.runs")

router = APIRouter(prefix="/api", tags=["runs"])


def _serialise(run: Run, target_name: Optional[str] = None) -> Dict[str, Any]:
    """One run as the UI wants it.

    While a run is in flight the live aggregate comes from the manager rather
    than the database, because the publisher only writes every second and a
    hundred-millisecond-old number looks stuck to somebody watching.
    """
    live = manager.live_stats(run.id) if not is_terminal(run.state) else None
    return {
        "id": run.id,
        "label": run.label,
        "target_id": run.target_id,
        "target_name": target_name,
        "scenario": run.scenario,
        "state": run.state,
        "virtual_users": run.virtual_users,
        "duration_s": run.duration_s,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "error": run.error,
        "stats": live or run.stats_json,
        "summary": run.summary_json,
    }


@router.get("/scenarios", response_model=List[ScenarioOut])
def get_scenarios() -> List[Dict[str, Any]]:
    return [scenario_summary(scenario) for scenario in list_scenarios()]


@router.get("/runs")
def get_runs(session: Session = Depends(get_session), limit: int = 50) -> List[Dict[str, Any]]:
    rows = session.execute(
        select(Run, Target.name)
        .join(Target, Target.id == Run.target_id, isouter=True)
        .order_by(Run.id.desc())
        .limit(max(1, min(limit, 500)))
    ).all()
    return [_serialise(run, name) for run, name in rows]


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
def create_run(body: RunCreate, session: Session = Depends(get_session)) -> Dict[str, Any]:
    try:
        body.check()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    target = session.get(Target, body.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"no target with id {body.target_id}")

    try:
        load_named_scenario(body.scenario)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    run = Run(
        label=body.label,
        target_id=body.target_id,
        scenario=body.scenario,
        virtual_users=body.virtual_users,
        duration_s=body.duration_s,
        arrival_rate_per_min=body.arrival_rate_per_min,
        pacing_s=body.pacing_s,
        evict_cache=body.evict_cache,
        evict_cache_indexes=",".join(body.evict_cache_indexes) or None,
        state="pending",
    )
    session.add(run)
    session.flush()
    run_id = run.id
    session.commit()

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
def stop_run(run_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    if is_terminal(run.state):
        return _serialise(run)
    if not manager.request_stop(run_id):
        # Not in flight in this process. It cannot be resumed either, so mark
        # it stopped rather than leaving a zombie in the list forever.
        run.state = "stopped"
        run.error = run.error or "the run was not in flight when a stop was requested"
        session.add(run)
    return _serialise(run)


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(run_id: int, session: Session = Depends(get_session)) -> Response:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    if not is_terminal(run.state):
        raise HTTPException(status_code=409, detail="stop the run before deleting it")
    session.delete(run)
    return Response(status_code=204)
