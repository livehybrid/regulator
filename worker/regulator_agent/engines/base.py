"""The contract between the scheduler and an engine.

The scheduler owns *when* work happens: virtual users, ramps, think time,
pacing and the corrected-latency arithmetic. An engine owns *how* one step is
executed: opening the connection, dispatching, polling, reading the answer.

Keeping that line clean is what lets the browser engine arrive in Phase 2
without the scheduler learning anything about browsers, and it is what lets the
scheduler be tested against a fake engine with no Splunk anywhere.

Division of labour on timing, which is the part most likely to be got wrong by
whoever touches this next:

* The **scheduler** decides ``intended_start`` and, after the engine returns,
  fills in ``latency_ms``, ``late_by_ms`` and ``co_corrected``. Only the
  scheduler knows what the schedule was.
* The **engine** fills ``service_time_ms`` (its own wall time), and everything
  underneath it: ``dispatch_ms``, ``ttfr_ms``, ``queued_ms``,
  ``results_fetch_ms``, the server-side job fields, and the outcome.

An engine must never raise for an ordinary failure. A timeout, a 503, a search
that failed to parse: all of those are results, and a load test that stops at
the first error measures nothing useful. Return a record with ``ok=False`` and
an ``error_class``. Raising is reserved for a genuinely unrecoverable
condition, such as the target's credentials being rejected outright, where
continuing would produce thousands of identical meaningless failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from ..results import StepRecord
from ..scenario import Scenario, Step
from ..timepolicy import TimeWindow


@dataclass(frozen=True)
class StepContext:
    """Everything an engine needs to execute one step.

    Note what is already resolved by the time this arrives: the SPL has had its
    placeholders substituted and its cache-busting marker appended, and the
    time window is a pair of absolute epochs. An engine does no templating and
    no clock arithmetic, which means the exact search that was dispatched is
    recoverable from the record without re-running any of that logic.
    """

    run_id: str
    slot: int
    vu_id: int
    iteration: int
    persona: str
    step: Step
    window: TimeWindow
    spl: str
    marker: str
    # The search BEFORE parameter substitution. Hashing the rendered SPL gives
    # a different identity for every draw, so `stats by spl_hash` produced one
    # group per execution and no per-search analysis was possible at all, which
    # is most of the value of the record.
    spl_template: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    # Where an engine leaves the record when it cannot return it: on
    # cancellation the engine must re-raise, so the record of the abandoned
    # search rides here for the scheduler to file. A list on a frozen
    # dataclass is the one mutable slot the contract allows.
    outcome: List[StepRecord] = field(default_factory=list)

    def blank_record(self) -> StepRecord:
        """A record pre-filled with the dimensions, ready for an engine to complete."""
        return StepRecord(
            run_id=self.run_id,
            slot=self.slot,
            vu_id=self.vu_id,
            iteration=self.iteration,
            persona=self.persona,
            step_id=self.step.id,
            step_class=self.step.step_class,
            step_type=self.step.type,
            engine=self.step.engine,
            earliest=self.window.earliest,
            latest=self.window.latest,
            span_s=self.window.span_s,
            marker=self.marker,
            params=dict(self.params),
        )


@dataclass
class TargetCapabilities:
    """What the probe found out about the system under test.

    The concurrency ceiling is the interesting one. Splunk computes it as
    ``base_max_searches + (max_searches_per_cpu * cpu_count)``, and on a search
    head cluster the scheduler is separately limited to ``max_searches_perc``
    of that. Knowing the number before the run starts means the report can draw
    the ceiling as a line on the concurrency chart, so the moment load crosses
    into queueing is visible rather than inferred.
    """

    version: str = ""
    build: str = ""
    server_name: str = ""
    cpu_count: int = 0
    server_roles: List[str] = field(default_factory=list)
    base_max_searches: Optional[int] = None
    max_searches_per_cpu: Optional[int] = None
    max_searches_perc: Optional[int] = None
    auth_method: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def max_hist_searches(self) -> Optional[int]:
        if self.base_max_searches is None or self.max_searches_per_cpu is None or not self.cpu_count:
            return None
        return self.base_max_searches + (self.max_searches_per_cpu * self.cpu_count)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "build": self.build,
            "server_name": self.server_name,
            "cpu_count": self.cpu_count,
            "server_roles": list(self.server_roles),
            "base_max_searches": self.base_max_searches,
            "max_searches_per_cpu": self.max_searches_per_cpu,
            "max_searches_perc": self.max_searches_perc,
            "max_hist_searches": self.max_hist_searches,
            "auth_method": self.auth_method,
            "notes": list(self.notes),
        }


@runtime_checkable
class Engine(Protocol):
    """What the scheduler requires of an engine."""

    name: str

    async def start(self) -> None:
        """Open connections, authenticate, warm pools. Called once."""

    async def probe(self) -> TargetCapabilities:
        """Ask the target what it is and what its limits are. Called once, before load."""

    async def validate(self, scenario: Scenario) -> List[str]:
        """Online lint: parse every SPL against the target, check what exists.

        Returns a list of human-readable problems, empty when the scenario is
        runnable. This is the half of linting that needs a connection, and it
        is the half that catches the expensive mistakes: a sourcetype that was
        renamed, an index the load-test account cannot read, a dashboard that
        does not exist in this environment.
        """

    async def resolve_parameters(self, resolver: Any) -> None:
        """Run any ``choice_from_search`` resolvers and bind their values."""

    async def execute(self, ctx: StepContext) -> StepRecord:
        """Execute one step and return its record. Must not raise for ordinary failures."""

    async def close(self) -> None:
        """Release connections. Called once, and must be safe to call twice."""
