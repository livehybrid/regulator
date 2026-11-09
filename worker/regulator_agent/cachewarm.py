"""Which SmartStore buckets a run will touch, and warming them before it starts.

Eviction (in :mod:`smartstore`) answers "what does this cluster do cold?".
This module answers the other two questions a SmartStore benchmark needs:

**Forecast.** Before a single search is dispatched, how much of what the run
will read is already on local disk? Every search window Regulator dispatches
is a deterministic function of the step's time policy, the run's start and its
duration, so the whole time range a step can touch is known in advance. Each
indexer's cache manager lists every bucket it holds, local or remote, with the
time range the bucket covers. Intersect the two per index and the answer is
"of the N buckets this run will read, X are local and Y GB will come from
object storage". Nothing has to be listed in S3 and ``dbinspect`` is not
needed: the cache manager already knows every bucket on its indexer, including
the ones that exist only remotely.

**Warm.** Optionally, read those buckets once before the measured run so it
starts from local disk. Warming goes through ordinary searches, never the
cache manager's undocumented ``_localize`` action, which community reports say
stops the indexer evicting normally until it is restarted: a benchmark tool
must not leave a cluster's cache unable to evict.

Three warm-up shapes, because which bucket files a search downloads depends on
the search:

``tsidx`` (default)
    ``| tstats count where index=X by _time`` over the run's range. Reads the
    index files of every bucket in range and no raw data, so it is cheap. It
    warms tstats and accelerated searches fully, and raw searches only as far
    as bucket elimination.
``full``
    Reads ``_raw`` for every event in range, which downloads the journal too.
    Warms everything, at the cost of reading all of the data once.
``replay``
    Runs each of the scenario's own searches once over its whole run range.
    Downloads what those search shapes download, but with one parameter draw,
    so a search that filters on a drawn value warms only what that value hits.

The cache manager fetches a bucket's files as a search needs them (Splunk's
documentation: tstats and metadata searches do not need the journal), so a
bucket it lists as ``local`` may hold only its index files. ``dbinspect`` tells
the two apart: see :func:`read_residency`.

The warm-up is itself load on the target. It runs before the measured run, at
bounded concurrency and within a time budget, and is reported, not hidden.
"""

from __future__ import annotations

import asyncio
import dataclasses
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .params import DrawContext, ParameterResolver
from .scenario import Scenario, Step, TimePolicy
from .smartstore import CACHEMAN_SEARCH_MAX_ROWS, Bucket, CacheState
from .splscope import Clause, all_indexes, clause_span, read_scope
from .splunk import SplunkClient
from .timepolicy import TimeWindow, resolve_window

log = logging.getLogger("regulator.cachewarm")

WARM_MODES = ("tsidx", "full", "replay")

Interval = Tuple[float, float]


def index_terms(spl: str) -> List[str]:
    """Every index name (or wildcard) the search names, in any clause."""
    return all_indexes(read_scope(spl).clauses)


def _fallback_indexes(scenario: Scenario, metrics: bool) -> List[str]:
    """What an index-less clause reads, approximated by the corpus indexes.

    Splunk would search the user's default indexes, which Regulator cannot
    see; the scenario's corpus is the best stand-in it has.
    """
    names = [scenario.corpus.index] if scenario.corpus.index else []
    if metrics and scenario.corpus.metric_index:
        names = [scenario.corpus.metric_index]
    return [name.lower() for name in names]


def step_clauses(scenario: Scenario, step: Step) -> Tuple[List[Clause], List[str]]:
    """The clauses a step reads, with the corpus standing in for no index."""
    if not step.spl:
        # A dashboard, or a saved search dispatched by name: its searches are
        # not visible here, so it reads the corpus over its window.
        return [Clause(indexes=tuple(_fallback_indexes(scenario, False)))], []
    scope = read_scope(step.spl)
    clauses = [
        c if c.indexes else Clause(tuple(_fallback_indexes(scenario, c.metrics)), c.earliest, c.latest, c.origin, c.metrics)
        for c in scope.clauses
    ]
    return clauses, scope.notes


def step_indexes(scenario: Scenario, step: Step) -> List[str]:
    return all_indexes(step_clauses(scenario, step)[0])


def policy_span(policy: TimePolicy, start: float, duration_s: float) -> Interval:
    """The whole time range a step with this policy can search during the run.

    The union of every window it could be dispatched with, from the first
    iteration at ``start`` to the last at ``start + duration_s``. Exact for
    ``pinned``; for ``rolling`` the jitter and alignment are taken at their
    widest, so the span is a superset by at most one alignment unit.
    """
    end = start + max(0.0, duration_s)
    if policy.mode == "pinned":
        window = resolve_window(policy, DrawContext(0, 0, 0, ""), now=start)
        return window.earliest, window.latest
    if policy.mode == "relative":
        first = resolve_window(policy, DrawContext(0, 0, 0, ""), now=start)
        last = resolve_window(policy, DrawContext(0, 0, 0, ""), now=end)
        return min(first.earliest, last.earliest), max(first.latest, last.latest)
    offset = float(policy.offset_s or 0.0)
    latest = end - offset
    earliest = start - offset - max(0.0, policy.jitter_s) - max(0.0, policy.align_s) - policy.window_s
    return earliest, latest


def merge(intervals: Iterable[Interval]) -> List[Interval]:
    out: List[Interval] = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _overlaps(bucket: Bucket, spans: Sequence[Interval]) -> bool:
    if bucket.earliest is None or bucket.latest is None:
        # A bucket with no reported range is counted as touched: under-reporting
        # the download a run will pay for is the worse mistake.
        return True
    return any(bucket.earliest <= hi and bucket.latest >= lo for lo, hi in spans)


@dataclass
class StepSpan:
    """One clause of one step: which indexes it reads, over what range."""

    step_id: str
    indexes: List[str]
    earliest: float
    latest: float
    origin: str = "search"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "origin": self.origin,
            "indexes": list(self.indexes),
            "earliest": round(self.earliest),
            "latest": round(self.latest),
        }


@dataclass
class IndexForecast:
    index: str
    buckets: int = 0
    local: int = 0
    bytes_total: int = 0
    bytes_remote: int = 0
    spans: List[Interval] = field(default_factory=list)

    @property
    def remote(self) -> int:
        return self.buckets - self.local

    @property
    def local_pct(self) -> float:
        return 100.0 * self.local / self.buckets if self.buckets else 100.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "buckets": self.buckets,
            "local": self.local,
            "remote": self.remote,
            "local_pct": round(self.local_pct, 2),
            "bytes_total": self.bytes_total,
            "bytes_remote": self.bytes_remote,
            "spans": [[round(lo), round(hi)] for lo, hi in self.spans],
        }


@dataclass
class Forecast:
    """How much of what a run will read is already local."""

    available: bool = False
    reason: str = ""
    start: float = 0.0
    duration_s: float = 0.0
    steps: List[StepSpan] = field(default_factory=list)
    per_index: Dict[str, IndexForecast] = field(default_factory=dict)
    capacity_bytes: Optional[int] = None
    notes: List[str] = field(default_factory=list)
    # Per touched bucket id: is any copy local. For the warm-up's chunking.
    touched: Dict[str, Bucket] = field(default_factory=dict)
    touched_local: Dict[str, bool] = field(default_factory=dict)
    # Each step's dispatch window over the run, before inline bounds.
    windows: Dict[str, Interval] = field(default_factory=dict)
    # How much of each touched bucket is on disk, from dbinspect, when read.
    residency: Optional["Residency"] = None

    @property
    def buckets(self) -> int:
        return sum(i.buckets for i in self.per_index.values())

    @property
    def local(self) -> int:
        return sum(i.local for i in self.per_index.values())

    @property
    def remote(self) -> int:
        return self.buckets - self.local

    @property
    def bytes_total(self) -> int:
        return sum(i.bytes_total for i in self.per_index.values())

    @property
    def bytes_remote(self) -> int:
        return sum(i.bytes_remote for i in self.per_index.values())

    @property
    def local_pct(self) -> float:
        return 100.0 * self.local / self.buckets if self.buckets else 100.0

    @property
    def capacity_pct(self) -> Optional[float]:
        if not self.capacity_bytes:
            return None
        return 100.0 * self.bytes_total / self.capacity_bytes

    def summary_line(self) -> str:
        if not self.available:
            return f"cache forecast unavailable: {self.reason}"
        line = (
            f"cache forecast: this run reads {self.buckets} bucket(s), "
            f"{self.bytes_total / 1e9:.1f} GB; {self.local} local ({self.local_pct:.0f}%), "
            f"{self.remote} remote, {self.bytes_remote / 1e9:.1f} GB to download"
        )
        if self.capacity_pct is not None:
            line += f"; working set is {self.capacity_pct:.0f}% of the cache"
        if self.residency is not None and self.residency.available:
            c = self.residency.counts()
            line += (
                f". On disk per dbinspect: {c[FULL]} full, {c[INDEX_ONLY]} index files only, "
                f"{c[PARTIAL]} partial, {c[STUB]} evicted to a stub; "
                f"{self.residency.bytes_outstanding / 1e9:.1f} GB not yet local"
            )
        return line

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "start": round(self.start),
            "duration_s": self.duration_s,
            "buckets": self.buckets,
            "local": self.local,
            "remote": self.remote,
            "local_pct": round(self.local_pct, 2),
            "bytes_total": self.bytes_total,
            "bytes_remote": self.bytes_remote,
            "capacity_bytes": self.capacity_bytes,
            "capacity_pct": round(self.capacity_pct, 2) if self.capacity_pct is not None else None,
            "per_index": {name: f.to_dict() for name, f in sorted(self.per_index.items())},
            "steps": [s.to_dict() for s in self.steps],
            "notes": list(self.notes),
            "residency": self.residency.to_dict() if self.residency is not None else None,
        }


def forecast(
    scenario: Scenario, state: CacheState, start: float, duration_s: float
) -> Forecast:
    """Intersect the run's time ranges with the cache manager's bucket list."""
    result = Forecast(start=start, duration_s=duration_s)
    if not state.available:
        result.reason = state.reason or "the SmartStore cache could not be read"
        return result
    if not state.buckets:
        result.reason = "the cache manager listed no buckets"
        return result

    wanted: Dict[str, List[Interval]] = {}
    end = start + max(0.0, duration_s)
    for step in scenario.steps:
        window = policy_span(scenario.resolve_time_policy(step), start, duration_s)
        result.windows[step.id] = window
        clauses, notes = step_clauses(scenario, step)
        for clause in clauses:
            (lo, hi), span_notes = clause_span(clause, window, start, end)
            notes = notes + span_notes
            names = list(clause.indexes)
            result.steps.append(StepSpan(step.id, names, lo, hi, clause.origin))
            for name in names:
                wanted.setdefault(name, []).append((lo, hi))
        result.notes.extend(f"step {step.id}: {note}" for note in dict.fromkeys(notes))
    if not wanted:
        result.reason = "no step names an index and the scenario declares no corpus index"
        return result
    patterns = {name: merge(spans) for name, spans in wanted.items()}

    # One entry per bucket id: a replicated bucket is listed once per indexer
    # that holds a copy, and a search reads one copy.
    copies: Dict[str, List[Bucket]] = {}
    for bucket in state.buckets:
        copies.setdefault(bucket.bid, []).append(bucket)

    no_range = 0
    for bid, held in copies.items():
        first = held[0]
        index = first.index.lower()
        spans: List[Interval] = []
        for pattern, pattern_spans in patterns.items():
            if fnmatch.fnmatchcase(index, pattern):
                spans.extend(pattern_spans)
        if not spans:
            continue
        spans = merge(spans)
        if not _overlaps(first, spans):
            continue
        if first.earliest is None or first.latest is None:
            no_range += 1
        size = max(b.size for b in held)
        is_local = any(b.is_local for b in held)
        entry = result.per_index.setdefault(first.index, IndexForecast(index=first.index))
        entry.spans = spans
        entry.buckets += 1
        entry.bytes_total += size
        if is_local:
            entry.local += 1
        else:
            entry.bytes_remote += size
        result.touched[bid] = first
        result.touched_local[bid] = is_local

    result.notes = list(dict.fromkeys(result.notes))
    result.available = True
    if state.max_cache_size_mb:
        result.capacity_bytes = int(state.max_cache_size_mb) * 1024 * 1024
    unmatched = [p for p in patterns if not any(fnmatch.fnmatchcase(i.lower(), p) for i in state.per_index)]
    if unmatched:
        result.notes.append(
            "no bucket in the cache manager matches index "
            + ", ".join(sorted(unmatched))
            + ": a non-SmartStore index, or one this account cannot see"
        )
    if no_range:
        result.notes.append(
            f"{no_range} bucket(s) reported no time range and are counted as touched"
        )
    if any(len(held) > 1 for held in copies.values()):
        result.notes.append(
            "a bucket counts as local when any indexer holding a copy has it local; a "
            "search reads one copy, so where replicas differ this is an upper bound"
        )
    if result.capacity_pct is not None and result.capacity_pct > 100.0:
        result.notes.append(
            f"the run's working set ({result.bytes_total / 1e9:.1f} GB) is larger than the "
            f"cache ({result.capacity_bytes / 1e9:.1f} GB): it cannot all be local at once, "
            "so warming will evict part of what it fetched and the run will churn"
        )
    return result


# ---------------------------------------------------------------- residency

# How much of a bucket is on local disk, from dbinspect. The cache manager's
# status says "local" as soon as any of a bucket's files are, and it fetches
# files as a search needs them, so a tstats leaves a bucket "local" with its
# index files and no journal. dbinspect cached=false examines the indexer's
# local copy and cached=true the remote manifest; the ratio of their sizes is
# how much of the bucket is here. dbinspect reads no bucket content, so asking
# downloads nothing.
FULL = "full"
INDEX_ONLY = "index_only"
PARTIAL = "partial"
STUB = "stub"
RESIDENCY_CLASSES = (FULL, INDEX_ONLY, PARTIAL, STUB)

_FULL_RATIO = 0.95
_STUB_RATIO = 0.02
_STUB_FLOOR = 2 * 1024 * 1024
_MIB = 1024 * 1024


def classify(local_bytes: float, full_bytes: float, journal_bytes: Optional[int]) -> str:
    """Full, index files only, partly downloaded, or evicted to a stub.

    ``stub`` is what eviction leaves: the directory with the small files the
    cache manager keeps (bloomfilter, metadata). ``index_only`` is a bucket
    holding roughly everything but its journal, which is what a tstats or a
    metadata search downloads.
    """
    if full_bytes <= 0:
        return PARTIAL if local_bytes > _STUB_FLOOR else STUB
    if local_bytes >= _FULL_RATIO * full_bytes:
        return FULL
    if local_bytes <= max(_STUB_RATIO * full_bytes, _STUB_FLOOR):
        return STUB
    if journal_bytes and 0 < journal_bytes < full_bytes:
        without_journal = full_bytes - journal_bytes
        if abs(local_bytes - without_journal) <= 0.15 * full_bytes:
            return INDEX_ONLY
    return PARTIAL


@dataclass
class BucketResidency:
    cls: str
    local_bytes: int
    full_bytes: int


@dataclass
class Residency:
    available: bool = False
    reason: str = ""
    per_bid: Dict[str, BucketResidency] = field(default_factory=dict)
    unmatched: int = 0
    notes: List[str] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        out = {name: 0 for name in RESIDENCY_CLASSES}
        for entry in self.per_bid.values():
            out[entry.cls] = out.get(entry.cls, 0) + 1
        return out

    @property
    def bytes_outstanding(self) -> int:
        return sum(max(0, e.full_bytes - e.local_bytes) for e in self.per_bid.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "buckets": len(self.per_bid),
            "counts": self.counts(),
            "bytes_outstanding": self.bytes_outstanding,
            "unmatched": self.unmatched,
            "notes": list(self.notes),
        }


def _dbinspect_spl(index: str, cached: bool, spans: Sequence[Interval]) -> str:
    quoted = '"' + index.replace('"', '\\"') + '"'
    where = " OR ".join(
        f"(endEpoch>={int(lo)} AND startEpoch<={int(hi)})" for lo, hi in spans
    )
    spl = (
        f"| dbinspect index={quoted} cached={'true' if cached else 'false'} timeformat=%s "
        "| eval bytes=sizeOnDiskMB*1048576"
    )
    if where:
        spl += f" | where isnull(startEpoch) OR {where}"
    return spl + " | fields splunk_server, bucketId, bytes"


async def read_residency(client: SplunkClient, fc: Forecast) -> Residency:
    """How much of each bucket the forecast touches is on local disk.

    Never raises: an account that may not run dbinspect, or a release that
    answers differently, leaves the forecast as the cache manager reported it,
    with a note.
    """
    result = Residency()
    if not fc.available or not fc.touched:
        result.reason = "nothing to inspect"
        return result
    local: Dict[str, float] = {}
    full: Dict[str, float] = {}
    try:
        for index, entry in sorted(fc.per_index.items()):
            window = TimeWindow(
                earliest=min(lo for lo, _ in entry.spans) if entry.spans else 0.0,
                latest=max(hi for _, hi in entry.spans) if entry.spans else time.time(),
            )
            for cached, into in ((False, local), (True, full)):
                rows, _ = await client.oneshot(
                    _dbinspect_spl(index, cached, entry.spans), window,
                    count=CACHEMAN_SEARCH_MAX_ROWS,
                )
                for row in rows:
                    bid = f"bid|{row.get('bucketId') or ''}|"
                    try:
                        size = float(row.get("bytes") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    # A replicated bucket has a row per peer; the copy with
                    # the most on disk is the one most likely to be read.
                    into[bid] = max(into.get(bid, 0.0), size)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - absence of the answer is a note
        result.reason = f"dbinspect could not be read: {str(exc)[:200]}"
        return result
    if not local and not full:
        result.reason = "dbinspect returned no buckets for the indexes this run reads"
        return result
    for bid, bucket in fc.touched.items():
        if bid not in local:
            result.unmatched += 1
            continue
        full_bytes = full.get(bid) or float(bucket.size or 0)
        result.per_bid[bid] = BucketResidency(
            cls=classify(local[bid], full_bytes, bucket.journal_size),
            local_bytes=int(local[bid]),
            full_bytes=int(full_bytes),
        )
    result.available = bool(result.per_bid)
    if not result.available:
        result.reason = "no dbinspect row matched a bucket the run reads"
    if result.unmatched:
        result.notes.append(
            f"{result.unmatched} bucket(s) the run reads had no dbinspect row and are "
            "left as the cache manager reported them"
        )
    return result


async def forecast_on_disk(
    client: SplunkClient, scenario: Scenario, state: CacheState, start: float, duration_s: float
) -> Forecast:
    """The forecast, refined by dbinspect where it can be read."""
    fc = forecast(scenario, state, start, duration_s)
    if fc.available and fc.touched:
        fc.residency = await read_residency(client, fc)
        if not fc.residency.available:
            fc.notes.append(f"bucket residency not read: {fc.residency.reason}")
    return fc


def needs_warming(fc: Forecast, bid: str, mode: str) -> bool:
    """Whether a warm-up in ``mode`` has anything left to fetch for a bucket."""
    entry = fc.residency.per_bid.get(bid) if fc.residency and fc.residency.available else None
    if entry is None:
        return not fc.touched_local.get(bid)
    if mode == "tsidx":
        return entry.cls in (STUB, PARTIAL)
    return entry.cls != FULL


# ---------------------------------------------------------------- warming


@dataclass
class WarmResult:
    mode: str
    searches: int = 0
    failed: int = 0
    skipped_local: int = 0
    not_started: int = 0
    seconds: float = 0.0
    errors: List[str] = field(default_factory=list)
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _chunks(spans: Sequence[Interval], chunk_s: float) -> List[Interval]:
    out: List[Interval] = []
    step = max(60.0, chunk_s)
    for lo, hi in spans:
        cursor = lo
        while cursor < hi:
            out.append((cursor, min(hi, cursor + step)))
            cursor += step
    return out


def _chunk_needs_warming(index: str, chunk: Interval, fc: Forecast, mode: str) -> bool:
    lo, hi = chunk
    for bid, bucket in fc.touched.items():
        if bucket.index != index or not needs_warming(fc, bid, mode):
            continue
        if bucket.earliest is None or bucket.latest is None:
            return True
        if bucket.earliest <= hi and bucket.latest >= lo:
            return True
    return False


def warm_searches(
    scenario: Scenario,
    fc: Forecast,
    mode: str,
    chunk_s: float,
    resolver: Optional[ParameterResolver] = None,
) -> Tuple[List[Tuple[str, TimeWindow]], int]:
    """The searches a warm-up will run, and how many chunks were already local."""
    if mode not in WARM_MODES:
        raise ValueError(f"warm-up mode must be one of {', '.join(WARM_MODES)}, got {mode!r}")
    planned: List[Tuple[str, TimeWindow]] = []
    skipped = 0
    if mode == "replay":
        seen = set()
        for step in scenario.steps:
            if step.type != "search" or not step.spl or step.dispatch == "saved":
                continue
            spl = step.spl
            if resolver is not None:
                spl, _ = resolver.render(spl, DrawContext(scenario.seed, 0, 0, step.id))
            patterns = {p for span in fc.steps if span.step_id == step.id for p in span.indexes}
            mine = [
                index for index in fc.per_index
                if any(fnmatch.fnmatchcase(index.lower(), p) for p in patterns)
            ]
            # Chunked over the dispatch window: a clause with its own inline
            # bounds reads those whatever window it is dispatched with.
            window = fc.windows.get(step.id)
            if window is None:
                continue
            for lo, hi in _chunks([window], chunk_s):
                if not any(_chunk_needs_warming(i, (lo, hi), fc, mode) for i in mine):
                    skipped += 1
                    continue
                key = (spl, lo, hi)
                if key in seen:
                    continue
                seen.add(key)
                planned.append((spl, TimeWindow(earliest=lo, latest=hi)))
        return planned, skipped

    for index, entry in sorted(fc.per_index.items()):
        quoted = '"' + index.replace('"', '\\"') + '"'
        if mode == "tsidx":
            spl = f"| tstats count where index={quoted} by _time span=1h"
        else:
            spl = f"search index={quoted} | eval regulator_warm=len(_raw) | stats sum(regulator_warm)"
        for chunk in _chunks(entry.spans, chunk_s):
            if not _chunk_needs_warming(index, chunk, fc, mode):
                skipped += 1
                continue
            planned.append((spl, TimeWindow(earliest=chunk[0], latest=chunk[1])))
    return planned, skipped


async def warm(
    client: SplunkClient,
    planned: Sequence[Tuple[str, TimeWindow]],
    *,
    mode: str,
    concurrency: int = 4,
    budget_s: float = 1800.0,
    skipped_local: int = 0,
    abort: Optional[asyncio.Event] = None,
) -> WarmResult:
    """Run the warm-up searches, at bounded concurrency, within a budget.

    Oneshot searches: they block until done and leave no job artefact, so
    there is nothing to poll (and no search id for a load-balanced cluster
    member to not know about) and nothing to delete. Each is bounded by the
    client's read timeout, which is why the range is cut into chunks.
    """
    result = WarmResult(mode=mode, skipped_local=skipped_local)
    started = time.perf_counter()
    deadline = started + max(0.0, budget_s)
    queue: "asyncio.Queue[Tuple[str, TimeWindow]]" = asyncio.Queue()
    for item in planned:
        queue.put_nowait(item)

    async def worker() -> None:
        while True:
            try:
                spl, window = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if time.perf_counter() >= deadline or (abort is not None and abort.is_set()):
                result.not_started += 1
                continue
            result.searches += 1
            try:
                await client.oneshot(spl, window, count=1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed chunk is a note
                result.failed += 1
                if len(result.errors) < 5:
                    result.errors.append(f"{window.describe()}: {str(exc)[:200]}")

    workers = [asyncio.create_task(worker()) for _ in range(max(1, int(concurrency)))]
    try:
        await asyncio.gather(*workers)
    finally:
        for task in workers:
            task.cancel()
    result.seconds = round(time.perf_counter() - started, 2)
    return result
