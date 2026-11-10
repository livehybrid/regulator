"""What the cluster thought was happening, pulled back after a run.

Everything else in Regulator measures from the outside: how long the client
waited, what the job reported. This module asks the cluster its own opinion,
and the difference between "p95 went from 4 s to 11 s" and "p95 went from 4 s to
11 s because indexer CPU hit 95% and 340 scheduled searches were skipped" is the
difference between a load test and a benchmark.

Seven questions, each answered by one search against Splunk's own internal
indexes:

``_audit``
    Every search dispatched during the window, with the total run time Splunk
    recorded for it. This is the server's own account of the same searches the
    client timed, so a disagreement between them is itself a finding: it means
    time went somewhere neither end owns, usually a queue.

    Note the count of *this run's own* searches is a floor rather than a total.
    Audit records keep arriving for a minute or two after a run finishes, and
    correlation happens immediately, so the aggregate probes are the ones to
    reason with. Measured on a live 10.4 instance rather than assumed.

``_introspection`` search telemetry
    Where the time actually went, split by phase. The indexer-side elapsed time
    is what separates "the search head is busy" from "the indexers are busy",
    and no amount of client-side timing can tell those apart.

``_internal`` scheduler
    Skipped and deferred scheduled searches. When a load test pushes a cluster
    past its concurrency ceiling, the first casualty is usually somebody else's
    scheduled work rather than the test's own searches, and that is a cost that
    would otherwise be invisible.

``_internal`` cache manager
    SmartStore downloads and evictions during the run, which corroborates the
    bucket-level provenance from the cache manager API with what actually
    happened over the wire.

``_introspection`` per-process resource usage
    Search head and indexer CPU over the run, so a latency curve can be laid
    against the machine's own load.

**Everything here is best effort.** A load-test account frequently cannot read
``_audit`` or ``_introspection``, and refusing to report a run because its
correlation failed would be absurd. Each query degrades to a note saying what
was not visible and why, and the run stands on its client-side measurement.

**These searches are themselves load.** They run on the cluster under test,
after the run rather than during it, and they are counted in the record as a
known bias rather than pretended away.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .params import sanitise_marker_part, sid_prefix_for
from .splunk import SplunkClient, SplunkError
from .timepolicy import TimeWindow

log = logging.getLogger("regulator.sut")

# A correlation search that has not answered in this long is not worth waiting
# for: the run is over and the operator wants their report.
QUERY_TIMEOUT_S = 90.0

# How long to wait before asking. Splunk's audit trail is written through the
# normal indexing pipeline, so a search dispatched a second ago is not yet
# searchable in _audit. Correlating immediately reports zero of this run's own
# searches, which looks like a broken query and is really just impatience.
# Measured against a live 10.4 instance: the events were present a few seconds
# later. Overridable, because a busy indexer takes longer.
SETTLE_S = 8.0

# How far past the end of the run to keep looking for the cluster's own records
# of it. See the note in correlate(): audit records for a run keep arriving
# after the load stops, so a window that ends when the run does finds nothing.
AUDIT_TAIL_S = 120.0


@dataclass
class Probe:
    """One question, and whether the cluster would answer it."""

    name: str
    spl: str
    description: str
    # For a probe that aggregates with no ``by`` clause: the column holding
    # its event count. ``stats`` with no ``by`` always returns one row, even
    # over no events, which used to show as "1 row(s)" of zeros. A probe
    # whose count is zero reports no rows instead.
    count_field: str = ""
    rows: List[Dict[str, Any]] = field(default_factory=list)
    available: bool = False
    reason: str = ""

    @property
    def index(self) -> str:
        """The internal index this probe reads, for the access check."""
        match = re.search(r"index=(_[A-Za-z0-9_]+)", self.spl)
        return match.group(1) if match else ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "description": self.description,
            "rows": self.rows,
        }


# The row an audit record lands in when its search string does not carry the
# run marker, so the step it belongs to cannot be read.
UNATTRIBUTED_STEP = "(step not in the audited search)"


def _marker_regex(marker_prefix: str) -> str:
    """The run's marker stem as a regular expression.

    The stem is already limited to letters, digits and ``_.:-`` (see
    ``sanitise_marker_part``), so the dot is the only character that means
    something else in a regex. A character class rather than a backslash,
    because the pattern sits inside a double-quoted SPL string and a class
    reads the same before and after SPL's own unescaping.
    """
    return marker_prefix.replace(".", "[.]")


def build_probes(marker_prefix: str, sid_prefix: str = "") -> List[Probe]:
    """The questions.

    ``sid_prefix`` is the stem of the search ids Regulator gave the run's jobs,
    and ``marker_prefix`` the run's cache-busting marker stem, which every
    dispatched search also carries as a Splunk comment. Matching on either
    isolates this run's searches from everything else the cluster was doing,
    which matters on a shared cluster where the answer would otherwise be
    everybody's traffic. The search id is the primary match because it does
    not depend on the comment surviving into the audited search string.
    """
    if sid_prefix:
        ours = f'(search_id="*{sid_prefix}*" OR "{marker_prefix}")'
    else:
        ours = f'"{marker_prefix}"'
    return [
        Probe(
            name="our_searches",
            description=(
                "the cluster's own account of the searches this run dispatched, matched "
                "by the run marker each one carries as a Splunk comment"
            ),
            # info=completed: Splunk writes a granted record at dispatch and a
            # completed record at the end, and only the second carries
            # total_run_time. Counting both reported twice the searches and
            # averaged a field half the rows did not have. Confirmed on a
            # live 10.4 instance.
            # The same audit record also carries the bucket-cache accounting
            # (index-file and rawdata hits and misses, with the seconds each
            # miss cost), the startup time and the bucket elimination counts.
            # Zero extra load, and it turns cache provenance into a cost per
            # search. Shape after silkyrich/cluster_health_tools; every field
            # confirmed on a live 10.4 instance.
            count_field="searches",
            spl=(
                f"search index=_audit action=search info=completed {ours} "
                "| stats count as searches, "
                "avg(total_run_time) as avg_run_time_s, "
                "perc95(total_run_time) as p95_run_time_s, "
                "max(total_run_time) as max_run_time_s, "
                "sum(scan_count) as scan_count, sum(event_count) as event_count, "
                "avg(search_startup_time) as avg_startup_ms, perc95(search_startup_time) as p95_startup_ms, "
                "sum(searched_buckets) as searched_buckets, sum(eliminated_buckets) as eliminated_buckets, "
                "sum(considered_events) as considered_events, sum(decompressed_slices) as decompressed_slices, "
                "sum(invocations_command_search_index_bucketcache_hit) as index_cache_hits, "
                "sum(invocations_command_search_index_bucketcache_miss) as index_cache_misses, "
                "sum(duration_command_search_index_bucketcache_miss) as index_miss_s, "
                "sum(invocations_command_search_rawdata_bucketcache_hit) as rawdata_cache_hits, "
                "sum(invocations_command_search_rawdata_bucketcache_miss) as rawdata_cache_misses, "
                "sum(duration_command_search_rawdata_bucketcache_miss) as rawdata_miss_s, "
                "sum(invocations_command_search_index_bucketcache_error) as index_cache_errors, "
                "sum(invocations_command_search_rawdata_bucketcache_error) as rawdata_cache_errors "
                "| eval cold_path_s=coalesce(index_miss_s, 0) + coalesce(rawdata_miss_s, 0), "
                "cache_lookups=coalesce(index_cache_hits, 0) + coalesce(index_cache_misses, 0) + "
                "coalesce(rawdata_cache_hits, 0) + coalesce(rawdata_cache_misses, 0), "
                "cache_miss_pct=if(cache_lookups > 0, round(100 * (coalesce(index_cache_misses, 0) + "
                "coalesce(rawdata_cache_misses, 0)) / cache_lookups, 2), null())"
            ),
        ),
        Probe(
            name="our_searches_by_step",
            description=(
                "the same audit records split by the step each search belongs to: buckets "
                "searched and eliminated, bucket-cache hits and misses, and the seconds each "
                "step's searches spent waiting on misses beside their run time"
            ),
            # The step is read from the run marker, which is the only place a
            # dispatched search says which step it is: the search id names the
            # run and the worker, not the step. A record whose audited search
            # string does not carry the marker is still counted, under a row
            # that says so, rather than dropped: the totals then still add up
            # to our_searches. Sorted by what the cold path cost, because
            # oneshot returns the first 50 rows and those are the ones to keep.
            # NOT yet confirmed on a live cluster that the comment survives
            # into _audit's search field on every version.
            spl=(
                f"search index=_audit action=search info=completed {ours} "
                f'| rex field=search "{_marker_regex(marker_prefix)}vu\\d+:i\\d+:(?<step>[A-Za-z0-9_.:-]+)" '
                f'| eval step=coalesce(step, "{UNATTRIBUTED_STEP}") '
                "| stats count as searches, sum(total_run_time) as run_time_s, "
                "sum(searched_buckets) as searched_buckets, sum(eliminated_buckets) as eliminated_buckets, "
                "sum(invocations_command_search_index_bucketcache_hit) as index_cache_hits, "
                "sum(invocations_command_search_index_bucketcache_miss) as index_cache_misses, "
                "sum(duration_command_search_index_bucketcache_miss) as index_miss_s, "
                "sum(invocations_command_search_rawdata_bucketcache_hit) as rawdata_cache_hits, "
                "sum(invocations_command_search_rawdata_bucketcache_miss) as rawdata_cache_misses, "
                "sum(duration_command_search_rawdata_bucketcache_miss) as rawdata_miss_s by step "
                "| eval cold_path_s=coalesce(index_miss_s, 0) + coalesce(rawdata_miss_s, 0), "
                "cache_hits=coalesce(index_cache_hits, 0) + coalesce(rawdata_cache_hits, 0), "
                "cache_misses=coalesce(index_cache_misses, 0) + coalesce(rawdata_cache_misses, 0), "
                "cache_miss_pct=if(cache_hits + cache_misses > 0, round(100 * cache_misses / (cache_hits + cache_misses), 2), null()), "
                "cold_path_pct=if(run_time_s > 0, round(100 * cold_path_s / run_time_s, 1), null()) "
                "| sort - cold_path_s, - run_time_s"
            ),
        ),
        Probe(
            name="queueing",
            description=(
                "the search head's own account of admission queueing during the window: "
                "how many searches were enqueued, how long they waited and how deep the "
                "queue got, from the search_queue_metrics group in metrics.log"
            ),
            # enqueue_seaches_count is Splunk's own spelling. Shape after
            # silkyrich/cluster_health_tools; confirmed on 10.4, which also
            # reports denied_queued_jobs_user_role and _systemwide.
            count_field="events",
            spl=(
                "search index=_internal sourcetype=splunkd group=search_concurrency name=search_queue_metrics "
                "| stats count as events, sum(enqueue_seaches_count) as enqueued, max(current_queue_size) as max_queue_size, "
                "max(largest_queue_size) as largest_queue_size, max(max_time_spent_in_queue) as max_queued_s, "
                "avg(avg_time_spent_in_queue) as avg_queued_s, "
                "sum(denied_queued_jobs_user_role) as denied_by_role, sum(denied_queued_jobs_systemwide) as denied_systemwide"
            ),
        ),
        Probe(
            name="queueing_reasons",
            description=(
                "why searches were queued, in the dispatcher's words: the instance's "
                "concurrent-search ceiling and a per-role quota look the same from the "
                "client and are different problems"
            ),
            spl=(
                'search index=_internal sourcetype=splunkd component=DispatchManager "Queued job" '
                "| stats count by reason"
            ),
        ),
        Probe(
            name="concurrency",
            description=(
                "concurrent searches as the search head counted them (the system total from "
                "the search_concurrency group), so the client's in-flight figure can be laid "
                "against the server's own"
            ),
            spl=(
                'search index=_internal sourcetype=splunkd group=search_concurrency "system total" '
                "| stats max(active_hist_searches) as peak_active_searches, avg(active_hist_searches) as avg_active_searches, "
                "max(active_realtime_searches) as peak_realtime_searches by host"
            ),
        ),
        Probe(
            name="scheduler_lag",
            description=(
                "the scheduler falling behind before it starts skipping: lag, delayed and "
                "dispatched counts from the searchscheduler group. The earlier and kinder "
                "signal than a skip"
            ),
            # By host: on a search head cluster only the captain schedules, and
            # a member that reports nothing is visible rather than averaged in.
            spl=(
                "search index=_internal sourcetype=splunkd group=searchscheduler "
                "| stats count as events, max(max_lag) as max_lag_s, max(total_lag) as max_total_lag_s, "
                "sum(delayed) as delayed, sum(skipped) as skipped, sum(dispatched) as dispatched, "
                "max(max_running) as max_running by host"
            ),
        ),
        Probe(
            name="cache_buckets",
            description=(
                "SmartStore cache hits, misses and evictions per indexer from the cachemgr_bucket "
                "metrics group: the structured counterpart of the cache manager's log lines"
            ),
            spl=(
                "search index=_internal sourcetype=splunkd group=cachemgr_bucket "
                "| stats sum(cache_hit) as cache_hits, sum(cache_miss) as cache_misses, sum(evict_bucket) as evictions, "
                "sum(manual_evict) as manual_evictions, sum(open) as opens by host"
            ),
        ),
        Probe(
            name="cache_downloads",
            description=(
                "bucket downloads from object storage per indexer, with how long each took and "
                "how much came down, from the cache manager's own download records"
            ),
            spl=(
                "search index=_internal sourcetype=splunkd component=CacheManager action=download status=succeeded "
                "| stats count as downloads, sum(kb) as kb_downloaded, perc95(elapsed_ms) as p95_download_ms, "
                "max(elapsed_ms) as max_download_ms by host"
            ),
        ),
        Probe(
            name="our_page_loads",
            description=(
                "dashboard loads this run's browser cohort made, matched by the marker "
                "each page URL carries. Browser searches are the page's own and carry "
                "no marker, so this is the browser channel's audit trail"
            ),
            count_field="page_loads",
            spl=(
                f'search index=_internal sourcetype=splunk_web_access "_reg={marker_prefix}" '
                "| stats count as page_loads, avg(spent) as avg_spent_ms, "
                "perc95(spent) as p95_spent_ms"
            ),
        ),
        Probe(
            name="all_searches",
            description=(
                "every search the cluster ran during the window, ours and everybody "
                "else's, so a shared cluster's other traffic is visible rather than "
                "silently mixed into the result"
            ),
            spl=(
                "search index=_audit action=search info=completed "
                "| eval search_type=coalesce(search_type, \"unknown\") "
                "| stats count as searches, dc(user) as users, "
                "perc95(total_run_time) as p95_run_time_s by search_type"
            ),
        ),
        Probe(
            name="search_telemetry",
            description=(
                "where the time went, split by phase. Indexer-side elapsed time is what "
                "separates a busy search head from busy indexers"
            ),
            # phase_0 is the map phase on the peers and phase_1 the reduce on
            # the search head. A standalone instance reports only phase_1
            # (checked live), so both are read and the summary says which.
            # Bare field names, not single-quoted: stats takes single quotes as
            # part of the name, so 'a.b' names a field that does not exist and
            # every aggregate came back empty (confirmed on a live instance).
            # Single quotes mean "field" only in eval and where. A name stats
            # cannot take bare, like search_commands{}, is double-quoted.
            count_field="searches",
            spl=(
                "search index=_introspection sourcetype=search_telemetry "
                "| stats count as searches, "
                'avg(phases.phase_0.elapsed_time_aggregations.avg) as avg_indexer_ms, '
                'perc95(phases.phase_0.elapsed_time_aggregations.max) as p95_indexer_ms, '
                'avg(phases.phase_1.elapsed_time_aggregations.avg) as avg_reduce_ms, '
                'perc95(phases.phase_1.elapsed_time_aggregations.max) as p95_reduce_ms, '
                'avg("search_commands{}.maxPeerDuration") as avg_max_peer_s'
            ),
        ),
        Probe(
            name="scheduler",
            description=(
                "scheduled searches skipped or deferred during the run. Past the "
                "concurrency ceiling the first casualty is usually somebody else's "
                "scheduled work rather than the test's own searches"
            ),
            spl=(
                "search index=_internal sourcetype=scheduler (status=skipped OR "
                "status=deferred) | stats count by status, reason"
            ),
        ),
        Probe(
            name="cache_manager",
            description=(
                "SmartStore downloads and evictions over the wire, corroborating the "
                "bucket-level provenance taken from the cache manager API"
            ),
            spl=(
                "search index=_internal sourcetype=splunkd component=CacheManager "
                "| eval kind=case(match(_raw, \"(?i)download\"), \"download\", "
                "match(_raw, \"(?i)evict\"), \"evict\", true(), \"other\") "
                "| stats count by kind, log_level"
            ),
        ),
        Probe(
            name="resource_usage",
            description=(
                "search head and indexer CPU over the run, so a latency curve can be "
                "laid against the machine's own load"
            ),
            # By host, so a search head and its indexers are never averaged
            # into one number that describes neither. Bare field names: see
            # search_telemetry.
            spl=(
                "search index=_introspection sourcetype=splunk_resource_usage component=Hostwide "
                '| stats avg(data.cpu_system_pct) as avg_system_cpu_pct, '
                'avg(data.cpu_user_pct) as avg_user_cpu_pct, '
                'max(data.cpu_user_pct) as max_user_cpu_pct by host'
            ),
        ),
    ]


async def correlate(
    client: SplunkClient,
    started_at: float,
    ended_at: float,
    marker_prefix: str,
    timeout_s: float = QUERY_TIMEOUT_S,
    settle_s: float = SETTLE_S,
    sid_prefix: str = "",
) -> Dict[str, Any]:
    """Ask the cluster what it thought was happening. Never raises."""
    if settle_s > 0:
        # Wait for the audit trail to catch up before asking it about searches
        # that finished moments ago.
        log.info("waiting %.0fs for the target's own logging to catch up", settle_s)
        await asyncio.sleep(settle_s)

    # The window extends well past the run's end, not just to "now". Splunk
    # writes an audit record for a search when it finishes and again as the job
    # is torn down, so the records describing a run keep arriving for a while
    # after the load stops. Measured against a live 10.4 instance: searching
    # only [start, end] found none of the run's own searches, while the same
    # query over [start, end + 90s] found all of them. Ending the window at the
    # run's end is the difference between "correlation is broken" and
    # "correlation works".
    latest = max(ended_at, time.time()) + AUDIT_TAIL_S
    window = TimeWindow(earliest=started_at, latest=latest)
    probes = build_probes(marker_prefix, sid_prefix)
    notes: List[str] = []

    # Which internal indexes this account can see at all. A search over an
    # index the account cannot read returns nothing and no error, so without
    # this "no access" and "nothing happened" look identical. None means the
    # check itself failed and every probe runs as before.
    readable = await _readable_indexes(client, timeout_s)

    async def run_probe(probe: Probe) -> None:
        try:
            rows, _ = await asyncio.wait_for(
                client.oneshot(probe.spl, window, count=50), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            probe.reason = f"the query did not answer within {timeout_s:.0f}s"
            return
        except SplunkError as exc:
            probe.reason = str(exc)[:300]
            return
        except Exception as exc:  # noqa: BLE001 - correlation never fails a run
            probe.reason = str(exc)[:300]
            return
        probe.available = True
        probe.rows = _drop_empty_aggregate(probe, rows)

    # Sequential rather than concurrent. These run against the cluster that has
    # just been load tested, and firing six searches at once at something still
    # recovering is a poor way to end a benchmark.
    for probe in probes:
        if readable is not None and probe.index and probe.index not in readable:
            probe.reason = (
                f"not run: this account cannot read {probe.index}, or it holds no events "
                "(| eventcount found none)"
            )
            notes.append(f"{probe.name}: {probe.reason}")
            continue
        await run_probe(probe)
        if not probe.available and probe.reason:
            notes.append(f"{probe.name}: {probe.reason}")

    result: Dict[str, Any] = {
        "window": {
            "earliest": started_at,
            "latest": latest,
            "run_ended_at": ended_at,
            "settled_s": settle_s,
        },
        "marker_prefix": marker_prefix,
        "sid_prefix": sid_prefix,
        "readable_indexes": sorted(readable) if readable is not None else None,
        "probes": {probe.name: probe.to_dict() for probe in probes},
        "notes": notes,
        # These searches ran on the cluster under test. Small, after the fact,
        # and recorded rather than pretended away.
        "self_load": {
            "queries": sum(1 for probe in probes if not probe.reason.startswith("not run:")) + 1,
            "note": (
                "these correlation searches ran on the target after the load stopped, so "
                "they add a small, known amount of work to the system being measured"
            ),
        },
    }
    result["findings"] = _findings(result)
    return result


ACCESS_CHECK_SPL = (
    "| eventcount summarize=false index=_audit index=_internal index=_introspection "
    "| stats sum(count) as events by index"
)


async def _readable_indexes(client: SplunkClient, timeout_s: float) -> Optional[set]:
    """The internal indexes this account can read that hold events, or None."""
    try:
        rows, _ = await asyncio.wait_for(
            client.oneshot(ACCESS_CHECK_SPL, TimeWindow(earliest=0, latest=time.time()), count=10),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - the check is a nicety, never a failure
        log.info("index access check failed, running every probe: %s", exc)
        return None
    if not any(isinstance(row, dict) and row.get("index") for row in rows):
        # Nothing usable came back: a target that does not answer eventcount
        # as expected is not evidence that every index is unreadable.
        return None
    readable = set()
    for row in rows:
        try:
            if float(row.get("events") or 0) > 0 and row.get("index"):
                readable.add(str(row["index"]))
        except (TypeError, ValueError):
            continue
    return readable


def _drop_empty_aggregate(probe: Probe, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """No rows, rather than one row of zeros, for an aggregate over nothing."""
    if not probe.count_field or not rows:
        return rows
    try:
        total = sum(float(row.get(probe.count_field) or 0) for row in rows)
    except (TypeError, ValueError):
        return rows
    return rows if total > 0 else []


def _findings(correlation: Dict[str, Any]) -> List[str]:
    """The handful of sentences a person actually reads."""
    findings: List[str] = []
    probes = correlation.get("probes", {})

    def first_row(name: str) -> Dict[str, Any]:
        probe = probes.get(name) or {}
        rows = probe.get("rows") or []
        return rows[0] if rows and probe.get("available") else {}

    ours = first_row("our_searches")
    ours_probe = probes.get("our_searches") or {}
    if ours.get("searches") is not None or ours_probe.get("available"):
        counted = int(float(ours.get("searches") or 0))
        p95 = _number(ours.get("p95_run_time_s"))
        tail = f", p95 run time {p95}s" if p95 != "unknown" else ""
        if counted:
            findings.append(
                f"the cluster's audit trail has {counted} completed searches from this "
                f"run{tail}. A floor rather than a total: audit records keep arriving for "
                "a minute or two after a run and correlation happens immediately"
            )
        else:
            pages = first_row("our_page_loads")
            if pages.get("page_loads") and int(float(pages["page_loads"] or 0)):
                findings.append(
                    f"this was a browser run: {int(float(pages['page_loads']))} dashboard "
                    f"loads carry the run marker in the web access log (p95 "
                    f"{_number(pages.get('p95_spent_ms'))}ms server side). The searches a "
                    "page fires are Splunk Web's own and carry no marker, so they appear "
                    "under the aggregate probes only"
                )
            else:
                findings.append(
                    "the cluster's audit trail has none of this run's searches yet. Audit "
                    "records go through the indexing pipeline, so on a busy indexer they can "
                    "take longer to appear than the settle delay allows"
                )

    scheduler = probes.get("scheduler") or {}
    if scheduler.get("available"):
        skipped = sum(
            int(float(row.get("count", 0) or 0))
            for row in scheduler.get("rows") or []
            if row.get("status") == "skipped"
        )
        if skipped:
            findings.append(
                f"{skipped} scheduled search(es) were skipped during this run. Past the "
                "concurrency ceiling the first casualty is usually somebody else's "
                "scheduled work rather than the test's own searches"
            )

    telemetry = first_row("search_telemetry")
    if telemetry.get("avg_indexer_ms"):
        findings.append(
            f"mean indexer-side (map phase) elapsed time {_number(telemetry.get('avg_indexer_ms'))}ms "
            f"against {_number(telemetry.get('avg_reduce_ms'))}ms on the search head: compare "
            "with client latency to see where the time went"
        )
    elif telemetry.get("avg_reduce_ms"):
        findings.append(
            f"search telemetry reports only a search-head phase (mean "
            f"{_number(telemetry.get('avg_reduce_ms'))}ms): there was no distributed map "
            "phase, which is what a single instance looks like"
        )

    resources_probe = probes.get("resource_usage") or {}
    for row in (resources_probe.get("rows") or []) if resources_probe.get("available") else []:
        if not row.get("max_user_cpu_pct"):
            continue
        peak = float(row["max_user_cpu_pct"])
        host = row.get("host") or "the target"
        findings.append(f"peak user CPU on {host} was {peak:.0f}%")
        if peak > 90:
            findings.append(
                f"{host} was CPU bound at peak, so latency past that point describes a "
                "saturated machine rather than a search-tier characteristic"
            )

    # The cold path's cost, from the same audit records: seconds the searches
    # spent waiting on bucket-cache misses, and what share of lookups missed.
    if ours.get("cache_lookups") not in (None, "", "0", 0):
        try:
            cold_s = float(ours.get("cold_path_s") or 0)
            miss_pct = float(ours.get("cache_miss_pct") or 0)
        except (TypeError, ValueError):
            cold_s, miss_pct = 0.0, 0.0
        if miss_pct > 0 or cold_s > 0:
            findings.append(
                f"{miss_pct:.1f}% of this run's bucket-cache lookups missed and the misses cost "
                f"{cold_s:.1f}s of search time in total: the cold path's price, per the audit trail"
            )
        else:
            findings.append("every bucket-cache lookup this run's searches made was a hit: a warm run by the cluster's own account")

    # And where that price was paid: the step whose searches waited longest on
    # misses, as a share of its own run time.
    by_step = probes.get("our_searches_by_step") or {}
    costly = None
    for row in (by_step.get("rows") or []) if by_step.get("available") else []:
        try:
            waited = float(row.get("cold_path_s") or 0)
            run_time = float(row.get("run_time_s") or 0)
            misses = int(float(row.get("cache_misses") or 0))
        except (TypeError, ValueError):
            continue
        if waited > 0 and row.get("step") != UNATTRIBUTED_STEP and (costly is None or waited > costly[1]):
            costly = (str(row.get("step")), waited, run_time, misses)
    if costly is not None:
        step, waited, run_time, misses = costly
        share = f"{100 * waited / run_time:.0f}% of its run time" if run_time > 0 else f"{waited:.1f}s"
        findings.append(
            f"step {step} spent {share} waiting on {misses} bucket-cache miss(es) "
            f"({waited:.1f}s of {run_time:.1f}s): the step the cold path cost most"
        )

    queueing = first_row("queueing")
    if queueing.get("enqueued") not in (None, "", "0", 0):
        try:
            enqueued = int(float(queueing.get("enqueued") or 0))
        except (TypeError, ValueError):
            enqueued = 0
        if enqueued:
            reasons_probe = probes.get("queueing_reasons") or {}
            reasons = [str(row.get("reason") or "") for row in (reasons_probe.get("rows") or []) if reasons_probe.get("available")]
            by_role = any("role" in reason.lower() for reason in reasons)
            findings.append(
                f"the search head queued {enqueued} search(es) in the window (deepest queue "
                f"{_number(queueing.get('largest_queue_size'))}, longest wait {_number(queueing.get('max_queued_s'))}s)"
                + (": a per-role quota was among the reasons, which is the account's limit rather than the instance's ceiling"
                   if by_role else ": the instance's concurrent-search ceiling")
            )

    if not findings:
        findings.append(
            "no server-side correlation was available: the load-test account most likely "
            "cannot read _audit, _internal or _introspection. The run stands on its "
            "client-side measurement"
        )
    return findings


def _number(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "unknown"


def marker_prefix_for(run_id: str) -> str:
    """The stem every search in a run carries, so _audit can isolate it.

    Matches ``cache_bust_marker`` in params.py, which builds
    ``reg:<run>:vu<n>:i<n>:<step>``. Only the run portion is needed to select a
    run's searches and exclude everyone else's.

    Sanitised through the same filter, because this value is interpolated into
    a quoted SPL operand: an unfiltered run label containing a double quote
    would close the string and append arbitrary SPL to a search that runs under
    the target's credentials.
    """
    return f"reg:{sanitise_marker_part(run_id)}:"
