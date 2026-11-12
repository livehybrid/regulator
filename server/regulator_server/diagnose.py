"""Why did searches in a run fail? Asked after the run, never during it.

A run keeps a ledger of the searches worth opening one at a time: the ones
that failed, were cut off by the end of the run or came back partial, and the
slowest that succeeded (see ``regulator_agent.results.SearchLedger``). This
module does two things with it.

First it reads Regulator's own record, which costs the target nothing and
already answers the commonest question: a search "cancelled" at the end of a
run was cut off by Regulator's own limits, not by Splunk, and the record says
how old each one was when that happened.

Then, for the search ids in the ledger and nothing else, it asks the target
three questions: what ``_audit`` recorded for each search, what each indexer
did for it (``splunkd_remote_searches``), and what splunkd logged about it at
WARN or ERROR. Three small searches over the run's own window, on a target
that is no longer under test.

Nothing here raises: a question the target could not answer is recorded as
unanswered, with the reason.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from regulator_agent.splunk import SplunkClient, SplunkError
from regulator_agent.timepolicy import TimeWindow

log = logging.getLogger("regulator.server.diagnose")

# How many search ids are looked up. Every one is a wildcard clause in three
# searches, so this is the bound on what a diagnosis costs the target.
MAX_SIDS = 60
SIDS_PER_QUERY = 30
QUERY_TIMEOUT_S = 90.0
# Audit and remote-search records for a job are written when it ends and when
# it is torn down, which for a search cut off at the end of a run is minutes
# after the run's own end.
TAIL_S = 180.0
LEAD_S = 60.0

# A search id is put into SPL inside double quotes. Regulator's own ids are
# letters, digits and underscores; splunkd's are those plus dots and hyphens.
# Anything else is left out of the lookup rather than escaped.
_SID_OK = re.compile(r"^[A-Za-z0-9_.\-]{4,200}$")

PROBLEM_OUTCOMES = ("failed", "unfinished", "finished_after_end", "still_running", "failed_after_end", "partial")


def ledger_of(stats: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ledger = (stats or {}).get("searches")
    return ledger if isinstance(ledger, dict) else {}


def ledger_sids(ledger: Dict[str, Any], limit: int = MAX_SIDS) -> List[str]:
    """The search ids to look up: every problem first, then the slowest."""
    out: List[str] = []
    seen = set()
    for entry in list(ledger.get("problems") or []) + list(ledger.get("slowest") or []):
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("sid") or "")
        if not sid or sid in seen or not _SID_OK.match(sid):
            continue
        seen.add(sid)
        out.append(sid)
        if len(out) >= limit:
            break
    return out


def _seconds(ms: Any) -> Optional[float]:
    try:
        return float(ms) / 1000.0
    except (TypeError, ValueError):
        return None


def _span(values: Sequence[float]) -> str:
    low, high = min(values), max(values)
    if round(low) == round(high):
        return f"{low:.0f}s"
    return f"between {low:.0f}s and {high:.0f}s"


def own_findings(
    ledger: Dict[str, Any],
    stats: Optional[Dict[str, Any]],
    *,
    state: str,
    duration_s: Optional[float],
    drain_budget_s: float,
    harvest_s: float,
) -> List[str]:
    """What Regulator's own record says, before the target is asked anything."""
    findings: List[str] = []
    problems = [e for e in (ledger.get("problems") or []) if isinstance(e, dict)]
    stats = stats or {}
    unfinished = stats.get("unfinished") or {}
    cut = int(unfinished.get("count") or 0)
    cut_entries = [
        e for e in problems
        if e.get("outcome") in ("unfinished", "finished_after_end", "still_running", "failed_after_end")
    ]

    if cut:
        ages = [s for s in (_seconds(e.get("service_time_ms")) for e in cut_entries) if s is not None]
        aged = f" They had been running {_span(ages)} at that point." if ages else ""
        ran = f" a {duration_s:.0f}s run and" if duration_s else ""
        findings.append(
            f"{cut} search(es) were still running when the run stopped waiting for them: after"
            f"{ran} the {drain_budget_s:.0f}s allowed for work in flight (REG_DRAIN_BUDGET_S)."
            f"{aged} That cut-off is Regulator's, not Splunk's, and it depends on when a search "
            "started rather than on how long it takes: these are counted as unfinished, not as errors."
        )
        if state != "completed":
            findings.append(
                f"The run ended as {state}, so the searches still in flight were deleted at once "
                "rather than followed: on the target they read as cancelled."
            )
        else:
            done = int(unfinished.get("finished_after_end") or 0)
            left = int(unfinished.get("still_running") or 0)
            if harvest_s <= 0:
                findings.append(
                    "REG_HARVEST_S is 0, so they were deleted as soon as the run ended: on the "
                    "target they read as cancelled."
                )
            else:
                if done:
                    longest = ((unfinished.get("run_duration") or {}).get("max_ms") or 0) / 1000.0
                    tail = f" (the longest ran {longest:.0f}s on the target)" if longest else ""
                    findings.append(
                        f"{done} of them finished while Regulator kept following them for up to "
                        f"{harvest_s:.0f}s after the run (REG_HARVEST_S){tail}."
                    )
                if left:
                    findings.append(
                        f"{left} were still running {harvest_s:.0f}s after the run and were deleted "
                        "then, which the target records as cancelled. To let searches this long "
                        "finish, raise REG_DRAIN_BUDGET_S (they are then measured like any other "
                        "search) or REG_HARVEST_S on the control plane, or make the run longer."
                    )

    failed = [e for e in problems if e.get("outcome") == "failed"]
    by_class = dict(stats.get("errors_by_class") or {})
    for error_class, count in sorted(by_class.items(), key=lambda kv: -int(kv[1] or 0)):
        if error_class == "cancelled" or not count:
            continue
        details = Counter(
            str(e.get("error_detail") or "")[:160] for e in failed if e.get("error_class") == error_class and e.get("error_detail")
        )
        common = details.most_common(1)
        ages = [s for s in (_seconds(e.get("service_time_ms")) for e in failed if e.get("error_class") == error_class) if s is not None]
        after = f" after {_span(ages)}" if ages else ""
        said = f' Most often: "{common[0][0]}".' if common else ""
        findings.append(f"{int(count)} search(es) failed as {error_class}{after}.{said}")

    partial = int(stats.get("partial") or 0)
    if partial:
        findings.append(
            f"{partial} search(es) finished but said their results were incomplete (a peer "
            "dropped out, the job was finalised early, or a limit truncated it)."
        )
    dropped = sum(int(v or 0) for v in (ledger.get("dropped") or {}).values())
    if dropped:
        findings.append(
            f"The ledger keeps a bounded sample: {dropped} more search(es) with a problem are "
            "counted in the totals but not listed one by one."
        )
    if not findings:
        findings.append("Regulator recorded no failed, unfinished or partial searches in this run.")
    return findings


# ---------------------------------------------------------------------------
# The questions for the target
# ---------------------------------------------------------------------------

def _sid_clause(sids: Sequence[str], field: Optional[str]) -> str:
    """The run's search ids as one OR clause.

    A wildcard either side: ``_audit`` wraps the id in single quotes, and an
    indexer knows the job as ``remote_<search head>_<sid>``.
    """
    if field:
        return "(" + " OR ".join(f'{field}="*{sid}*"' for sid in sids) + ")"
    return "(" + " OR ".join(f'"*{sid}*"' for sid in sids) + ")"


def audit_spl(sids: Sequence[str]) -> str:
    # One row a search: every audit record of it folded together. granted is
    # written at dispatch, and completed, canceled or failed when it ends, so
    # the set of info values is the search's history. The counts and the
    # bucket-cache accounting are on the completed record only. Field names as
    # in the probes of regulator_agent.sut, confirmed there on a live 10.4.
    return (
        f"search index=_audit action=search {_sid_clause(sids, 'search_id')} "
        "| eval sid=trim(search_id, \"'\") "
        "| stats values(info) as info, values(user) as user, values(host) as host, "
        "min(_time) as first_seen, max(_time) as last_seen, "
        "max(total_run_time) as total_run_time, max(scan_count) as scan_count, "
        "max(event_count) as event_count, max(result_count) as result_count, "
        "max(search_startup_time) as startup_ms, "
        "max(searched_buckets) as searched_buckets, max(eliminated_buckets) as eliminated_buckets, "
        "max(considered_events) as considered_events, max(decompressed_slices) as decompressed_slices, "
        "max(invocations_command_search_index_bucketcache_hit) as index_cache_hits, "
        "max(invocations_command_search_index_bucketcache_miss) as index_cache_misses, "
        "max(duration_command_search_index_bucketcache_miss) as index_miss_s, "
        "max(invocations_command_search_rawdata_bucketcache_hit) as rawdata_cache_hits, "
        "max(invocations_command_search_rawdata_bucketcache_miss) as rawdata_cache_misses, "
        "max(duration_command_search_rawdata_bucketcache_miss) as rawdata_miss_s by sid "
        '| eval info=mvjoin(info, ","), user=mvjoin(user, ","), host=mvjoin(host, ",")'
    )


def indexers_spl(sids: Sequence[str]) -> str:
    # What each indexer did for each search: the line an indexer writes to
    # remote_searches.log when a search head's stream to it closes. The line
    # and every field below confirmed on a live 10.6 cluster (the line was
    # first written here as "terminated", which matched nothing). The indexer
    # knows the job as remote_<search head>_<sid>, hence the wildcards.
    return (
        "search index=_internal sourcetype=splunkd_remote_searches "
        f'"Streamed search connection closed" {_sid_clause(sids, "search_id")} '
        "| stats sum(elapsedTime) as elapsed_s, sum(scan_count) as scan_count, "
        "sum(events_count) as events, sum(considered_buckets) as considered_buckets, "
        "sum(eliminated_buckets) as eliminated_buckets, sum(drop_count) as dropped, "
        "sum(search_index_bucketcache_hit) as index_cache_hits, "
        "sum(search_index_bucketcache_miss) as index_cache_misses, "
        "sum(search_rawdata_bucketcache_hit) as rawdata_cache_hits, "
        "sum(search_rawdata_bucketcache_miss) as rawdata_cache_misses, "
        "sum(search_index_bucketcache_miss_wait) as index_miss_wait_s, "
        "sum(search_rawdata_bucketcache_miss_wait) as rawdata_miss_wait_s by search_id, host "
        "| sort - elapsed_s"
    )


def logs_spl(sids: Sequence[str]) -> str:
    # What splunkd itself complained about, by name. Grouped so a thousand
    # repeats of one line come back as one row with a count.
    return (
        "search index=_internal sourcetype=splunkd (log_level=ERROR OR log_level=WARN OR log_level=FATAL) "
        f"{_sid_clause(sids, None)} "
        "| eval message=substr(coalesce(event_message, message, _raw), 1, 400) "
        "| stats count, min(_time) as first_seen, max(_time) as last_seen by host, component, log_level, message "
        "| sort - count"
    )


def _number(value: Any) -> Optional[float]:
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return "" if value is None else str(value)


def _clean(row: Dict[str, Any], numeric: Sequence[str], text: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in numeric:
        number = _number(row.get(key))
        if number is not None:
            out[key] = int(number) if float(number).is_integer() else round(number, 3)
    for key in text:
        value = _text(row.get(key))
        if value:
            out[key] = value
    return out


_AUDIT_NUMBERS = (
    "first_seen", "last_seen", "total_run_time", "scan_count", "event_count", "result_count",
    "startup_ms", "searched_buckets", "eliminated_buckets", "considered_events", "decompressed_slices",
    "index_cache_hits", "index_cache_misses", "index_miss_s",
    "rawdata_cache_hits", "rawdata_cache_misses", "rawdata_miss_s",
)
_INDEXER_NUMBERS = (
    "elapsed_s", "scan_count", "events", "considered_buckets", "eliminated_buckets", "dropped",
    "index_cache_hits", "index_cache_misses", "rawdata_cache_hits", "rawdata_cache_misses",
    "index_miss_wait_s", "rawdata_miss_wait_s",
)


def _owner(value: str, sids: Sequence[str]) -> Optional[str]:
    """Which of our search ids a value from the target is about.

    The longest match, because one id can be the start of another
    (``..._1`` and ``..._12``).
    """
    best: Optional[str] = None
    for sid in sids:
        if sid in value and (best is None or len(sid) > len(best)):
            best = sid
    return best


def final_info(info: str) -> str:
    """A search's audit history as one word: how it ended."""
    seen = {part.strip() for part in info.split(",") if part.strip()}
    for word in ("failed", "canceled", "completed"):
        if word in seen:
            return word
    return "granted" if "granted" in seen else (sorted(seen)[0] if seen else "unknown")


def fold(
    sids: Sequence[str],
    audit_rows: Sequence[Dict[str, Any]],
    indexer_rows: Sequence[Dict[str, Any]],
    log_rows: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """What the target said, by search id."""
    out: Dict[str, Dict[str, Any]] = {}

    def slot(sid: str) -> Dict[str, Any]:
        return out.setdefault(sid, {})

    for row in audit_rows:
        sid = _owner(_text(row.get("sid")), sids)
        if sid is None:
            continue
        audit = _clean(row, _AUDIT_NUMBERS, ("info", "user", "host"))
        audit["ended"] = final_info(audit.get("info", ""))
        slot(sid)["audit"] = audit
    for row in indexer_rows:
        sid = _owner(_text(row.get("search_id")), sids)
        if sid is None:
            continue
        slot(sid).setdefault("indexers", []).append(_clean(row, _INDEXER_NUMBERS, ("host",)))
    for row in log_rows:
        message = _text(row.get("message"))
        sid = _owner(message, sids)
        if sid is None:
            continue
        logs = slot(sid).setdefault("logs", [])
        if len(logs) < 8:
            logs.append(_clean(row, ("count", "first_seen", "last_seen"), ("host", "component", "log_level", "message")))
    return out


def target_findings(
    ledger: Dict[str, Any],
    sids: Sequence[str],
    by_sid: Dict[str, Dict[str, Any]],
    answered: Dict[str, bool],
    log_rows: Sequence[Dict[str, Any]],
) -> List[str]:
    findings: List[str] = []
    outcome_of = {
        str(e.get("sid")): str(e.get("outcome"))
        for e in list(ledger.get("problems") or []) + list(ledger.get("slowest") or [])
        if isinstance(e, dict) and e.get("sid")
    }
    problem_sids = [sid for sid in sids if outcome_of.get(sid) in PROBLEM_OUTCOMES]

    if answered.get("audit") and problem_sids:
        ended = Counter(((by_sid.get(sid) or {}).get("audit") or {}).get("ended", "not found") for sid in problem_sids)
        said = ", ".join(f"{count} {word}" for word, count in ended.most_common())
        findings.append(
            f"The target's audit trail, for the {len(problem_sids)} problem search(es) looked up: {said}."
        )
        cancelled = [
            (by_sid[sid]["audit"], outcome_of.get(sid))
            for sid in problem_sids
            if ((by_sid.get(sid) or {}).get("audit") or {}).get("ended") == "canceled"
        ]
        if cancelled:
            times = [float(a["total_run_time"]) for a, _ in cancelled if a.get("total_run_time") is not None]
            users = sorted({u for a, _ in cancelled for u in str(a.get("user") or "").split(",") if u})
            ours = sum(1 for _, outcome in cancelled if outcome in ("unfinished", "still_running", "failed"))
            when = f" after {_span(times)} of run time" if times else ""
            who = f", as {', '.join(users)}" if users else ""
            findings.append(
                f"{len(cancelled)} were recorded as canceled{when}{who}. {ours} of those are searches "
                "Regulator itself deleted (cut off at the end of the run, or given up on after an "
                "error), so the cancel is Regulator's delete and not a limit on the target."
                if ours
                else f"{len(cancelled)} were recorded as canceled{when}{who}, and Regulator did not "
                "delete them: something on the target did."
            )
        failed = [sid for sid in problem_sids if ((by_sid.get(sid) or {}).get("audit") or {}).get("ended") == "failed"]
        if failed:
            findings.append(f"{len(failed)} ended as failed on the target itself: its search log says why.")
        missing = ended.get("not found", 0)
        if missing:
            findings.append(
                f"{missing} have no audit record at all. Either they were refused before a job "
                "existed (a quota or an HTTP error at dispatch), or this account cannot read _audit."
            )

    if answered.get("indexers"):
        per_host: Dict[str, float] = {}
        for detail in by_sid.values():
            for row in detail.get("indexers") or []:
                host = str(row.get("host") or "")
                if host and row.get("elapsed_s") is not None:
                    per_host[host] = per_host.get(host, 0.0) + float(row["elapsed_s"])
        if len(per_host) >= 3:
            ordered = sorted(per_host.values())
            median = ordered[len(ordered) // 2]
            slowest_host = max(per_host, key=lambda h: per_host[h])
            if median > 0 and per_host[slowest_host] > 2.0 * median:
                findings.append(
                    f"Indexer {slowest_host} spent {per_host[slowest_host]:.0f}s on the searches "
                    f"looked up, against a median of {median:.0f}s across {len(per_host)} indexers."
                )

    if answered.get("logs") and log_rows:
        lines = Counter()
        for row in log_rows:
            key = (_text(row.get("component")), _text(row.get("log_level")))
            lines[key] += int(_number(row.get("count")) or 1)
        said = ", ".join(f"{component} {level} x{count}" for (component, level), count in lines.most_common(4))
        findings.append(f"splunkd logged about these searches by name: {said}.")
    elif answered.get("logs"):
        findings.append("splunkd logged nothing at WARN or ERROR that names these searches.")
    return findings


async def ask_target(
    client: SplunkClient, sids: Sequence[str], earliest: float, latest: float, timeout_s: float = QUERY_TIMEOUT_S
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Run the three questions, one after another. Returns (queries, rows)."""
    window = TimeWindow(earliest=earliest, latest=latest)
    questions = (
        ("audit", "what _audit recorded for each search", audit_spl, 200),
        ("indexers", "what each indexer did for each search (splunkd_remote_searches)", indexers_spl, 1000),
        ("logs", "what splunkd logged at WARN or ERROR that names a search", logs_spl, 200),
    )
    queries: List[Dict[str, Any]] = []
    rows: Dict[str, List[Dict[str, Any]]] = {name: [] for name, *_ in questions}
    chunks = [list(sids[i:i + SIDS_PER_QUERY]) for i in range(0, len(sids), SIDS_PER_QUERY)]
    # One at a time, as the correlation probes are: the target has just been
    # load tested and is owed a quiet afterwards.
    for name, description, build, count in questions:
        query: Dict[str, Any] = {"name": name, "description": description, "available": False, "rows": 0, "searches": 0}
        started = time.time()
        for chunk in chunks:
            query["searches"] += 1
            try:
                got, _ = await asyncio.wait_for(client.oneshot(build(chunk), window, count=count), timeout=timeout_s)
            except asyncio.TimeoutError:
                query["reason"] = f"the query did not answer within {timeout_s:.0f}s"
                break
            except SplunkError as exc:
                query["reason"] = str(exc)[:300]
                break
            except Exception as exc:  # noqa: BLE001 - a diagnosis never fails outright
                query["reason"] = f"{type(exc).__name__}: {exc}"[:300]
                break
            rows[name].extend(row for row in got if isinstance(row, dict))
        else:
            query["available"] = True
        query["rows"] = len(rows[name])
        query["seconds"] = round(time.time() - started, 2)
        queries.append(query)
    return queries, rows


async def diagnose(
    client: Optional[SplunkClient],
    stats: Optional[Dict[str, Any]],
    *,
    state: str,
    started_at: Optional[float],
    ended_at: Optional[float],
    duration_s: Optional[float],
    drain_budget_s: float,
    harvest_s: float,
    timeout_s: float = QUERY_TIMEOUT_S,
) -> Dict[str, Any]:
    """The whole diagnosis. ``client`` is None to read Regulator's record only."""
    ledger = ledger_of(stats)
    sids = ledger_sids(ledger)
    result: Dict[str, Any] = {
        "at": time.time(),
        "own": own_findings(
            ledger, stats, state=state, duration_s=duration_s,
            drain_budget_s=drain_budget_s, harvest_s=harvest_s,
        ),
        "sids": len(sids),
        "queries": [],
        "searches": {},
        "target": [],
    }
    if not ledger:
        result["note"] = (
            "this run kept no search ledger (it finished on a version before the ledger "
            "existed), so there are no search ids to look up"
        )
        return result
    if not sids:
        result["note"] = "none of the searches in the ledger has a search id to look up"
        return result
    if client is None:
        return result

    earliest = float(started_at or ended_at or time.time()) - LEAD_S
    latest = min(time.time() + 60.0, float(ended_at or time.time()) + harvest_s + drain_budget_s + TAIL_S)
    latest = max(latest, earliest + 60.0)
    queries, rows = await ask_target(client, sids, earliest, latest, timeout_s)
    by_sid = fold(sids, rows["audit"], rows["indexers"], rows["logs"])
    answered = {q["name"]: bool(q["available"]) for q in queries}
    result.update(
        {
            "window": {"earliest": earliest, "latest": latest},
            "queries": queries,
            "searches": by_sid,
            "target": target_findings(ledger, sids, by_sid, answered, rows["logs"]),
            "self_load": {
                "queries": sum(int(q["searches"]) for q in queries),
                "note": "these searches ran on the target after the run, over the run's own window",
            },
        }
    )
    return result
