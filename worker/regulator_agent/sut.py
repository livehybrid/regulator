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
    # How many rows to ask for. Most probes return a handful; one split two
    # ways (step by member) can return many more.
    max_rows: int = 50
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


# Written into every correlation probe's search string, so the probes can
# leave their own audit records out of the run's. See build_probes.
CORRELATION_TAG = "regulator-correlation-probe"

# The member an audit record lands under when it names no host.
UNKNOWN_MEMBER = "(member not recorded)"
# The member a search with an issue lands under when the audit trail held no
# record of its search id when it was read.
NO_AUDIT_RECORD = "(no audit record yet)"
# Steps by members can pass the usual 50 rows: 40 steps on 3 members is 120.
MEMBER_PROBE_ROWS = 500

# What each step's audit records add up to, shared by the split by step and
# the split by step and member so the two can never disagree on a figure.
_ATTRIBUTION_STATS = (
    "count as searches, sum(total_run_time) as run_time_s, "
    "sum(searched_buckets) as searched_buckets, sum(eliminated_buckets) as eliminated_buckets, "
    "sum(invocations_command_search_index_bucketcache_hit) as index_cache_hits, "
    "sum(invocations_command_search_index_bucketcache_miss) as index_cache_misses, "
    "sum(duration_command_search_index_bucketcache_miss) as index_miss_s, "
    "sum(invocations_command_search_rawdata_bucketcache_hit) as rawdata_cache_hits, "
    "sum(invocations_command_search_rawdata_bucketcache_miss) as rawdata_cache_misses, "
    "sum(duration_command_search_rawdata_bucketcache_miss) as rawdata_miss_s"
)
_ATTRIBUTION_EVALS = (
    "cold_path_s=coalesce(index_miss_s, 0) + coalesce(rawdata_miss_s, 0), "
    "cache_hits=coalesce(index_cache_hits, 0) + coalesce(rawdata_cache_hits, 0), "
    "cache_misses=coalesce(index_cache_misses, 0) + coalesce(rawdata_cache_misses, 0), "
    "cache_miss_pct=if(cache_hits + cache_misses > 0, round(100 * cache_misses / (cache_hits + cache_misses), 2), null()), "
    "cold_path_pct=if(run_time_s > 0, round(100 * cold_path_s / run_time_s, 1), null())"
)


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
    # Not the correlation's own searches. Each probe names the run marker in
    # its own search string, so a probe's audit record matched the next
    # probe's selection and counted as one of the run's searches (seen on the
    # contract cluster). The exclusion term is written into every probe by
    # this very clause, so a probe excludes itself and every other probe,
    # and no search a scenario dispatches carries it.
    ours = f'{ours} NOT "{CORRELATION_TAG}"'
    attribution_stats, attribution_evals = _ATTRIBUTION_STATS, _ATTRIBUTION_EVALS
    step_from_marker = (
        f'| rex field=search "{_marker_regex(marker_prefix)}vu\\d+:i\\d+:(?<reg_step>[A-Za-z0-9_.:-]+)" '
        f'| eval step=coalesce(reg_step, "{UNATTRIBUTED_STEP}")'
    )
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
                f"{step_from_marker} "
                f"| stats {attribution_stats} by step "
                f"| eval {attribution_evals} "
                "| sort - cold_path_s, - run_time_s"
            ),
        ),
        Probe(
            name="our_searches_by_member",
            description=(
                "the same split again by search head cluster member: the host on each audit "
                "record is the member that dispatched and ran the search, which behind a load "
                "balancer is the only place the run can learn it"
            ),
            # The host of an _audit record is the instance that wrote it, and
            # only the member that dispatched a job ever knows it (contract
            # suite, 10.6), so the writer is the member that served it. On a
            # standalone search head every row names the one host. A record
            # without a host is counted under a row that says so.
            max_rows=MEMBER_PROBE_ROWS,
            spl=(
                f"search index=_audit action=search info=completed {ours} "
                f"{step_from_marker} "
                f'| eval member=coalesce(host, "{UNKNOWN_MEMBER}") '
                f"| stats {attribution_stats} by step, member "
                f"| eval {attribution_evals} "
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
    issue_sids: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ask the cluster what it thought was happening. Never raises.

    ``issue_sids`` is the run's ``IssueSids`` document (results.py): the search
    ids of its searches with an issue. Given one, the issues are also split by
    the search head cluster member that served each search.
    """
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
                client.oneshot(probe.spl, window, count=probe.max_rows), timeout=timeout_s
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

    by_member = None
    lookups = 0
    if issue_sids and any(issue_sids.get(kind) for kind in ISSUE_KINDS):
        if readable is not None and "_audit" not in readable:
            by_member = {
                "available": False,
                "reason": "not run: this account cannot read _audit, or it holds no events",
            }
        else:
            by_member, lookups = await _issues_by_member(client, window, issue_sids, timeout_s)
        if not by_member.get("available") and by_member.get("reason"):
            notes.append(f"issues_by_member: {by_member['reason']}")

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
            "queries": sum(1 for probe in probes if not probe.reason.startswith("not run:")) + 1 + lookups,
            "note": (
                "these correlation searches ran on the target after the load stopped, so "
                "they add a small, known amount of work to the system being measured"
            ),
        },
    }
    if by_member is not None:
        result["issues_by_member"] = by_member
    result["findings"] = _findings(result)
    return result


# ---------------------------------------------------------------------------
# Issues by search head cluster member
# ---------------------------------------------------------------------------

# The kinds results.IssueSids keeps, in the order the panel shows them.
ISSUE_KINDS = ("failed", "partial", "queued")
# Search ids per lookup search: long enough to keep the number of searches
# small, short enough to keep each search string reasonable.
MEMBER_LOOKUP_BATCH = 100
# A search id goes into a quoted SPL operand, so only the characters a search
# id is made of are let through; anything else is reported as unattributed.
_SAFE_SID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def member_lookup_spl(sids: List[str]) -> str:
    """One search naming the member that wrote the audit records of each sid.

    ``_audit`` writes search_id wrapped in single quotes, so each id is
    matched both quoted and bare, and the quotes are trimmed before grouping.
    Exact values rather than wildcards: ``*_1*`` would also match ``_10``
    to ``_19``. Any action=search record will do: every one of them is
    written by the member that holds the job.
    """
    terms = ", ".join(f'"{sid}", "\'{sid}\'"' for sid in sids)
    return (
        f"search index=_audit action=search search_id IN ({terms}) "
        "| eval sid=trim(search_id, \"'\") "
        "| stats latest(host) as member, dc(host) as members by sid"
    )


async def members_for_sids(
    client: SplunkClient, window: TimeWindow, sids: List[str], timeout_s: float = QUERY_TIMEOUT_S
) -> "tuple[Dict[str, str], int, str]":
    """sid -> member, from the audit trail; with the searches it took and a reason if any failed."""
    wanted = sorted({sid for sid in sids if _SAFE_SID.match(sid)})
    found: Dict[str, str] = {}
    queries = 0
    reason = ""
    for start in range(0, len(wanted), MEMBER_LOOKUP_BATCH):
        batch = wanted[start:start + MEMBER_LOOKUP_BATCH]
        queries += 1
        try:
            rows, _ = await asyncio.wait_for(
                client.oneshot(member_lookup_spl(batch), window, count=len(batch) + 10),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            reason = f"a member lookup did not answer within {timeout_s:.0f}s"
            break
        except Exception as exc:  # noqa: BLE001 - correlation never fails a run
            reason = str(exc)[:300]
            break
        batch_set = set(batch)
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("sid") or "").strip("'")
            member = str(row.get("member") or "")
            if sid in batch_set and member:
                found[sid] = member
    return found, queries, reason


def split_issues_by_member(issue_sids: Dict[str, Any], members: Dict[str, str]) -> List[Dict[str, Any]]:
    """Rows of (step, member) with the count of each issue kind, failures by class."""
    rows: Dict[tuple, Dict[str, Any]] = {}
    for kind in ISSUE_KINDS:
        for entry in issue_sids.get(kind) or []:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            sid, step = str(entry[0]), str(entry[1])
            member = members.get(sid, NO_AUDIT_RECORD)
            row = rows.setdefault(
                (step, member),
                {"step": step, "member": member, "failed": 0, "partial": 0, "queued": 0, "errors_by_class": {}},
            )
            row[kind] += 1
            if kind == "failed":
                cls = str(entry[2]) if len(entry) > 2 and entry[2] else "client"
                row["errors_by_class"][cls] = row["errors_by_class"].get(cls, 0) + 1
    return sorted(rows.values(), key=lambda r: (r["step"], r["member"] == NO_AUDIT_RECORD, r["member"]))


async def _issues_by_member(
    client: SplunkClient, window: TimeWindow, issue_sids: Dict[str, Any], timeout_s: float
) -> "tuple[Dict[str, Any], int]":
    sids = [
        str(entry[0])
        for kind in ISSUE_KINDS
        for entry in issue_sids.get(kind) or []
        if isinstance(entry, (list, tuple)) and entry
    ]
    members, queries, reason = await members_for_sids(client, window, sids, timeout_s)
    if reason and not members:
        return {"available": False, "reason": reason}, queries
    out: Dict[str, Any] = {
        "available": True,
        "reason": reason,
        "rows": split_issues_by_member(issue_sids, members),
        "members": sorted(set(members.values())),
        "looked_up": len(set(sids)),
        "found": len(members),
        # Kept per kind by the worker up to a cap; past it, counted only. A
        # split with anything here is a sample of that kind, not all of it.
        "dropped": dict(issue_sids.get("dropped") or {}),
        # Issues on searches that never had a search id: no member to name.
        "no_sid": dict(issue_sids.get("no_sid") or {}),
    }
    return out, queries


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

    findings.extend(_member_findings(correlation.get("issues_by_member") or {}))

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


# A member is called out when it holds at least this share of a kind of issue
# that at least MEMBER_FINDING_MIN searches had, across two members or more.
MEMBER_FINDING_SHARE = 0.6
MEMBER_FINDING_MIN = 5


def _member_findings(by_member: Dict[str, Any]) -> List[str]:
    """A member that had most of an issue, which points at the member rather than the cluster."""
    if not by_member.get("available"):
        return []
    named = [m for m in by_member.get("members") or [] if m not in (NO_AUDIT_RECORD, UNKNOWN_MEMBER)]
    if len(named) < 2:
        return []
    out: List[str] = []
    labels = {"failed": "failures", "partial": "partial results", "queued": "queued searches"}
    for kind in ISSUE_KINDS:
        totals: Dict[str, int] = {}
        for row in by_member.get("rows") or []:
            member = row.get("member")
            if member in (NO_AUDIT_RECORD, UNKNOWN_MEMBER):
                continue
            totals[member] = totals.get(member, 0) + int(row.get(kind) or 0)
        attributed = sum(totals.values())
        if attributed < MEMBER_FINDING_MIN:
            continue
        member, most = max(totals.items(), key=lambda item: item[1])
        if most / attributed >= MEMBER_FINDING_SHARE:
            sample = " (of a sample)" if (by_member.get("dropped") or {}).get(kind) else ""
            out.append(
                f"search head cluster member {member} had {most} of the {attributed} {labels[kind]} "
                f"the audit trail could place{sample}, across {len(named)} members: look at that member "
                "before the cluster"
            )
    return out


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
