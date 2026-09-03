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

from regulator_agent.engines import BrowserUnavailable, get_engine
from regulator_agent.hec import HecEmitter
from regulator_agent.params import ParameterResolver, sanitise_marker_part
from regulator_agent.results import RunStats
from regulator_agent.scenario import is_advice, lint, scenario_digest
from regulator_agent.scheduler import Scheduler
from regulator_agent.smartstore import cache_state, delta as cache_delta, evict_all
from regulator_agent.sut import correlate, marker_prefix_for

from .adapters import hec_config, load_named_scenario, worker_config
from .config import get_settings
from .db import session_scope
from .models import Run, Target, is_terminal

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
    # The last snapshot the run thread published. Requests read this rather
    # than calling stats.snapshot() themselves: the aggregate is mutated by
    # the run thread, and iterating its step dictionary from a request thread
    # while a new step was being added raised "dictionary changed size during
    # iteration", which the UI turned into a dead poll chain.
    snapshot: Optional[Dict[str, Any]] = None


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
            return entry.snapshot if entry else None

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

        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise RunRejected(f"run {run_id} does not exist")
            target = session.get(Target, run.target_id) if run.target_id else None
            if target is None:
                raise RunRejected("the target for this run has been deleted")

            scenario = load_named_scenario(run.scenario)
            blocking = [line for line in lint(scenario) if not is_advice(line)]
            if blocking:
                raise RunRejected(
                    "the scenario does not lint: " + "; ".join(blocking[:3])
                )

            # The ceiling applies to whatever the load model is. The open
            # model has no virtual users to count, so the arrival rate is
            # bounded by what the same ceiling of in-flight searches allows;
            # the previous check only looked at the closed-model field and an
            # arrival rate of a hundred thousand a minute walked straight past
            # it.
            engines = {step.engine for step in scenario.steps}
            vus = run.virtual_users or scenario.load.virtual_users
            if run.arrival_rate_per_min:
                if run.arrival_rate_per_min > settings.max_virtual_users * 60:
                    raise RunRejected(
                        f"an arrival rate of {run.arrival_rate_per_min:.0f}/min exceeds what "
                        f"this control plane's ceiling of {settings.max_virtual_users} in-flight "
                        "searches can honestly generate"
                    )
            elif vus > settings.max_virtual_users:
                raise RunRejected(
                    f"{vus} virtual users exceeds this control plane's ceiling of "
                    f"{settings.max_virtual_users}. It generates the load in its own "
                    "process, so beyond that it becomes the bottleneck and the result "
                    "would describe the server rather than Splunk"
                )
            duration = run.duration_s or scenario.load.duration_s or 0.0
            if duration > settings.max_run_duration_s:
                raise RunRejected(
                    f"a duration of {duration:.0f}s exceeds this control plane's maximum of "
                    f"{settings.max_run_duration_s:.0f}s (REG_MAX_RUN_DURATION_S)"
                )
            if len(engines) > 1:
                raise RunRejected(
                    "the scenario mixes the api and browser engines, which needs the "
                    "worker fleet. Split it into an api scenario and a browser scenario"
                )
            engine_name = next(iter(engines)) if engines else "api"
            if engine_name == "browser" and not target.web_url:
                raise RunRejected(
                    "a browser scenario needs the target's Web URL (Splunk Web, normally "
                    "port 8000): edit the target and add it"
                )

            # Eviction needs an explicit scope, exactly as the evict button
            # does. A run whose scenario named no corpus index would otherwise
            # have dropped the whole cache on a shared cluster.
            if run.evict_cache:
                scope = [i for i in (run.evict_cache_indexes or "").split(",") if i]
                if "*" not in scope and not scope and not scenario.corpus.index:
                    raise RunRejected(
                        "evict_cache needs at least one index: the scenario declares no "
                        "corpus index and none was named on the run"
                    )

            # The marker every dispatched search carries. Unique per run row,
            # so two runs that share a label never share a marker and the
            # audit correlation of one cannot pick up the other's searches.
            marker_run_id = f"r{run.id}"
            if run.label:
                marker_run_id += "-" + sanitise_marker_part(run.label)[:40]

            digest = scenario_digest(scenario)
            config = worker_config(
                target,
                scenario_path=run.scenario,
                virtual_users=run.virtual_users,
                duration_s=run.duration_s,
                arrival_rate_per_min=run.arrival_rate_per_min,
                pacing_s=run.pacing_s,
                run_label=marker_run_id,
                evict_cache=run.evict_cache,
                evict_cache_indexes=(run.evict_cache_indexes or "").split(",")
                if run.evict_cache_indexes
                else [],
                hec=hec_config(settings),
                seed=run.seed,
            )
            run.state = "pending"
            run.scenario_digest = digest
            session.add(run)

        stats = RunStats(run_id=str(run_id))
        entry = ActiveRun(run_id=run_id, stats=stats)
        thread = threading.Thread(
            target=self._thread_main,
            args=(run_id, config, entry, engine_name),
            name=f"regulator-run-{run_id}",
            daemon=True,
        )
        entry.thread = thread
        with self._lock:
            # Checked and registered under one lock, so two simultaneous
            # launches cannot both pass the count and both start.
            if len(self._active) >= settings.max_concurrent_runs:
                raise RunRejected(
                    f"{settings.max_concurrent_runs} run(s) already in flight. The control "
                    "plane generates the load itself, so running more at once would mean "
                    "measuring contention between your own tests"
                )
            self._active[run_id] = entry
        thread.start()

    def reconcile_at_boot(self) -> int:
        """Mark runs that were in flight when the previous process died.

        A run executes in this process and nowhere else, so a row still
        pending or running at start-up describes a run that no longer exists.
        Left alone it polls forever in the UI and refuses deletion.
        """
        fixed = 0
        with session_scope() as session:
            for run in session.query(Run).filter(Run.state.in_(("pending", "running"))).all():
                run.state = "failed"
                run.ended_at = time.time()
                run.error = (
                    "the control plane restarted while this run was in flight, so it was "
                    "never completed. Whatever it measured before that is in its live "
                    "statistics"
                )
                session.add(run)
                fixed += 1
        if fixed:
            log.warning("marked %d run(s) failed that were in flight at the last shutdown", fixed)
        return fixed

    def _thread_main(self, run_id: int, config, entry: ActiveRun, engine_name: str) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._execute(run_id, config, entry, engine_name))
        except RunRejected as exc:
            self._finish(run_id, state="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never lost
            log.exception("run %s failed", run_id)
            self._finish(run_id, state="failed", error=str(exc))
        finally:
            try:
                loop.close()
            finally:
                with self._lock:
                    self._active.pop(run_id, None)

    async def _execute(self, run_id: int, config, entry: ActiveRun, engine_name: str) -> None:
        scenario = load_named_scenario(config.scenario_path)
        try:
            engine = get_engine(engine_name, config)
        except BrowserUnavailable as exc:
            raise RunRejected(str(exc))
        publisher: Optional[asyncio.Task[None]] = None
        hec: Optional[HecEmitter] = None

        try:
            await engine.start()
        except BrowserUnavailable as exc:
            # The control-plane image carries no browser. Say so in the run
            # rather than dispatching dashboard names as searches, which is
            # what happened when every run went through the API engine.
            raise RunRejected(
                f"{exc}. Browser scenarios run from the browser worker image "
                "(ghcr.io/livehybrid/regulator-worker:browser) until the fleet lands"
            )
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

            where = {
                "indexer_urls": config.indexer_urls,
                "indexer_token": config.indexer_token,
                "indexer_username": config.indexer_username,
                "indexer_password": config.indexer_password,
            }
            eviction = None
            if config.evict_cache:
                if "*" in config.evict_cache_indexes:
                    indexes = None  # every index, asked for explicitly
                else:
                    indexes = list(config.evict_cache_indexes) or (
                        [scenario.corpus.index] if scenario.corpus.index else None
                    )
                eviction = await evict_all(engine.client, indexes=indexes, **where)

            before = await cache_state(engine.client, **where)

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

            emitters: List[Any] = []
            if config.hec is not None:
                hec = HecEmitter(config.hec, run_id=config.run_id)
                await hec.start()
                emitters.append(hec)
            scheduler.emitters = emitters

            publisher = asyncio.create_task(self._publish_loop(run_id, entry))
            summary = await scheduler.run()
            summary.scenario_digest = scenario_digest(scenario)

            summary.cache = await self._cache_provenance(engine.client, before, eviction, where)
            if summary.started_at and summary.ended_at:
                summary.sut = await correlate(
                    engine.client,
                    summary.started_at,
                    summary.ended_at,
                    marker_prefix_for(config.run_id),
                )

            payload = summary.to_dict()
            if hec is not None:
                await hec.flush()
                payload["telemetry"] = hec.stats()
                hec.emit_summary(payload)
                await hec.flush()
            self._finish(
                run_id,
                state=summary.outcome,
                summary=payload,
                stats=payload.get("stats"),
            )
        finally:
            if hec is not None:
                try:
                    await hec.close()
                except Exception:  # noqa: BLE001
                    log.warning("closing the telemetry emitter failed", exc_info=True)
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

    async def _cache_provenance(self, client, before, eviction, where) -> Optional[Dict[str, Any]]:
        if not before.available:
            return {"available": False, "reason": before.reason}
        after = await cache_state(client, **where)
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
                # Taken on the run thread, which is the only thread that
                # mutates the aggregate, then handed to requests as a value.
                snapshot = entry.stats.snapshot()
                with self._lock:
                    entry.snapshot = snapshot
                with session_scope() as session:
                    run = session.get(Run, run_id)
                    if run is None:
                        return
                    run.stats_json = snapshot
                    session.add(run)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - publishing must never kill the run
            log.warning("publishing live stats failed, retrying", exc_info=True)
            # One failed write (a busy database, most likely) must not end
            # live statistics for the rest of the run.
            await asyncio.sleep(PUBLISH_INTERVAL_S)
            await self._publish_loop(run_id, entry)

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
