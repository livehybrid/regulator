"""The worker's wire protocol: claim, ready, heartbeat, final.

Every request carries ``Authorization: Bearer <per-run token>``, minted for
exactly one run when it was launched. The token proves which run a worker
belongs to; the lease id it receives on claim proves which slot, and every
later call is fenced by it, so a container that restarts and claims again
cannot speak for the slot its previous incarnation held.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import fleet
from ..config import get_settings
from ..crypto import verify_run_token
from ..db import get_session
from ..models import Run
from ..runner import manager

log = logging.getLogger("regulator.server.agent")

router = APIRouter(prefix="/api/agent", tags=["agent"])


class ClaimRequest(BaseModel):
    holder: str = Field(min_length=1, max_length=128)
    hint_slot: Optional[int] = Field(default=None, ge=0)
    protocol_version: int = 1


class ReadyRequest(BaseModel):
    slot: int = Field(ge=0)
    lease_id: str = Field(min_length=1, max_length=64)


class HeartbeatRequest(BaseModel):
    slot: int = Field(ge=0)
    lease_id: str = Field(min_length=1, max_length=64)
    protocol_version: int = 1
    state: str = "ready"
    stats: Optional[Dict[str, Any]] = None


class FinalRequest(BaseModel):
    slot: int = Field(ge=0)
    lease_id: str = Field(min_length=1, max_length=64)
    summary: Dict[str, Any]
    log_tail: List[str] = Field(default_factory=list)


def require_run_token(run_id: int, authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing run token")
    token = authorization[7:].strip()
    max_age = int(get_settings().max_run_duration_s) + 2 * 3600
    if not verify_run_token(token, run_id, max_age):
        log.info("run %s: a worker presented a token that is not this run's", run_id)
        raise HTTPException(status_code=401, detail="invalid run token")


def _run(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return run


@router.post("/runs/{run_id}/claim", dependencies=[Depends(require_run_token)])
def claim(run_id: int, body: ClaimRequest, session: Session = Depends(get_session)) -> Dict[str, Any]:
    run = _run(session, run_id)
    supervisor = manager.fleet_run(run_id)
    if supervisor is None:
        raise HTTPException(status_code=409, detail="this run has no fleet supervisor in this process")
    if body.protocol_version != fleet.PROTOCOL_VERSION:
        raise HTTPException(status_code=409, detail=f"protocol version {body.protocol_version} is not supported")
    return fleet.claim(session, run, body.holder, body.hint_slot, supervisor.files_by_engine)


@router.post("/runs/{run_id}/ready", dependencies=[Depends(require_run_token)])
def ready(run_id: int, body: ReadyRequest, session: Session = Depends(get_session)) -> Dict[str, Any]:
    run = _run(session, run_id)
    fleet.mark_ready(session, run, body.slot, body.lease_id)
    return {}


@router.post("/runs/{run_id}/heartbeat", dependencies=[Depends(require_run_token)])
def heartbeat(run_id: int, body: HeartbeatRequest, session: Session = Depends(get_session)) -> Dict[str, Any]:
    run = _run(session, run_id)
    return fleet.heartbeat(session, run, body.slot, body.lease_id, body.model_dump())


@router.post("/runs/{run_id}/final", dependencies=[Depends(require_run_token)])
def final(run_id: int, body: FinalRequest, session: Session = Depends(get_session)) -> Dict[str, Any]:
    run = _run(session, run_id)
    fleet.final(session, run, body.slot, body.lease_id, body.summary, body.log_tail)
    return {}
