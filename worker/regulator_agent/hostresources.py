"""What the machines were doing while the run was going: CPU, memory, disk.

A latency curve says the target got slower. It does not say which tier ran
out, or of what. splunkd already records the answer: every full Splunk
instance writes its host's CPU, memory and load to ``_introspection`` every
ten seconds (``splunk_resource_usage``, component ``Hostwide``) and its disks'
throughput and wait every minute (component ``IOStats``). This module reads
those back after the run, in time buckets, and sorts the hosts into search
heads and indexers so the two tiers can be laid against each other and against
the run's own latency on one time axis.

Three searches and three REST reads, after the load has stopped. Nothing is
sampled during the run: the numbers are the target's own and were going to be
written whether or not anyone read them.

Two things this cannot do, and says so rather than guessing:

* It sees only the hosts whose ``_introspection`` is searchable from the
  search head the run talked to. Indexers always are. Other search head
  cluster members are only if they forward their internal logs to the
  indexers, which is the usual practice and not a guarantee.
* A host's role is taken from the search head's own lists of its peers and
  cluster members. A host in the data that is on neither list is reported as
  "unknown" rather than assigned to a tier.

The ``Hostwide`` CPU fields are the ones the correlation probes already use,
confirmed on a live 10.4 instance. The memory, load and ``IOStats`` field
names are from splunkd's documented schema and have NOT been confirmed on a
live cluster: a field a version does not write comes back empty, and the
column for it stays empty, rather than anything being invented.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .splunk import SplunkClient, SplunkError
from .timepolicy import TimeWindow

log = logging.getLogger("regulator.hostresources")

QUERY_TIMEOUT_S = 90.0
# Before the run, for a baseline: what the machines were doing unprovoked.
LEAD_S = 180.0
# After it: the tail of the last searches, and the recovery.
TAIL_S = 60.0
# About this many buckets across the window, whatever the run's length.
TARGET_POINTS = 90
# Hostwide is written every 10 s; a bucket narrower than three samples is noise.
SPANS_S = (30, 60, 120, 300, 600, 900, 1800, 3600)
MAX_ROWS = 20000
MAX_MOUNTS = 40

ROLE_SEARCH_HEAD = "search head"
ROLE_INDEXER = "indexer"
ROLE_OTHER = "other"
ROLE_UNKNOWN = "unknown"
ROLE_ORDER = (ROLE_SEARCH_HEAD, ROLE_INDEXER, ROLE_OTHER, ROLE_UNKNOWN)

# Where "busy" starts, for the findings. Statements of fact are made at these
# lines; nothing is aborted or scored on them.
CPU_HOT_PCT = 90.0
MEM_HOT_PCT = 90.0
LOAD_HOT = 1.0
IO_WAIT_HOT_MS = 50.0

_SH_ROLES = {"shc_member", "shc_captain", "cluster_search_head", "search_head"}
_SHC_ROLES = {"shc_member", "shc_captain", "cluster_search_head"}
_PEER_ROLES = {"cluster_peer", "cluster_slave"}


def span_for(window_s: float) -> int:
    """The bucket width that gives about TARGET_POINTS buckets."""
    wanted = max(1.0, window_s) / TARGET_POINTS
    for span in SPANS_S:
        if span >= wanted:
            return span
    return SPANS_S[-1]


def hostwide_spl(span_s: int) -> str:
    # eval takes single quotes as "field"; stats takes the name bare (see the
    # search_telemetry probe in sut.py, where quoting in stats returned
    # nothing on a live instance).
    return (
        "search index=_introspection sourcetype=splunk_resource_usage component=Hostwide "
        "| eval cpu_pct='data.cpu_system_pct' + 'data.cpu_user_pct', "
        "mem_pct=if('data.mem' > 0, 100 * 'data.mem_used' / 'data.mem', null()) "
        f"| bin _time span={int(span_s)}s "
        "| stats avg(cpu_pct) as cpu_pct, max(cpu_pct) as cpu_peak_pct, "
        "avg(mem_pct) as mem_pct, max(mem_pct) as mem_peak_pct, "
        "avg(data.normalized_load_avg_1min) as load, max(data.normalized_load_avg_1min) as load_peak, "
        "max(data.mem) as mem_mb, max(data.swap_used) as swap_used_mb, "
        "max(data.cpu_count) as cores, max(data.virtual_cpu_count) as vcpus by _time, host "
        "| eval t=_time | fields - _time"
    )


def io_series_spl(span_s: int) -> str:
    # Per mount first, then per host: throughput adds up across a host's
    # disks, and the wait that matters is the worst disk's.
    return (
        "search index=_introspection sourcetype=splunk_resource_usage component=IOStats "
        f"| bin _time span={int(span_s)}s "
        "| stats avg(data.reads_kb_ps) as read_kb_ps, avg(data.writes_kb_ps) as write_kb_ps, "
        "avg(data.reads_ps) as reads_ps, avg(data.writes_ps) as writes_ps, "
        "avg(data.avg_total_ms) as wait_ms by _time, host, data.mount_point "
        "| stats sum(read_kb_ps) as read_kb_ps, sum(write_kb_ps) as write_kb_ps, "
        "sum(reads_ps) as reads_ps, sum(writes_ps) as writes_ps, max(wait_ms) as wait_ms by _time, host "
        "| eval t=_time | fields - _time"
    )


def io_mounts_spl() -> str:
    return (
        "search index=_introspection sourcetype=splunk_resource_usage component=IOStats "
        "| stats avg(data.avg_total_ms) as avg_wait_ms, max(data.avg_total_ms) as max_wait_ms, "
        "avg(data.reads_kb_ps) as read_kb_ps, avg(data.writes_kb_ps) as write_kb_ps, "
        "avg(data.reads_ps) as reads_ps, avg(data.writes_ps) as writes_ps, "
        "latest(data.fs_type) as fs_type, latest(data.device) as device by host, data.mount_point "
        '| rename "data.mount_point" as mount '
        "| sort 0 - max_wait_ms"
    )


# ---------------------------------------------------------------------------
# Which host is what
# ---------------------------------------------------------------------------

def _is_address(text: str) -> bool:
    return ":" in text or (bool(text) and all(ch.isdigit() or ch == "." for ch in text))


def _names(*values: Any) -> List[str]:
    """Every spelling a host might have in ``host=``: as given, and short.

    Takes a bare name, a ``host:port`` peer name or a management URI. An
    address is matched whole or not at all: its first octet is nobody's name.
    """
    out: List[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if "://" in text:
            text = text.split("://", 1)[1]
        text = text.split("/", 1)[0]
        if text.count(":") == 1:
            text = text.split(":", 1)[0]
        if not text:
            continue
        candidates = [text] if _is_address(text) else [text, text.split(".", 1)[0]]
        for name in candidates:
            if name and name not in out:
                out.append(name)
    return out


def _role_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [part.strip().lower() for part in value.replace(",", " ").split() if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip().lower() for part in value if str(part).strip()]
    return []


def peer_role(roles: Sequence[str]) -> str:
    """The tier a search peer belongs to, from the roles it reports."""
    have = set(roles)
    if have & _PEER_ROLES:
        return ROLE_INDEXER
    if have & _SHC_ROLES:
        return ROLE_SEARCH_HEAD
    if "indexer" in have or not have:
        # A search peer that reports no roles is still a search peer.
        return ROLE_INDEXER
    if have & _SH_ROLES:
        return ROLE_SEARCH_HEAD
    return ROLE_OTHER


async def host_roles(client: SplunkClient) -> Tuple[Dict[str, str], List[str], int]:
    """Host name (lower case, long and short) to tier, and how many indexers
    the search head knows of. Never raises."""
    roles: Dict[str, str] = {}
    notes: List[str] = []
    indexers = 0

    def put(names: Iterable[str], role: str, override: bool = False) -> None:
        for name in names:
            if override or name not in roles:
                roles[name] = role

    try:
        peers = await client.entries("/services/search/distributed/peers", "distributed peers")
    except Exception as exc:  # noqa: BLE001 - a role map is a nicety
        peers = []
        notes.append(f"the search peers could not be listed: {str(exc)[:200]}")
    for peer in peers:
        role = peer_role(_role_list(peer.get("server_roles")))
        indexers += 1 if role == ROLE_INDEXER else 0
        put(_names(peer.get("peerName"), peer.get("host"), peer.get("host_fqdn"), peer.get("name")), role)

    try:
        members = await client.entries("/services/shcluster/member/members", "search head cluster members")
    except Exception:  # noqa: BLE001
        members = []
    for member in members:
        put(_names(member.get("label"), member.get("mgmt_uri")), ROLE_SEARCH_HEAD, override=True)

    try:
        info = await client.server_info()
    except Exception as exc:  # noqa: BLE001
        info = {}
        notes.append(f"this instance's own name could not be read: {str(exc)[:200]}")
    # Last and overriding: the instance the run talked to is a search head,
    # whatever else it also is (a standalone instance is its own indexer).
    put(_names(info.get("serverName"), info.get("host"), info.get("host_fqdn")), ROLE_SEARCH_HEAD, override=True)
    return roles, notes, indexers


def role_of(host: str, roles: Dict[str, str]) -> str:
    for name in _names(host):
        if name in roles:
            return roles[name]
    return ROLE_UNKNOWN


# ---------------------------------------------------------------------------
# Folding rows into series
# ---------------------------------------------------------------------------

def _f(value: Any) -> Optional[float]:
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _r(value: Optional[float], places: int = 1) -> Optional[float]:
    return None if value is None else round(value, places)


def _avg(values: Sequence[Optional[float]]) -> Optional[float]:
    have = [v for v in values if v is not None]
    return sum(have) / len(have) if have else None


def _max(values: Sequence[Optional[float]]) -> Optional[float]:
    have = [v for v in values if v is not None]
    return max(have) if have else None


def _sum(values: Sequence[Optional[float]]) -> Optional[float]:
    have = [v for v in values if v is not None]
    return sum(have) if have else None


class _Host:
    """One host's buckets, by bucket start time."""

    def __init__(self, name: str, role: str) -> None:
        self.name = name
        self.role = role
        self.cpu: Dict[float, Dict[str, Optional[float]]] = {}
        self.io: Dict[float, Dict[str, Optional[float]]] = {}
        self.cores: Optional[float] = None
        self.mem_mb: Optional[float] = None

    @staticmethod
    def _inside(table: Dict[float, Dict[str, Optional[float]]], start: float, end: float, span: int) -> List[float]:
        """The buckets that describe the run.

        Those wholly inside it: a bucket that straddles the start is partly
        the idle machine and one that straddles the end partly the recovery,
        and either would dilute the figure. A run shorter than two buckets
        has none of those, and takes the ones that overlap it instead.
        """
        whole = [t for t in table if t >= start and t + span <= end]
        return whole or [t for t in table if t + span > start and t < end]

    def during(self, table: Dict[float, Dict[str, Optional[float]]], key: str, start: float, end: float, span: int) -> List[Optional[float]]:
        return [table[t].get(key) for t in self._inside(table, start, end, span)]

    def before(self, table: Dict[float, Dict[str, Optional[float]]], key: str, start: float, span: int) -> List[Optional[float]]:
        return [row.get(key) for t, row in table.items() if t + span <= start]

    def summary(self, start: float, end: float, span: int) -> Dict[str, Any]:
        cpu = self.during(self.cpu, "cpu_pct", start, end, span)
        peaks = [
            (self.cpu[t]["cpu_peak_pct"], t)
            for t in self._inside(self.cpu, start, end, span)
            if self.cpu[t].get("cpu_peak_pct") is not None
        ]
        peak, peak_at = max(peaks) if peaks else (None, None)
        hot = sum(1 for v in cpu if v is not None and v >= CPU_HOT_PCT)
        overloaded = sum(1 for v in self.during(self.cpu, "load", start, end, span) if v is not None and v >= LOAD_HOT)
        out: Dict[str, Any] = {
            "host": self.name,
            "role": self.role,
            "cores": int(self.cores) if self.cores else None,
            "mem_mb": int(self.mem_mb) if self.mem_mb else None,
            "cpu": {
                "avg_pct": _r(_avg(cpu)),
                "peak_pct": _r(peak),
                "peak_at": peak_at,
                "before_pct": _r(_avg(self.before(self.cpu, "cpu_pct", start, span))),
                "hot_s": hot * span,
            },
            "mem": {
                "avg_pct": _r(_avg(self.during(self.cpu, "mem_pct", start, end, span))),
                "peak_pct": _r(_max(self.during(self.cpu, "mem_peak_pct", start, end, span))),
                "before_pct": _r(_avg(self.before(self.cpu, "mem_pct", start, span))),
                "swap_used_mb": _r(_max(self.during(self.cpu, "swap_used_mb", start, end, span)), 0),
            },
            "load": {
                "avg": _r(_avg(self.during(self.cpu, "load", start, end, span)), 2),
                "peak": _r(_max(self.during(self.cpu, "load_peak", start, end, span)), 2),
                "over_s": overloaded * span,
            },
        }
        if self.io:
            out["io"] = {
                "read_mb_s": _r(_avg(self.during(self.io, "read_mb_s", start, end, span)), 2),
                "write_mb_s": _r(_avg(self.during(self.io, "write_mb_s", start, end, span)), 2),
                "peak_read_mb_s": _r(_max(self.during(self.io, "read_mb_s", start, end, span)), 2),
                "iops": _r(_avg(self.during(self.io, "iops", start, end, span)), 0),
                "wait_ms": _r(_avg(self.during(self.io, "wait_ms", start, end, span)), 1),
                "peak_wait_ms": _r(_max(self.during(self.io, "wait_ms", start, end, span)), 1),
                "before_wait_ms": _r(_avg(self.before(self.io, "wait_ms", start, span)), 1),
            }
        return {key: value for key, value in out.items() if value is not None}


def fold(
    hostwide_rows: Sequence[Dict[str, Any]],
    io_rows: Sequence[Dict[str, Any]],
    roles: Dict[str, str],
) -> Dict[str, _Host]:
    hosts: Dict[str, _Host] = {}

    def host(name: str) -> _Host:
        if name not in hosts:
            hosts[name] = _Host(name, role_of(name, roles))
        return hosts[name]

    for row in hostwide_rows:
        name, t = str(row.get("host") or ""), _f(row.get("t"))
        if not name or t is None:
            continue
        h = host(name)
        h.cpu[t] = {key: _f(row.get(key)) for key in ("cpu_pct", "cpu_peak_pct", "mem_pct", "mem_peak_pct", "load", "load_peak", "swap_used_mb")}
        cores = _f(row.get("vcpus")) or _f(row.get("cores"))
        if cores:
            h.cores = max(h.cores or 0.0, cores)
        mem = _f(row.get("mem_mb"))
        if mem:
            h.mem_mb = max(h.mem_mb or 0.0, mem)
    for row in io_rows:
        name, t = str(row.get("host") or ""), _f(row.get("t"))
        if not name or t is None:
            continue
        read, write = _f(row.get("read_kb_ps")), _f(row.get("write_kb_ps"))
        host(name).io[t] = {
            "read_mb_s": None if read is None else read / 1024.0,
            "write_mb_s": None if write is None else write / 1024.0,
            "iops": _sum([_f(row.get("reads_ps")), _f(row.get("writes_ps"))]),
            "wait_ms": _f(row.get("wait_ms")),
        }
    return hosts


def role_series(hosts: Sequence[_Host]) -> Dict[str, Any]:
    """One tier over time: the mean across its hosts, and its busiest host.

    A mean alone hides one indexer pinned at 100% among nine idle ones; a
    line a host does not scale past a handful. So two lines a tier.
    """
    times = sorted({t for h in hosts for t in h.cpu} | {t for h in hosts for t in h.io})

    def across(table: str, key: str, how) -> List[Optional[float]]:
        return [_r(how([getattr(h, table).get(t, {}).get(key) for h in hosts]), 2) for t in times]

    out: Dict[str, Any] = {
        "hosts": len(hosts),
        "t": times,
        "cpu_avg": across("cpu", "cpu_pct", _avg),
        "cpu_max": across("cpu", "cpu_pct", _max),
        "mem_avg": across("cpu", "mem_pct", _avg),
        "mem_max": across("cpu", "mem_pct", _max),
        "load_max": across("cpu", "load", _max),
    }
    if any(h.io for h in hosts):
        out.update(
            {
                "read_mb_s": across("io", "read_mb_s", _sum),
                "write_mb_s": across("io", "write_mb_s", _sum),
                "io_wait_max": across("io", "wait_ms", _max),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

def _plural(role: str, count: int) -> str:
    if role in (ROLE_OTHER, ROLE_UNKNOWN):
        return f"{count} host(s) of {role} role"
    return f"{count} {role}{'s' if count != 1 else ''}"


def _mins(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60.0:.0f}m"


def findings(summaries: Sequence[Dict[str, Any]], run_s: float, expected_indexers: int) -> List[str]:
    out: List[str] = []
    for role in ROLE_ORDER:
        tier = [s for s in summaries if s.get("role") == role]
        if not tier:
            continue
        cpu = [s["cpu"] for s in tier if s.get("cpu")]
        avg = _avg([c.get("avg_pct") for c in cpu])
        before = _avg([c.get("before_pct") for c in cpu])
        busiest = max(tier, key=lambda s: (s.get("cpu") or {}).get("peak_pct") or -1.0)
        peak = (busiest.get("cpu") or {}).get("peak_pct")
        if avg is not None:
            was = f" ({before:.0f}% before it)" if before is not None else ""
            top = f", peaking at {peak:.0f}% on {busiest['host']}" if peak is not None else ""
            out.append(f"{_plural(role, len(tier)).capitalize()}: CPU averaged {avg:.0f}% during the run{was}{top}.")
        for s in sorted(tier, key=lambda s: -((s.get("cpu") or {}).get("hot_s") or 0)):
            hot = (s.get("cpu") or {}).get("hot_s") or 0
            if hot and run_s > 0 and hot >= max(60.0, 0.1 * run_s):
                out.append(
                    f"{s['host']} ({role}) was at or above {CPU_HOT_PCT:.0f}% CPU for {_mins(hot)} of the "
                    f"{_mins(run_s)} run: it was CPU-bound for that time."
                )
        for s in tier:
            mem = s.get("mem") or {}
            if (mem.get("peak_pct") or 0) >= MEM_HOT_PCT:
                swap = f", with {mem['swap_used_mb']:.0f} MB of swap in use" if mem.get("swap_used_mb") else ""
                out.append(f"{s['host']} ({role}) reached {mem['peak_pct']:.0f}% of memory used{swap}.")
            load = s.get("load") or {}
            over = load.get("over_s") or 0
            if over and run_s > 0 and over >= max(60.0, 0.1 * run_s):
                out.append(
                    f"{s['host']} ({role}) had more runnable work than cores for {_mins(over)} "
                    f"(load per core peaked at {load.get('peak')})."
                )
            io = s.get("io") or {}
            if (io.get("wait_ms") or 0) >= IO_WAIT_HOT_MS:
                was = f" against {io['before_wait_ms']:.0f} ms before the run" if io.get("before_wait_ms") is not None else ""
                out.append(
                    f"{s['host']} ({role}) averaged {io['wait_ms']:.0f} ms per disk request on its slowest "
                    f"disk during the run{was}, reading {io.get('read_mb_s') or 0:.0f} MB/s."
                )
    indexers = sum(1 for s in summaries if s.get("role") == ROLE_INDEXER)
    if expected_indexers and not indexers:
        out.append(
            f"None of this search head's {expected_indexers} search peers reported resource usage: "
            "their _introspection is not searchable from here, so only the search tier is shown."
        )
    unknown = [s["host"] for s in summaries if s.get("role") == ROLE_UNKNOWN]
    if unknown:
        out.append(
            f"{len(unknown)} host(s) in the data are neither this search head, one of its peers nor a "
            f"cluster member, so their tier is not known: {', '.join(sorted(unknown)[:6])}."
        )
    return out


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

async def host_resources(
    client: SplunkClient,
    started_at: float,
    ended_at: float,
    timeout_s: float = QUERY_TIMEOUT_S,
) -> Dict[str, Any]:
    """CPU, memory and disk on the target's hosts across a run. Never raises."""
    earliest = float(started_at) - LEAD_S
    latest = max(float(ended_at), float(started_at) + 1.0) + TAIL_S
    span = span_for(latest - earliest)
    window = TimeWindow(earliest=earliest, latest=latest)
    result: Dict[str, Any] = {
        "available": False,
        "reason": "",
        "window": {"earliest": earliest, "latest": latest, "run_started_at": started_at, "run_ended_at": ended_at},
        "span_s": span,
        "queries": [],
        "notes": [],
    }

    async def ask(name: str, description: str, spl: str, count: int) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"name": name, "description": description, "available": False, "rows": 0}
        began = time.time()
        rows: List[Dict[str, Any]] = []
        try:
            got, _ = await asyncio.wait_for(client.oneshot(spl, window, count=count), timeout=timeout_s)
            rows = [row for row in got if isinstance(row, dict)]
            query["available"] = True
        except asyncio.TimeoutError:
            query["reason"] = f"the query did not answer within {timeout_s:.0f}s"
        except SplunkError as exc:
            query["reason"] = str(exc)[:300]
        except Exception as exc:  # noqa: BLE001 - never fatal to a run
            query["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        query["rows"] = len(rows)
        query["seconds"] = round(time.time() - began, 2)
        result["queries"].append(query)
        return rows

    try:
        hostwide = await ask("hostwide", "CPU, memory and load by host over time", hostwide_spl(span), MAX_ROWS)
        if not hostwide:
            first = result["queries"][0]
            result["reason"] = first.get("reason") or (
                "no splunk_resource_usage events in _introspection for the time of the run: the "
                "account cannot read _introspection, or the hosts do not send it where this "
                "search head can search it"
            )
            return result
        io_rows = await ask("io_series", "disk throughput and wait by host over time", io_series_spl(span), MAX_ROWS)
        mount_rows = await ask("io_mounts", "disk wait and throughput by mount", io_mounts_spl(), 2000)
        roles, notes, expected_indexers = await host_roles(client)
        result["notes"].extend(notes)

        hosts = fold(hostwide, io_rows, roles)
        summaries = [h.summary(float(started_at), float(ended_at), span) for h in hosts.values()]
        order = {role: n for n, role in enumerate(ROLE_ORDER)}
        summaries.sort(key=lambda s: (order.get(s["role"], 9), -((s.get("cpu") or {}).get("avg_pct") or 0.0)))
        by_role: Dict[str, Any] = {}
        for role in ROLE_ORDER:
            tier = [h for h in hosts.values() if h.role == role]
            if tier:
                by_role[role] = role_series(tier)

        mounts: List[Dict[str, Any]] = []
        for row in mount_rows[:MAX_MOUNTS]:
            name = str(row.get("host") or "")
            read, write = _f(row.get("read_kb_ps")), _f(row.get("write_kb_ps"))
            mount = {
                "host": name,
                "role": role_of(name, roles),
                "mount": str(row.get("mount") or ""),
                "device": str(row.get("device") or "") or None,
                "fs_type": str(row.get("fs_type") or "") or None,
                "avg_wait_ms": _r(_f(row.get("avg_wait_ms")), 1),
                "max_wait_ms": _r(_f(row.get("max_wait_ms")), 1),
                "read_mb_s": _r(None if read is None else read / 1024.0, 2),
                "write_mb_s": _r(None if write is None else write / 1024.0, 2),
                "iops": _r(_sum([_f(row.get("reads_ps")), _f(row.get("writes_ps"))]), 0),
            }
            mounts.append({key: value for key, value in mount.items() if value is not None})

        if len(hostwide) >= MAX_ROWS:
            result["notes"].append(
                f"the host series was cut at {MAX_ROWS} rows: with this many hosts the later part "
                "of the window is missing for some of them"
            )
        if not io_rows:
            result["notes"].append(
                "no disk figures: splunkd writes IOStats on Linux only, and not on every version"
            )
        result.update(
            {
                "available": True,
                "hosts": summaries,
                "roles": by_role,
                "mounts": mounts,
                "findings": findings(summaries, max(0.0, float(ended_at) - float(started_at)), expected_indexers),
                "self_load": {
                    "queries": len(result["queries"]),
                    "note": (
                        "these searches ran on the target after the load stopped; nothing was "
                        "sampled while the run was going"
                    ),
                },
            }
        )
    except Exception as exc:  # noqa: BLE001 - a nicety, never fatal to a run
        log.warning("reading host resources failed: %s", exc, exc_info=True)
        result["available"] = False
        result["reason"] = f"{type(exc).__name__}: {exc}"[:300]
    return result
