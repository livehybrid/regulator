"""SmartStore cache state, and the option to throw it away.

On a SmartStore indexer the local disk is a cache in front of object storage.
That single fact quietly invalidates more Splunk benchmarks than anything else
in this codebase, because it means the same search over the same data can be
two completely different pieces of work:

* **Warm.** Every bucket it needs is already local. The search reads local
  disk and you are measuring the search tier.
* **Cold.** Some buckets are remote. Before the search can filter anything it
  has to download them, so you are measuring object storage, the network and
  the cache manager, with a bit of search on the end.

Both numbers are real and both are worth having. What is not acceptable is not
knowing which one you got, and that is the failure mode this module exists to
prevent: every run records the cache state before and after, how much data the
run pulled down, and therefore whether the result came off local disk or off
the network.

**Ground truth.** Everything here was verified against a real Splunk 10.4.0
with SmartStore enabled rather than taken from documentation:

``GET /services/admin/cacheman?count=0``
    One entry per bucket, named ``bid|<index>~<id>~<guid>|``. Its
    ``cm:bucket`` content carries ``status`` (``local`` or ``remote``),
    ``estimated_size``, ``journal_size``, ``cache_priority``, ``ref_count``,
    ``download_status`` and the bucket's time range.

``POST /services/admin/cacheman/<url-encoded bid>/evict``
    Evicts one bucket from the local cache. A GET answers
    "All custom actions of this endpoint require POST", which is how the
    action was confirmed to exist without evicting anything.

``GET /services/configs/conf-server/cachemanager``
    ``max_cache_size`` in MB, plus ``eviction_policy`` and
    ``hotlist_recency_secs``. This is what turns "48 GB local" into "62% full",
    which is the number anyone actually wants.

**Eviction is not free and not hidden.** It is opt-in, it logs loudly, and it
is never the default. Throwing away a warm cache means the next run pays to
re-download everything it touches, which on a cloud object store costs real
money and real time. That is exactly the point when you are deliberately
measuring the cold path, and an unpleasant surprise otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .splunk import SplunkClient, SplunkError

log = logging.getLogger("regulator.smartstore")

CACHEMAN_PATH = "/services/admin/cacheman"
CACHEMAN_CONFIG_PATH = "/services/configs/conf-server/cachemanager"

STATUS_LOCAL = "local"
STATUS_REMOTE = "remote"

# How many evictions to run at once. Eviction is cheap per bucket but there can
# be thousands, and firing them all simultaneously at the instance we are about
# to benchmark would itself be a load test of the cache manager.
EVICT_CONCURRENCY = 8


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def cache_size_gb(max_cache_size_mb: Optional[int]) -> float:
    """Convert the configured cache ceiling to decimal GB.

    max_cache_size is in MiB. Bucket sizes are reported here in decimal GB, and
    mixing the two makes the summary read "78.8 GB of 75 GB, 98% full", which
    looks like arithmetic nobody should trust. 76800 MiB is 80.5 GB.
    """
    return ((max_cache_size_mb or 0) * 1024 * 1024) / 1e9


def _index_of(bucket_id: str) -> str:
    """Pull the index name out of ``bid|<index>~<id>~<guid>|``."""
    try:
        return bucket_id.split("|")[1].split("~")[0]
    except (IndexError, AttributeError):
        return "unknown"


@dataclass
class IndexCache:
    index: str
    local_buckets: int = 0
    remote_buckets: int = 0
    local_bytes: int = 0
    total_bytes: int = 0

    @property
    def local_pct(self) -> float:
        total = self.local_buckets + self.remote_buckets
        return (100.0 * self.local_buckets / total) if total else 0.0


@dataclass
class CacheState:
    """A point-in-time picture of what is on local disk."""

    available: bool = False
    reason: str = ""
    local_buckets: int = 0
    remote_buckets: int = 0
    local_bytes: int = 0
    total_bytes: int = 0
    max_cache_size_mb: Optional[int] = None
    eviction_policy: Optional[str] = None
    hotlist_recency_secs: Optional[int] = None
    per_index: Dict[str, IndexCache] = field(default_factory=dict)

    @property
    def total_buckets(self) -> int:
        return self.local_buckets + self.remote_buckets

    @property
    def local_pct(self) -> float:
        return (100.0 * self.local_buckets / self.total_buckets) if self.total_buckets else 0.0

    @property
    def fill_pct(self) -> Optional[float]:
        """How full the cache is against its configured ceiling.

        The number that matters for capacity: a cache sitting at 95% is one
        where the next wide search evicts something somebody else was using,
        and the benchmark starts measuring churn.
        """
        if not self.max_cache_size_mb:
            return None
        return 100.0 * (self.local_bytes / (self.max_cache_size_mb * 1024 * 1024))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "local_buckets": self.local_buckets,
            "remote_buckets": self.remote_buckets,
            "total_buckets": self.total_buckets,
            "local_pct": round(self.local_pct, 2),
            "local_bytes": self.local_bytes,
            "total_bytes": self.total_bytes,
            "max_cache_size_mb": self.max_cache_size_mb,
            "fill_pct": round(self.fill_pct, 2) if self.fill_pct is not None else None,
            "eviction_policy": self.eviction_policy,
            "hotlist_recency_secs": self.hotlist_recency_secs,
            "per_index": {
                name: {
                    "local_buckets": i.local_buckets,
                    "remote_buckets": i.remote_buckets,
                    "local_pct": round(i.local_pct, 2),
                    "local_bytes": i.local_bytes,
                    "total_bytes": i.total_bytes,
                }
                for name, i in sorted(self.per_index.items())
            },
        }


@dataclass
class CacheDelta:
    """What a run did to the cache, which is what says warm or cold.

    ``buckets_downloaded`` is the honest measure of cache misses served during
    the run: buckets that were remote before and local afterwards. If it is
    zero the searches ran entirely off local disk and the numbers describe the
    search tier. If it is large, a good part of what was measured is object
    storage and the network.
    """

    available: bool = False
    buckets_downloaded: int = 0
    bytes_downloaded: int = 0
    buckets_evicted_during: int = 0
    local_before: int = 0
    local_after: int = 0
    fill_pct_before: Optional[float] = None
    fill_pct_after: Optional[float] = None

    @property
    def provenance(self) -> str:
        if not self.available:
            return "unknown"
        if self.buckets_downloaded == 0:
            return "warm"
        if self.local_before == 0:
            return "cold"
        return "mixed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "provenance": self.provenance,
            "buckets_downloaded": self.buckets_downloaded,
            "bytes_downloaded": self.bytes_downloaded,
            "buckets_evicted_during": self.buckets_evicted_during,
            "local_before": self.local_before,
            "local_after": self.local_after,
            "fill_pct_before": self.fill_pct_before,
            "fill_pct_after": self.fill_pct_after,
        }


async def cache_state(client: SplunkClient) -> CacheState:
    """Read the cache manager. Never raises: absence is an answer."""
    state = CacheState()

    try:
        entries = await client.entries(CACHEMAN_PATH, "smartstore cache")
    except SplunkError as exc:
        state.reason = f"the cache manager was not readable: {exc}"
        return state

    if not entries:
        # Either not SmartStore, or an account without the capability. Both
        # mean the same thing here: no cache picture, say so rather than
        # reporting zeroes that look like an empty cache.
        state.reason = (
            "no cache manager entries: this instance is not using SmartStore, or the "
            "account cannot read /services/admin/cacheman"
        )
        return state

    state.available = True
    for entry in entries:
        bucket = entry.get("cm:bucket") or {}
        name = entry.get("name") or ""
        index = _index_of(name)
        size = _int(bucket.get("estimated_size")) or _int(bucket.get("journal_size"))
        per = state.per_index.setdefault(index, IndexCache(index=index))
        per.total_bytes += size
        state.total_bytes += size
        if str(bucket.get("status", "")).lower() == STATUS_LOCAL:
            state.local_buckets += 1
            state.local_bytes += size
            per.local_buckets += 1
            per.local_bytes += size
        else:
            state.remote_buckets += 1
            per.remote_buckets += 1

    config = await _cache_config(client)
    state.max_cache_size_mb = config.get("max_cache_size")
    state.eviction_policy = config.get("eviction_policy")
    state.hotlist_recency_secs = config.get("hotlist_recency_secs")
    return state


async def _cache_config(client: SplunkClient) -> Dict[str, Any]:
    try:
        entries = await client.entries(CACHEMAN_CONFIG_PATH, "cachemanager config")
    except SplunkError:
        return {}
    if not entries:
        return {}
    content = entries[0]
    return {
        "max_cache_size": _int(content.get("max_cache_size")) or None,
        "eviction_policy": content.get("eviction_policy"),
        "hotlist_recency_secs": _int(content.get("hotlist_recency_secs")) or None,
    }


def delta(before: CacheState, after: CacheState) -> CacheDelta:
    """What changed between two cache readings."""
    if not (before.available and after.available):
        return CacheDelta(available=False)

    downloaded = max(0, after.local_buckets - before.local_buckets)
    evicted = max(0, before.local_buckets - after.local_buckets)
    return CacheDelta(
        available=True,
        buckets_downloaded=downloaded,
        bytes_downloaded=max(0, after.local_bytes - before.local_bytes),
        buckets_evicted_during=evicted,
        local_before=before.local_buckets,
        local_after=after.local_buckets,
        fill_pct_before=round(before.fill_pct, 2) if before.fill_pct is not None else None,
        fill_pct_after=round(after.fill_pct, 2) if after.fill_pct is not None else None,
    )


@dataclass
class EvictionResult:
    attempted: int = 0
    evicted: int = 0
    failed: int = 0
    bytes_evicted: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "evicted": self.evicted,
            "failed": self.failed,
            "bytes_evicted": self.bytes_evicted,
            # Only the first few: a cache with thousands of buckets failing the
            # same way does not need thousands of identical lines in a report.
            "errors": self.errors[:5],
        }


async def evict_all(
    client: SplunkClient,
    indexes: Optional[Sequence[str]] = None,
    concurrency: int = EVICT_CONCURRENCY,
) -> EvictionResult:
    """Evict every locally cached bucket, optionally only for named indexes.

    This is the cold-start button. It is destructive to performance, not to
    data: SmartStore re-downloads whatever a later search needs, so nothing is
    lost except the time and the object-storage egress to fetch it again.

    Restricting to the indexes a scenario actually searches is strongly
    preferred over evicting everything. On a shared cluster the rest of the
    cache belongs to other people's dashboards, and flushing it to measure your
    own cold path makes their afternoon slow for no benefit to your result.
    """
    result = EvictionResult()
    wanted = {i.lower() for i in indexes} if indexes else None

    try:
        entries = await client.entries(CACHEMAN_PATH, "smartstore cache")
    except SplunkError as exc:
        result.errors.append(f"could not list the cache: {exc}")
        return result

    targets: List[tuple[str, int]] = []
    for entry in entries:
        bucket = entry.get("cm:bucket") or {}
        if str(bucket.get("status", "")).lower() != STATUS_LOCAL:
            continue
        name = entry.get("name") or ""
        if wanted is not None and _index_of(name).lower() not in wanted:
            continue
        size = _int(bucket.get("estimated_size")) or _int(bucket.get("journal_size"))
        targets.append((name, size))

    if not targets:
        log.info("nothing to evict: no locally cached buckets matched")
        return result

    log.warning(
        "EVICTING %d cached bucket(s) (%.1f GB) from %s. The next searches will "
        "re-download what they need from object storage, which is the point of a "
        "cold-cache run and is not free",
        len(targets),
        sum(size for _, size in targets) / 1e9,
        ", ".join(sorted(wanted)) if wanted else "every index",
    )

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def evict_one(bucket_id: str, size: int) -> None:
        async with semaphore:
            encoded = urllib.parse.quote(bucket_id, safe="")
            try:
                await client.post_action(f"{CACHEMAN_PATH}/{encoded}/evict", "evict bucket")
            except SplunkError as exc:
                result.failed += 1
                if len(result.errors) < 20:
                    result.errors.append(f"{bucket_id}: {exc}")
            else:
                result.evicted += 1
                result.bytes_evicted += size

    result.attempted = len(targets)
    await asyncio.gather(*(evict_one(bid, size) for bid, size in targets))
    log.warning(
        "eviction finished: %d evicted, %d failed, %.1f GB dropped",
        result.evicted,
        result.failed,
        result.bytes_evicted / 1e9,
    )
    return result


def render(state: CacheState) -> str:
    """A short summary for a human."""
    if not state.available:
        return f"smartstore    not available ({state.reason})"

    fill = f"{state.fill_pct:.0f}% full" if state.fill_pct is not None else "fill unknown"
    lines = [
        f"smartstore    {state.local_buckets}/{state.total_buckets} buckets local "
        f"({state.local_pct:.0f}%), {state.local_bytes / 1e9:.1f} GB of "
        f"{cache_size_gb(state.max_cache_size_mb):.1f} GB cache, {fill}, "
        f"policy {state.eviction_policy or 'unknown'}"
    ]
    busy = sorted(
        state.per_index.values(), key=lambda i: i.local_bytes, reverse=True
    )[:5]
    for index in busy:
        lines.append(
            f"                {index.index:<20} {index.local_buckets:>5} local / "
            f"{index.remote_buckets:>5} remote  ({index.local_pct:.0f}% local)"
        )
    return "\n".join(lines)
