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
        self._fleets: Dict[int, Any] = {}
        # Cache epochs marked on runs in flight (an eviction from the button
        # or a run's own timer), folded into the summary at the end.
        self._epochs: Dict[int, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def active_count(self) -> int:
        with self._lock:
            return len(self._active) + len(self._fleets)

    def is_active(self, run_id: int) -> bool:
        with self._lock:
            return run_id in self._active or run_id in self._fleets

    def fleet_run(self, run_id: int) -> Optional[Any]:
        with self._lock:
            return self._fleets.get(run_id)

    def forget(self, run_id: int) -> None:
        with self._lock:
            self._fleets.pop(run_id, None)

    def mark_cache_epoch(self, run_id: int, eviction: Optional[Dict[str, Any]] = None, source: str = "button") -> Optional[Dict[str, Any]]:
        """An eviction happened while this run was in flight: a new cache epoch.

        Recorded on the run (the charts draw it, the summary lists it) so a
        purge mid-run is a marked instant rather than unexplained churn.
        """
        with self._lock:
            if run_id not in self._active and run_id not in self._fleets:
                return None
            epochs = self._epochs.setdefault(run_id, [])
            epoch = {
                "epoch": len(epochs) + 1,
                "requested_at": time.time(),
                "source": source,
                **{k: v for k, v in (eviction or {}).items() if k in ("attempted", "evicted", "confirmed", "failed", "bytes_evicted", "duration_s")},
            }
            epochs.append(epoch)
            entry = self._active.get(run_id)
        if entry is not None and getattr(entry, "stats", None) is not None:
            set_epoch = getattr(entry.stats, "set_epoch", None)
            if set_epoch is not None:
                set_epoch(epoch["epoch"], epoch["requested_at"])
        return epoch

    def epochs_for(self, run_id: int, forget: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            epochs = list(self._epochs.get(run_id) or [])
            if forget:
                self._epochs.pop(run_id, None)
        return epochs

    def drain_fleets(self, timeout_s: float = 15.0) -> int:
        """Stop every fleet supervisor and wait for its thread. Returns how many.

        A shutdown seam (and a test seam): a supervisor thread that outlives
        the process's database engine would otherwise race the next one.
        """
        with self._lock:
            supervisors = list(self._fleets.values())
        for supervisor in supervisors:
            try:
                supervisor.request_stop()
            except Exception:  # noqa: BLE001 - draining is best effort
                log.warning("stop request during drain failed", exc_info=True)
        deadline = time.time() + timeout_s
        for supervisor in supervisors:
            supervisor.join(timeout=max(0.0, deadline - time.time()))
        return len(supervisors)

    def live_stats(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._active.get(run_id)
            if entry is not None:
                return entry.snapshot
            supervised = run_id in self._fleets
        if supervised:
            from . import fleet as fleet_module

            with session_scope() as session:
                run = session.get(Run, run_id)
                if run is None:
                    return None
                return fleet_module.live_snapshot(run, fleet_module._leases(session, run))
        return None

    def request_stop(self, run_id: int) -> bool:
        with self._lock:
            entry = self._active.get(run_id)
            supervisor = self._fleets.get(run_id)
        if supervisor is not None:
            supervisor.request_stop()
            return True
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
            fleet_kind = run.fleet or "inprocess"
            # The ceiling is the in-process generator's, and only its. A fleet
            # exists to go past it; the per-worker share is what bounds a
            # fleet, and the planner sizes that from REG_VUS_PER_WORKER.
            if fleet_kind == "inprocess":
                if run.arrival_rate_per_min:
                    if run.arrival_rate_per_min > settings.max_virtual_users * 60:
                        raise RunRejected(
                            f"an arrival rate of {run.arrival_rate_per_min:.0f}/min exceeds what "
                            f"this control plane's ceiling of {settings.max_virtual_users} in-flight "
                            "searches can honestly generate. Launch it on a fleet"
                        )
                elif vus > settings.max_virtual_users:
                    raise RunRejected(
                        f"{vus} virtual users exceeds this control plane's ceiling of "
                        f"{settings.max_virtual_users}. It generates the load in its own "
                        "process, so beyond that it becomes the bottleneck and the result "
                        "would describe the server rather than Splunk. Launch it on a fleet"
                    )
                if len(engines) > 1:
                    raise RunRejected(
                        "the scenario mixes the api and browser engines, which needs the "
                        "worker fleet: launch it on one, or split it into an api scenario "
                        "and a browser scenario"
                    )
            duration = run.duration_s or scenario.load.duration_s or 0.0
            if duration > settings.max_run_duration_s:
                raise RunRejected(
                    f"a duration of {duration:.0f}s exceeds this control plane's maximum of "
                    f"{settings.max_run_duration_s:.0f}s (REG_MAX_RUN_DURATION_S)"
                )
            engine_name = "browser" if engines == {"browser"} else "api"
            if "browser" in engines and not target.web_url:
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
            if fleet_kind != "inprocess":
                run.scenario_digest = digest
                session.add(run)
                session.commit()
                self._start_fleet(run.id, fleet_kind, settings)
                return
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
                cold_window_s=run.cold_window_s,
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

    def _start_fleet(self, run_id: int, fleet_kind: str, settings) -> None:
        from . import fleet as fleet_module
        from .drivers import DriverError

        with self._lock:
            if len(self._active) + len(self._fleets) >= settings.max_concurrent_runs:
                raise RunRejected(
                    f"{settings.max_concurrent_runs} run(s) already in flight"
                )
        try:
            supervisor = fleet_module.launch(run_id, start=False)
        except (fleet_module.FleetError, DriverError, FileNotFoundError) as exc:
            raise RunRejected(str(exc)) from exc
        # Registered before it starts: a supervisor that fails fast calls
        # forget() from its own thread, and a registration after that would
        # leave a ghost entry holding a concurrency slot for the process life.
        with self._lock:
            self._fleets[run_id] = supervisor
        supervisor.start()

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
                f"{exc}. Browser scenarios run on a fleet (swarm or k8s), which starts "
                "the browser worker image (ghcr.io/livehybrid/regulator-worker:browser) "
                "for them: launch this run on a fleet rather than in-process"
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
            if eviction is not None:
                self._lifecycle(run_id, "cache_evicted", **{k: v for k, v in eviction.to_dict().items() if k != "errors"})
            self._cache_sample(run_id, "before", before.to_dict())
            self._lifecycle(run_id, "cache_before", **_cache_facts(before.to_dict()))

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
            evictor: Optional[asyncio.Task[None]] = None
            with session_scope() as session:
                row = session.get(Run, run_id)
                every = float(row.evict_every_s or 0) if row is not None else 0.0
                scope = (row.evict_cache_indexes or "") if row is not None else ""
                target_id = row.target_id if row is not None else None
            if every > 0:
                periodic_indexes = None if "*" in scope else ([i for i in scope.split(",") if i] or indexes)
                evictor = asyncio.create_task(self._evict_loop(run_id, engine.client, where, periodic_indexes, every, target_id))
            try:
                summary = await scheduler.run()
            finally:
                if evictor is not None:
                    evictor.cancel()
                    try:
                        await evictor
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            summary.scenario_digest = scenario_digest(scenario)

            summary.cache = await self._cache_provenance(engine.client, before, eviction, where)
            if isinstance(summary.cache, dict) and summary.cache.get("after"):
                self._cache_sample(run_id, "after", summary.cache["after"], {"delta": summary.cache.get("delta")})
                self._lifecycle(run_id, "cache_after", **_cache_facts(summary.cache["after"]), delta=summary.cache.get("delta"))
            if summary.started_at and summary.ended_at:
                summary.sut = await correlate(
                    engine.client,
                    summary.started_at,
                    summary.ended_at,
                    marker_prefix_for(config.run_id),
                )
                if isinstance(summary.sut, dict):
                    self._lifecycle(run_id, "correlation", findings=summary.sut.get("findings"), probes=sorted((summary.sut.get("probes") or {}).keys()))

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

    async def _evict_loop(self, run_id: int, client, where: Dict[str, Any], indexes, every_s: float, target_id: Optional[int]) -> None:
        """Evict on a clock during the run: each eviction starts a cache epoch.

        Never overlapping: the next eviction is due ``every_s`` after the last
        one was due, and one that takes longer than the interval pushes the
        clock rather than stacking. The per-target lock keeps it clear of the
        Purge button and of another run on the same target.
        """
        from .cachelock import evict_lock
        from .samples import store_cache_sample

        due = time.time() + every_s
        try:
            while True:
                await asyncio.sleep(max(0.2, due - time.time()))
                started = time.time()
                try:
                    async with evict_lock(target_id or 0):
                        result = await asyncio.wait_for(evict_all(client, indexes=indexes, **where), timeout=min(every_s, 600.0))
                    outcome = result.to_dict()
                except asyncio.TimeoutError:
                    outcome = {"attempted": 0, "evicted": 0, "confirmed": 0, "failed": 0, "bytes_evicted": 0, "status": "timeout"}
                except Exception as exc:  # noqa: BLE001 - an eviction failure is noted, never fatal
                    outcome = {"attempted": 0, "evicted": 0, "confirmed": 0, "failed": 0, "bytes_evicted": 0, "status": f"failed: {str(exc)[:120]}"}
                duration = round(time.time() - started, 1)
                epoch = self.mark_cache_epoch(run_id, {**outcome, "duration_s": duration}, source="timer")
                if epoch is not None and duration > every_s:
                    epoch["note"] = f"the eviction took {duration}s, longer than the {every_s:.0f}s interval: epochs are irregular"
                try:
                    after = await cache_state(client, **where)
                    with session_scope() as session:
                        store_cache_sample(session, target_id, run_id, "epoch", after.to_dict(), {"epoch": epoch})
                    self._lifecycle(run_id, "cache_epoch", **(epoch or {}), **_cache_facts(after.to_dict()))
                except Exception:  # noqa: BLE001
                    log.debug("cache reading after the periodic eviction failed", exc_info=True)
                due = max(time.time(), due + every_s)
        except asyncio.CancelledError:
            return

    async def _publish_loop(self, run_id: int, entry: ActiveRun) -> None:
        """Write the live aggregate back so the UI has something to poll.

        Every few seconds it also takes a sample: the interval since the last
        one plus the cumulative figures, stored for the graphs and shipped
        over HEC as one regulator:sample event.
        """
        from .samples import build_sample, store_sample
        from .telemetry import telemetry

        sample_every = max(1.0, float(getattr(get_settings(), "sample_interval_s", 5.0)))
        last_sample = time.time()
        try:
            while True:
                await asyncio.sleep(PUBLISH_INTERVAL_S)
                # Taken on the run thread, which is the only thread that
                # mutates the aggregate, then handed to requests as a value.
                snapshot = entry.stats.snapshot()
                with self._lock:
                    entry.snapshot = snapshot
                now = time.time()
                sample = None
                if now - last_sample >= sample_every:
                    sample = build_sample(entry.stats.take_interval(), snapshot, now)
                    last_sample = now
                target_name = None
                with session_scope() as session:
                    run = session.get(Run, run_id)
                    if run is None:
                        return
                    run.stats_json = snapshot
                    session.add(run)
                    if sample is not None:
                        store_sample(session, run_id, sample)
                        target_name = run.target.name if run.target is not None else None
                if sample is not None:
                    telemetry.sample(run, sample, target_name)
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
        from .telemetry import telemetry

        target_name = None
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            run.state = state
            if started and run.started_at is None:
                run.started_at = time.time()
            session.add(run)
            target_name = run.target.name if run.target is not None else None
        if started:
            telemetry.lifecycle("run_started", run, target_name, virtual_users=run.virtual_users, duration_s=run.duration_s)

    def _lifecycle(self, run_id: int, kind: str, **detail: Any) -> None:
        """One lifecycle event about a run, with its join keys, best effort."""
        from .telemetry import telemetry

        try:
            with session_scope() as session:
                run = session.get(Run, run_id)
                if run is None:
                    return
                target_name = run.target.name if run.target is not None else None
            telemetry.lifecycle(kind, run, target_name, **detail)
        except Exception:  # noqa: BLE001 - telemetry never fails a run
            log.debug("lifecycle event %s for run %s not sent", kind, run_id, exc_info=True)

    def _cache_sample(self, run_id: int, kind: str, state: Dict[str, Any], detail: Optional[Dict[str, Any]] = None) -> None:
        from .samples import store_cache_sample

        try:
            with session_scope() as session:
                run = session.get(Run, run_id)
                if run is None:
                    return
                store_cache_sample(session, run.target_id, run_id, kind, state, detail)
        except Exception:  # noqa: BLE001
            log.debug("cache sample for run %s not stored", run_id, exc_info=True)

    def _finish(
        self,
        run_id: int,
        state: str,
        summary: Optional[Dict[str, Any]] = None,
        stats: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        from .telemetry import telemetry

        target_name = None
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            run.state = state
            run.ended_at = time.time()
            target_name = run.target.name if run.target is not None else None
            epochs = self.epochs_for(run_id, forget=True)
            if summary is not None:
                if epochs:
                    cache = summary.get("cache") if isinstance(summary.get("cache"), dict) else {}
                    summary["cache"] = {**cache, "epochs": epochs}
                run.summary_json = summary
            if stats is not None:
                run.stats_json = stats
            if error is not None:
                run.error = error
            session.add(run)
        headline = (stats or {}) if isinstance(stats, dict) else {}
        telemetry.lifecycle(
            f"run_{state}",
            run,
            target_name,
            outcome=state,
            valid=(summary or {}).get("valid") if isinstance(summary, dict) else None,
            invalid_reason=(summary or {}).get("invalid_reason") if isinstance(summary, dict) else None,
            executions=headline.get("executions"),
            errors=headline.get("errors"),
            p95_ms=(headline.get("latency") or {}).get("p95_ms"),
            error_rate_pct=headline.get("error_rate_pct"),
            error=error,
        )
        if isinstance(summary, dict):
            telemetry.run_final(run, summary, scope="inprocess", target_name=target_name)


from .samples import cache_facts as _cache_facts  # noqa: E402 - shared with the fleet supervisor

manager = RunManager()
