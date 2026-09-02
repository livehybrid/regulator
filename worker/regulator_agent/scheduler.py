"""When work happens: virtual users, ramps, pacing and honest latency.

This module owns the load shape. An engine knows how to run one search; the
scheduler decides how many are in flight, when the next one starts, and what
"how long did it take" actually means.

## The two models

**Closed.** A fixed population of virtual users, each looping: do an iteration,
think, repeat. Concurrency is what you set, throughput is whatever the system
can deliver. This is the dial people mean by "200 concurrent users" and it is
what a capacity question wants, because it mirrors a real user population that
does not grow just because the system got slow.

**Open.** A fixed arrival rate, independent of how long anything takes. Work
arrives whether or not the previous work finished. This is the model you need
at saturation, because it is the only one that measures the queue.

## Coordinated omission, and why the closed model needs help

A load generator that waits for response N before issuing request N+1 never
issues the requests that would have piled up behind a stall. If the server
freezes for ten seconds, a naive closed-loop generator records one slow request
rather than the hundred requests that a real user population would have
generated during the freeze. The tail of the distribution, which is the only
part anybody cares about, simply vanishes.

The correction is to measure latency from when a request *should* have been
issued rather than from when the generator got round to issuing it. That
requires a schedule, which the open model has by definition and the closed
model only has if you give it one. So:

* Open model: every arrival has a scheduled time, and latency runs from there.
* Closed model with ``pacing_s`` set: each virtual user's iteration k is due at
  ``start + k * pacing_s``, and latency runs from there. If an iteration
  overruns, the next one is already late and says so.
* Closed model without pacing: latency and service time are equal, and the
  record says ``co_corrected: false`` so nobody mistakes the p99 for a
  saturation measurement. Throughput is the honest signal in that mode.

## The first step in an iteration carries the schedule

An iteration is a sequence of steps: open a dashboard, then run a search, then
run another. A real user only asks the second question after reading the first
answer, so there is no meaningful schedule for the later steps: their intended
start genuinely is the moment the previous one finished. Only the first step of
an iteration carries the accumulated schedule debt. That keeps the correction
honest instead of attributing one stall to every step behind it.

## The generator is a system under test too

Every record carries how late its schedule was. If the worker cannot keep to
its own timetable, the numbers describe the worker, not Splunk, and the run is
marked invalid rather than reported. An invalid benchmark that says so is worth
more than a confident one that is wrong.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .config import Config
from .engines.base import Engine, StepContext, TargetCapabilities
from .params import (
    DrawContext,
    ParameterResolver,
    apply_cache_bust,
    cache_bust_marker,
    persona_for_vu,
    think_time_for,
)
from .results import (
    OUTCOME_ABORTED,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_STOPPED,
    NdjsonEmitter,
    RunStats,
    RunSummary,
    StepRecord,
)
from .scenario import LoadModel, Persona, RampStage, Scenario, Step
from .timepolicy import resolve_window

log = logging.getLogger("regulator.scheduler")

# How often the supervisor re-evaluates the ramp and the guard rails. Fast
# enough that a ramp looks smooth and an abort is prompt, slow enough that it
# is not itself a measurable load on the worker.
TICK_S = 0.25

# Guard rails are not evaluated until this many step executions have happened.
# Without it, the first request failing during startup trips a 100% error rate
# and aborts a run that was about to be fine.
MIN_SAMPLES_FOR_GUARD = 20

# When a paced iteration falls further behind than this, stop trying to catch
# up and re-anchor the schedule. Unbounded catch-up turns a brief stall into a
# thundering herd that attacks the target exactly when it is least able to
# cope, which is both unrealistic and unkind.
MAX_CATCHUP_S = 30.0

# How long in-flight work is allowed to finish after a stop is requested.
DRAIN_BUDGET_S = 30.0


class Ramp:
    """Target concurrency (or arrival rate) as a function of elapsed time.

    Stages are walked in order. A stage either climbs linearly to a target over
    a period, or holds the current target for one. After the last stage the
    final target holds for the rest of the run.

    Ramping is not decoration. Going from nothing to full load in one step
    measures a cold cluster's panic response, not its steady state, and there
    is a documented case of a naive ramp taking a search head down with it.
    """

    def __init__(self, stages: Sequence[RampStage], final_target: float) -> None:
        self._legs: List[tuple[float, float, float]] = []  # (duration, from, to)
        # A ramp made only of holds has nothing to climb to, so starting from
        # zero meant it held zero: the run created no virtual users, did no
        # work, and still reported completed and valid. Start from the declared
        # target when no stage names one.
        current = 0.0 if any(stage.to is not None for stage in stages) else float(final_target)
        for stage in stages:
            if stage.to is not None:
                self._legs.append((max(0.0, stage.over_s), current, float(stage.to)))
                current = float(stage.to)
            elif stage.hold_s:
                self._legs.append((stage.hold_s, current, current))
        if not self._legs:
            # No ramp declared: full load immediately, held forever. Legal, and
            # sometimes what you want for a short smoke, but the plateau is the
            # only thing it measures.
            self._legs.append((0.0, final_target, final_target))
            current = final_target
        self._final = current

    @property
    def duration_s(self) -> float:
        return sum(leg[0] for leg in self._legs)

    @property
    def final_target(self) -> float:
        return self._final

    def target_at(self, elapsed_s: float) -> float:
        remaining = elapsed_s
        for duration, start, end in self._legs:
            if duration <= 0:
                if remaining <= 0:
                    return end
                continue
            if remaining < duration:
                fraction = remaining / duration
                return start + (end - start) * fraction
            remaining -= duration
        return self._final


@dataclass
class _Iteration:
    """One pass through a persona's steps by one virtual user."""

    vu_id: int
    persona: Persona
    iteration: int
    intended_start: float  # monotonic


class Scheduler:
    """Drives an engine according to a scenario's load model."""

    def __init__(
        self,
        scenario: Scenario,
        config: Config,
        engine: Engine,
        resolver: ParameterResolver,
        stats: Optional[RunStats] = None,
        emitters: Optional[Sequence[Any]] = None,
        capabilities: Optional[TargetCapabilities] = None,
    ) -> None:
        self.scenario = scenario
        self.config = config
        self.engine = engine
        self.resolver = resolver
        self.stats = stats or RunStats(run_id=config.run_id, slot=config.slot)
        self.emitters: List[Any] = list(emitters or [])
        self.capabilities = capabilities

        self.load = self._effective_load()
        self.duration_s = self._effective_duration()
        self.ramp = Ramp(
            self.load.ramp,
            float(self.load.virtual_users)
            if self.load.model == "closed"
            else float(self.load.arrival_rate_per_min),
        )

        self._stop = asyncio.Event()
        self._fatal: Optional[BaseException] = None
        self._abort_reason: Optional[str] = None
        self._invalid_reason: Optional[str] = None
        self._vu_tasks: Dict[int, asyncio.Task[None]] = {}
        self._open_tasks: set[asyncio.Task[None]] = set()
        self._peak_vus = 0
        self._arrivals_missed = 0
        self._t0_monotonic = 0.0
        self._t0_wall = 0.0

    # ------------------------------------------------------------------
    # Configuration resolution
    # ------------------------------------------------------------------

    def _effective_load(self) -> LoadModel:
        """Environment overrides the scenario.

        One scenario, many sizes. A CI job wants to run the same soc-analyst
        scenario at 10 virtual users on every pull request and 400 nightly, and
        editing the scenario file to do that would mean the two runs were not
        the same test.
        """
        load = self.scenario.load
        vus = self.config.virtual_users
        rate = self.config.arrival_rate_per_min
        pacing = self.config.pacing_s

        if rate:
            return LoadModel(
                model="open",
                virtual_users=load.virtual_users,
                arrival_rate_per_min=rate,
                pacing_s=0.0,
                ramp=load.ramp if load.model == "open" else [],
                duration_s=load.duration_s,
            )
        if vus or pacing:
            return LoadModel(
                model="closed",
                virtual_users=vus or load.virtual_users,
                arrival_rate_per_min=0.0,
                pacing_s=pacing if pacing is not None else load.pacing_s,
                # A ramp declared for a different virtual-user count would
                # climb to the wrong plateau, so an override drops it unless
                # the target matches. Being explicit beats a ramp that quietly
                # tops out at a tenth of what was asked for.
                ramp=load.ramp if (vus is None or vus == load.virtual_users) else [],
                duration_s=load.duration_s,
            )
        return load

    def _effective_duration(self) -> float:
        for candidate in (self.config.duration_s, self.load.duration_s):
            if candidate:
                return float(candidate)
        ramp_total = Ramp(
            self.load.ramp,
            float(self.load.virtual_users)
            if self.load.model == "closed"
            else float(self.load.arrival_rate_per_min),
        ).duration_s
        if ramp_total > 0:
            return ramp_total
        raise ValueError(
            "no run duration: set REG_DURATION_S, load.duration in the scenario, or a "
            "ramp with at least one timed stage"
        )

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def request_stop(self, reason: str = "requested") -> None:
        """Ask the run to wind down. Safe from a signal handler."""
        if not self._stop.is_set():
            log.info("stop requested: %s", reason)
            self._abort_reason = self._abort_reason or reason
            self._stop.set()

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------

    async def run(self) -> RunSummary:
        self._t0_monotonic = time.perf_counter()
        self._t0_wall = time.time()
        self.stats.started_at = self._t0_wall

        log.info(
            "starting %s: model=%s duration=%.0fs target=%s co_corrected=%s",
            self.scenario.name,
            self.load.model,
            self.duration_s,
            self.config.target.url,
            self.load.co_corrected,
        )

        try:
            if self.load.model == "closed":
                await self._run_closed()
            else:
                await self._run_open()
        except asyncio.CancelledError:
            self.request_stop("cancelled")
            raise
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised as a summary
            self._fatal = exc
            log.error("run failed: %s", exc)
        finally:
            await self._drain()

        return self._summarise()

    async def _run_closed(self) -> None:
        """Maintain a population of virtual users that follows the ramp."""
        next_tick_due = time.perf_counter() + TICK_S
        next_vu_id = self.config.slot * 1_000_000  # slot-disjoint ids, so a fleet's
        # virtual users never collide in the record even though each worker
        # numbers its own from zero.

        while not self._stop.is_set():
            elapsed = self._elapsed()
            if elapsed >= self.duration_s:
                break

            target = int(round(self.ramp.target_at(elapsed)))
            target = max(0, target)

            # Grow.
            while len(self._vu_tasks) < target:
                vu_id = next_vu_id
                next_vu_id += 1
                task = asyncio.create_task(self._virtual_user(vu_id), name=f"vu-{vu_id}")
                self._vu_tasks[vu_id] = task
                task.add_done_callback(lambda t, i=vu_id: self._vu_tasks.pop(i, None))
            self._peak_vus = max(self._peak_vus, len(self._vu_tasks))

            # Shrink. Cancelling mid-iteration would discard work already paid
            # for and skew the record, so retirement is co-operative: the task
            # is asked to finish its current iteration and stop.
            while len(self._vu_tasks) > target:
                vu_id, task = next(iter(sorted(self._vu_tasks.items(), reverse=True)))
                self._vu_tasks.pop(vu_id, None)
                task.cancel()

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass

            # How late was this tick? Under a slow target the loop is simply
            # awaiting and this stays near zero. Under CPU starvation it grows,
            # which is the condition the generator guard is for.
            now = time.perf_counter()
            self.stats.record_loop_lag((now - next_tick_due) * 1000.0)
            next_tick_due = max(now, next_tick_due + TICK_S)

            # Guards are evaluated AFTER the tick is measured, not before. A
            # single very late tick can be the last one before the duration
            # expires, and checking first meant the lag that proved the
            # generator was starved was recorded and then never looked at.
            self._check_guards()

    async def _run_open(self) -> None:
        """Issue arrivals on a schedule, whatever the target is doing.

        The schedule is computed from the ramp and never adjusted for how long
        work is taking. That is the entire point: if the target stalls, the
        arrivals keep coming and the queue is visible in the numbers.
        """
        vu_id = self.config.slot * 1_000_000
        iteration = 0
        next_arrival = self._t0_monotonic

        while not self._stop.is_set():
            now = time.perf_counter()
            elapsed = now - self._t0_monotonic
            if elapsed >= self.duration_s:
                break

            rate_per_min = max(0.0, self.ramp.target_at(elapsed))
            if rate_per_min <= 0:
                await asyncio.sleep(TICK_S)
                continue
            interval = 60.0 / rate_per_min

            if now < next_arrival:
                await asyncio.sleep(min(next_arrival - now, TICK_S))
                self._check_guards()
                continue

            # An arrival issued after it was due, while there was capacity to
            # issue it, is the generator failing to keep time. Shedding because
            # max_in_flight is reached is a different thing and is counted
            # separately as a missed arrival.
            if len(self._open_tasks) < self.config.max_in_flight:
                self.stats.record_loop_lag((now - next_arrival) * 1000.0)

            if len(self._open_tasks) >= self.config.max_in_flight:
                # Shedding rather than blocking. Blocking here would reintroduce
                # exactly the coordinated omission the open model exists to
                # avoid, so the arrival is counted as missed and the schedule
                # carries on. A run with missed arrivals is marked invalid,
                # because the generator, not the target, set the ceiling.
                self._arrivals_missed += 1
            else:
                persona = persona_for_vu(self.scenario.personas, vu_id, self.scenario.seed)
                job = _Iteration(
                    vu_id=vu_id,
                    persona=persona,
                    iteration=iteration,
                    intended_start=next_arrival,
                )
                task = asyncio.create_task(self._run_iteration(job), name=f"arrival-{iteration}")
                self._open_tasks.add(task)
                task.add_done_callback(self._open_tasks.discard)
                self._peak_vus = max(self._peak_vus, len(self._open_tasks))

            vu_id += 1
            iteration += 1
            next_arrival += interval
            # If we have fallen far behind the schedule, re-anchor rather than
            # firing a burst of overdue arrivals at a target that is already
            # struggling.
            if next_arrival < now - MAX_CATCHUP_S:
                skipped = int((now - next_arrival) / interval)
                self._arrivals_missed += skipped
                next_arrival = now
            self._check_guards()

    async def _virtual_user(self, vu_id: int) -> None:
        """One simulated person, looping until the run ends."""
        persona = persona_for_vu(self.scenario.personas, vu_id, self.scenario.seed)
        iteration = 0
        due = time.perf_counter()

        try:
            while not self._stop.is_set():
                if self._elapsed() >= self.duration_s:
                    return

                job = _Iteration(
                    vu_id=vu_id,
                    persona=persona,
                    iteration=iteration,
                    intended_start=due,
                )
                await self._run_iteration(job)
                iteration += 1
                self.stats.iterations_completed += 1

                if self.load.pacing_s > 0:
                    # Paced: the next iteration is due on the timetable,
                    # whatever this one cost. Falling behind is recorded rather
                    # than absorbed.
                    due += self.load.pacing_s
                    now = time.perf_counter()
                    if due < now - MAX_CATCHUP_S:
                        due = now
                    delay = due - now
                    if delay > 0:
                        await self._sleep_or_stop(delay)
                else:
                    think = think_time_for(
                        persona.think_time,
                        DrawContext(self.scenario.seed, vu_id, iteration, "_think"),
                        self.scenario.seed,
                    )
                    if think > 0:
                        await self._sleep_or_stop(think)
                    due = time.perf_counter()
        except asyncio.CancelledError:
            # Co-operative retirement during a ramp down. Not an error.
            return

    async def _run_iteration(self, job: _Iteration) -> None:
        """Execute every step of one iteration, in order."""
        intended = job.intended_start
        for step in job.persona.steps:
            if self._stop.is_set():
                return
            record = await self._run_step(job, step, intended)
            if record is None:
                return
            # Only the first step carries the schedule. A user asks the second
            # question after reading the first answer, so its intended start
            # genuinely is now.
            intended = time.perf_counter()

    async def _run_step(
        self, job: _Iteration, step: Step, intended_start: float
    ) -> Optional[StepRecord]:
        draw = DrawContext(
            scenario_seed=self.scenario.seed,
            vu_id=job.vu_id,
            iteration=job.iteration,
            step_id=step.id,
        )
        window = resolve_window(self.scenario.resolve_time_policy(step), draw)
        marker = cache_bust_marker(self.config.run_id, job.vu_id, job.iteration, step.id)

        source_text = step.spl if step.type == "search" else (step.dashboard or "")
        rendered, params = self.resolver.render(source_text or "", draw)
        if step.type == "search" and self.config.cache_bust:
            rendered = apply_cache_bust(rendered, marker)

        ctx = StepContext(
            spl_template=step.spl or "",
            run_id=self.config.run_id,
            slot=self.config.slot,
            vu_id=job.vu_id,
            iteration=job.iteration,
            persona=job.persona.name,
            step=step,
            window=window,
            spl=rendered,
            marker=marker,
            params=params,
        )

        self.stats.enter()
        began = time.perf_counter()
        try:
            record = await self.engine.execute(ctx)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - an engine that raises is fatal by contract
            self._fatal = exc
            self.request_stop(f"engine raised: {exc}")
            return None
        finally:
            self.stats.leave()

        finished = time.perf_counter()
        # The engine stamps service_time_ms before its own cleanup (deleting
        # the job on the search head), so `finished` includes a REST DELETE
        # that the service time does not. Reconstructing the completion instant
        # from the service time keeps latency and service time on the same
        # footing, so latency minus service time is schedule debt and nothing
        # else. The contamination was load-correlated, since the DELETE hits the
        # same saturated search head, so it inflated the tail most.
        if record.service_time_ms:
            finished = began + (record.service_time_ms / 1000.0)

        # The scheduler owns these three fields. See engines/base.py.
        record.intended_start = self._t0_wall + (intended_start - self._t0_monotonic)
        record.started_at = self._t0_wall + (began - self._t0_monotonic)
        record.late_by_ms = max(0.0, (began - intended_start) * 1000.0)
        record.co_corrected = self.load.co_corrected
        if self.load.co_corrected:
            record.latency_ms = (finished - intended_start) * 1000.0
        else:
            record.latency_ms = record.service_time_ms or (finished - began) * 1000.0

        self.stats.record(record)
        for emitter in self.emitters:
            try:
                emitter.emit(record)
            except Exception:  # noqa: BLE001 - telemetry must never break the run
                log.warning("emitter failed", exc_info=True)
        return record

    async def _sleep_or_stop(self, delay: float) -> None:
        """Sleep, but wake immediately if the run is stopping."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    def _elapsed(self) -> float:
        return time.perf_counter() - self._t0_monotonic

    # ------------------------------------------------------------------
    # Guard rails
    # ------------------------------------------------------------------

    def _check_guards(self) -> None:
        """Stop the run before it does damage, or before it lies.

        Two different jobs in one place. The first three predicates protect the
        target. The last protects the result: a worker that cannot keep its own
        schedule is measuring itself, so the run is stopped and marked invalid
        rather than reported.
        """
        guards = self.scenario.abort_if

        # Generator health is measured by whether THIS PROCESS can run its own
        # scheduling loop on time, not by how far behind the timetable the work
        # fell.
        #
        # Those are different things and conflating them was a real defect. In
        # the paced closed model, schedule debt is created by an iteration
        # overrunning its pacing interval, which happens because the TARGET is
        # slow. Guarding on it meant that any target running slower than the
        # pacing interval invalidated the run on its second iteration and
        # blamed the load box, discarding exactly the saturation measurement
        # the tool exists to take.
        #
        # Loop lag does not move when the target is slow (the loop is awaiting,
        # not working) and does move when this process is starved of CPU, which
        # is the condition the guard is for.
        if (
            guards.generator_drift_ms is not None
            and self.stats.max_loop_lag_ms > guards.generator_drift_ms
        ):
            self._invalid_reason = (
                f"the generator's own scheduling loop ran {self.stats.max_loop_lag_ms:.0f}ms "
                f"late (limit {guards.generator_drift_ms:.0f}ms): this worker was starved, "
                f"so the numbers describe it rather than the target"
            )
            self.request_stop("generator loop lag")
            return

        if self.stats.executions < MIN_SAMPLES_FOR_GUARD:
            return

        if guards.error_rate_pct is not None and self.stats.error_rate_pct > guards.error_rate_pct:
            self.request_stop(
                f"error rate {self.stats.error_rate_pct:.1f}% exceeded the "
                f"{guards.error_rate_pct:.1f}% ceiling"
            )
            return

        if guards.p95_ms is not None:
            p95 = self.stats.overall_latency.percentile_ms(95)
            if p95 > guards.p95_ms:
                self.request_stop(
                    f"p95 latency {p95:.0f}ms exceeded the {guards.p95_ms:.0f}ms ceiling"
                )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _drain(self) -> None:
        """Let in-flight work finish, within a budget.

        A single deadline for the whole drain, not one per task. Stoker learned
        this the hard way: per-task timeouts multiply, and a fleet that will
        not exit is worse than one that abandons a few records.
        """
        self._stop.set()
        pending = [t for t in list(self._vu_tasks.values()) + list(self._open_tasks) if not t.done()]
        if not pending:
            return
        log.info("draining %d in-flight tasks (budget %.0fs)", len(pending), DRAIN_BUDGET_S)
        done, still_pending = await asyncio.wait(pending, timeout=DRAIN_BUDGET_S)
        for task in still_pending:
            task.cancel()
        if still_pending:
            log.warning("cancelled %d tasks that overran the drain budget", len(still_pending))
            await asyncio.gather(*still_pending, return_exceptions=True)

    def _summarise(self) -> RunSummary:
        ended = time.time()

        if self._arrivals_missed and not self._invalid_reason:
            self._invalid_reason = (
                f"{self._arrivals_missed} scheduled arrivals were never issued because the "
                f"worker hit its in-flight ceiling of {self.config.max_in_flight}: the "
                f"generator set the ceiling, not the target"
            )

        if self._fatal is not None:
            outcome = OUTCOME_FAILED
        elif self._invalid_reason:
            outcome = OUTCOME_ABORTED
        elif self._abort_reason:
            outcome = OUTCOME_ABORTED if "exceeded" in self._abort_reason else OUTCOME_STOPPED
        else:
            outcome = OUTCOME_COMPLETED

        snapshot = self.stats.snapshot()
        snapshot["arrivals_missed"] = self._arrivals_missed
        if self.capabilities is not None:
            snapshot["target"] = self.capabilities.to_dict()

        return RunSummary(
            run_id=self.config.run_id,
            slot=self.config.slot,
            scenario=self.scenario.name,
            outcome=outcome,
            valid=self._invalid_reason is None and self._fatal is None,
            invalid_reason=self._invalid_reason
            or (f"the run failed: {self._fatal}" if self._fatal else None),
            started_at=self._t0_wall,
            ended_at=ended,
            duration_s=round(ended - self._t0_wall, 3),
            target_url=self.config.target.url,
            self_instrumented=self.config.self_instrumented,
            co_corrected=self.load.co_corrected,
            load_model=self.load.model,
            peak_virtual_users=self._peak_vus,
            stats=snapshot,
            abort_reason=self._abort_reason,
            scenario_seed=self.scenario.seed,
            agent_version=__version__,
        )
