"""Targets, and the actions you can take against one.

The actions are the buttons: test, report, cache, evict. Each one is a thin
wrapper around the worker's own code, so what the UI shows is exactly what the
command line would produce.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from regulator_agent.engines.api import ApiEngine
from regulator_agent.report import target_report
from regulator_agent.smartstore import cache_state, evict_all
from regulator_agent.splunk import SplunkClient

from ..adapters import target_config, worker_config
from ..crypto import encrypt
from ..db import get_session
from ..models import Target
from ..schemas import EvictRequest, TargetCreate, TargetOut, TargetTestResult

log = logging.getLogger("regulator.server.targets")

router = APIRouter(prefix="/api/targets", tags=["targets"])

# A report walks every bucket in the cache manager and runs a tstats census, so
# it is slow by nature on a large estate. Bounded so a hung target cannot pin a
# worker thread forever.
REPORT_TIMEOUT_S = 120.0
ACTION_TIMEOUT_S = 60.0
EVICT_TIMEOUT_S = 600.0


def _get_target(session: Session, target_id: int) -> Target:
    target = session.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"no target with id {target_id}")
    return target


async def _with_client(target: Target, coro_factory, timeout_s: float):
    """Open a client, do one thing, close it. Never leak a connection pool."""
    client = SplunkClient(target_config(target))
    await client.start()
    try:
        return await asyncio.wait_for(coro_factory(client), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"the target did not answer within {timeout_s:.0f}s",
        )
    finally:
        await client.close()


@router.get("", response_model=List[TargetOut])
def list_targets(session: Session = Depends(get_session)) -> List[Target]:
    return list(session.scalars(select(Target).order_by(Target.id)))


@router.post("", response_model=TargetOut, status_code=201)
def create_target(body: TargetCreate, session: Session = Depends(get_session)) -> Target:
    try:
        body.check_credentials()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    target = Target(
        name=body.name,
        mgmt_url=body.mgmt_url,
        web_url=body.web_url,
        token_encrypted=encrypt(body.token),
        username=body.username,
        password_encrypted=encrypt(body.password),
        verify_tls=body.verify_tls,
        app=body.app,
        owner=body.owner,
        api_version=body.api_version,
    )
    session.add(target)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"a target named {body.name!r} already exists")
    return target


@router.delete("/{target_id}", status_code=204)
def delete_target(target_id: int, session: Session = Depends(get_session)) -> None:
    session.delete(_get_target(session, target_id))


@router.post("/{target_id}/test", response_model=TargetTestResult)
async def test_target(target_id: int, session: Session = Depends(get_session)) -> TargetTestResult:
    target = _get_target(session, target_id)

    async def probe(client: SplunkClient):
        engine = ApiEngine(
            worker_config(target, scenario_path="smoke"), client=client
        )
        return await engine.probe()

    try:
        caps = await _with_client(target, probe, ACTION_TIMEOUT_S)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed probe is a result, not a crash
        target.health = "error"
        target.health_detail = str(exc)[:500]
        session.add(target)
        return TargetTestResult(ok=False, detail=str(exc))

    target.health = "ok"
    target.health_detail = None
    session.add(target)
    return TargetTestResult(
        ok=True,
        detail=f"Splunk {caps.version} ({', '.join(caps.server_roles) or 'no roles reported'})",
        version=caps.version,
        roles=list(caps.server_roles),
        cores=caps.cpu_count,
        max_hist_searches=caps.max_hist_searches,
    )


@router.post("/{target_id}/report")
async def report_target(target_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """The full picture: what it is, what it can run at once, what is in it."""
    target = _get_target(session, target_id)
    report = await _with_client(target, target_report, REPORT_TIMEOUT_S)
    target.last_report_json = report
    target.health = "ok" if report.get("reachable") else "error"
    session.add(target)
    return report


@router.get("/{target_id}/cache")
async def target_cache(target_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    target = _get_target(session, target_id)
    state = await _with_client(target, cache_state, ACTION_TIMEOUT_S)
    return state.to_dict()


@router.post("/{target_id}/evict")
async def evict_target_cache(
    target_id: int, body: EvictRequest, session: Session = Depends(get_session)
) -> Dict[str, Any]:
    """Drop cached buckets so the next searches read cold.

    Requires either named indexes or an explicit all-indexes flag. There is no
    undo beyond waiting for everything to re-download, and on a shared cluster
    most of that cache belongs to other people's dashboards.
    """
    try:
        body.check()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    target = _get_target(session, target_id)
    indexes = body.indexes or None

    async def do_evict(client: SplunkClient):
        before = await cache_state(client)
        if not before.available:
            raise HTTPException(
                status_code=409,
                detail=f"there is no SmartStore cache to evict: {before.reason}",
            )
        result = await evict_all(client, indexes=indexes)
        after = await cache_state(client)
        return {
            "indexes": indexes or "all",
            "eviction": result.to_dict(),
            "before": before.to_dict(),
            "after": after.to_dict(),
        }

    log.warning(
        "evicting the SmartStore cache on target %s (%s) for %s",
        target.id,
        target.name,
        ", ".join(indexes) if indexes else "every index",
    )
    return await _with_client(target, do_evict, EVICT_TIMEOUT_S)
