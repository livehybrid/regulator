"""Warming a confirmed plan: one implementation, two callers.

The Warm cache button on a target and a run's own warm-up before it starts do
the same thing: take the plan exactly as the operator confirmed it, forecast
it, run the searches that fetch what is not yet local, and forecast it again.
Kept here so the two cannot drift, and so the routes and the runners can both
use it without importing each other.

Nothing in this module derives a plan. A plan arrives already confirmed by a
person; what is warmed is that plan and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from regulator_agent import cachewarm
from regulator_agent.smartstore import CacheState, cache_state
from regulator_agent.splunk import SplunkClient

log = logging.getLogger("regulator.server.warmup")

DEFAULT_CHUNK_S = 86400.0

# Runs whose warm-up is in flight, and when it began. In memory: a warm-up
# belongs to the process running it, and a restart ends it with the run.
_warming: Dict[int, float] = {}


def warming_since(run_id: int) -> Optional[float]:
    """When this run's warm-up began, while it is still going."""
    return _warming.get(int(run_id))


def plan_text(lines: Sequence[Dict[str, Any]]) -> str:
    """A resolved plan as one line, for the audit trail."""
    return "; ".join(f"{l['index']} {l['earliest_iso']}..{l['latest_iso']}" for l in lines)


async def warm_confirmed_plan(
    client: SplunkClient,
    plan: Sequence["cachewarm.WarmTarget"],
    *,
    mode: str,
    concurrency: int,
    budget_s: float,
    chunk_s: float = DEFAULT_CHUNK_S,
    where: Optional[Dict[str, Any]] = None,
    abort: Optional[asyncio.Event] = None,
) -> Tuple[Dict[str, Any], CacheState]:
    """Warm exactly ``plan``. Returns the result and the cache as it was left.

    The result carries the plan as resolved, and a forecast of it before and
    after, so "what did the warm-up change" is answered by the record itself.
    """
    where = where or {}
    before = await cachewarm.plan_on_disk(client, plan, await cache_state(client, **where))
    planned, skipped = cachewarm.plan_searches(plan, before, mode, chunk_s)
    result = await cachewarm.warm(
        client, planned, mode=mode, concurrency=concurrency,
        budget_s=budget_s, skipped_local=skipped, abort=abort,
    )
    result.plan = [t.to_dict() for t in plan]
    state_after = await cache_state(client, **where)
    after = await cachewarm.plan_on_disk(client, plan, state_after)
    result.before, result.after = before.to_dict(), after.to_dict()
    return result.to_dict(), state_after


def run_plan(spec: Optional[Dict[str, Any]]) -> List["cachewarm.WarmTarget"]:
    """The plan stored on a run, resolved now.

    A run keeps its lines as the operator wrote them, so ``-7d@d`` means seven
    days before the warm-up starts and not seven days before the launch
    button was pressed. Raises :class:`cachewarm.PlanError`.
    """
    return cachewarm.parse_plan((spec or {}).get("plan") or [])


async def warm_before_run(
    run_id: int,
    client: SplunkClient,
    spec: Dict[str, Any],
    *,
    where: Dict[str, Any],
    lock: asyncio.Lock,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Tuple[Dict[str, Any], Optional[CacheState]]:
    """A run's warm-up, once, before any load is generated. Never raises.

    Holds the target's cache lock, as the button does. ``should_stop`` is
    polled so a run stopped while it warms does not spend the rest of its
    budget fetching buckets for a test nobody is waiting for.

    A warm-up that fails outright is recorded on the run, under ``error``,
    and the run goes ahead: its forecast and its provenance then say what
    state it really started in, which is more use than a run that never
    happened. The cache reading is None in that case.
    """
    started = time.time()
    abort = asyncio.Event()

    async def watch() -> None:
        while not abort.is_set():
            if should_stop is not None and should_stop():
                abort.set()
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch())
    _warming[int(run_id)] = started
    try:
        plan = run_plan(spec)
        async with lock:
            return await warm_confirmed_plan(
                client, plan,
                mode=str(spec.get("mode") or "tsidx"),
                concurrency=int(spec.get("concurrency") or 4),
                budget_s=float(spec.get("budget_s") or 1800.0),
                where=where, abort=abort,
            )
    except Exception as exc:  # noqa: BLE001 - recorded on the run, never fatal to it
        log.warning("the warm-up before run %s failed: %s", run_id, exc)
        return (
            {
                "mode": str(spec.get("mode") or "tsidx"),
                "plan": list(spec.get("plan") or []),
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "seconds": round(time.time() - started, 2),
            },
            None,
        )
    finally:
        _warming.pop(int(run_id), None)
        abort.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher


def scenario_duration(scenario: Any) -> float:
    """How long a scenario runs when the launch names no duration.

    A ramp-only scenario declares none; its ramp says, and an hour stands in
    when even that does not.
    """
    from regulator_agent.scheduler import Ramp

    if scenario.load.duration_s:
        return float(scenario.load.duration_s)
    base = float(scenario.load.virtual_users) if scenario.load.model == "closed" else float(scenario.load.arrival_rate_per_min)
    return Ramp(scenario.load.ramp, base).duration_s or 3600.0


async def forecast_for_run(
    client: SplunkClient, scenario: Any, state: CacheState, duration_s: float
) -> Optional[Dict[str, Any]]:
    """What the run will read and how much of it is local, or None. Never raises.

    Taken from the cache reading the run calls "before", so after any warm-up
    or eviction: it describes the state the load really started in. It costs
    the target two dbinspect searches per index the run reads.
    """
    if not state.available or duration_s <= 0:
        return None
    try:
        fc = await cachewarm.forecast_on_disk(client, scenario, state, time.time(), duration_s)
    except Exception:  # noqa: BLE001 - a nicety, never fatal to a run
        log.debug("the cache forecast failed", exc_info=True)
        return None
    return fc.to_dict()


def record_run_warm(run_id: int, result: Dict[str, Any], state_after: Optional[CacheState]) -> None:
    """What a run's warm-up did, where the button's is recorded too.

    A point on the target's cache history, a lifecycle event, and an audit row
    naming the run: the row written at launch says who confirmed the plan.
    The run's clock is moved to now, because a run starts when its load does
    and not when its warm-up did. Best effort: none of this fails a run.
    """
    from .audit import record as audit_record
    from .db import session_scope
    from .models import Run
    from .samples import store_cache_sample
    from .telemetry import telemetry

    try:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            target_id = run.target_id
            target_name = run.target.name if run.target is not None else None
            if run.started_at is not None:
                run.started_at = time.time()
                session.add(run)
            if state_after is not None:
                store_cache_sample(
                    session, target_id, run_id, "warm", state_after.to_dict(),
                    {"warm": {k: v for k, v in result.items() if k not in ("before", "after")}},
                )
        if result.get("error"):
            telemetry.lifecycle(
                "cache_warm_failed", run, target_name, target=target_name,
                mode=result.get("mode"), error=result.get("error"),
            )
            audit_record(
                "cache_warm_failed", target_id=target_id,
                detail=f"run {run_id} on {target_name}: {result.get('error')}; the run went ahead",
            )
            return
        telemetry.lifecycle(
            "cache_warmed", run, target_name, target=target_name, mode=result.get("mode"),
            searches=result.get("searches"), failed=result.get("failed"), seconds=result.get("seconds"),
        )
        audit_record(
            "cache_warmed", target_id=target_id,
            detail=f"run {run_id} on {target_name}: {summary_line(result)}",
        )
    except Exception:  # noqa: BLE001 - the record of a warm-up never fails the run
        log.warning("could not record the warm-up of run %s", run_id, exc_info=True)


def summary_line(result: Dict[str, Any]) -> str:
    """What a warm-up did, in the words the audit trail already uses."""
    before, after = result.get("before") or {}, result.get("after") or {}
    return (
        f"{result.get('mode')}: {result.get('searches', 0)} search(es) in {result.get('seconds', 0)}s, "
        f"{result.get('failed', 0)} failed, {result.get('not_started', 0)} not started; plan buckets local "
        f"{before.get('local_pct', 0)}% -> {after.get('local_pct', 0)}%"
    )
