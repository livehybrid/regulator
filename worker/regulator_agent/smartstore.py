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
from typing import Any, Dict, List, Optional, Sequence, Set

from .config import TargetConfig
from .splunk import SplunkClient, SplunkError

log = logging.getLogger("regulator.smartstore")

# cacheman is one entry per bucket and a large estate has tens of thousands.
# Paged, so a single response is never hundreds of megabytes of JSON built
# synchronously by an instance that has just finished a load test.
CACHEMAN_PAGE = 5000
PEERS_PATH = "/services/search/distributed/peers"

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
    # Which indexers answered, and which did not and why. The cache lives on
    # the indexers, and on a distributed target the search head has none.
    peers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    # WHICH buckets are local, not just how many. Counting alone reports a
    # cache that downloaded 3000 buckets and evicted 3000 to make room as
    # "warm, nothing was downloaded", which is the exact opposite of the truth
    # and is the normal behaviour of a cache at its ceiling.
    local_ids: Set[str] = field(default_factory=set)
    local_bytes_by_id: Dict[str, int] = field(default_factory=dict)

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
            "peers": dict(self.peers),
            "notes": list(self.notes),
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
            # Evicting without downloading is somebody else's churn, not ours,
            # but it still means the cache was under pressure during the run.
            return "churning" if self.buckets_evicted_during else "warm"
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


@dataclass
class Indexer:
    """One place a cache lives, and a client that can reach it."""

    name: str
    client: SplunkClient
    owned: bool  # whether we opened it and must close it


async def _cacheman_entries(client: SplunkClient) -> List[Dict[str, Any]]:
    """Every bucket the cache manager knows about, a page at a time."""
    entries: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = await client.entries(
            CACHEMAN_PATH,
            "smartstore cache",
            params={"count": str(CACHEMAN_PAGE), "offset": str(offset)},
        )
        entries.extend(page)
        if len(page) < CACHEMAN_PAGE:
            return entries
        offset += CACHEMAN_PAGE


async def discover_indexers(
    client: SplunkClient,
    *,
    indexer_urls: Sequence[str] = (),
    indexer_token: Optional[str] = None,
    indexer_username: Optional[str] = None,
    indexer_password: Optional[str] = None,
) -> tuple[List[Indexer], List[str]]:
    """Where the cache is.

    The cache manager is per indexer. Against a single instance that is the
    target itself. Against a search head, it is the search peers: either the
    ones named in configuration, or the ones the search head lists, reached
    with the indexer credential if one was given and the target's own if
    not. Reading cacheman on the search head of a distributed deployment
    reported "no SmartStore" for every real cluster, which is the case the
    whole module exists for.
    """
    notes: List[str] = []
    target = client.target
    urls = list(indexer_urls)
    if not urls:
        try:
            peers = await client.entries(PEERS_PATH, "search peers")
        except SplunkError as exc:
            peers = []
            notes.append(f"could not list search peers: {exc}")
        for peer in peers:
            if str(peer.get("disabled", "")).lower() in ("1", "true"):
                continue
            name = str(peer.get("name") or "")
            if not name:
                continue
            scheme = "https" if target.url.startswith("https") else "http"
            urls.append(f"{scheme}://{name}")
    if not urls:
        return [Indexer(name=target.url, client=client, owned=False)], notes

    credential = dict(
        token=indexer_token or target.token,
        username=indexer_username or target.username,
        password=indexer_password or target.password,
    )
    if not indexer_token and not (indexer_username and indexer_password):
        notes.append(
            "reaching the indexers with the search head's own credential: set the "
            "indexer credential on the target if that is not valid on the peers"
        )
    indexers: List[Indexer] = []
    for url in urls:
        try:
            config = TargetConfig(
                url=url,
                verify_tls=target.verify_tls,
                api_version=target.api_version,
                **credential,
            )
        except Exception as exc:  # noqa: BLE001 - a bad peer URL is a note
            notes.append(f"{url}: {exc}")
            continue
        indexers.append(Indexer(name=url, client=SplunkClient(config), owned=True))
    return indexers, notes


async def _close(indexers: Sequence[Indexer]) -> None:
    for indexer in indexers:
        if indexer.owned:
            try:
                await indexer.client.close()
            except Exception:  # noqa: BLE001
                pass


async def cache_state(client: SplunkClient, **where: Any) -> CacheState:
    """Read the cache manager on every indexer. Never raises: absence is an answer.

    ``where`` is the indexer location and credential, as keyword arguments
    matching :func:`discover_indexers`.
    """
    state = CacheState()
    indexers, notes = await discover_indexers(client, **where)
    state.notes.extend(notes)
    answered = 0
    try:
        for indexer in indexers:
            try:
                entries = await _cacheman_entries(indexer.client)
            except SplunkError as exc:
                state.peers[indexer.name] = {"available": False, "reason": str(exc)[:200]}
                continue
            except Exception as exc:  # noqa: BLE001 - an unreachable peer is a note
                state.peers[indexer.name] = {"available": False, "reason": str(exc)[:200]}
                continue
            if not entries:
                state.peers[indexer.name] = {
                    "available": False,
                    "reason": "no cache manager entries: this instance is not using SmartStore, "
                    "or the account cannot read /services/admin/cacheman",
                }
                continue
            answered += 1
            local = 0
            local_bytes = 0
            for entry in entries:
                bucket = entry.get("cm:bucket") or {}
                bid = entry.get("name") or ""
                # Namespaced per indexer: a replicated bucket exists on
                # several peers and its cache state differs on each.
                name = f"{indexer.name}|{bid}" if len(indexers) > 1 else bid
                index = _index_of(bid)
                size = _int(bucket.get("estimated_size")) or _int(bucket.get("journal_size"))
                per = state.per_index.setdefault(index, IndexCache(index=index))
                per.total_bytes += size
                state.total_bytes += size
                if str(bucket.get("status", "")).lower() == STATUS_LOCAL:
                    state.local_buckets += 1
                    state.local_bytes += size
                    state.local_ids.add(name)
                    state.local_bytes_by_id[name] = size
                    per.local_buckets += 1
                    per.local_bytes += size
                    local += 1
                    local_bytes += size
                else:
                    state.remote_buckets += 1
                    per.remote_buckets += 1
            state.peers[indexer.name] = {
                "available": True,
                "buckets": len(entries),
                "local_buckets": local,
                "local_bytes": local_bytes,
            }
            if state.max_cache_size_mb is None:
                config = await _cache_config(indexer.client)
                state.max_cache_size_mb = config.get("max_cache_size")
                state.eviction_policy = config.get("eviction_policy")
                state.hotlist_recency_secs = config.get("hotlist_recency_secs")
    finally:
        await _close(indexers)

    if not answered:
        reasons = {p.get("reason", "") for p in state.peers.values()}
        state.reason = "; ".join(sorted(r for r in reasons if r)) or (
            "no cache manager entries: this instance is not using SmartStore, or the "
            "account cannot read /services/admin/cacheman"
        )
        return state
    state.available = True
    if state.max_cache_size_mb is not None and len(indexers) > 1:
        # max_cache_size is per indexer; the fill figure below is against the
        # estate's total, so scale it by the peers that answered.
        state.max_cache_size_mb = state.max_cache_size_mb * answered
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
    """What changed between two cache readings.

    Compares the SETS of locally cached buckets, not the counts. Net counting
    was a real defect: a cache at its ceiling downloads and evicts in equal
    measure, so a run that fetched three thousand buckets from object storage
    and evicted three thousand to make room reported "warm, nothing was
    downloaded". That is the opposite of the truth, and a cache at its ceiling
    is the normal case rather than an edge one, which the report itself warns
    about elsewhere.

    With sets, downloads and evictions can both be non-zero, which is what
    actually happens.
    """
    if not (before.available and after.available):
        return CacheDelta(available=False)

    # Fall back to counts only when the identities were not captured, which
    # happens for hand-built states in tests.
    if before.local_ids or after.local_ids:
        downloaded_ids = after.local_ids - before.local_ids
        evicted_ids = before.local_ids - after.local_ids
        downloaded = len(downloaded_ids)
        evicted = len(evicted_ids)
        bytes_downloaded = sum(
            after.local_bytes_by_id.get(bucket_id, 0) for bucket_id in downloaded_ids
        )
    else:
        downloaded = max(0, after.local_buckets - before.local_buckets)
        evicted = max(0, before.local_buckets - after.local_buckets)
        bytes_downloaded = max(0, after.local_bytes - before.local_bytes)

    return CacheDelta(
        available=True,
        buckets_downloaded=downloaded,
        bytes_downloaded=bytes_downloaded,
        buckets_evicted_during=evicted,
        local_before=before.local_buckets,
        local_after=after.local_buckets,
        fill_pct_before=round(before.fill_pct, 2) if before.fill_pct is not None else None,
        fill_pct_after=round(after.fill_pct, 2) if after.fill_pct is not None else None,
    )


@dataclass
class EvictionResult:
    attempted: int = 0
    # Buckets that were local before and are remote after, which is the only
    # proof an eviction happened: the cache manager answers 200 to a request
    # it then declines (a live reader, the hotlist), so the accepted count
    # alone was a fiction.
    confirmed: int = 0
    evicted: int = 0
    failed: int = 0
    bytes_evicted: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "evicted": self.evicted,
            "confirmed": self.confirmed,
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
    **where: Any,
) -> EvictionResult:
    """Evict every locally cached bucket, optionally only for named indexes.

    This is the cold-start button. It is destructive to performance, not to
    data: SmartStore re-downloads whatever a later search needs, so nothing is
    lost except the time and the object-storage egress to fetch it again.

    Restricting to the indexes a scenario actually searches is strongly
    preferred over evicting everything. On a shared cluster the rest of the
    cache belongs to other people's dashboards, and flushing it to measure your
    own cold path makes their afternoon slow for no benefit to your result.

    Runs on every indexer, and confirms by reading the cache back: the
    cache manager answers 200 to an eviction it then declines, so the count
    of accepted requests is reported alongside the count of buckets that
    actually left.
    """
    result = EvictionResult()
    wanted = {i.lower() for i in indexes} if indexes else None
    indexers, notes = await discover_indexers(client, **where)
    result.errors.extend(notes)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    try:
        for indexer in indexers:
            try:
                entries = await _cacheman_entries(indexer.client)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{indexer.name}: could not list the cache: {exc}")
                continue

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
                continue

            log.warning(
                "EVICTING %d cached bucket(s) (%.1f GB) from %s on %s. The next searches "
                "will re-download what they need from object storage, which is the point "
                "of a cold-cache run and is not free",
                len(targets),
                sum(size for _, size in targets) / 1e9,
                ", ".join(sorted(wanted)) if wanted else "every index",
                indexer.name,
            )

            async def evict_one(bucket_id: str, size: int, peer: SplunkClient = indexer.client) -> None:
                async with semaphore:
                    encoded = urllib.parse.quote(bucket_id, safe="")
                    try:
                        await peer.post_action(f"{CACHEMAN_PATH}/{encoded}/evict", "evict bucket")
                    except SplunkError as exc:
                        result.failed += 1
                        if len(result.errors) < 20:
                            result.errors.append(f"{bucket_id}: {exc}")
                    else:
                        result.evicted += 1
                        result.bytes_evicted += size

            result.attempted += len(targets)
            await asyncio.gather(*(evict_one(bid, size) for bid, size in targets))

            # Confirm. A 200 means the request was accepted, not that the
            # bucket left: a live reader or the hotlist keeps it.
            try:
                after = await _cacheman_entries(indexer.client)
            except Exception:  # noqa: BLE001 - unconfirmed is reported as such
                continue
            still_local = {
                entry.get("name")
                for entry in after
                if str((entry.get("cm:bucket") or {}).get("status", "")).lower() == STATUS_LOCAL
            }
            result.confirmed += sum(1 for bid, _ in targets if bid not in still_local)
    finally:
        await _close(indexers)

    log.warning(
        "eviction finished: %d accepted, %d confirmed gone, %d failed, %.1f GB requested",
        result.evicted,
        result.confirmed,
        result.failed,
        result.bytes_evicted / 1e9,
    )
    if result.evicted and result.confirmed < result.evicted:
        result.errors.append(
            f"{result.evicted - result.confirmed} bucket(s) were accepted for eviction and "
            "are still local: a search is reading them or they are inside the hotlist "
            "window, which is correct cache-manager behaviour rather than a fault"
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
