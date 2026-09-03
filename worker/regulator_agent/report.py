"""Describe a target cluster well enough to design a benchmark for it.

Run this against a Splunk you have never load-tested and it answers the
questions that decide whether a benchmark is worth running at all, and how to
read the numbers afterwards:

* **What is it?** Version, roles, cores, memory. A single instance and a
  six-indexer cluster behind a search head produce completely different numbers
  from the same scenario, and only one of them tells you anything about
  distributed search.
* **How much can it run at once?** ``base_max_searches + (max_searches_per_cpu
  x cores)`` is the ceiling everything queues behind. Without it a report can
  say latency got worse but not that it got worse *because you crossed the
  line*.
* **Is there anything in it?** A search against an empty index returns nothing
  in milliseconds and makes a cluster look gloriously fast. Event counts and
  time ranges per index are what say whether a scenario has a corpus, and which
  scenario is appropriate.
* **Is it SmartStore?** If it is, a rare search over a wide window may be
  measuring an object-storage fetch rather than the search tier, which is worth
  knowing before drawing conclusions about indexer sizing.
* **Can this account actually work?** Being able to log in, read configuration
  and dispatch a search are three different permissions, and a load test that
  discovers the third one is missing does so after wasting everybody's evening.

Everything here degrades: a permission the account lacks becomes a note in the
report, never a failure. The point is to get a useful answer from whatever
access you have, then say plainly what was not visible.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .smartstore import cache_state
from .splunk import SplunkClient, SplunkError
from .timepolicy import TimeWindow

# Indexes Splunk ships and manages itself. They are worth reporting separately:
# _internal always has data, so it makes a scenario runnable against an
# otherwise empty cluster, but a benchmark built on it measures Splunk's own
# logging rather than the customer's workload.
INTERNAL_PREFIXES = ("_",)


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None



# How far back the sourcetype census looks. Long enough to see anything with a
# regular heartbeat, short enough that tstats stays quick on a large estate.
SOURCETYPE_WINDOW_S = 7 * 86400
SOURCETYPE_LIMIT = 40


async def _sourcetypes(client: SplunkClient, notes: List[str]) -> List[Dict[str, Any]]:
    """What is actually in the indexes, by volume.

    Runs against the TSIDX metadata rather than the raw events, so it costs
    almost nothing and does not itself become a load test.
    """
    now = time.time()
    window = TimeWindow(earliest=now - SOURCETYPE_WINDOW_S, latest=now)
    spl = "| tstats count where index=* by index, sourcetype | sort - count"
    try:
        rows, _ = await client.oneshot(spl, window, count=SOURCETYPE_LIMIT)
    except SplunkError as exc:
        notes.append(
            f"the sourcetype census failed ({exc}), so this report cannot say whether a "
            "scenario's searches match anything in this cluster"
        )
        return []

    census = []
    for row in rows:
        count = _int(row.get("count")) or 0
        census.append(
            {
                "index": row.get("index"),
                "sourcetype": row.get("sourcetype"),
                "events_7d": count,
            }
        )
    if not census:
        notes.append(
            f"no events at all in the last {SOURCETYPE_WINDOW_S // 86400} days. The "
            "cluster may hold only older data, in which case scenarios using a rolling "
            "time window will search empty ranges and return instantly"
        )
    return census


async def target_report(
    client: SplunkClient,
    scenarios: Optional[List[Dict[str, Any]]] = None,
    **where: Any,
) -> Dict[str, Any]:
    """Everything Regulator can discover about a target, as plain JSON.

    ``scenarios`` is the scenario library as summaries (name plus
    corpus.sourcetypes), so the report can say which of them this cluster
    actually holds data for. ``where`` locates the indexers for the cache.
    """
    report: Dict[str, Any] = {
        "generated_at": time.time(),
        "target_url": client.target.url,
        "auth_method": client.auth_method,
        "notes": [],
    }
    notes: List[str] = report["notes"]

    # ---------------------------------------------------------------- what
    try:
        info = await client.server_info()
    except SplunkError as exc:
        report["reachable"] = False
        notes.append(f"could not read /services/server/info: {exc}")
        return report

    report["reachable"] = True
    roles = info.get("server_roles") or []
    if isinstance(roles, str):
        roles = [roles]
    cores = _int(info.get("numberOfVirtualCores")) or _int(info.get("numberOfCores")) or 0
    report["instance"] = {
        "version": info.get("version"),
        "build": info.get("build"),
        "server_name": info.get("serverName"),
        "roles": list(roles),
        "cores": cores,
        "physical_memory_mb": _int(info.get("physicalMemoryMB")),
        "os_name": info.get("os_name"),
        "product_type": info.get("product_type"),
        "mode": info.get("mode"),
    }

    # ------------------------------------------------------------ ceilings
    limits = {}
    try:
        limits = await client.search_limits()
    except SplunkError as exc:
        notes.append(f"limits.conf [search] not readable: {exc}")
    base = _int(limits.get("base_max_searches"))
    per_cpu = _int(limits.get("max_searches_per_cpu"))
    ceiling = base + (per_cpu * cores) if (base is not None and per_cpu is not None and cores) else None
    report["concurrency"] = {
        "base_max_searches": base,
        "max_searches_per_cpu": per_cpu,
        "max_searches_perc": _int(limits.get("max_searches_perc")),
        "max_rt_search_multiplier": _int(limits.get("max_rt_search_multiplier")),
        "max_hist_searches": ceiling,
    }
    if ceiling is None:
        notes.append(
            "the concurrent-search ceiling could not be computed, so a report cannot show "
            "where queueing starts. Grant the account list_settings if you want that line"
        )

    # -------------------------------------------------------- search tier
    peers = await client.entries(
        "/services/search/distributed/peers", "distributed peers"
    )
    report["search_peers"] = [
        {
            "name": p.get("name"),
            "status": p.get("status"),
            "version": p.get("version"),
            "disabled": p.get("disabled"),
        }
        for p in peers
    ]
    if not peers:
        notes.append(
            "no distributed search peers: this is a single instance, so a run here "
            "exercises nothing of bundle replication, the map phase across peers, or the "
            "search-head concurrency split. Useful for proving the harness works, not for "
            "sizing a cluster"
        )

    shc = await client.entries("/services/shcluster/member/info", "search head cluster")
    report["search_head_cluster"] = shc[0] if shc else None

    # -------------------------------------------------------------- corpus
    indexes = await client.entries(
        "/services/data/indexes", "indexes", params={"datatype": "all"}
    )
    described: List[Dict[str, Any]] = []
    smartstore = 0
    for index in indexes:
        name = index.get("name") or ""
        if str(index.get("disabled")) in ("1", "true", "True"):
            continue
        events = _int(index.get("totalEventCount")) or 0
        remote = index.get("remotePath")
        if remote:
            smartstore += 1
        if not events and name.startswith(INTERNAL_PREFIXES):
            continue
        described.append(
            {
                "name": name,
                "datatype": index.get("datatype", "event"),
                "events": events,
                "size_mb": _int(index.get("currentDBSizeMB")),
                "earliest": index.get("minTime"),
                "latest": index.get("maxTime"),
                "smartstore": bool(remote),
                "internal": name.startswith(INTERNAL_PREFIXES),
            }
        )
    described.sort(key=lambda i: i["events"], reverse=True)
    report["indexes"] = described
    report["smartstore_indexes"] = smartstore

    if not indexes:
        notes.append(
            "the index list was not readable by this account, so the report cannot say "
            "whether there is a corpus to search. Check by hand before choosing a scenario"
        )
    else:
        with_data = [i for i in described if i["events"] and not i["internal"]]
        if not with_data:
            notes.append(
                "no non-internal index holds any events: every scenario except 'smoke' "
                "would search nothing and return in milliseconds, which reads as a very "
                "fast cluster. Fill the cluster with Stoker first, or run 'smoke' only"
            )
        if smartstore:
            notes.append(
                f"{smartstore} index(es) use SmartStore: a rare search over a wide window "
                "may be measuring a cache miss and an object-storage fetch rather than the "
                "search tier. Size the local cache before drawing conclusions"
            )

    # --------------------------------------------------------- sourcetypes
    # Knowing an index holds 800 million events does not tell you what is in
    # it, and a scenario written against sourcetypes this cluster does not have
    # searches nothing while looking perfectly healthy. One tstats over the
    # accelerated metadata answers it in seconds even on a large estate.
    report["sourcetypes"] = await _sourcetypes(client, notes)

    # -------------------------------------------------- smartstore cache
    # On SmartStore the local disk is a cache in front of object storage, so
    # the same search is a different piece of work depending on what is already
    # local. How full that cache is decides whether a wide search evicts
    # somebody else's buckets while it runs.
    cache = await cache_state(client, **where)
    report["smartstore_cache"] = cache.to_dict()
    if cache.available:
        fill = cache.fill_pct
        if fill is not None and fill > 90:
            notes.append(
                f"the SmartStore cache is {fill:.0f}% full: a wide search will evict "
                "buckets other searches are using, so a benchmark here measures cache "
                "churn as much as the search tier"
            )
        if cache.local_pct < 50:
            notes.append(
                f"only {cache.local_pct:.0f}% of buckets are local: searches over older "
                "data will pay an object-storage fetch before they can filter anything. "
                "Expect a cold first run and a much faster second one"
            )

    # ------------------------------------------------------- can we work?
    now = time.time()
    try:
        await client.oneshot("| makeresults count=1", TimeWindow(now - 60, now), count=1)
        report["can_dispatch"] = True
    except SplunkError as exc:
        report["can_dispatch"] = False
        notes.append(
            f"this account cannot dispatch a trivial search ({exc}), so no scenario can "
            "run. It needs a role with the search capability and srchIndexesAllowed "
            "covering the corpus"
        )

    report["scenario_fit"] = _scenario_fit(report, scenarios or [])
    report["recommended_scenario"] = _recommend(report)
    return report


def _scenario_fit(report: Dict[str, Any], scenarios: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Which scenarios this cluster holds data for, from the sourcetype census.

    A scenario that names sourcetypes the census does not show searches
    nothing and reports a magnificent cluster, so the fit is worked out
    before anyone chooses. Scenarios that declare no sourcetypes are listed
    as unknown rather than guessed at.
    """
    present = {str(row.get("sourcetype")) for row in report.get("sourcetypes") or []}
    fit: List[Dict[str, Any]] = []
    for scenario in scenarios:
        wanted = [st for st in (scenario.get("sourcetypes") or []) if st]
        if not wanted:
            fit.append({"name": scenario.get("name"), "fit": "unknown", "missing": []})
            continue
        missing = [st for st in wanted if st not in present]
        fit.append(
            {
                "name": scenario.get("name"),
                "fit": "full" if not missing else ("partial" if len(missing) < len(wanted) else "none"),
                "missing": missing,
            }
        )
    order = {"full": 0, "partial": 1, "unknown": 2, "none": 3}
    fit.sort(key=lambda f: (order.get(f["fit"], 9), str(f["name"])))
    return fit


def _recommend(report: Dict[str, Any]) -> str:
    """Say which shipped scenario is honest to run against this target."""
    if not report.get("can_dispatch"):
        return "none: the account cannot dispatch a search"
    with_data = [
        i for i in report.get("indexes", []) if i.get("events") and not i.get("internal")
    ]
    if not with_data:
        return (
            "smoke: nothing else has a corpus. Fill with Stoker, then search-classes"
        )
    fits = [f["name"] for f in report.get("scenario_fit", []) if f.get("fit") == "full"]
    partial = [f["name"] for f in report.get("scenario_fit", []) if f.get("fit") == "partial"]
    if fits:
        names = ", ".join(str(n) for n in fits[:4])
        suffix = (
            "; read it as a harness check rather than a sizing exercise, a single instance "
            "has no distributed search to measure"
            if not report.get("search_peers")
            else ""
        )
        return f"the census matches {names}{suffix}"
    if partial:
        return (
            f"no scenario has its whole corpus here; {', '.join(str(n) for n in partial[:3])} "
            "partly. Fill with Stoker, or import the cluster's own saved searches"
        )
    if not report.get("search_peers"):
        return (
            "search-classes, but read it as a harness check rather than a sizing exercise: "
            "a single instance has no distributed search to measure"
        )
    return "search-classes first (one search per class), then soc-analyst-morning"


def render(report: Dict[str, Any]) -> str:
    """A short human summary, for pasting into a chat rather than a dashboard."""
    if not report.get("reachable"):
        return f"UNREACHABLE {report['target_url']}: {'; '.join(report.get('notes', []))}"

    instance = report.get("instance", {})
    concurrency = report.get("concurrency", {})
    peers = report.get("search_peers", [])
    indexes = [i for i in report.get("indexes", []) if not i["internal"] and i["events"]]

    lines = [
        f"target        {report['target_url']} ({report.get('auth_method')})",
        f"instance      Splunk {instance.get('version')} on {instance.get('os_name')}, "
        f"{instance.get('cores')} cores, {instance.get('physical_memory_mb')} MB",
        f"roles         {', '.join(instance.get('roles') or []) or 'unknown'}",
        f"search peers  {len(peers)}",
        f"concurrency   max_hist_searches={concurrency.get('max_hist_searches')} "
        f"(base {concurrency.get('base_max_searches')} + "
        f"{concurrency.get('max_searches_per_cpu')}/cpu x {instance.get('cores')})",
        f"smartstore    {report.get('smartstore_indexes', 0)} index(es)",
        f"can dispatch  {report.get('can_dispatch')}",
        f"recommended   {report.get('recommended_scenario')}",
        "",
        "indexes with data:",
    ]
    if indexes:
        for index in indexes[:15]:
            lines.append(
                f"  {index['name']:<24} {index['events']:>12,} events  "
                f"{index['size_mb'] or 0:>8} MB  {index['datatype']}"
                + ("  smartstore" if index["smartstore"] else "")
            )
    else:
        lines.append("  (none)")

    census = report.get("sourcetypes") or []
    if census:
        lines += ["", "sourcetypes with data (last 7 days):"]
        for row in census[:12]:
            lines.append(
                f"  {str(row['index'])[:20]:<20} {str(row['sourcetype'])[:30]:<30} "
                f"{row['events_7d']:>12,}"
            )

    cache = report.get("smartstore_cache") or {}
    if cache.get("available"):
        lines += [
            "",
            f"cache         {cache['local_buckets']}/{cache['total_buckets']} buckets local "
            f"({cache['local_pct']:.0f}%), {cache['local_bytes'] / 1e9:.1f} GB of "
            f"{((cache.get('max_cache_size_mb') or 0) * 1024 * 1024) / 1e9:.1f} GB"
            + (f", {cache['fill_pct']:.0f}% full" if cache.get("fill_pct") is not None else "")
            + f", policy {cache.get('eviction_policy') or 'unknown'}",
        ]

    fits = report.get("scenario_fit") or []
    if fits:
        lines += ["", "scenario fit against the census:"]
        for entry in fits[:12]:
            missing = f"  missing {', '.join(entry['missing'])}" if entry.get("missing") else ""
            lines.append(f"  {str(entry['name'])[:28]:<28} {entry['fit']:<8}{missing}")

    if report.get("notes"):
        lines += ["", "notes:"]
        lines += [f"  - {note}" for note in report["notes"]]
    return "\n".join(lines)
