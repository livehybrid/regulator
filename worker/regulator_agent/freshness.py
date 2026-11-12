"""Ingest-to-searchable latency: how stale is what a search returns?

Ingest rate and search latency are each measured elsewhere. This is the number
between them. While a run goes, Regulator sends a small probe event to the
target over HTTP Event Collector every few seconds, each carrying a unique id
and its send time, and searches for the probes every few seconds. For each one
found it records how long after sending it first became searchable.

Why Regulator sends them itself rather than leaving it to the ingest tool
(Stoker) that is generating the ingest load: the send time and the time it was
found are then both read from this process's own monotonic clock, so the
headline figure has no clock skew in it at all. The ingest load can come from
Stoker, from production, or from nowhere; the probe measures freshness under
whatever is happening.

Two figures do cross clocks, and are reported as such:

* send to ``_indextime`` (how long the pipeline took to index it), which is the
  indexer's wall clock against this host's, and
* ``_indextime`` to found (how long after indexing it became searchable).

``_indextime`` is whole seconds, so both carry a second of rounding on top of
any skew. A negative send-to-index delay is impossible without skew, so one is
reported as evidence of it rather than averaged in.

Resolution. A probe is known to have become searchable between the start of
the last search that missed it and the end of the first that found it. The
figure reported is the middle of that interval, and the interval's mean width
is reported as the resolution. Searching faster narrows it and costs more.

Cost. One event of about 200 bytes per interval, and one small search per
poll over a single sourcetype in one index for the run's time range. At the
defaults (an event every 10 s, a search every 5 s) that is twelve searches a
minute: noise next to a load test, and stated in the result.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import httpx

from . import catrust
from .splunk import SplunkClient, SplunkError
from .timepolicy import TimeWindow

log = logging.getLogger("regulator.freshness")

SOURCETYPE = "regulator:freshness"
DEFAULT_INTERVAL_S = 10.0
MIN_INTERVAL_S = 2.0
# How long to keep looking for probes after the run has stopped sending.
TAIL_S = 120.0
SEARCH_TIMEOUT_S = 60.0
MAX_POINTS = 2000
MAX_ERRORS = 8
_INDEX_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,79}$")
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9_.:\-]")


def valid_index(name: str) -> bool:
    """An index name that is safe to put in a search unquoted."""
    return bool(_INDEX_OK.match(name or ""))


def poll_for(interval_s: float) -> float:
    """How often to search: twice per probe, between 2 and 5 seconds."""
    return max(2.0, min(5.0, interval_s / 2.0))


@dataclass
class Probe:
    probe_id: str
    seq: int
    sent_wall: float
    sent_mono: float
    sent_ok: bool = False
    send_error: Optional[str] = None
    # The start of the last search that ran after the send and missed it.
    missed_mono: Optional[float] = None
    found_lo_s: Optional[float] = None
    found_hi_s: Optional[float] = None
    found_wall: Optional[float] = None
    indextime: Optional[float] = None

    @property
    def found(self) -> bool:
        return self.found_hi_s is not None

    @property
    def latency_s(self) -> Optional[float]:
        if self.found_hi_s is None:
            return None
        return (self.found_lo_s + self.found_hi_s) / 2.0


def _pct(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile of a small list. None for an empty one."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def distribution(values: Sequence[float]) -> Dict[str, Any]:
    values = [v for v in values if v is not None]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean_s": round(sum(values) / len(values), 3),
        "p50_s": round(_pct(values, 50), 3),
        "p95_s": round(_pct(values, 95), 3),
        "p99_s": round(_pct(values, 99), 3),
        "max_s": round(max(values), 3),
        "min_s": round(min(values), 3),
    }


@dataclass
class FreshnessProbe:
    """Send probes and find them again, until told to stop, then follow up."""

    hec_url: str
    hec_token: str
    client: SplunkClient
    index: str
    run_label: str
    interval_s: float = DEFAULT_INTERVAL_S
    verify_tls: bool = True
    ca_bundle: Optional[str] = None
    tail_s: float = TAIL_S
    transport: Optional[httpx.AsyncBaseTransport] = None
    probes: List[Probe] = field(default_factory=list)
    searches: int = 0
    search_failures: int = 0
    errors: List[str] = field(default_factory=list)
    started_wall: Optional[float] = None
    stopped_wall: Optional[float] = None

    def __post_init__(self) -> None:
        self.interval_s = max(MIN_INTERVAL_S, float(self.interval_s or DEFAULT_INTERVAL_S))
        self.poll_s = poll_for(self.interval_s)
        self.source = "regulator:" + _LABEL_SAFE.sub("_", self.run_label)[:80]
        self._stop = asyncio.Event()
        self._by_id: Dict[str, Probe] = {}

    # ------------------------------------------------------------- sending
    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=catrust.verify_arg(self.verify_tls, self.ca_bundle),
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
            transport=self.transport,
        )

    def _note(self, message: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(message[:300])

    async def _send(self, http: httpx.AsyncClient, seq: int) -> None:
        probe = Probe(
            probe_id=f"{secrets.token_hex(8)}",
            seq=seq,
            sent_wall=time.time(),
            sent_mono=time.perf_counter(),
        )
        payload = {
            "time": round(probe.sent_wall, 3),
            "host": "regulator",
            "source": self.source,
            "sourcetype": SOURCETYPE,
            "index": self.index,
            "event": {"probe": probe.probe_id, "seq": seq, "run": self.run_label, "sent_at": round(probe.sent_wall, 3)},
            # Indexed, so the search can find the run's probes from the index
            # files alone, and the id comes back whatever the sourcetype's
            # search-time extraction does.
            "fields": {"regulator_probe": probe.probe_id, "regulator_run": self.run_label},
        }
        url = self.hec_url.rstrip("/") + "/services/collector/event"
        self.probes.append(probe)
        self._by_id[probe.probe_id] = probe
        try:
            response = await http.post(url, json=payload, headers={"Authorization": f"Splunk {self.hec_token}"})
        except httpx.HTTPError as exc:
            probe.send_error = f"{type(exc).__name__}: {exc}"[:200]
            self._note(f"sending probe {seq}: {probe.send_error}")
            return
        if 200 <= response.status_code < 300:
            probe.sent_ok = True
        else:
            probe.send_error = f"HTTP {response.status_code}: {response.text[:160]}"
            self._note(f"sending probe {seq}: {probe.send_error}")

    async def _send_loop(self) -> None:
        seq = 0
        async with self._http() as http:
            due = time.perf_counter()
            while not self._stop.is_set():
                seq += 1
                await self._send(http, seq)
                due += self.interval_s
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, due - time.perf_counter()))
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------ finding
    def search_spl(self) -> str:
        # The run's probes by their indexed run field, the id from the indexed
        # field or, failing that, from the event itself.
        return (
            f'search index={self.index} sourcetype="{SOURCETYPE}" source="{self.source}" '
            '| eval probe=coalesce(regulator_probe, spath(_raw, "probe")), indextime=_indextime '
            "| stats min(indextime) as indextime by probe"
        )

    def _waiting(self) -> List[Probe]:
        return [p for p in self.probes if p.sent_ok and not p.found]

    async def _search_once(self) -> None:
        waiting = self._waiting()
        if not waiting:
            return
        began = time.perf_counter()
        first = min(p.sent_wall for p in self.probes)
        # Wide on both sides: the events carry this host's clock as their
        # time, and the target's may disagree.
        window = TimeWindow(earliest=first - 600.0, latest=time.time() + 600.0)
        self.searches += 1
        try:
            rows, _ = await asyncio.wait_for(
                self.client.oneshot(self.search_spl(), window, count=0), timeout=SEARCH_TIMEOUT_S
            )
        except (asyncio.TimeoutError, SplunkError, Exception) as exc:  # noqa: BLE001 - a missed poll is not fatal
            self.search_failures += 1
            self._note(f"searching for probes: {type(exc).__name__}: {exc}")
            return
        ended = time.perf_counter()
        ended_wall = time.time()
        seen: Dict[str, Optional[float]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = row.get("probe")
            if isinstance(pid, list):
                pid = pid[0] if pid else None
            if not pid:
                continue
            try:
                seen[str(pid)] = float(row.get("indextime"))
            except (TypeError, ValueError):
                seen[str(pid)] = None
        for probe in waiting:
            if probe.probe_id in seen:
                lower = (probe.missed_mono - probe.sent_mono) if probe.missed_mono is not None else 0.0
                probe.found_lo_s = max(0.0, lower)
                probe.found_hi_s = max(probe.found_lo_s, ended - probe.sent_mono)
                probe.found_wall = ended_wall
                probe.indextime = seen[probe.probe_id]
            elif began >= probe.sent_mono:
                probe.missed_mono = began

    async def _search_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_s)
            except asyncio.TimeoutError:
                pass
            await self._search_once()

    # ------------------------------------------------------------- driving
    async def run(self) -> None:
        """Send and search until :meth:`stop`, then follow up for ``tail_s``."""
        self.started_wall = time.time()
        sender = asyncio.create_task(self._send_loop())
        searcher = asyncio.create_task(self._search_loop())
        try:
            await self._stop.wait()
        finally:
            for task in (sender, searcher):
                task.cancel()
            await asyncio.gather(sender, searcher, return_exceptions=True)
            self.stopped_wall = time.time()
        deadline = time.perf_counter() + max(0.0, self.tail_s)
        while self._waiting() and time.perf_counter() < deadline:
            await asyncio.sleep(self.poll_s)
            await self._search_once()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- report
    def result(self, run_started_at: Optional[float] = None, run_ended_at: Optional[float] = None) -> Dict[str, Any]:
        """What was measured. Figures are over the probes sent during the run.

        Probes sent before the load started (a fleet's provisioning, say) are
        the idle baseline and are reported apart.
        """
        start = run_started_at if run_started_at is not None else self.started_wall
        end = run_ended_at if run_ended_at is not None else self.stopped_wall

        def during(probe: Probe) -> bool:
            return (start is None or probe.sent_wall >= start) and (end is None or probe.sent_wall <= end)

        sent_ok = [p for p in self.probes if p.sent_ok]
        in_run = [p for p in sent_ok if during(p)]
        before = [p for p in sent_ok if start is not None and p.sent_wall < start]
        found = [p for p in in_run if p.found]
        index_delays = [p.indextime - p.sent_wall for p in found if p.indextime is not None]
        after_index = [p.found_wall - p.indextime for p in found if p.indextime is not None and p.found_wall]
        widths = [p.found_hi_s - p.found_lo_s for p in found]
        skewed = [d for d in index_delays if d < -1.0]

        points = [
            [round(p.sent_wall, 3),
             round(p.latency_s, 3) if p.found else None,
             round(p.indextime - p.sent_wall, 3) if p.indextime is not None else None]
            for p in sent_ok
        ]
        if len(points) > MAX_POINTS:
            step = math.ceil(len(points) / MAX_POINTS)
            points = points[::step]

        out: Dict[str, Any] = {
            "available": bool(sent_ok),
            "index": self.index,
            "sourcetype": SOURCETYPE,
            "source": self.source,
            "interval_s": self.interval_s,
            "poll_s": self.poll_s,
            "sent": len(self.probes),
            "send_failed": len(self.probes) - len(sent_ok),
            "during_run": len(in_run),
            "found": len(found),
            "not_found": len(in_run) - len(found),
            "tail_s": self.tail_s,
            "latency": distribution([p.latency_s for p in found]),
            "resolution_s": round(sum(widths) / len(widths), 2) if widths else None,
            "before_run": distribution([p.latency_s for p in before if p.found]),
            "index_delay": distribution(index_delays),
            "after_index": distribution(after_index),
            "skew_evident": bool(skewed),
            "points": points,
            "searches": self.searches,
            "search_failures": self.search_failures,
            "errors": list(self.errors),
            "self_load": {
                "events": len(self.probes),
                "searches": self.searches,
                "note": "probe events were sent to the target and searched for on it during the run",
            },
        }
        if not sent_ok:
            out["reason"] = (
                self.errors[0] if self.errors else "no probe was accepted by HTTP Event Collector"
            )
        out["findings"] = findings(out)
        return out


def findings(result: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if not result.get("available"):
        out.append(f"No ingest-to-searchable figure: {result.get('reason') or 'no probe was sent'}.")
        return out
    latency = result.get("latency") or {}
    if latency.get("count"):
        out.append(
            f"Events became searchable {latency['p50_s']:.1f}s after they were sent at the median, "
            f"{latency['p95_s']:.1f}s at the 95th percentile and {latency['max_s']:.1f}s at worst "
            f"({latency['count']} probes, to within about {result.get('resolution_s') or 0:.1f}s)."
        )
    before = result.get("before_run") or {}
    if before.get("count") and latency.get("count"):
        out.append(
            f"Before the load started the median was {before['p50_s']:.1f}s ({before['count']} probes)."
        )
    missing = int(result.get("not_found") or 0)
    if missing:
        out.append(
            f"{missing} probe(s) sent during the run were not searchable {result.get('tail_s', TAIL_S):.0f}s "
            "after the run ended: they were lost, or indexing fell that far behind."
        )
    if result.get("send_failed"):
        out.append(f"{result['send_failed']} probe(s) were refused by HTTP Event Collector: see the errors.")
    index_delay, after = result.get("index_delay") or {}, result.get("after_index") or {}
    if result.get("skew_evident"):
        out.append(
            "Some probes were indexed before they were sent by this host's clock, which is only possible "
            "if the clocks disagree: the split between indexing and searching is unreliable for this run. "
            "The end-to-end figure is not affected."
        )
    elif index_delay.get("count") and after.get("count"):
        out.append(
            f"Of that, indexing took {index_delay['p50_s']:.1f}s at the median (p95 {index_delay['p95_s']:.1f}s) "
            f"and becoming searchable once indexed {after['p50_s']:.1f}s (p95 {after['p95_s']:.1f}s), "
            "across the indexer's clock and this host's, to the second."
        )
    return out
