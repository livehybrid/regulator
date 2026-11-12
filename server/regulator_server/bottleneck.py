"""Where the limit was: one reading of everything a run already collected.

A finished run carries a dozen separate signals: how well the generator kept
time, how many searches waited to be admitted, what the search heads and the
indexers were doing with their CPU, memory and disks, what the bucket cache
cost, and what failed. Each is on the run page, and reading them against each
other is the actual job. This module does that reading and says what it found,
with the evidence beside it.

It is rules, not a model, and deliberately plain:

* Each candidate limit is scored 0 to 1 from thresholds stated below. A score
  is how much of the run the signal was past its line, not a probability.
* A limit is named only at 0.5 or more, and called "suggested" below 0.75.
  Nothing over the line means the verdict says no limit was reached, which is
  a result: the run did not push the target far enough to find one.
* What was checked and found fine is listed, and so is what could not be
  checked, because "the indexers were not CPU-bound" and "nobody could see the
  indexers" must not look the same.

Computed when a run is read rather than stored with it: every input is already
on the run, so an old run gets a verdict and a better rule improves all of them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

NAMED_AT = 0.5
STRONG_AT = 0.75

# The lines. Each is where a signal starts to count, and where it counts fully.
QUEUED_PCT = (2.0, 20.0)        # share of searches that waited to be admitted
CPU_HOT_SHARE = (0.1, 0.5)      # share of the run a host spent at 90% CPU or more
CPU_AVG_PCT = (70.0, 90.0)      # a tier's busiest host, averaged over the run
MEM_PEAK_PCT = (90.0, 98.0)
IO_WAIT_MS = (20.0, 80.0)       # mean time per disk request on the slowest disk
COLD_SHARE = (0.1, 0.4)         # share of search run time spent waiting on cache misses
ERROR_PCT = (2.0, 20.0)
LOOP_LAG_MS = (500.0, 2000.0)


def _f(value: Any) -> Optional[float]:
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ramp(value: Optional[float], line: Tuple[float, float]) -> float:
    """0 at the first number, 1 at the second, straight between."""
    if value is None:
        return 0.0
    low, high = line
    if value <= low:
        return 0.0
    if value >= high:
        return 1.0
    return (value - low) / (high - low)


def _probe_row(sut: Dict[str, Any], name: str) -> Dict[str, Any]:
    probe = (sut.get("probes") or {}).get(name) or {}
    rows = probe.get("rows") or []
    return rows[0] if rows and probe.get("available") and isinstance(rows[0], dict) else {}


def _factor(kind: str, label: str, score: float, evidence: List[str], advice: str = "") -> Dict[str, Any]:
    out = {"kind": kind, "label": label, "score": round(min(1.0, max(0.0, score)), 2), "evidence": evidence}
    if advice:
        out["advice"] = advice
    return out


# ---------------------------------------------------------------------------
# One function a candidate. Each returns (factor or None, "fine" line or None).
# ---------------------------------------------------------------------------

def _generator(stats: Dict[str, Any], summary: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    generator = stats.get("generator") or {}
    lag = _f(generator.get("max_loop_lag_ms")) or 0.0
    shed = int(_f(stats.get("arrivals_shed")) or 0) + int(_f(stats.get("arrivals_skipped")) or 0)
    reason = str(summary.get("invalid_reason") or "")
    own = any(word in reason for word in ("generator", "worker", "arrivals"))
    score = 1.0 if (summary.get("valid") is False and own) else max(_ramp(lag, LOOP_LAG_MS), 0.9 if shed else 0.0)
    if score >= 0.3:
        evidence = [f"The generator's own loop ran up to {lag:.0f} ms late."]
        if shed:
            evidence.append(f"{shed} scheduled arrivals were never issued or were skipped.")
        if reason and own:
            evidence.append(f"The run was marked invalid: {reason}")
        return (
            _factor(
                "generator", "The load generator, not the target", score, evidence,
                "The numbers describe the worker. Use more workers or fewer users per worker and run again.",
            ),
            None,
        )
    return None, f"The load generator kept to its schedule (its loop ran at most {lag:.0f} ms late)."


def _admission(stats: Dict[str, Any], sut: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    queueing = stats.get("queueing") or {}
    jobs = int(_f(queueing.get("job_executions")) or 0)
    pct = _f(queueing.get("queued_pct")) or 0.0
    if not jobs:
        return None, None
    p95 = _f((queueing.get("queued_ms") or {}).get("p95_ms"))
    ceiling = _f((stats.get("target") or {}).get("max_hist_searches"))
    peak = _f(stats.get("peak_in_flight"))
    at_ceiling = bool(ceiling and peak and peak >= ceiling)
    score = _ramp(pct, QUEUED_PCT)
    if at_ceiling and score > 0:
        score = min(1.0, score + 0.2)
    reasons_probe = (sut.get("probes") or {}).get("queueing_reasons") or {}
    reasons = [str(row.get("reason") or "") for row in (reasons_probe.get("rows") or []) if reasons_probe.get("available")]
    by_role = any("role" in reason.lower() or "quota" in reason.lower() for reason in reasons)
    if score >= 0.3:
        wait = f", waiting {p95 / 1000.0:.1f} s at the 95th percentile" if p95 else ""
        evidence = [f"{pct:.1f}% of searches waited to be admitted before they ran{wait}."]
        if ceiling and peak:
            evidence.append(f"Searches in flight peaked at {peak:.0f} against a ceiling of {ceiling:.0f}.")
        target_side = _probe_row(sut, "queueing")
        if _f(target_side.get("enqueued")):
            evidence.append(
                f"The search head's own log counts {_f(target_side.get('enqueued')):.0f} queued searches "
                f"(longest wait {_f(target_side.get('max_queued_s')) or 0:.0f} s)."
            )
        if by_role:
            return (
                _factor(
                    "admission_role", "Admission: the account's search quota", score,
                    evidence + ["The dispatcher gave a per-role or per-user quota as a reason."],
                    "This is the test account's limit, not the cluster's. Raise the role's srchJobsQuota "
                    "or spread the load over several accounts.",
                ),
                None,
            )
        return (
            _factor(
                "admission", "Admission: the search head's concurrent-search ceiling", score, evidence,
                "Searches were held back before they started, so latency past this point is queueing. "
                "More search head CPU (or more members) raises the ceiling; faster searches free slots sooner.",
            ),
            None,
        )
    return None, f"Admission: {pct:.1f}% of searches waited to be admitted."


def _tier_cpu(
    resources: Dict[str, Any], role: str, kind: str, label: str, run_s: float
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    hosts = [h for h in (resources.get("hosts") or []) if h.get("role") == role]
    if not hosts:
        return None, None

    def hot_share(host: Dict[str, Any]) -> float:
        return (_f((host.get("cpu") or {}).get("hot_s")) or 0.0) / run_s if run_s > 0 else 0.0

    busiest = max(hosts, key=lambda h: (hot_share(h), _f((h.get("cpu") or {}).get("avg_pct")) or 0.0))
    cpu = busiest.get("cpu") or {}
    avg, peak, share = _f(cpu.get("avg_pct")), _f(cpu.get("peak_pct")), hot_share(busiest)
    hot_hosts = [h for h in hosts if hot_share(h) >= CPU_HOT_SHARE[0]]
    score = max(_ramp(share, CPU_HOT_SHARE), 0.8 * _ramp(avg, CPU_AVG_PCT))
    plural = f"{role}s" if len(hosts) != 1 else role
    if score >= 0.3:
        evidence = [
            f"{busiest['host']} averaged {avg or 0:.0f}% CPU during the run and spent "
            f"{100 * share:.0f}% of it at 90% or more (peak {peak or 0:.0f}%)."
        ]
        if len(hosts) > 1:
            evidence.append(
                f"{len(hot_hosts)} of {len(hosts)} {plural} were at 90% or more for a tenth of the run or longer."
            )
            if len(hot_hosts) == 1 and len(hosts) >= 3:
                evidence.append("One host, not the tier: look for an uneven share of the data or the searches.")
        before = _f(cpu.get("before_pct"))
        if before is not None:
            evidence.append(f"It was at {before:.0f}% in the three minutes before the run.")
        load = busiest.get("load") or {}
        if (_f(load.get("peak")) or 0.0) >= 1.0:
            evidence.append(f"Its load per core peaked at {_f(load.get('peak')):.2f}: more runnable work than cores.")
        return _factor(kind, label, score, evidence), None
    return None, f"{plural.capitalize()}: the busiest averaged {avg or 0:.0f}% CPU and spent {100 * share:.0f}% of the run at 90% or more."


def _memory(resources: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    hosts = [h for h in (resources.get("hosts") or []) if (h.get("mem") or {}).get("peak_pct") is not None]
    if not hosts:
        return None, None
    worst = max(hosts, key=lambda h: _f(h["mem"].get("peak_pct")) or 0.0)
    peak = _f(worst["mem"].get("peak_pct")) or 0.0
    swap = _f(worst["mem"].get("swap_used_mb")) or 0.0
    score = _ramp(peak, MEM_PEAK_PCT) * 0.6 + (0.3 if peak >= MEM_PEAK_PCT[0] and swap > 0 else 0.0)
    if peak >= MEM_PEAK_PCT[0]:
        score = max(score, 0.5)
    if score >= 0.3:
        evidence = [f"{worst['host']} ({worst.get('role')}) reached {peak:.0f}% of its memory in use."]
        if swap > 0:
            evidence.append(f"It had {swap:.0f} MB of swap in use, so it was paging.")
        return (
            _factor(
                "memory", "Memory", score, evidence,
                "This is the host's total, page cache included on some systems: check which search "
                "processes grew before sizing on it.",
            ),
            None,
        )
    return None, f"Memory: the fullest host peaked at {peak:.0f}% used."


def _disk(resources: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    hosts = [h for h in (resources.get("hosts") or []) if (h.get("io") or {}).get("wait_ms") is not None]
    if not hosts:
        return None, None
    worst = max(hosts, key=lambda h: _f(h["io"].get("wait_ms")) or 0.0)
    io = worst["io"]
    wait, before = _f(io.get("wait_ms")) or 0.0, _f(io.get("before_wait_ms"))
    score = _ramp(wait, IO_WAIT_MS)
    if before is not None and before > 0 and wait < 2.0 * before:
        # Slow before the run as well: the disk, not the load.
        score *= 0.5
    if score >= 0.3:
        evidence = [
            f"{worst['host']} ({worst.get('role')}) averaged {wait:.0f} ms per disk request on its slowest "
            f"disk, reading {_f(io.get('read_mb_s')) or 0:.0f} MB/s."
        ]
        if before is not None:
            evidence.append(f"The same disk averaged {before:.0f} ms before the run.")
        return (
            _factor(
                "disk", "Disk on the " + ("indexers" if worst.get("role") == "indexer" else f"{worst.get('role')}s"),
                score, evidence,
            ),
            None,
        )
    return None, f"Disk: the slowest averaged {wait:.0f} ms per request."


def _cache(sut: Dict[str, Any], summary: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ours = _probe_row(sut, "our_searches")
    lookups = _f(ours.get("cache_lookups"))
    if not lookups:
        return None, None
    cold_s = _f(ours.get("cold_path_s")) or 0.0
    searches, avg_run = _f(ours.get("searches")) or 0.0, _f(ours.get("avg_run_time_s")) or 0.0
    total = searches * avg_run
    share = cold_s / total if total > 0 else 0.0
    miss_pct = _f(ours.get("cache_miss_pct")) or 0.0
    score = _ramp(share, COLD_SHARE)
    if score >= 0.3:
        evidence = [
            f"{miss_pct:.1f}% of bucket-cache lookups missed, and waiting on them was {100 * share:.0f}% "
            f"of this run's search run time ({cold_s:.0f} s of {total:.0f} s)."
        ]
        by_step = (sut.get("probes") or {}).get("our_searches_by_step") or {}
        rows = [r for r in (by_step.get("rows") or []) if by_step.get("available") and _f(r.get("cold_path_s"))]
        if rows:
            top = max(rows, key=lambda r: _f(r.get("cold_path_s")) or 0.0)
            evidence.append(f"Step {top.get('step')} paid the most: {_f(top.get('cold_path_s')):.0f} s waiting on misses.")
        provenance = ((summary.get("cache") or {}).get("delta") or {}).get("provenance")
        if provenance:
            evidence.append(f"Regulator's own cache reading calls this run {provenance}.")
        return (
            _factor(
                "cache", "The bucket cache: searches waited on downloads from object storage", score, evidence,
                "This measures object storage and the network as much as search. Warm the cache first "
                "to measure the search tier, or keep it cold if cold is the question.",
            ),
            None,
        )
    return None, f"Bucket cache: {miss_pct:.1f}% of lookups missed, {100 * share:.0f}% of search run time."


def _errors(stats: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    executions = int(_f(stats.get("executions")) or 0)
    if not executions:
        return None, None
    pct = _f(stats.get("error_rate_pct")) or 0.0
    score = _ramp(pct, ERROR_PCT)
    if score >= 0.3:
        by_class = sorted((stats.get("errors_by_class") or {}).items(), key=lambda kv: -int(kv[1] or 0))
        said = ", ".join(f"{count} {name}" for name, count in by_class[:3] if name != "cancelled")
        return (
            _factor(
                "errors", "Searches failed rather than slowed", score,
                [f"{pct:.1f}% of searches failed ({said})."],
                "Latency over the searches that succeeded flatters the result. See the failed searches "
                "below and Diagnose for why.",
            ),
            None,
        )
    return None, f"Errors: {pct:.2f}% of searches failed."


def _where_time_went(sut: Dict[str, Any]) -> Optional[str]:
    telemetry = _probe_row(sut, "search_telemetry")
    indexer, reduce = _f(telemetry.get("avg_indexer_ms")), _f(telemetry.get("avg_reduce_ms"))
    if indexer and reduce:
        share = 100.0 * indexer / (indexer + reduce)
        return (
            f"By the target's own search telemetry, {share:.0f}% of a search's time was the map phase on "
            f"the indexers ({indexer:.0f} ms) and the rest the reduce on the search head ({reduce:.0f} ms)."
        )
    return None


def assess(stats: Optional[Dict[str, Any]], summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The verdict for a finished run, or None when there is nothing to read."""
    stats = stats if isinstance(stats, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    if not stats.get("executions") and not summary:
        return None
    sut = summary.get("sut") if isinstance(summary.get("sut"), dict) else {}
    resources = summary.get("resources") if isinstance(summary.get("resources"), dict) else {}
    have_resources = bool(resources.get("available"))
    window = resources.get("window") or {}
    run_s = max(0.0, (_f(window.get("run_ended_at")) or 0.0) - (_f(window.get("run_started_at")) or 0.0))

    checks = [
        _generator(stats, summary),
        _admission(stats, sut),
        _errors(stats),
        _cache(sut, summary),
    ]
    if have_resources:
        checks += [
            _tier_cpu(resources, "search head", "search_head_cpu", "CPU on the search heads", run_s),
            _tier_cpu(resources, "indexer", "indexer_cpu", "CPU on the indexers", run_s),
            _memory(resources),
            _disk(resources),
        ]
    factors = sorted((f for f, _ in checks if f is not None), key=lambda f: -f["score"])
    fine = [line for _, line in checks if line]

    not_checked: List[str] = []
    if not have_resources:
        not_checked.append(
            "Host CPU, memory and disk: " + (str(resources.get("reason")) if resources.get("reason") else
                                           "this run has no host resource figures (it finished before they were collected)")
        )
    elif not any(h.get("role") == "indexer" for h in resources.get("hosts") or []):
        not_checked.append("The indexers: none of them reported resource usage where this search head can search it.")
    if not _probe_row(sut, "our_searches"):
        not_checked.append("The bucket cache's cost per search: the target's audit trail had no account of this run's searches.")

    named = [f for f in factors if f["score"] >= NAMED_AT]
    # The generator trumps everything: if it could not keep time, nothing else
    # the run measured can be trusted to be about the target.
    named.sort(key=lambda f: (f["kind"] != "generator", -f["score"]))
    if named:
        top = named[0]
        strength = "strong" if top["score"] >= STRONG_AT else "suggested"
        headline = top["label"]
        also = [f for f in named[1:] if f["score"] >= top["score"] - 0.15 and top["kind"] != "generator"]
        if also:
            headline += ", with " + also[0]["label"][0].lower() + also[0]["label"][1:]
    else:
        strength = "none"
        headline = (
            "No limit was reached: nothing this run measured was past its line"
            if not not_checked
            else "No limit was found in what could be checked"
        )
    for factor in factors:
        factor["named"] = factor["score"] >= NAMED_AT
    return {
        "headline": headline,
        "strength": strength,
        "factors": factors,
        "fine": fine,
        "not_checked": not_checked,
        "context": _where_time_went(sut),
        "method": (
            "Rules over this run's own figures, not a model. A limit is named when its signal was past "
            "its line for enough of the run; the score is how far past, not a probability."
        ),
    }
