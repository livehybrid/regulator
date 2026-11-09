"""Targets, and the actions you can take against one.

The actions are the buttons: test, report, cache, evict. Each one is a thin
wrapper around the worker's own code, so what the UI shows is exactly what the
command line would produce.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from regulator_agent import bucketmap, cachewarm
from regulator_agent import savedsearches as ss
from regulator_agent.ceilings import search_ceilings
from regulator_agent.engines.api import ApiEngine
from regulator_agent.report import target_report
from regulator_agent.smartstore import cache_state, evict_all
from regulator_agent.splunk import SplunkClient

from ..adapters import (
    indexer_settings,
    list_scenarios,
    load_named_scenario,
    scenario_summary,
    target_config,
    worker_config,
)
from ..audit import record as audit_record
from ..crypto import DecryptionError, encrypt
from ..db import get_session
from ..models import Run, Target
from ..runner import manager
from ..schemas import (
    TARGET_SECRET_COLUMNS,
    EvictRequest,
    SavedSearchPreview,
    TargetCreate,
    TargetOut,
    TargetTestResult,
    TargetUpdate,
    WarmRequest,
    WarmSuggestRequest,
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
    if target is not None:
        return target
    # A bare "no target with id 7" is not enough to act on, and this endpoint
    # has produced a 404 for an id the caller had just been handed a 201 for.
    # Whether that id is below or above the highest one this database has ever
    # issued separates the two explanations, and neither is guessable after the
    # fact: below means the row existed and is gone (deleted, or a different
    # database is being read than was written), above means it was never here
    # (the caller is talking to a different control plane than it created on).
    # Only what can actually be known. The highest id is taken from the rows
    # that survive, not from a sequence: SQLite reuses ids after a delete
    # unless the column is AUTOINCREMENT, so "this id was never issued" is not
    # something this can honestly claim.
    highest = session.scalar(select(func.max(Target.id)))
    if highest is None:
        hint = (
            "this control plane has no targets at all, so either every one was "
            "deleted or this is not the database the target was created in"
        )
    elif target_id <= highest:
        hint = (
            f"targets up to id {highest} exist here, so this id is within range and "
            "the target was deleted, or this is not the database it was created in"
        )
    else:
        hint = (
            f"the highest id here is {highest}, so this id is beyond anything this "
            "control plane has: check it is the one the target was created on"
        )
    raise HTTPException(status_code=404, detail=f"no target with id {target_id}; {hint}")


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
        # Committed here rather than left to the request teardown, and this is
        # the fix for the intermittent "created a target, then got a 404 for
        # its own id". A 201 hands the caller an id and the caller uses it at
        # once; leaving the commit to teardown meant the response went out
        # first and the commit raced whatever the caller did next.
        #
        # Measured against a real uvicorn with 24 concurrent callers: 15 of 120
        # create-then-read pairs got 201 then 404 without this commit, and 0 of
        # 120 with it. The rows were never lost, they landed a moment later,
        # which is why it presented as an intermittent 404 nobody could
        # reproduce by hand.
        #
        # It does NOT reproduce through FastAPI's TestClient, which is single
        # threaded and always finishes one request's teardown before starting
        # the next. test_target_create_is_committed.py therefore runs a real
        # server on a real socket. Runs already committed here for the same
        # reason; targets did not, and that was the difference.
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"a target named {body.name!r} already exists")
    return target


# Changing any of these changes what a Test proved, so the health goes back to
# unknown until the next one.
_CONNECTION_FIELDS = {"mgmt_url", "token", "username", "password", "verify_tls", "api_version"}


@router.patch("/{target_id}", response_model=TargetOut)
def update_target(
    target_id: int, body: TargetUpdate, request: Request, session: Session = Depends(get_session)
) -> Target:
    """Change a target in place: the id, its runs and its baselines stay.

    The common case is an expired token on a target restored from yesterday's
    config: send the new token and nothing else. Secrets are write-only; one
    left out or sent empty is kept, and one named in ``clear`` is removed.
    """
    target = _get_target(session, target_id)
    sent = body.model_dump(exclude_unset=True)
    clear = set(sent.pop("clear", []) or [])
    changed = []

    for field_name in ("name", "mgmt_url", "web_url", "verify_tls", "app", "owner", "api_version"):
        if field_name not in sent:
            continue
        value = sent[field_name]
        if field_name == "web_url":
            value = value or None  # the one optional field: empty clears it
        elif value is None:
            continue
        if getattr(target, field_name) != value:
            setattr(target, field_name, value)
            changed.append(field_name)

    if "indexer_urls" in sent and sent["indexer_urls"] is not None:
        value = ",".join(sent["indexer_urls"]) or None
        if target.indexer_urls != value:
            target.indexer_urls = value
            changed.append("indexer_urls")

    for field_name in ("username", "indexer_username"):
        value = (sent.get(field_name) or "").strip()
        if field_name in clear:
            if getattr(target, field_name):
                setattr(target, field_name, None)
                changed.append(field_name)
        elif value and getattr(target, field_name) != value:
            setattr(target, field_name, value)
            changed.append(field_name)

    for field_name, column in TARGET_SECRET_COLUMNS.items():
        value = sent.get(field_name) or ""
        if field_name in clear:
            if getattr(target, column):
                setattr(target, column, None)
                changed.append(field_name)
        elif value:
            setattr(target, column, encrypt(value))
            changed.append(field_name)

    if not (target.token_encrypted or (target.username and target.password_encrypted)):
        session.rollback()
        raise HTTPException(
            status_code=422,
            detail="this would leave the target with no credential: keep or supply a token, "
            "or both a username and a password",
        )
    if _CONNECTION_FIELDS.intersection(changed):
        target.health = "unknown"
        target.health_detail = None
    session.add(target)
    try:
        session.flush()
        # Committed here for the same reason as create: the caller acts on the
        # response at once (Test, launch), and teardown would race it.
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"a target named {body.name!r} already exists")
    if changed:
        # Field names only: a credential's value never reaches the audit log.
        audit_record("target_updated", request=request, target_id=target.id,
                     detail=f"{target.name}: {', '.join(sorted(set(changed)))}")
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
        found = await engine.probe()
        try:
            limits = await search_ceilings(client, target.mgmt_url, found)
        except Exception:  # noqa: BLE001 - the breakdown is a nicety, the Test is not
            log.warning("search ceiling breakdown failed for target %s", target.id, exc_info=True)
            limits = None
        return found, limits

    try:
        caps, ceilings = await _with_client(target, probe, ACTION_TIMEOUT_S)
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
        ceilings=ceilings,
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
    from ..samples import store_cache_sample

    target = _get_target(session, target_id)
    extra = _cache_kwargs(target)
    state = await _with_client(target, lambda client: cache_state(client, **extra), ACTION_TIMEOUT_S)
    document = state.to_dict()
    store_cache_sample(session, target.id, None, "read", document)
    return document


@router.get("/{target_id}/samples")
def target_samples(target_id: int, since: Optional[float] = None, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """The cache's history on this target: every reading, with what took it."""
    from ..samples import cache_samples_for

    target = _get_target(session, target_id)
    return {"target_id": target.id, "samples": cache_samples_for(session, target.id, since=since)}


from ..cachelock import evict_lock  # noqa: E402 - one eviction at a time per target


def _runs_in_flight(session: Session, target_id: int) -> List[int]:
    return list(session.scalars(select(Run.id).where(Run.target_id == target_id, Run.state.in_(("pending", "running")))))


@router.post("/{target_id}/evict")
async def evict_target_cache(
    target_id: int,
    body: EvictRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Drop cached buckets so the next searches read cold.

    Requires either named indexes or an explicit all-indexes flag (the purge
    button sends the flag). There is no undo beyond waiting for everything to
    re-download, and on a shared cluster most of that cache belongs to other
    people's dashboards. A run in flight on the target is told, so its next
    epoch starts here rather than the eviction reading as churn.
    """
    import time as _time

    from ..samples import cache_facts, store_cache_sample
    from ..telemetry import telemetry

    try:
        body.check()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    target = _get_target(session, target_id)
    indexes = body.indexes or None
    extra = _cache_kwargs(target)
    lock = evict_lock(target.id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="an eviction is already running on this target; wait for it")
    in_flight = _runs_in_flight(session, target.id)
    scope_text = ", ".join(indexes) if indexes else "every index"

    async def do_evict(client: SplunkClient):
        before = await cache_state(client, **extra)
        if not before.available:
            raise HTTPException(
                status_code=409,
                detail=f"there is no SmartStore cache to evict: {before.reason}",
            )
        started = _time.time()
        result = await evict_all(client, indexes=indexes, **extra)
        after = await cache_state(client, **extra)
        return {
            "indexes": indexes or "all",
            "eviction": result.to_dict(),
            "before": before.to_dict(),
            "after": after.to_dict(),
            "duration_s": round(_time.time() - started, 1),
            "in_flight_runs": in_flight,
        }

    log.warning("evicting the SmartStore cache on target %s (%s) for %s", target.id, target.name, scope_text)
    async with lock:
        try:
            outcome = await _with_client(target, do_evict, EVICT_TIMEOUT_S)
        except HTTPException as exc:
            audit_record("cache_evict_failed", request=request, target_id=target.id, detail=f"{target.name}: {scope_text}: {exc.detail}")
            raise
    eviction = outcome["eviction"]
    # The audit row is written after the result, with the counts: an eviction
    # that evicted nothing must not read as one that did.
    audit_record(
        "cache_evicted",
        request=request,
        target_id=target.id,
        detail=(
            f"{target.name}: {scope_text}: {eviction.get('confirmed', 0)} of {eviction.get('attempted', 0)} "
            f"bucket(s) confirmed evicted, {eviction.get('bytes_evicted', 0)} bytes, {outcome['duration_s']}s"
            + (f", during run(s) {', '.join(str(r) for r in in_flight)}" if in_flight else "")
        ),
    )
    store_cache_sample(session, target.id, in_flight[0] if in_flight else None, "evict", outcome["after"], {"eviction": eviction, "before": cache_facts(outcome["before"])})
    telemetry.lifecycle(
        "cache_evicted", None, target.name, target=target.name, scope=indexes or "all", in_flight_runs=in_flight,
        **{k: v for k, v in eviction.items() if k != "errors"}, duration_s=outcome["duration_s"],
    )
    # A run in flight starts a new cache epoch here.
    for run_id in in_flight:
        manager.mark_cache_epoch(run_id, eviction)
    return outcome


# --------------------------------------------------------------- bucket map

# dbinspect examines every bucket of every index it is given, so the on-disk
# detail is only read for a handful of named indexes at a time.
MAX_DETAIL_INDEXES = 10


@router.get("/{target_id}/buckets")
async def target_bucket_map(
    target_id: int,
    indexes: Optional[str] = Query(default=None, description="comma-separated; default the largest"),
    earliest: Optional[str] = Query(default=None, description="epoch, ISO 8601 or a modifier such as -30d"),
    latest: Optional[str] = Query(default=None),
    bins: int = Query(default=120, ge=bucketmap.MIN_BINS, le=bucketmap.MAX_BINS),
    detail: bool = Query(default=False, description="also read dbinspect for full or partial"),
    scenario: Optional[str] = Query(default=None, description="overlay the ranges its searches read"),
    duration_s: Optional[float] = Query(default=None, gt=0, le=7 * 86400),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Buckets by index and time, by how much of each is on local disk.

    Read on demand only. Listing every bucket is load on the cluster under
    test, and the on-disk detail more so, which is why ``detail`` needs named
    indexes.
    """
    import time as _time

    target = _get_target(session, target_id)
    names = [n.strip() for n in (indexes or "").split(",") if n.strip()]
    if detail and not names:
        raise HTTPException(status_code=422, detail="name the indexes to read on-disk detail for")
    if detail and len(names) > MAX_DETAIL_INDEXES:
        raise HTTPException(
            status_code=422,
            detail=f"on-disk detail reads dbinspect per index; name at most {MAX_DETAIL_INDEXES}",
        )
    now = _time.time()
    try:
        lo = cachewarm.parse_time(earliest, now) if earliest else None
        hi = cachewarm.parse_time(latest, now) if latest else None
    except cachewarm.PlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if lo is not None and hi is not None and hi <= lo:
        raise HTTPException(status_code=422, detail="latest is not after earliest")
    chosen = None
    if scenario:
        try:
            chosen = load_named_scenario(scenario)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
    extra = _cache_kwargs(target)

    async def read(client: SplunkClient) -> Dict[str, Any]:
        state = await cache_state(client, **extra)
        residency = None
        if detail and state.available:
            copies = bucketmap.group_by_id(state.buckets, names)
            firsts = {bid: held[0] for bid, held in copies.items()}
            dated = [b for b in firsts.values() if b.earliest is not None and b.latest is not None]
            span = (
                lo if lo is not None else min((b.earliest for b in dated), default=0.0),
                hi if hi is not None else max((b.latest for b in dated), default=now),
            )
            residency = await cachewarm.inspect_buckets(
                client, firsts, {name: [span] for name in sorted({b.index for b in firsts.values()})}
            )
        document = bucketmap.build(
            state, indexes=names or None, earliest=lo, latest=hi, bins=bins, residency=residency, now=now
        )
        if chosen is not None and document["available"]:
            duration = float(duration_s or _scenario_duration(chosen))
            fc = cachewarm.forecast(chosen, state, now, duration)
            document["overlay"] = {
                "scenario": chosen.name,
                "duration_s": duration,
                "spans": bucketmap.overlay(
                    fc.steps, [row["index"] for row in document["indexes"]],
                    document["earliest"], document["latest"],
                ),
                "notes": [n for n in fc.notes if n.startswith("step ")],
            }
        return document

    return await _with_client(target, read, ACTION_TIMEOUT_S * 2)


# --------------------------------------------------------------- warm-up
#
# The tool suggests, the operator decides. /warm/suggest reads a scenario's
# searches and proposes indexes and time ranges; the operator edits them; /warm
# warms exactly the edited plan, and only with confirmed set. The audit trail
# records the plan as confirmed, by whom, and what it did.

# The last warm-up per target, for GET /warm. In memory: a warm-up belongs to
# the process running it, and a restart ends it.
_warm_jobs: Dict[int, Dict[str, Any]] = {}


def _scenario_duration(scenario: Any) -> float:
    from regulator_agent.scheduler import Ramp

    if scenario.load.duration_s:
        return float(scenario.load.duration_s)
    base = float(scenario.load.virtual_users) if scenario.load.model == "closed" else float(scenario.load.arrival_rate_per_min)
    return Ramp(scenario.load.ramp, base).duration_s or 3600.0


@router.post("/{target_id}/warm/suggest")
async def suggest_warm_plan(
    target_id: int, body: WarmSuggestRequest, session: Session = Depends(get_session)
) -> Dict[str, Any]:
    """A suggested warm-up plan from what a scenario's run would read.

    A suggestion only. It is read from the search strings, which can hide what
    they read (a macro, an eventtype, a token), so the operator is expected to
    check and edit it before anything is warmed.
    """
    target = _get_target(session, target_id)
    try:
        scenario = load_named_scenario(body.scenario)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    duration = float(body.duration_s or _scenario_duration(scenario))
    extra = _cache_kwargs(target)

    async def suggest(client: SplunkClient) -> Dict[str, Any]:
        import time as _time

        state = await cache_state(client, **extra)
        fc = await cachewarm.forecast_on_disk(client, scenario, state, _time.time(), duration)
        plan = cachewarm.suggest_plan(fc, body.mode) if fc.available else []
        return {
            "scenario": scenario.name,
            "duration_s": duration,
            "mode": body.mode,
            "plan": [t.to_dict() for t in plan],
            "forecast": fc.to_dict(),
        }

    return await _with_client(target, suggest, ACTION_TIMEOUT_S * 2)


@router.get("/{target_id}/warm")
def warm_status(target_id: int, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """The last warm-up on this target: running, done or failed, and its result."""
    target = _get_target(session, target_id)
    return _warm_jobs.get(target.id) or {"state": "idle"}


@router.post("/{target_id}/warm", status_code=202)
async def warm_target_cache(
    target_id: int,
    body: WarmRequest,
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Warm exactly the confirmed plan, in the background. Poll GET /warm."""
    import time as _time

    if not body.confirmed:
        raise HTTPException(
            status_code=422,
            detail=(
                "confirm the plan first: check that these are the indexes and time ranges you "
                "want warmed. Warming is load on the target and the suggestion is only a "
                "reading of the scenario's search strings"
            ),
        )
    target = _get_target(session, target_id)
    try:
        plan = cachewarm.parse_plan([line.model_dump() for line in body.plan])
    except cachewarm.PlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    lock = evict_lock(target.id)
    current = _warm_jobs.get(target.id) or {}
    if lock.locked() or current.get("state") == "running":
        raise HTTPException(
            status_code=409,
            detail="a warm-up or an eviction is already running on this target; wait for it",
        )
    extra = _cache_kwargs(target)
    lines = [t.to_dict() for t in plan]
    plan_text = "; ".join(f"{l['index']} {l['earliest_iso']}..{l['latest_iso']}" for l in lines)
    job: Dict[str, Any] = {
        "state": "running",
        "plan": lines,
        "mode": body.mode,
        "scenario": body.scenario,
        "started_at": _time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    }
    _warm_jobs[target.id] = job
    audit_record(
        "cache_warm_started", request=request, target_id=target.id,
        detail=f"{target.name}: {body.mode} warm-up of a confirmed plan: {plan_text}"
        + (f" (suggested from scenario {body.scenario})" if body.scenario else ""),
    )
    target_name = target.name
    target_pk = target.id
    # The completion rows are written after the response, with no request:
    # carry who started it so the audit trail still says.
    from ..audit import actor_of

    who = {"actor": actor_of(request), "client": request.client.host if request.client else None}

    async def do_warm(client: SplunkClient) -> Dict[str, Any]:
        before = await cachewarm.plan_on_disk(client, plan, await cache_state(client, **extra))
        planned, skipped = cachewarm.plan_searches(plan, before, body.mode, body.chunk_s)
        result = await cachewarm.warm(
            client, planned, mode=body.mode, concurrency=body.concurrency,
            budget_s=body.budget_s, skipped_local=skipped,
        )
        result.plan = lines
        state_after = await cache_state(client, **extra)
        after = await cachewarm.plan_on_disk(client, plan, state_after)
        result.before, result.after = before.to_dict(), after.to_dict()
        return {"result": result.to_dict(), "cache_after": state_after.to_dict()}

    async def run_in_background() -> None:
        from ..db import session_scope
        from ..samples import store_cache_sample
        from ..telemetry import telemetry

        try:
            async with lock:
                outcome = await _with_client(target, do_warm, body.budget_s + EVICT_TIMEOUT_S)
        except HTTPException as exc:
            job.update(state="failed", error=str(exc.detail), finished_at=_time.time())
            audit_record("cache_warm_failed", target_id=target_pk, **who, detail=f"{target_name}: {exc.detail}")
            return
        except Exception as exc:  # noqa: BLE001 - a failed warm-up is reported, not raised
            log.exception("warm-up on target %s failed", target_pk)
            job.update(state="failed", error=f"{type(exc).__name__}: {exc}", finished_at=_time.time())
            audit_record("cache_warm_failed", target_id=target_pk, **who, detail=f"{target_name}: {exc}")
            return
        result = outcome["result"]
        job.update(state="done", result=result, finished_at=_time.time())
        before, after = result.get("before") or {}, result.get("after") or {}
        audit_record(
            "cache_warmed", target_id=target_pk, **who,
            detail=(
                f"{target_name}: {body.mode}: {result['searches']} search(es) in {result['seconds']}s, "
                f"{result['failed']} failed, {result['not_started']} not started; plan buckets local "
                f"{before.get('local_pct', 0)}% -> {after.get('local_pct', 0)}%"
            ),
        )
        with session_scope() as scoped:
            store_cache_sample(scoped, target_pk, None, "warm", outcome["cache_after"], {"warm": {k: v for k, v in result.items() if k not in ("before", "after")}})
        telemetry.lifecycle(
            "cache_warmed", None, target_name, target=target_name, mode=body.mode,
            searches=result["searches"], failed=result["failed"], seconds=result["seconds"],
        )

    background.add_task(run_in_background)
    return job


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


@router.get("/{target_id}/apps")
async def list_target_apps(target_id: int, session: Session = Depends(get_session)) -> List[Dict[str, Any]]:
    """The apps on the target, for choosing which app's saved searches to load.

    An account that cannot list apps gets an empty list rather than an error,
    and the console then lets the operator type an app name instead.
    """
    target = _get_target(session, target_id)
    entries = await _with_client(
        target, lambda client: client.entries("/services/apps/local", "apps"), ACTION_TIMEOUT_S
    )
    apps = []
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name or str(entry.get("disabled")).lower() in ("1", "true"):
            continue
        apps.append({"name": name, "label": str(entry.get("label") or name)})
    apps.sort(key=lambda a: a["label"].lower())
    return apps


def _only_named(searches: List[ss.SavedSearch], names: Optional[List[str]]) -> List[ss.SavedSearch]:
    if not names:
        return searches
    wanted = set(names)
    return [s for s in searches if s.name in wanted]


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
    name: Optional[List[str]] = Query(default=None),
) -> str:
    """The same searches as a savedsearches.conf a Splunk admin would recognise."""
    target = _get_target(session, target_id)
    entries = await _with_client(
        target, lambda client: client.saved_searches(app=app), ACTION_TIMEOUT_S
    )
    searches = _only_named(ss.from_rest_entries(entries), name)
    header = (
        f"Exported by Regulator from {target.mgmt_url}"
        + (f", app {app}" if app else "")
        + (f", {len(searches)} selected search(es)" if name else "")
        + "\nAlert actions and display settings are omitted on purpose: a load test replays "
        "the search, never its consequences."
    )
    return ss.render_savedsearches(searches, header=header)
