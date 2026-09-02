"""What the cluster thought was happening, pulled back after a run.

Everything else in Regulator measures from the outside: how long the client
waited, what the job reported. This module asks the cluster its own opinion,
and the difference between "p95 went from 4 s to 11 s" and "p95 went from 4 s to
11 s because indexer CPU hit 95% and 340 scheduled searches were skipped" is the
difference between a load test and a benchmark.

Five questions, each answered by one search against Splunk's own internal
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
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .params import sanitise_marker_part
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
    rows: List[Dict[str, Any]] = field(default_factory=list)
    available: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "description": self.description,
            "rows": self.rows,
        }


def build_probes(marker_prefix: str) -> List[Probe]:
    """The five questions.

    ``marker_prefix`` is the run's cache-busting marker stem, which every
    dispatched search carries as a Splunk comment. Matching on it isolates this
    run's searches from everything else the cluster was doing, which matters on
    a shared cluster where the answer would otherwise be everybody's traffic.
    """
    return [
        Probe(
            name="our_searches",
            description=(
                "the cluster's own account of the searches this run dispatched, matched "
                "by the run marker each one carries as a Splunk comment"
            ),
            spl=(
                f'search index=_audit action=search "{marker_prefix}" '
                "| stats count as searches, "
                "avg(total_run_time) as avg_run_time_s, "
                "perc95(total_run_time) as p95_run_time_s, "
                "max(total_run_time) as max_run_time_s"
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
                "search index=_audit action=search "
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
            spl=(
                "search index=_introspection sourcetype=search_telemetry "
                "| stats count as searches, "
                "avg('phases.phase_0.elapsed_time_aggregations.avg') as avg_indexer_ms, "
                "perc95('phases.phase_0.elapsed_time_aggregations.max') as p95_indexer_ms"
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
                "search index=_internal component=CacheManager "
                "| stats count by log_level, component"
            ),
        ),
        Probe(
            name="resource_usage",
            description=(
                "search head and indexer CPU over the run, so a latency curve can be "
                "laid against the machine's own load"
            ),
            spl=(
                "search index=_introspection component=Hostwide "
                "| stats avg('data.cpu_system_pct') as avg_system_cpu_pct, "
                "avg('data.cpu_user_pct') as avg_user_cpu_pct, "
                "max('data.cpu_user_pct') as max_user_cpu_pct"
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
    probes = build_probes(marker_prefix)
    notes: List[str] = []

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
        probe.rows = rows

    # Sequential rather than concurrent. These run against the cluster that has
    # just been load tested, and firing six searches at once at something still
    # recovering is a poor way to end a benchmark.
    for probe in probes:
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
        "probes": {probe.name: probe.to_dict() for probe in probes},
        "notes": notes,
        # These searches ran on the cluster under test. Small, after the fact,
        # and recorded rather than pretended away.
        "self_load": {
            "queries": len(probes),
            "note": (
                "these correlation searches ran on the target after the load stopped, so "
                "they add a small, known amount of work to the system being measured"
            ),
        },
    }
    result["findings"] = _findings(result)
    return result


def _findings(correlation: Dict[str, Any]) -> List[str]:
    """The handful of sentences a person actually reads."""
    findings: List[str] = []
    probes = correlation.get("probes", {})

    def first_row(name: str) -> Dict[str, Any]:
        probe = probes.get(name) or {}
        rows = probe.get("rows") or []
        return rows[0] if rows and probe.get("available") else {}

    ours = first_row("our_searches")
    if ours.get("searches"):
        counted = int(float(ours["searches"] or 0))
        p95 = _number(ours.get("p95_run_time_s"))
        tail = f", p95 run time {p95}s" if p95 != "unknown" else ""
        if counted:
            findings.append(
                f"the cluster's audit trail has at least {counted} of this run's "
                f"searches{tail}. This is a floor rather than a total: audit records "
                "keep arriving for a minute or two after a run, and correlation happens "
                "immediately, so use the aggregate probes below for analysis"
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
            f"mean indexer-side elapsed time {_number(telemetry.get('avg_indexer_ms'))}ms: "
            "compare with client latency to see whether the time went to the indexers or "
            "to the search head"
        )

    resources = first_row("resource_usage")
    if resources.get("max_user_cpu_pct"):
        peak = float(resources["max_user_cpu_pct"])
        findings.append(f"peak user CPU on the target was {peak:.0f}%")
        if peak > 90:
            findings.append(
                "the target was CPU bound at peak, so latency past that point describes "
                "a saturated machine rather than a search-tier characteristic"
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
