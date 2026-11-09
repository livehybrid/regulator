"""The bucket map: buckets by index and time, by how much is on local disk."""

from __future__ import annotations

from regulator_agent import bucketmap, cachewarm
from regulator_agent.smartstore import Bucket, CacheState, IndexCache

GB = 1_000_000_000
T0 = 10_000_000.0
DAY = 86400.0


def bucket(n, index="main", status="remote", day=0, days=1, size=GB, server="idx1", dated=True):
    return Bucket(
        server=server, bid=f"bid|{index}~{n}~G|", index=index, status=status, size=size,
        earliest=T0 + day * DAY if dated else None,
        latest=T0 + (day + days) * DAY if dated else None,
    )


def state_of(buckets):
    state = CacheState(available=True, buckets=list(buckets))
    for b in buckets:
        state.per_index.setdefault(b.index, IndexCache(index=b.index))
    return state


def residency_of(**classes):
    r = cachewarm.Residency(available=True)
    for bid, cls in classes.items():
        r.per_bid[bid] = cachewarm.BucketResidency(cls=cls, local_bytes=0, full_bytes=GB)
    return r


def test_buckets_land_in_their_time_slices_by_state():
    state = state_of([
        bucket(1, status="local", day=0),
        bucket(2, status="remote", day=1),
        bucket(3, status="remote", day=9),
    ])
    m = bucketmap.build(state, bins=10, now=T0)
    assert m["available"] and m["detail"] == "cache"
    assert m["states"] == ["remote", "local"]
    assert (m["earliest"], m["latest"], m["bins"]) == (int(T0), int(T0 + 10 * DAY), 10)
    row, = m["indexes"]
    cells = {c["bin"]: c for c in row["cells"]}
    assert set(cells) == {0, 1, 9}
    assert cells[0]["bytes"] == {"local": GB} and cells[0]["buckets"] == 1
    assert cells[1]["bytes"] == {"remote": GB}
    assert row["buckets"] == 3 and row["bytes"] == 3 * GB
    assert row["local_pct"] == round(100 / 3, 2)


def test_a_bucket_spanning_slices_splits_its_bytes_and_a_row_sums_to_the_index():
    state = state_of([bucket(1, status="local", day=0, days=4), bucket(2, day=4, days=6)])
    m = bucketmap.build(state, bins=10, now=T0)
    row, = m["indexes"]
    assert [c["bin"] for c in row["cells"]] == list(range(10))
    assert row["cells"][0]["bytes"] == {"local": GB // 4}
    total = sum(v for c in row["cells"] for v in c["bytes"].values())
    assert abs(total - row["bytes"]) < 10  # integer rounding only


def test_on_disk_detail_refines_local_and_a_stub_counts_as_remote():
    state = state_of([
        bucket(1, status="local", day=0), bucket(2, status="local", day=1),
        bucket(3, status="local", day=2), bucket(4, status="local", day=3),
        bucket(5, status="remote", day=4), bucket(6, status="local", day=5),
    ])
    residency = residency_of(**{
        "bid|main~1~G|": cachewarm.FULL, "bid|main~2~G|": cachewarm.INDEX_ONLY,
        "bid|main~3~G|": cachewarm.PARTIAL, "bid|main~4~G|": cachewarm.STUB,
        "bid|main~5~G|": cachewarm.STUB,
    })
    residency.unmatched = 1
    m = bucketmap.build(state, bins=6, residency=residency, now=T0)
    assert m["detail"] == "disk"
    # Least to most ready, with "local, detail not read" last.
    assert m["states"] == ["remote", "partial", "index_only", "full", "local"]
    by_state = m["indexes"][0]["by_state"]
    assert by_state["remote"]["buckets"] == 2
    assert by_state["local"]["buckets"] == 1
    assert any("no dbinspect row" in note for note in m["notes"])


def test_a_replicated_bucket_is_one_bucket_local_if_any_copy_is():
    state = state_of([
        bucket(1, status="remote", server="idx1"), bucket(1, status="local", server="idx2"),
    ])
    row, = bucketmap.build(state, bins=10, now=T0)["indexes"]
    assert row["buckets"] == 1 and row["bytes"] == GB
    assert list(row["by_state"]) == ["local"]


def test_a_range_clips_and_counts_what_falls_outside():
    state = state_of([bucket(n, day=n) for n in range(10)] + [bucket(99, dated=False)])
    m = bucketmap.build(state, earliest=T0 + 2 * DAY, latest=T0 + 5 * DAY, bins=10, now=T0)
    row, = m["indexes"]
    assert row["outside"] == 7 and row["undated"] == 1
    assert row["buckets"] == 11  # the totals are the index's, not the window's
    assert sum(v for c in row["cells"] for v in c["bytes"].values()) == 3 * GB


def test_indexes_are_filtered_ordered_by_size_and_capped():
    state = state_of(
        [bucket(1, index="small", size=GB)] + [bucket(10 + n, index="big", day=n) for n in range(3)]
        + [bucket(20, index="mid", size=2 * GB)]
    )
    assert [r["index"] for r in bucketmap.build(state, now=T0)["indexes"]] == ["big", "mid", "small"]
    assert [r["index"] for r in bucketmap.build(state, indexes=["MID"], now=T0)["indexes"]] == ["mid"]
    capped = bucketmap.build(state, max_indexes=2, now=T0)
    assert len(capped["indexes"]) == 2 and any("largest" in n for n in capped["notes"])


def test_no_cache_and_no_buckets_are_answers():
    m = bucketmap.build(CacheState(available=False, reason="not SmartStore"))
    assert not m["available"] and m["reason"] == "not SmartStore"
    m = bucketmap.build(state_of([bucket(1)]), indexes=["nope"], now=T0)
    assert not m["available"] and "nope" in m["reason"]


def test_bins_are_bounded():
    state = state_of([bucket(1)])
    assert bucketmap.build(state, bins=1, now=T0)["bins"] == bucketmap.MIN_BINS
    assert bucketmap.build(state, bins=10_000, now=T0)["bins"] == bucketmap.MAX_BINS


def test_the_overlay_matches_wildcards_merges_and_clips():
    spans = [
        cachewarm.StepSpan("a", ["web*"], T0 + DAY, T0 + 3 * DAY),
        cachewarm.StepSpan("b", ["web_prod"], T0 + 2 * DAY, T0 + 20 * DAY),
        cachewarm.StepSpan("c", ["fw"], T0 - 9 * DAY, T0 - 8 * DAY),
    ]
    out = bucketmap.overlay(spans, ["web_prod", "fw", "main"], T0, T0 + 10 * DAY)
    assert out == [{
        "index": "web_prod", "earliest": int(T0 + DAY), "latest": int(T0 + 20 * DAY), "clipped": True,
    }]
