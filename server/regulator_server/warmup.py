"""Warming a confirmed plan, shared by the Warm cache button and by runs.

One place for the sequence (forecast the plan, search what is not local,
forecast it again) so the button, an in-process run and a fleet run cannot
drift apart. The plan is always one an operator confirmed: this module never
derives what to warm.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from regulator_agent import cachewarm
from regulator_agent.smartstore import CacheState, cache_state
from regulator_agent.splunk import SplunkClient

from .cachelock import evict_lock

log = logging.getLogger("regulator.server.warmup")

DEFAULT_CONCURRENCY = 4
DEFAULT_BUDGET_S = 1800.0
DEFAULT_CHUNK_S = 86400.0


class StopFlag:
    """An abort signal for a warm-up: set once the run has been asked to stop.

    Runs carry ``stop_requested`` as a plain attribute; the warm-up wants
    something with ``is_set()``.
    """

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def is_set(self) -> bool:
        return bool(getattr(self._owner, "stop_requested", False))


async def run_plan(
    client: SplunkClient,
    plan: Sequence[cachewarm.WarmTarget],
    *,
    mode: str,
    where: Dict[str, Any],
    concurrency: int = DEFAULT_CONCURRENCY,
    budget_s: float = DEFAULT_BUDGET_S,
    chunk_s: float = DEFAULT_CHUNK_S,
    abort: Any = None,
) -> Dict[str, Any]:
    """Warm exactly ``plan``. Returns the result and the cache reading after."""
    lines = [t.to_dict() for t in plan]
    before = await cachewarm.plan_on_disk(client, plan, await cache_state(client, **where))
    planned, skipped = cachewarm.plan_searches(plan, before, mode, chunk_s)
    result = await cachewarm.warm(
        client, planned, mode=mode, concurrency=concurrency, budget_s=budget_s,
        skipped_local=skipped, abort=abort,
    )
    result.plan = lines
    state_after = await cache_state(client, **where)
    after = await cachewarm.plan_on_disk(client, plan, state_after)
    result.before, result.after = before.to_dict(), after.to_dict()
    return {"result": result.to_dict(), "cache_after": state_after.to_dict()}


def describe(lines: Sequence[Dict[str, Any]]) -> str:
    """A plan in one line, for the audit trail."""
    return "; ".join(
        f"{line.get('index')} {line.get('earliest_iso') or line.get('earliest')}.."
        f"{line.get('latest_iso') or line.get('latest')}"
        for line in lines
    )


async def warm_before_run(
    client: SplunkClient,
    spec: Dict[str, Any],
    *,
    where: Dict[str, Any],
    target_id: Optional[int],
    abort: Any = None,
) -> Dict[str, Any]:
    """The warm-up a run was launched with. Never raises.

    ``spec`` is what the launch stored: the operator's lines as they wrote
    them, so relative times are resolved now, at the start of the run, not
    when the run was queued. A warm-up that fails is recorded on the run and
    the run goes ahead: its forecast and provenance say what state it really
    started in, which is more use than a run that never happened.
    """
    started = time.time()
    try:
        plan = cachewarm.parse_plan(list(spec.get("plan") or []), now=started)
        async with evict_lock(int(target_id or 0)):
            outcome = await run_plan(
                client, plan,
                mode=str(spec.get("mode") or "tsidx"),
                where=where,
                concurrency=int(spec.get("concurrency") or DEFAULT_CONCURRENCY),
                budget_s=float(spec.get("budget_s") or DEFAULT_BUDGET_S),
                abort=abort,
            )
        return outcome
    except Exception as exc:  # noqa: BLE001 - recorded on the run, never fatal to it
        log.warning("the warm-up before a run failed: %s", exc)
        return {
            "result": {
                "mode": spec.get("mode") or "tsidx",
                "plan": list(spec.get("plan") or []),
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "seconds": round(time.time() - started, 2),
            },
            "cache_after": None,
        }


async def forecast_for_run(
    client: SplunkClient, scenario: Any, state: CacheState, duration_s: float
) -> Optional[Dict[str, Any]]:
    """What the run will read and how much of it is local, or None. Never raises."""
    if not state.available or duration_s <= 0:
        return None
    try:
        fc = await cachewarm.forecast_on_disk(client, scenario, state, time.time(), duration_s)
    except Exception:  # noqa: BLE001 - a nicety, never fatal
        log.debug("the cache forecast failed", exc_info=True)
        return None
    return fc.to_dict()


def summarise(result: Dict[str, Any]) -> Dict[str, Any]:
    """The few numbers worth a lifecycle event or an audit line."""
    before, after = result.get("before") or {}, result.get("after") or {}
    return {
        "mode": result.get("mode"),
        "searches": result.get("searches", 0),
        "failed": result.get("failed", 0),
        "not_started": result.get("not_started", 0),
        "seconds": result.get("seconds", 0),
        "local_pct_before": before.get("local_pct"),
        "local_pct_after": after.get("local_pct"),
        "error": result.get("error"),
    }


def spec_from(lines: List[Dict[str, Any]], mode: str, concurrency: Optional[int], budget_s: Optional[float]) -> Dict[str, Any]:
    """What a run stores: the operator's own lines, unresolved."""
    return {
        "plan": [
            {"index": str(l["index"]).strip(), "earliest": l["earliest"], "latest": l.get("latest", "now")}
            for l in lines
        ],
        "mode": mode,
        "concurrency": int(concurrency or DEFAULT_CONCURRENCY),
        "budget_s": float(budget_s or DEFAULT_BUDGET_S),
    }
