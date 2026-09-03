"""Targets, and the actions you can take against one.

The actions are the buttons: test, report, cache, evict. Each one is a thin
wrapper around the worker's own code, so what the UI shows is exactly what the
command line would produce.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from regulator_agent import savedsearches as ss
from regulator_agent.engines.api import ApiEngine
from regulator_agent.report import target_report
from regulator_agent.smartstore import cache_state, evict_all
from regulator_agent.splunk import SplunkClient

from ..adapters import indexer_settings, list_scenarios, scenario_summary, target_config, worker_config
from ..audit import record as audit_record
from ..crypto import DecryptionError, encrypt
from ..db import get_session
from ..models import Run, Target
from ..schemas import (
    EvictRequest,
    SavedSearchPreview,
    TargetCreate,
    TargetOut,
    TargetTestResult,
)

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
    try:
        config = target_config(target)
    except DecryptionError as exc:
        # The master key changed since this credential was stored. A 409
        # with the explanation beats a 500 with none.
        raise HTTPException(status_code=409, detail=str(exc))
    client = SplunkClient(config)
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


def _cache_kwargs(target: Target) -> Dict[str, Any]:
    """How to reach the indexers, for the SmartStore code."""
    try:
        settings = indexer_settings(target)
    except DecryptionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "indexer_urls": settings["indexer_urls"],
        "indexer_token": settings["indexer_token"],
        "indexer_username": settings["indexer_username"],
        "indexer_password": settings["indexer_password"],
    }


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
        indexer_urls=",".join(body.indexer_urls) or None,
        indexer_token_encrypted=encrypt(body.indexer_token),
        indexer_username=body.indexer_username,
        indexer_password_encrypted=encrypt(body.indexer_password),
    )
    session.add(target)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"a target named {body.name!r} already exists")
    return target


@router.delete("/{target_id}", status_code=204)
def delete_target(
    target_id: int, request: Request, session: Session = Depends(get_session)
) -> None:
    target = _get_target(session, target_id)
    # Runs are measurements and outlive the target they were taken against.
    # Done explicitly rather than trusting the foreign key, which SQLite
    # only honours with a pragma and which used to be NOT NULL anyway.
    for run in session.scalars(select(Run).where(Run.target_id == target_id)):
        run.target_id = None
        session.add(run)
    audit_record("target_deleted", request=request, target_id=target_id, detail=target.name)
    session.delete(target)


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
    library = [scenario_summary(sc, origin) for sc, origin in list_scenarios()]
    extra = _cache_kwargs(target)
    report = await _with_client(
        target,
        lambda client: target_report(client, scenarios=library, **extra),
        REPORT_TIMEOUT_S,
    )
    target.last_report_json = report
    target.health = "ok" if report.get("reachable") else "error"
    session.add(target)
    return report


@router.get("/{target_id}/cache")
async def target_cache(target_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    target = _get_target(session, target_id)
    extra = _cache_kwargs(target)
    state = await _with_client(target, lambda client: cache_state(client, **extra), ACTION_TIMEOUT_S)
    return state.to_dict()


@router.post("/{target_id}/evict")
async def evict_target_cache(
    target_id: int,
    body: EvictRequest,
    request: Request,
    session: Session = Depends(get_session),
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
    extra = _cache_kwargs(target)

    async def do_evict(client: SplunkClient):
        before = await cache_state(client, **extra)
        if not before.available:
            raise HTTPException(
                status_code=409,
                detail=f"there is no SmartStore cache to evict: {before.reason}",
            )
        result = await evict_all(client, indexes=indexes, **extra)
        after = await cache_state(client, **extra)
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
    audit_record(
        "cache_evicted",
        request=request,
        target_id=target.id,
        detail=f"{target.name}: {', '.join(indexes) if indexes else 'every index'}",
    )
    return await _with_client(target, do_evict, EVICT_TIMEOUT_S)


# --------------------------------------------------------------- saved searches


def _preview(search: ss.SavedSearch, skipped: Dict[str, str]) -> SavedSearchPreview:
    firings = None
    if search.cron:
        try:
            firings = round(ss.cron_firings_per_day(search.cron), 3)
        except ss.CronError:
            firings = None
    return SavedSearchPreview(
        name=search.name,
        app=search.app,
        search=search.search,
        cron=search.cron,
        scheduled=search.scheduled,
        disabled=search.disabled,
        earliest=search.earliest,
        latest=search.latest,
        guessed_class=search.annotated_class or ss.classify(search.search, search.earliest),
        side_effects=ss.side_effects(search.search),
        firings_per_day=firings,
        skipped_reason=skipped.get(search.name),
    )


@router.get("/{target_id}/savedsearches", response_model=List[SavedSearchPreview])
async def list_target_saved_searches(
    target_id: int,
    session: Session = Depends(get_session),
    app: Optional[str] = None,
) -> List[SavedSearchPreview]:
    """The saved searches on the target, as a scenario would see them.

    Every search comes back, with the reason it would be left out of a
    scenario under the default rules (disabled, real-time, writes somewhere),
    so the operator chooses with the consequences in view.
    """
    target = _get_target(session, target_id)
    entries = await _with_client(
        target, lambda client: client.saved_searches(app=app), ACTION_TIMEOUT_S
    )
    searches = ss.from_rest_entries(entries)
    selection = ss.select_searches(searches)
    return [_preview(search, selection.skipped) for search in searches]


@router.get("/{target_id}/savedsearches.conf", response_class=PlainTextResponse)
async def export_target_saved_searches(
    target_id: int,
    session: Session = Depends(get_session),
    app: Optional[str] = None,
) -> str:
    """The same searches as a savedsearches.conf a Splunk admin would recognise."""
    target = _get_target(session, target_id)
    entries = await _with_client(
        target, lambda client: client.saved_searches(app=app), ACTION_TIMEOUT_S
    )
    searches = ss.from_rest_entries(entries)
    header = (
        f"Exported by Regulator from {target.mgmt_url}"
        + (f", app {app}" if app else "")
        + "\nAlert actions and display settings are omitted on purpose: a load test replays "
        "the search, never its consequences."
    )
    return ss.render_savedsearches(searches, header=header)
