"""Executing a run, in this process.

The control plane runs the scenario itself rather than launching a fleet. That
is a deliberate first step, not a shortcut that has to be undone: Stoker has
exactly the same in-process driver alongside its Swarm and Kubernetes ones,
because being able to run without any container orchestration at all is what
makes the thing debuggable.

The cost is honest and bounded. The server is the load generator, so past a
few hundred virtual users it becomes the constraint. That does not silently
corrupt a result: the worker's generator-drift guard marks a run invalid when
this process cannot keep to its own schedule, so the failure mode is a run that
says "the load box was too small" rather than a number that quietly describes
the server instead of Splunk.

Each run gets a thread with its own event loop. FastAPI's loop stays free, the
run outlives the request that started it, and stopping is co-operative through
the scheduler's own ``request_stop``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from regulator_agent.engines.api import ApiEngine
from regulator_agent.params import ParameterResolver
from regulator_agent.results import RunStats
from regulator_agent.scenario import lint, load_scenario
from regulator_agent.scheduler import Scheduler
from regulator_agent.smartstore import cache_state, delta as cache_delta, evict_all

from .adapters import load_named_scenario, worker_config
from .config import get_settings
from .db import session_scope
from .models import Run, Target

log = logging.getLogger("regulator.server.runner")

# How often the live aggregate is written back to the database. The UI polls
# every couple of seconds, so anything faster is writes nobody reads.
PUBLISH_INTERVAL_S = 1.5


class RunRejected(RuntimeError):
    """The run cannot start, and the operator needs to know why now."""


@dataclass
class ActiveRun:
    run_id: int
    stats: RunStats
    scheduler: Optional[Scheduler] = None
    thread: Optional[threading.Thread] = None
    stop_requested: bool = False
    started_at: float = field(default_factory=time.time)


class RunManager:
    """Owns the running scenarios. One per process."""

    def __init__(self) -> None:
        self._active: Dict[int, ActiveRun] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def is_active(self, run_id: int) -> bool:
        with self._lock:
            return run_id in self._active

    def live_stats(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._active.get(run_id)
        return entry.stats.snapshot() if entry else None

    def request_stop(self, run_id: int) -> bool:
        with self._lock:
            entry = self._active.get(run_id)
        if entry is None:
            return False
        entry.stop_requested = True
        if entry.scheduler is not None:
            entry.scheduler.request_stop("stopped from the web interface")
        return True

    # ------------------------------------------------------------------

    def start(self, run_id: int) -> None:
        """Validate, then hand the run to its own thread."""
        settings = get_settings()
        if self.active_count() >= settings.max_concurrent_runs:
            raise RunRejected(
                f"{settings.max_concurrent_runs} run(s) already in flight. The control "
                "plane generates the load itself, so running more at once would mean "
                "measuring contention between your own tests"
            )

        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise RunRejected(f"run {run_id} does not exist")
            target = session.get(Target, run.target_id)
            if target is None:
                raise RunRejected("the target for this run has been deleted")

            scenario = load_named_scenario(run.scenario)
            blocking = [line for line in lint(scenario) if not line.startswith("advice: ")]
            if blocking:
                raise RunRejected(
                    "the scenario does not lint: " + "; ".join(blocking[:3])
                )

            vus = run.virtual_users or scenario.load.virtual_users
            if vus > settings.max_virtual_users:
                raise RunRejected(
                    f"{vus} virtual users exceeds this control plane's ceiling of "
                    f"{settings.max_virtual_users}. It generates the load in its own "
                    "process, so beyond that it becomes the bottleneck and the result "
                    "would describe the server rather than Splunk"
                )

            config = worker_config(
                target,
                scenario_path=run.scenario,
                virtual_users=run.virtual_users,
                duration_s=run.duration_s,
                arrival_rate_per_min=run.arrival_rate_per_min,
                pacing_s=run.pacing_s,
                run_label=run.label or f"run-{run.id}",
                evict_cache=run.evict_cache,
                evict_cache_indexes=(run.evict_cache_indexes or "").split(",")
                if run.evict_cache_indexes
                else [],
            )
            run.state = "pending"
            session.add(run)

        stats = RunStats(run_id=str(run_id))
        entry = ActiveRun(run_id=run_id, stats=stats)
        thread = threading.Thread(
            target=self._thread_main,
            args=(run_id, config, entry),
            name=f"regulator-run-{run_id}",
            daemon=True,
        )
        entry.thread = thread
        with self._lock:
            self._active[run_id] = entry
        thread.start()

    # ------------------------------------------------------------------

    def _thread_main(self, run_id: int, config, entry: ActiveRun) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._execute(run_id, config, entry))
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never lost
            log.exception("run %s failed", run_id)
            self._finish(run_id, state="failed", error=str(exc))
        finally:
            try:
                loop.close()
            finally:
                with self._lock:
                    self._active.pop(run_id, None)

    async def _execute(self, run_id: int, config, entry: ActiveRun) -> None:
        scenario = load_named_scenario(config.scenario_path)
        engine = ApiEngine(config)
        publisher: Optional[asyncio.Task[None]] = None

        await engine.start()
        try:
            self._set_state(run_id, "running", started=True)

            capabilities = await engine.probe()

            problems = [p for p in await engine.validate(scenario) if not p.startswith("advice: ")]
            if problems:
                raise RunRejected(
                    "the scenario does not match this target: " + "; ".join(problems[:3])
                )

            resolver = ParameterResolver(scenario, seed=config.seed)
            if resolver.dynamic_parameters:
                await engine.resolve_parameters(resolver)

            eviction = None
            if config.evict_cache:
                indexes = list(config.evict_cache_indexes) or (
                    [scenario.corpus.index] if scenario.corpus.index else None
                )
                eviction = await evict_all(engine.client, indexes=indexes)

            before = await cache_state(engine.client)

            scheduler = Scheduler(
                scenario=scenario,
                config=config,
                engine=engine,
                resolver=resolver,
                stats=entry.stats,
                capabilities=capabilities,
            )
            entry.scheduler = scheduler
            if entry.stop_requested:
                # Stopped between being queued and starting. Honour it rather
                # than running a test nobody is waiting for any more.
                scheduler.request_stop("stopped before it started")

            publisher = asyncio.create_task(self._publish_loop(run_id, entry))
            summary = await scheduler.run()

            summary.cache = await self._cache_provenance(engine.client, before, eviction)

            payload = summary.to_dict()
            self._finish(
                run_id,
                state=summary.outcome,
                summary=payload,
                stats=payload.get("stats"),
            )
        finally:
            if publisher is not None:
                publisher.cancel()
                try:
                    await publisher
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            try:
                await engine.close()
            except Exception:  # noqa: BLE001 - closing must not mask the result
                log.warning("closing the engine failed", exc_info=True)

    async def _cache_provenance(self, client, before, eviction) -> Optional[Dict[str, Any]]:
        if not before.available:
            return {"available": False, "reason": before.reason}
        after = await cache_state(client)
        payload = {
            "before": before.to_dict(),
            "after": after.to_dict(),
            "delta": cache_delta(before, after).to_dict(),
        }
        if eviction is not None:
            payload["eviction"] = eviction.to_dict()
        return payload

    async def _publish_loop(self, run_id: int, entry: ActiveRun) -> None:
        """Write the live aggregate back so the UI has something to poll."""
        try:
            while True:
                await asyncio.sleep(PUBLISH_INTERVAL_S)
                snapshot = entry.stats.snapshot()
                with session_scope() as session:
                    run = session.get(Run, run_id)
                    if run is None:
                        return
                    run.stats_json = snapshot
                    session.add(run)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - publishing must never kill the run
            log.warning("publishing live stats failed", exc_info=True)

    # ------------------------------------------------------------------

    def _set_state(self, run_id: int, state: str, started: bool = False) -> None:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            run.state = state
            if started and run.started_at is None:
                run.started_at = time.time()
            session.add(run)

    def _finish(
        self,
        run_id: int,
        state: str,
        summary: Optional[Dict[str, Any]] = None,
        stats: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            run.state = state
            run.ended_at = time.time()
            if summary is not None:
                run.summary_json = summary
            if stats is not None:
                run.stats_json = stats
            if error is not None:
                run.error = error
            session.add(run)


manager = RunManager()
