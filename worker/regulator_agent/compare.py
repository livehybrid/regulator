"""Comparing two runs, and deciding whether the difference matters.

A single run is a number. Two runs are a comparison, which is the entire point:
"is this cluster shape faster than that one", "did this release make search
slower", "does adding two indexers help". This module turns a pair of run
summaries into a per-step delta and a verdict against gates the operator wrote.

Three things it refuses to do, each of which would make it lie.

**It will not compare an invalid run.** A run whose generator could not keep to
its own schedule measured the load box, not Splunk. Comparing it to anything is
meaningless, so the comparison fails with that as the reason rather than
producing a confident percentage.

**It will not silently compare a warm run to a cold one.** On SmartStore those
are different pieces of work, sometimes by an order of magnitude. If the cache
provenance differs the comparison still runs, because sometimes that is exactly
what you are measuring, but it is flagged loudly enough that nobody reads the
percentage without seeing it.

**It will not compare different workloads.** A different scenario, or the same
scenario with a different seed or a different virtual-user count, is a different
question. Those differences are reported as warnings alongside the result.

The gate language is deliberately small. Anything a person cannot read aloud in
a code review is a gate that will eventually be misread::

    p95 <= baseline + 15%      the common one: no more than 15% slower
    p95 <= 5000ms              an absolute ceiling
    error_rate <= 2%
    throughput >= baseline - 10%
    queued == 0                nothing waited at the concurrency ceiling
    valid                      the run measured Splunk rather than itself
    p95[rare-bucket-policy] <= baseline + 25%    one step, not the whole run
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Metrics a gate can name. Latency percentiles come from the merged histogram,
# so they are the whole run rather than an average of parts.
_RUN_METRICS = {
    "p50": ("latency", "p50_ms"),
    "p90": ("latency", "p90_ms"),
    "p95": ("latency", "p95_ms"),
    "p99": ("latency", "p99_ms"),
    "mean": ("latency", "mean_ms"),
    "max": ("latency", "max_ms"),
}

# Where a bigger number is better, so "worse" means down rather than up.
_HIGHER_IS_BETTER = {"throughput", "executions"}

_GATE_RE = re.compile(
    r"""^\s*
    (?P<metric>[a-z_0-9]+)
    (?:\[(?P<step>[^\]]+)\])?
    \s*
    (?:
        (?P<op>&lt;=|>=|<=|<|>|==|!=)
        \s*
        (?P<threshold>.+?)
    )?
    \s*$""",
    re.VERBOSE,
)

_BASELINE_RE = re.compile(
    r"^baseline\s*(?P<sign>[+-])\s*(?P<amount>[0-9.]+)\s*(?P<unit>%|ms|s)?$", re.IGNORECASE
)
_ABSOLUTE_RE = re.compile(r"^(?P<amount>[0-9.]+)\s*(?P<unit>%|ms|s)?$", re.IGNORECASE)


class GateError(ValueError):
    """A gate expression that cannot be parsed. Fatal, and names the expression."""


@dataclass
class Gate:
    raw: str
    metric: str
    op: str
    step: Optional[str] = None
    # Exactly one of these is set.
    relative_pct: Optional[float] = None
    relative_abs: Optional[float] = None
    absolute: Optional[float] = None
    # A bare `valid` or `queued == 0` style boolean gate.
    boolean: bool = False


def parse_gate(expression: str) -> Gate:
    match = _GATE_RE.match(expression.replace("&lt;", "<"))
    if not match:
        raise GateError(f"cannot parse the gate {expression!r}")

    metric = match.group("metric")
    step = match.group("step")
    op = match.group("op")
    threshold = (match.group("threshold") or "").strip()

    if op is None:
        # A bare metric name is a truthiness gate: `valid`.
        return Gate(raw=expression, metric=metric, op="==", step=step, boolean=True, absolute=1.0)

    baseline = _BASELINE_RE.match(threshold)
    if baseline:
        amount = float(baseline.group("amount"))
        if baseline.group("sign") == "-":
            amount = -amount
        unit = (baseline.group("unit") or "%").lower()
        if unit == "%":
            return Gate(raw=expression, metric=metric, op=op, step=step, relative_pct=amount)
        seconds = 1000.0 if unit == "s" else 1.0
        return Gate(
            raw=expression, metric=metric, op=op, step=step, relative_abs=amount * seconds
        )

    absolute = _ABSOLUTE_RE.match(threshold)
    if absolute:
        amount = float(absolute.group("amount"))
        unit = (absolute.group("unit") or "").lower()
        if unit == "s":
            amount *= 1000.0
        return Gate(raw=expression, metric=metric, op=op, step=step, absolute=amount)

    raise GateError(
        f"cannot parse the threshold {threshold!r} in {expression!r}. Expected something "
        "like 'baseline + 15%', '5000ms', '2%' or '0'"
    )


# ---------------------------------------------------------------------------


def _stats(summary: Dict[str, Any]) -> Dict[str, Any]:
    return summary.get("stats") or {}


def metric_value(summary: Dict[str, Any], metric: str, step: Optional[str] = None) -> Optional[float]:
    """Pull one number out of a run summary, for the whole run or for one step."""
    stats = _stats(summary)

    if step:
        steps = {s.get("step_id"): s for s in stats.get("steps") or []}
        entry = steps.get(step)
        if entry is None:
            return None
        if metric in _RUN_METRICS:
            return (entry.get("latency") or {}).get(_RUN_METRICS[metric][1])
        if metric == "error_rate":
            return entry.get("error_rate_pct")
        if metric == "executions":
            return entry.get("executions")
        if metric == "dispatch_p95":
            return (entry.get("dispatch") or {}).get("p95_ms")
        if metric == "ttfr_p95":
            return (entry.get("ttfr") or {}).get("p95_ms")
        return None

    if metric in _RUN_METRICS:
        section, key = _RUN_METRICS[metric]
        return (stats.get(section) or {}).get(key)
    if metric == "throughput":
        return stats.get("throughput_per_s")
    if metric == "error_rate":
        return stats.get("error_rate_pct")
    if metric == "executions":
        return stats.get("executions")
    if metric == "queued":
        return (stats.get("queueing") or {}).get("searches_queued")
    if metric == "queued_pct":
        return (stats.get("queueing") or {}).get("queued_pct")
    if metric == "drift":
        return (stats.get("generator") or {}).get("max_drift_ms")
    if metric == "valid":
        return 1.0 if summary.get("valid") else 0.0
    return None


def _compare(op: str, actual: float, limit: float) -> bool:
    if op == "<=":
        return actual <= limit
    if op == "<":
        return actual < limit
    if op == ">=":
        return actual >= limit
    if op == ">":
        return actual > limit
    if op == "==":
        return actual == limit
    if op == "!=":
        return actual != limit
    raise GateError(f"unknown operator {op!r}")


@dataclass
class GateResult:
    gate: str
    passed: bool
    metric: str
    step: Optional[str]
    actual: Optional[float]
    limit: Optional[float]
    baseline: Optional[float]
    detail: str


@dataclass
class StepDelta:
    step_id: str
    step_class: str
    baseline_p95_ms: Optional[float]
    candidate_p95_ms: Optional[float]
    delta_ms: Optional[float]
    delta_pct: Optional[float]
    baseline_executions: int = 0
    candidate_executions: int = 0
    baseline_scan_count: Optional[int] = None
    candidate_scan_count: Optional[int] = None

    @property
    def scanned_more(self) -> bool:
        """Did the candidate simply do more work?

        The distinction that settles most arguments about a benchmark: latency
        up with scan count flat is contention or queueing, latency up with scan
        count up means the workload changed and the comparison was never valid.
        """
        if not self.baseline_scan_count or not self.candidate_scan_count:
            return False
        return self.candidate_scan_count > self.baseline_scan_count * 1.1


@dataclass
class Comparison:
    ok: bool
    blocked: Optional[str]
    warnings: List[str] = field(default_factory=list)
    gates: List[GateResult] = field(default_factory=list)
    steps: List[StepDelta] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)

    @property
    def failed_gates(self) -> List[GateResult]:
        return [g for g in self.gates if not g.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "blocked": self.blocked,
            "warnings": list(self.warnings),
            "gates": [g.__dict__ for g in self.gates],
            "steps": [
                {**s.__dict__, "scanned_more": s.scanned_more} for s in self.steps
            ],
            "summary": dict(self.summary),
        }

    def explain(self) -> str:
        """A report a human reads in a pull request, not a JSON blob."""
        lines: List[str] = []
        if self.blocked:
            lines.append(f"COMPARISON BLOCKED: {self.blocked}")
            return "\n".join(lines)

        verdict = "PASS" if self.ok else "FAIL"
        lines.append(f"{verdict}: {len(self.gates) - len(self.failed_gates)}/{len(self.gates)} gates met")
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        for gate in self.gates:
            mark = "ok  " if gate.passed else "FAIL"
            lines.append(f"  {mark} {gate.gate}: {gate.detail}")

        if self.steps:
            lines.append("")
            lines.append(f"  {'step':<28} {'baseline':>10} {'candidate':>10} {'delta':>10}")
            for step in sorted(
                self.steps, key=lambda s: (s.delta_pct if s.delta_pct is not None else -1e9),
                reverse=True,
            ):
                if step.candidate_p95_ms is None or step.baseline_p95_ms is None:
                    continue
                note = "  (scanned more)" if step.scanned_more else ""
                lines.append(
                    f"  {step.step_id[:28]:<28} {step.baseline_p95_ms:>9.0f}ms "
                    f"{step.candidate_p95_ms:>9.0f}ms {step.delta_pct:>+9.1f}%{note}"
                )
        return "\n".join(lines)


def compare_runs(
    candidate: Dict[str, Any],
    baseline: Optional[Dict[str, Any]] = None,
    gates: Optional[List[str]] = None,
    allow_invalid: bool = False,
) -> Comparison:
    """Compare a run against a baseline and judge it against gates."""
    parsed = [parse_gate(g) for g in (gates or [])]
    comparison = Comparison(ok=True, blocked=None)

    # An invalid run measured the load generator rather than Splunk. Comparing
    # it to anything produces a confident number about the wrong system.
    if not candidate.get("valid", True) and not allow_invalid:
        comparison.ok = False
        comparison.blocked = (
            f"the candidate run is invalid and cannot be compared: "
            f"{candidate.get('invalid_reason') or 'no reason recorded'}"
        )
        return comparison
    if baseline is not None and not baseline.get("valid", True) and not allow_invalid:
        comparison.ok = False
        comparison.blocked = (
            f"the baseline run is invalid and cannot be compared against: "
            f"{baseline.get('invalid_reason') or 'no reason recorded'}"
        )
        return comparison

    if baseline is not None:
        comparison.warnings.extend(_workload_warnings(candidate, baseline))
        comparison.steps = _step_deltas(candidate, baseline)

    for gate in parsed:
        comparison.gates.append(_evaluate(gate, candidate, baseline))

    comparison.ok = all(g.passed for g in comparison.gates)
    comparison.summary = {
        "candidate": _headline(candidate),
        "baseline": _headline(baseline) if baseline else None,
    }
    return comparison


def _headline(summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if summary is None:
        return {}
    stats = _stats(summary)
    cache = ((summary.get("cache") or {}).get("delta") or {})
    return {
        "scenario": summary.get("scenario"),
        "outcome": summary.get("outcome"),
        "valid": summary.get("valid"),
        "executions": stats.get("executions"),
        "p95_ms": (stats.get("latency") or {}).get("p95_ms"),
        "throughput_per_s": stats.get("throughput_per_s"),
        "error_rate_pct": stats.get("error_rate_pct"),
        "queued": (stats.get("queueing") or {}).get("searches_queued"),
        "cache_provenance": cache.get("provenance"),
        "co_corrected": summary.get("co_corrected"),
        "peak_virtual_users": summary.get("peak_virtual_users"),
    }


def _workload_warnings(candidate: Dict[str, Any], baseline: Dict[str, Any]) -> List[str]:
    """Everything that makes two runs less comparable than they look."""
    warnings: List[str] = []

    if candidate.get("scenario") != baseline.get("scenario"):
        warnings.append(
            f"different scenarios ({baseline.get('scenario')} versus "
            f"{candidate.get('scenario')}): this is a different question, not a regression"
        )
    if candidate.get("scenario_seed") != baseline.get("scenario_seed"):
        warnings.append(
            "different seeds, so the two runs issued different searches over different "
            "time windows. The comparison is between workloads, not between clusters"
        )
    if candidate.get("peak_virtual_users") != baseline.get("peak_virtual_users"):
        warnings.append(
            f"different concurrency ({baseline.get('peak_virtual_users')} versus "
            f"{candidate.get('peak_virtual_users')} virtual users)"
        )
    if candidate.get("co_corrected") != baseline.get("co_corrected"):
        warnings.append(
            "one run is coordinated-omission corrected and the other is not, so their "
            "latency percentiles do not mean the same thing"
        )

    # The SmartStore one, which can be an order of magnitude.
    candidate_cache = ((candidate.get("cache") or {}).get("delta") or {}).get("provenance")
    baseline_cache = ((baseline.get("cache") or {}).get("delta") or {}).get("provenance")
    if candidate_cache and baseline_cache and candidate_cache != baseline_cache:
        warnings.append(
            f"cache provenance differs ({baseline_cache} versus {candidate_cache}): on "
            "SmartStore a cold run reads from object storage and a warm one reads local "
            "disk, which can differ by an order of magnitude"
        )

    if candidate.get("target_url") != baseline.get("target_url"):
        warnings.append(
            f"different targets ({baseline.get('target_url')} versus "
            f"{candidate.get('target_url')})"
        )
    return warnings


def _step_deltas(candidate: Dict[str, Any], baseline: Dict[str, Any]) -> List[StepDelta]:
    base_steps = {s.get("step_id"): s for s in _stats(baseline).get("steps") or []}
    deltas: List[StepDelta] = []
    for step in _stats(candidate).get("steps") or []:
        step_id = step.get("step_id")
        base = base_steps.get(step_id) or {}
        candidate_p95 = (step.get("latency") or {}).get("p95_ms")
        baseline_p95 = (base.get("latency") or {}).get("p95_ms")
        delta_ms = None
        delta_pct = None
        if candidate_p95 is not None and baseline_p95:
            delta_ms = candidate_p95 - baseline_p95
            delta_pct = 100.0 * delta_ms / baseline_p95
        deltas.append(
            StepDelta(
                step_id=step_id,
                step_class=step.get("class", "unclassified"),
                baseline_p95_ms=baseline_p95,
                candidate_p95_ms=candidate_p95,
                delta_ms=delta_ms,
                delta_pct=delta_pct,
                baseline_executions=base.get("executions", 0),
                candidate_executions=step.get("executions", 0),
                baseline_scan_count=base.get("scan_count_total"),
                candidate_scan_count=step.get("scan_count_total"),
            )
        )
    return deltas


def _evaluate(
    gate: Gate, candidate: Dict[str, Any], baseline: Optional[Dict[str, Any]]
) -> GateResult:
    actual = metric_value(candidate, gate.metric, gate.step)
    if actual is None:
        return GateResult(
            gate=gate.raw,
            passed=False,
            metric=gate.metric,
            step=gate.step,
            actual=None,
            limit=None,
            baseline=None,
            detail=(
                f"the run has no {gate.metric!r}"
                + (f" for step {gate.step!r}" if gate.step else "")
            ),
        )

    baseline_value: Optional[float] = None
    if gate.relative_pct is not None or gate.relative_abs is not None:
        if baseline is None:
            return GateResult(
                gate=gate.raw,
                passed=False,
                metric=gate.metric,
                step=gate.step,
                actual=actual,
                limit=None,
                baseline=None,
                detail="this gate is relative to a baseline, and no baseline was given",
            )
        baseline_value = metric_value(baseline, gate.metric, gate.step)
        if baseline_value is None:
            return GateResult(
                gate=gate.raw,
                passed=False,
                metric=gate.metric,
                step=gate.step,
                actual=actual,
                limit=None,
                baseline=None,
                detail=f"the baseline has no {gate.metric!r} to compare against",
            )
        if gate.relative_pct is not None:
            limit = baseline_value * (1.0 + gate.relative_pct / 100.0)
        else:
            limit = baseline_value + (gate.relative_abs or 0.0)
    else:
        limit = gate.absolute if gate.absolute is not None else 0.0

    passed = _compare(gate.op, actual, limit)

    if gate.boolean:
        detail = "the run is valid" if passed else "the run is not valid"
    elif baseline_value is not None:
        change = (
            100.0 * (actual - baseline_value) / baseline_value if baseline_value else 0.0
        )
        direction = "better" if (
            (change < 0) != (gate.metric in _HIGHER_IS_BETTER)
        ) else "worse"
        detail = (
            f"{actual:.1f} versus a baseline of {baseline_value:.1f} "
            f"({change:+.1f}%, {direction}), limit {limit:.1f}"
        )
    else:
        detail = f"{actual:.1f} against a limit of {limit:.1f}"

    return GateResult(
        gate=gate.raw,
        passed=passed,
        metric=gate.metric,
        step=gate.step,
        actual=actual,
        limit=limit,
        baseline=baseline_value,
        detail=detail,
    )
