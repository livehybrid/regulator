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


async def target_report(client: SplunkClient) -> Dict[str, Any]:
    """Everything Regulator can discover about a target, as plain JSON."""
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

    report["recommended_scenario"] = _recommend(report)
    return report


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

    if report.get("notes"):
        lines += ["", "notes:"]
        lines += [f"  - {note}" for note in report["notes"]]
    return "\n".join(lines)
