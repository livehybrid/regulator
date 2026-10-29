"""Launching runs and watching them."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi import Request

from ..adapters import load_named_scenario
from ..audit import record as audit_record
from ..config import get_settings
from ..db import get_session
from ..models import Run, Target, is_terminal
from .. import runcontrol
from ..fleet import paused_seconds
from .. import configsource, runarchive
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
        "p50_ms": (stats.get("latency") or {}).get("p50_ms"),
        "p99_ms": (stats.get("latency") or {}).get("p99_ms"),
        "elapsed_s": stats.get("elapsed_s"),
        "peak_virtual_users": summary.get("peak_virtual_users"),
        "valid": summary.get("valid"),
        "invalid_reason": summary.get("invalid_reason"),
        "outcome": summary.get("outcome"),
        "cache_provenance": ((summary.get("cache") or {}).get("delta") or {}).get("provenance"),
        "load_model": summary.get("load_model"),
    }


def _scenario_load(name: Optional[str], cache: Optional[Dict[str, Any]]) -> Any:
    """The scenario's own load model, or None if it is gone from the library."""
    if not name:
        return None
    if cache is not None and name in cache:
        return cache[name]
    try:
        load = load_named_scenario(name).load
    except Exception:  # noqa: BLE001 - a deleted or broken scenario just leaves gaps
        load = None
    if cache is not None:
        cache[name] = load
    return load


def _planned(run: Run, summary: Optional[Dict[str, Any]], cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the run was asked to do, with the scenario's defaults filled in.

    A run launched without overrides stores no virtual users or duration of
    its own, so the list could not say that one run had one user and the next
    two. Resolved in order: what the launch set; for an in-process run, what
    its summary recorded it actually configured; then the scenario as it is
    now (which may have been edited since a run that predates the summary).
    """
    configured = {}
    if (run.fleet or "inprocess") == "inprocess":
        configured = (summary or {}).get("configured_load") or {}
    load = None

    def scenario_value(attr: str) -> Any:
        nonlocal load
        if load is None:
            load = _scenario_load(run.scenario, cache) or False
        return getattr(load, attr, None) if load else None

    def pick(field_name: str, own: Any) -> Any:
        if own:
            return own
        if configured.get(field_name):
            return configured[field_name]
        return scenario_value(field_name) or None

    open_model = bool(run.arrival_rate_per_min) or (
        not run.virtual_users
        and ((summary or {}).get("load_model") or scenario_value("model")) == "open"
    )
    return {
        "load_model": "open" if open_model else "closed",
        "virtual_users": None if open_model else pick("virtual_users", run.virtual_users),
        "arrival_rate_per_min": pick("arrival_rate_per_min", run.arrival_rate_per_min) if open_model else None,
        "pacing_s": pick("pacing_s", run.pacing_s),
        "duration_s": pick("duration_s", run.duration_s),
        "workers": run.workers or 1,
    }


def _serialise(
    run: Run,
    target_name: Optional[str] = None,
    full: bool = True,
    scenario_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One run as the UI wants it.

    While a run is in flight the live aggregate comes from the manager rather
    than the database, because the publisher only writes every second and a
    hundred-millisecond-old number looks stuck to somebody watching.
    """
    live = manager.live_stats(run.id) if not is_terminal(run.state) else None
    base = {
        "id": run.id,
        "uid": run.uid,
        "label": run.label,
        "target_id": run.target_id,
        "target_name": target_name,
        "scenario": run.scenario,
        "state": run.state,
        "virtual_users": run.virtual_users,
        "duration_s": run.duration_s,
        "arrival_rate_per_min": run.arrival_rate_per_min,
        "pacing_s": run.pacing_s,
        "evict_cache": run.evict_cache,
        "evict_cache_indexes": run.evict_cache_indexes,
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
        "t0": run.t0,
        "error": run.error,
        "note": run.note,
        "paused": run.paused_at is not None and not is_terminal(run.state),
        "paused_s": round(paused_seconds(run), 1) if (run.paused_at or run.paused_s) else 0.0,
        "load_override": run.load_override,
        "planned": _planned(run, run.summary_json, scenario_cache),
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
    scenarios: Dict[str, Any] = {}
    return [_serialise(run, name, full=False, scenario_cache=scenarios) for run, name in rows]


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


@router.get("/runs/{run_id}/export")
def export_run(
    run_id: int,
    request: Request,
    points: int = 5000,
    session: Session = Depends(get_session),
) -> Response:
    """One run as a single self-contained JSON document, for download.

    The thing a consultant hands a customer at the end of an engagement. Every
    piece of this was already reachable over the API, but across four
    endpoints, which in practice meant the answer to "can you send me those
    numbers?" was a screenshot of the run page.

    What travels: the run row and its headline stats, the full summary with its
    per-step aggregates, the cluster's own account of what happened
    (``summary.sut``), the whole time series at its native resolution, the
    eviction markers, the per-worker leases, and - the part that makes it
    worth keeping - **the scenario that produced it, inline**. A latency number
    with no searches beside it is not a result anybody can check a year later,
    and a scenario can be edited after the run that used it.

    Deliberately NOT included: credentials of any kind. The target is named and
    its management URI recorded, because a result that does not say what it
    measured is not a result, but no token, password or CA material goes near
    this file.
    """
    row = session.execute(
        select(Run, Target)
        .join(Target, Target.id == Run.target_id, isouter=True)
        .where(Run.id == run_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    run, target = row
    document = runarchive.build_document(session, run, target, points=points)

    audit_record(
        "run_exported",
        request=request,
        target_id=run.target_id,
        detail=f"run {run_id}: {len(document['samples'])} sample(s)",
    )
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", f"regulator-run-{run_id}-{run.scenario}").strip("-")
    return Response(
        content=json.dumps(document, indent=2, default=str) + "\n",
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{label}.json"',
            "X-Regulator-Run": str(run_id),
            "X-Regulator-Run-Samples": str(len(document["samples"])),
        },
    )


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


class RunUpdate(BaseModel):
    """What can change once a run has launched. Only the fields sent change."""

    label: Optional[str] = Field(default=None, max_length=128)
    note: Optional[str] = Field(default=None, max_length=4000)
    # The run's total load, by its model: users for closed, arrivals for open.
    virtual_users: Optional[int] = Field(default=None, ge=0)
    arrival_rate_per_min: Optional[float] = Field(default=None, ge=0)


def _controlled(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    return run


@router.patch("/runs/{run_id}")
def update_run(
    run_id: int, body: RunUpdate, request: Request, session: Session = Depends(get_session)
) -> Dict[str, Any]:
    """Relabel or annotate any run; change the load of one that is running."""
    run = _controlled(session, run_id)
    sent = body.model_fields_set
    if "virtual_users" in sent and "arrival_rate_per_min" in sent:
        raise HTTPException(status_code=422, detail="send virtual_users or arrival_rate_per_min, not both")
    runcontrol.edit(run, {name: getattr(body, name) for name in ("label", "note") if name in sent})
    for figure in ("virtual_users", "arrival_rate_per_min"):
        value = getattr(body, figure)
        if figure in sent and value is not None:
            try:
                applied = runcontrol.set_load(session, run, float(value))
            except runcontrol.ControlRefused as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc))
            if applied != figure:
                raise HTTPException(
                    status_code=422,
                    detail=f"this run's load is set by {applied}, not {figure}",
                )
            audit_record("run_load_changed", request=request, target_id=run.target_id,
                         detail=f"run {run_id}: {figure} {value:g}")
    session.add(run)
    session.commit()
    return _serialise(run, run.target.name if run.target is not None else None)


@router.post("/runs/{run_id}/pause")
def pause_run(run_id: int, request: Request, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Stop issuing searches and stop the run clock; in-flight searches finish."""
    run = _controlled(session, run_id)
    try:
        runcontrol.pause(session, run)
    except runcontrol.ControlRefused as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    session.add(run)
    session.commit()
    audit_record("run_paused", request=request, target_id=run.target_id, detail=f"run {run_id}")
    return _serialise(run, run.target.name if run.target is not None else None)


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: int, request: Request, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Start issuing searches again, with every schedule re-anchored at now."""
    run = _controlled(session, run_id)
    try:
        runcontrol.resume(session, run)
    except runcontrol.ControlRefused as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    session.add(run)
    session.commit()
    audit_record("run_resumed", request=request, target_id=run.target_id, detail=f"run {run_id}")
    return _serialise(run, run.target.name if run.target is not None else None)


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(run_id: int, session: Session = Depends(get_session)) -> Response:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    if not is_terminal(run.state):
        raise HTTPException(status_code=409, detail="stop the run before deleting it")
    # Or the next pull from the config source brings it back.
    configsource.forget_run(run.uid)
    session.delete(run)
    return Response(status_code=204)
