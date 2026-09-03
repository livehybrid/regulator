"""What a step execution produces, and where it goes.

One :class:`StepRecord` per search dispatched or dashboard opened. It carries
both halves of the measurement, and carrying both is the point.

The **client half** is what the user experienced: how long from wanting the
answer to having it. The **server half** is what Splunk says it did: how long
the job ran, how many events it scanned, how many results came out. A load test
that reports only the client half tells you something got slower. A load test
that reports both tells you whether it got slower because it scanned four times
as much data, or because it sat in a dispatch queue, or because the search head
was starved of CPU. That difference is the entire value of the exercise.

Three destinations, and the split matters:

* NDJSON, one record per line, to stdout or a file. Always on, because a load
  generator whose output you can only see through a web UI is one you cannot
  debug at three in the morning.
* Splunk over HEC, optional. Historical benchmark data queryable next to
  everything else in the estate.
* An in-memory aggregate, always on, which is what the live charts and the
  abort predicates read.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, TextIO

from .histogram import LatencyHistogram

# Error taxonomy. Deliberately coarse: the value is in being able to say "the
# run degraded because of timeouts, not because of auth", and a hundred
# fine-grained categories make that harder to see rather than easier.
ERROR_AUTH = "auth"
ERROR_TIMEOUT = "timeout"
ERROR_QUOTA = "quota"
ERROR_PARSE = "parse"
ERROR_SERVER = "server"
ERROR_CLIENT = "client"
ERROR_CANCELLED = "cancelled"
ERROR_SEARCH_FAILED = "search_failed"

# A run ends in exactly one of these states.
OUTCOME_COMPLETED = "completed"
OUTCOME_STOPPED = "stopped"
OUTCOME_ABORTED = "aborted"
OUTCOME_FAILED = "failed"


@dataclass
class StepRecord:
    """One step execution, client half and server half."""

    # Identity and dimensions.
    run_id: str
    slot: int
    vu_id: int
    iteration: int
    persona: str
    step_id: str
    step_class: str = "unclassified"
    step_type: str = "search"
    engine: str = "api"

    # Timing, client half. Everything in milliseconds, everything measured on
    # the same monotonic clock so subtractions are meaningful.
    #
    # latency_ms versus service_time_ms is the coordinated-omission distinction.
    # service_time is how long the request took once it was issued. latency is
    # how long it took from when it *should* have been issued. Under a healthy
    # system they are the same. Under a stalled one, service_time flatters the
    # result by exactly the amount of queueing the generator absorbed on the
    # target's behalf, which is precisely the number a capacity test must not
    # hide.
    started_at: float = 0.0
    intended_start: float = 0.0
    latency_ms: float = 0.0
    service_time_ms: float = 0.0
    late_by_ms: float = 0.0
    co_corrected: bool = False

    dispatch_ms: Optional[float] = None
    ttfr_ms: Optional[float] = None
    queued_ms: Optional[float] = None
    results_fetch_ms: Optional[float] = None
    result_bytes: Optional[int] = None
    poll_count: int = 0

    # Timing, server half: taken from the job's own REST record.
    sid: Optional[str] = None
    run_duration_s: Optional[float] = None
    scan_count: Optional[int] = None
    event_count: Optional[int] = None
    result_count: Optional[int] = None
    dispatch_state: Optional[str] = None
    is_finalized: Optional[bool] = None

    # What was actually asked.
    earliest: Optional[float] = None
    latest: Optional[float] = None
    span_s: Optional[float] = None
    spl_hash: Optional[str] = None
    marker: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)

    # Outcome.
    ok: bool = True
    http_status: Optional[int] = None
    error_class: Optional[str] = None
    error_detail: Optional[str] = None
    # A job can finish DONE and still not have done the work asked of it: a
    # peer dropped out ("results might be incomplete"), the job was finalized
    # early, or a limit truncated it. splunkd says so in the job's messages
    # and answers HTTP 200 regardless. Those are the most misleading records a
    # capacity test can produce, because the search is fast precisely because
    # it did less, so they are flagged rather than filed as clean successes.
    partial: bool = False
    messages: Optional[List[str]] = None

    # Browser half, populated by the Phase 2 engine and left null by the API
    # engine. Declared here rather than in a subclass so one record shape
    # covers both channels and the analysis does not need to branch.
    ttfb_ms: Optional[float] = None
    dom_content_loaded_ms: Optional[float] = None
    load_ms: Optional[float] = None
    lcp_ms: Optional[float] = None
    first_panel_ms: Optional[float] = None
    panels: Optional[int] = None
    xhr_count: Optional[int] = None
    static_bytes: Optional[int] = None
    js_errors: Optional[int] = None
    sids: Optional[List[str]] = None

    @property
    def events_per_s(self) -> Optional[float]:
        """Splunk's own throughput metric for a search.

        Only meaningful when the job reported both numbers, which rules out
        oneshot and export dispatches, hence the None.
        """
        if not self.scan_count or not self.run_duration_s:
            return None
        return self.scan_count / self.run_duration_s

    def to_dict(self) -> Dict[str, Any]:
        record = asdict(self)
        eps = self.events_per_s
        if eps is not None:
            record["events_per_s"] = round(eps, 2)
        return record


class NdjsonEmitter:
    """One JSON object per line.

    Line-buffered and flushed per record when writing to a terminal, because a
    load test that buffers its output for 4 kB tells you nothing while it is
    running, which is when you want to know.
    """

    def __init__(self, stream: Optional[TextIO] = None, path: Optional[str] = None) -> None:
        self._own_stream = False
        if path:
            self._stream: TextIO = open(path, "a", encoding="utf-8")
            self._own_stream = True
        else:
            self._stream = stream or sys.stdout

    def emit(self, record: StepRecord) -> None:
        self._stream.write(json.dumps(record.to_dict(), separators=(",", ":")) + "\n")
        self._stream.flush()

    def emit_raw(self, payload: Dict[str, Any]) -> None:
        self._stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._stream.flush()

    def close(self) -> None:
        if self._own_stream:
            self._stream.close()


@dataclass
class StepStats:
    """Rolling aggregate for one step id."""

    step_id: str
    step_class: str
    executions: int = 0
    errors: int = 0
    latency: LatencyHistogram = field(default_factory=LatencyHistogram)
    service_time: LatencyHistogram = field(default_factory=LatencyHistogram)
    dispatch: LatencyHistogram = field(default_factory=LatencyHistogram)
    ttfr: LatencyHistogram = field(default_factory=LatencyHistogram)
    failure_latency: LatencyHistogram = field(default_factory=LatencyHistogram)
    scan_count_total: int = 0
    result_count_total: int = 0
    run_duration_total_s: float = 0.0
    errors_by_class: Dict[str, int] = field(default_factory=dict)
    partial: int = 0

    def add(self, record: StepRecord) -> None:
        self.executions += 1
        if record.partial:
            self.partial += 1
        # Only successful executions feed the latency histograms.
        #
        # A search head at its admission ceiling refuses work in the time of one
        # POST, so a few tens of milliseconds. Mixing those into the latency
        # distribution makes REPORTED LATENCY IMPROVE AS THE TARGET FAILS: at
        # 96% refusals the p95 is the refusal time, the progress line shows the
        # numbers getting better while the cluster collapses, and a p95 guard
        # never fires. Failure latency is kept separately, because how fast a
        # target refuses is worth knowing and is not the same measurement.
        if record.ok:
            self.latency.record_ms(record.latency_ms)
            self.service_time.record_ms(record.service_time_ms)
            if record.dispatch_ms is not None:
                self.dispatch.record_ms(record.dispatch_ms)
            if record.ttfr_ms is not None:
                self.ttfr.record_ms(record.ttfr_ms)
        else:
            self.failure_latency.record_ms(record.service_time_ms)
        # Server-side totals come from successful jobs only. A refused or
        # timed-out dispatch has no scan count worth adding, and a failed job
        # that scanned half an index before dying would otherwise inflate the
        # per-search scan figure the comparison relies on.
        if record.ok:
            if record.scan_count:
                self.scan_count_total += record.scan_count
            if record.result_count:
                self.result_count_total += record.result_count
            if record.run_duration_s:
                self.run_duration_total_s += record.run_duration_s
        if not record.ok:
            self.errors += 1
            cls = record.error_class or ERROR_CLIENT
            self.errors_by_class[cls] = self.errors_by_class.get(cls, 0) + 1

    @property
    def successes(self) -> int:
        return self.executions - self.errors

    def histograms(self) -> Dict[str, Dict[str, Any]]:
        """The raw histograms, for merging across a fleet.

        Percentiles cannot be averaged across workers; the buckets can be
        added. This is what a worker ships in its final report so the control
        plane computes the fleet's percentiles once, over the merged whole.
        """
        return {
            "latency": self.latency.to_dict(),
            "service_time": self.service_time.to_dict(),
            "dispatch": self.dispatch.to_dict(),
            "ttfr": self.ttfr.to_dict(),
            "failure_latency": self.failure_latency.to_dict(),
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "class": self.step_class,
            "executions": self.executions,
            "successes": self.successes,
            "partial": self.partial,
            "errors": self.errors,
            "error_rate_pct": round(100.0 * self.errors / self.executions, 3) if self.executions else 0.0,
            "errors_by_class": dict(self.errors_by_class),
            "latency": self.latency.summary(),
            "failure_latency": self.failure_latency.summary(),
            "service_time": self.service_time.summary(),
            "dispatch": self.dispatch.summary(),
            "ttfr": self.ttfr.summary(),
            "scan_count_total": self.scan_count_total,
            # Per successful search, so two runs of different length or speed
            # can be compared on how much work each search did rather than on
            # how many searches the run managed to fit in.
            "scan_count_per_search": (
                round(self.scan_count_total / self.successes, 1) if self.successes else None
            ),
            "result_count_total": self.result_count_total,
            "mean_events_per_s": (
                round(self.scan_count_total / self.run_duration_total_s, 1)
                if self.run_duration_total_s
                else None
            ),
        }


class RunStats:
    """Live aggregate over a whole run.

    Read by three consumers: the periodic progress line, the abort predicates,
    and (in Phase 1) the heartbeat that feeds the control plane's live charts.
    All three want the same numbers, so they share one aggregate rather than
    three that can disagree.
    """

    def __init__(self, run_id: str, slot: int = 0) -> None:
        self.run_id = run_id
        self.slot = slot
        self.started_at = time.time()
        self.steps: Dict[str, StepStats] = {}
        self.executions = 0
        self.errors = 0
        self.errors_by_class: Dict[str, int] = {}
        # Successful executions only. See StepStats.add for why.
        self.overall_latency = LatencyHistogram()
        self.failure_latency = LatencyHistogram()
        # Generator health. If these go bad, the run is measuring the load
        # generator rather than Splunk, and the result is invalid rather than
        # merely disappointing.
        # Schedule debt: how far behind its timetable the work fell. This is a
        # TARGET signal in the paced closed model, because an iteration that
        # overruns its pacing interval does so because the searches were slow.
        self.max_drift_ms = 0.0
        self.drift = LatencyHistogram()
        # Generator health: whether this process could run its own scheduling
        # loop on time. Independent of how slow the target is, which is exactly
        # why the two are kept apart. Conflating them blames a saturated
        # cluster on the load box and throws the run away.
        self.max_loop_lag_ms = 0.0
        self.loop_lag = LatencyHistogram()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.iterations_completed = 0
        # Queueing is a measurement, not a failure. When load crosses the
        # target's concurrent-search ceiling, splunkd starts holding searches in
        # QUEUED before running them: that is the exact moment a capacity test
        # is looking for, so it is counted rather than treated as an error.
        self.queued_executions = 0
        self.queued = LatencyHistogram()
        # Executions that could have reported queueing at all: a search
        # dispatched as a job and polled to completion. Failures, oneshots and
        # dashboard loads never set queued_ms, so counting them in the
        # denominator understates the queueing rate exactly when it matters.
        self.job_executions = 0
        self.partial = 0
        # Work that was still in flight when the run ended and had to be
        # abandoned. Recorded as failures with the time they had accrued, and
        # counted here so the summary can say the tail is a floor.
        self.abandoned = 0

    def record(self, record: StepRecord) -> None:
        stats = self.steps.get(record.step_id)
        if stats is None:
            stats = StepStats(step_id=record.step_id, step_class=record.step_class)
            self.steps[record.step_id] = stats
        stats.add(record)

        self.executions += 1
        if record.partial:
            self.partial += 1
        if record.error_class == ERROR_CANCELLED:
            self.abandoned += 1
        if record.ok and record.sid and record.step_type == "search":
            self.job_executions += 1
        if record.ok:
            self.overall_latency.record_ms(record.latency_ms)
        else:
            self.failure_latency.record_ms(record.service_time_ms)
        if record.late_by_ms:
            self.drift.record_ms(record.late_by_ms)
            self.max_drift_ms = max(self.max_drift_ms, record.late_by_ms)
        if record.queued_ms:
            self.queued_executions += 1
            self.queued.record_ms(record.queued_ms)
        if not record.ok:
            self.errors += 1
            cls = record.error_class or ERROR_CLIENT
            self.errors_by_class[cls] = self.errors_by_class.get(cls, 0) + 1

    def record_loop_lag(self, lag_ms: float) -> None:
        """How late the scheduling loop was for its own tick.

        The honest measure of whether the generator is keeping up: it rises
        when this process is starved of CPU and does not move when the target
        is merely slow.
        """
        if lag_ms <= 0:
            return
        self.loop_lag.record_ms(lag_ms)
        self.max_loop_lag_ms = max(self.max_loop_lag_ms, lag_ms)

    def enter(self) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def leave(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    @property
    def error_rate_pct(self) -> float:
        return 100.0 * self.errors / self.executions if self.executions else 0.0

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    def histograms(self) -> Dict[str, Any]:
        """Every histogram in the aggregate, keyed for a fleet merge."""
        return {
            "latency": self.overall_latency.to_dict(),
            "failure_latency": self.failure_latency.to_dict(),
            "queued": self.queued.to_dict(),
            "loop_lag": self.loop_lag.to_dict(),
            "drift": self.drift.to_dict(),
            "steps": {step_id: stats.histograms() for step_id, stats in self.steps.items()},
        }

    def snapshot(self, include_histograms: bool = False) -> Dict[str, Any]:
        document = self._snapshot()
        if include_histograms:
            document["histograms"] = self.histograms()
        return document

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "slot": self.slot,
            "elapsed_s": round(self.elapsed_s, 2),
            "executions": self.executions,
            "successes": self.executions - self.errors,
            "iterations_completed": self.iterations_completed,
            "errors": self.errors,
            "error_rate_pct": round(self.error_rate_pct, 3),
            "errors_by_class": dict(self.errors_by_class),
            "partial": self.partial,
            "abandoned": self.abandoned,
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "throughput_per_s": (
                round(self.executions / self.elapsed_s, 2) if self.elapsed_s > 0 else 0.0
            ),
            "latency": self.overall_latency.summary(),
            "failure_latency": self.failure_latency.summary(),
            "queueing": {
                "searches_queued": self.queued_executions,
                # Over the searches that could have queued, not over every
                # execution: see job_executions above.
                "job_executions": self.job_executions,
                "queued_pct": (
                    round(100.0 * self.queued_executions / self.job_executions, 2)
                    if self.job_executions
                    else 0.0
                ),
                "queued_ms": self.queued.summary(),
            },
            "generator": {
                # Generator health. These are about this process.
                "max_loop_lag_ms": round(self.max_loop_lag_ms, 1),
                "loop_lag_p95_ms": round(self.loop_lag.percentile_ms(95), 1),
                # Schedule debt. This is about the target: how far behind the
                # timetable its slowness pushed the work.
                "max_schedule_debt_ms": round(self.max_drift_ms, 1),
                "schedule_debt_p95_ms": round(self.drift.percentile_ms(95), 1),
                # Kept under the old name so nothing downstream breaks, but it
                # is the debt rather than generator lag: see above.
                "max_drift_ms": round(self.max_drift_ms, 1),
            },
            "steps": [s.summary() for s in self.steps.values()],
        }


@dataclass
class RunSummary:
    """The final word on a run.

    ``valid`` is the field that matters most and the one most load tools do not
    have. A run where the generator could not keep to its own schedule, or
    where a worker saturated its CPU, produced real numbers about the wrong
    system. Marking it invalid is more useful than reporting it, because a
    silently invalid benchmark is worse than no benchmark.
    """

    run_id: str
    slot: int
    scenario: str
    outcome: str
    valid: bool
    invalid_reason: Optional[str]
    started_at: float
    ended_at: float
    duration_s: float
    target_url: str
    self_instrumented: bool
    co_corrected: bool
    load_model: str
    peak_virtual_users: int
    stats: Dict[str, Any]
    # SmartStore provenance. Without it, a fast run and a slow run of the same
    # scenario are not comparable and nobody can tell why.
    cache: Optional[Dict[str, Any]] = None
    # What the cluster itself recorded: its account of our searches, where the
    # time went by phase, scheduled searches it had to skip, and its own CPU.
    sut: Optional[Dict[str, Any]] = None
    abort_reason: Optional[str] = None
    scenario_seed: int = 0
    # The seed the draws actually used. An override (REG_SEED) changes every
    # draw, and reporting only the scenario's own seed let two runs with
    # different overrides compare as the same workload.
    effective_seed: int = 0
    # Content address of the scenario files, so "same scenario name" and
    # "same test" can be told apart.
    scenario_digest: Optional[str] = None
    # What was asked for, as distinct from what happened: virtual users or
    # arrival rate, pacing, arrival process. peak_virtual_users is an outcome
    # in the open model and comparing outcomes as if they were settings
    # produced spurious "different concurrency" warnings.
    configured_load: Dict[str, Any] = field(default_factory=dict)
    agent_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def summarise(records: Iterable[StepRecord]) -> Dict[str, Any]:
    """Aggregate a finished set of records, for offline analysis of NDJSON."""
    stats = RunStats(run_id="offline")
    for record in records:
        stats.record(record)
    return stats.snapshot()
