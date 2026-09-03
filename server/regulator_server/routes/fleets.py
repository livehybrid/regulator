"""Which fleets this control plane can launch, and a run's workers."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..drivers import DriverError, DriverRef, get_driver
from ..models import Run, WorkerLease

router = APIRouter(prefix="/api", tags=["fleets"])


@router.get("/fleets")
def list_fleets() -> List[Dict[str, Any]]:
    settings = get_settings()
    fleet = settings.fleet
    return [
        {
            "kind": "inprocess",
            "available": True,
            "default": fleet.default_fleet == "inprocess",
            "detail": f"this process, up to {settings.max_virtual_users} virtual users",
        },
        {
            "kind": "swarm",
            "available": fleet.swarm_available,
            "default": fleet.default_fleet == "swarm",
            "detail": (
                f"Docker Swarm through Portainer at {fleet.portainer_host}, {fleet.vus_per_worker} users per worker"
                if fleet.swarm_available
                else "set PORTAINER_HOST and PORTAINER_TOKEN"
            ),
        },
        {
            "kind": "k8s",
            "available": fleet.k8s_available,
            "default": fleet.default_fleet == "k8s",
            "detail": (
                f"Kubernetes namespace {fleet.k8s_namespace}, {fleet.vus_per_worker} users per worker"
                if fleet.k8s_available
                else "no kubeconfig and not running in a cluster"
            ),
        },
    ]


@router.get("/runs/{run_id}/workers")
def run_workers(run_id: int, session: Session = Depends(get_session)) -> List[Dict[str, Any]]:
    if session.get(Run, run_id) is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    leases = session.scalars(
        select(WorkerLease).where(WorkerLease.run_id == run_id).order_by(WorkerLease.slot)
    ).all()
    return [
        {
            "slot": lease.slot,
            "engine": lease.engine,
            "state": lease.state,
            "holder": lease.holder,
            "share": lease.share_json,
            "last_heartbeat_at": lease.last_heartbeat_at,
            "restarts": lease.restarts,
            "stats": lease.stats_json,
            "outcome": (lease.summary_json or {}).get("outcome"),
            "valid": (lease.summary_json or {}).get("valid"),
        }
        for lease in leases
    ]


@router.get("/runs/{run_id}/logs")
def run_logs(run_id: int, session: Session = Depends(get_session), tail: int = 200) -> Dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")
    chunks: List[Dict[str, Any]] = []
    for doc in run.driver_refs_json or []:
        ref = DriverRef.from_json(doc)
        if ref is None:
            continue
        try:
            text = get_driver(ref.kind).logs(ref, max(1, min(tail, 2000)))
        except DriverError as exc:
            text = f"(unavailable: {exc})"
        chunks.append({"group": ref.group, "kind": ref.kind, "id": ref.id, "text": text})
    leases = session.scalars(select(WorkerLease).where(WorkerLease.run_id == run_id)).all()
    for lease in leases:
        if lease.log_tail_json:
            chunks.append({"group": lease.engine, "kind": "final", "id": f"slot {lease.slot}", "text": "\n".join(lease.log_tail_json)})
    return {"run_id": run_id, "chunks": chunks}
