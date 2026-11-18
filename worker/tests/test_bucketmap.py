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


# ------------------------------------------------------------ plan map


def test_plan_map_draws_the_plans_indexes_and_lines_with_a_margin():
    state = state_of([
        bucket(1, index="web_a", day=0), bucket(2, index="web_b", day=2),
        bucket(3, index="other", day=1, size=50 * GB),
    ])
    plan = cachewarm.parse_plan([
        {"index": "web_*", "earliest": T0 + DAY, "latest": T0 + 2 * DAY},
        {"index": "absent", "earliest": T0, "latest": T0 + DAY},
    ], now=T0)
    m = bucketmap.plan_map(state, plan, bins=20, now=T0)
    assert m["available"]
    # The wildcard is matched against what the cache manager lists; the
    # largest index on the target is not drawn because the plan does not name it.
    assert sorted(r["index"] for r in m["indexes"]) == ["web_a", "web_b"]
    # The axis reaches past the plan on both sides.
    assert m["earliest"] < T0 and m["latest"] > T0 + 2 * DAY
    spans = sorted((s["index"], s["earliest"], s["latest"]) for s in m["overlay"]["spans"])
    assert spans == [
        ("web_a", int(T0 + DAY), int(T0 + 2 * DAY)),
        ("web_b", int(T0 + DAY), int(T0 + 2 * DAY)),
    ]
    assert m["overlay"]["plan"] is True
    assert any("absent" in n for n in m["overlay"]["notes"])


def test_plan_map_says_why_when_the_cache_cannot_be_read():
    plan = cachewarm.parse_plan([{"index": "main", "earliest": T0, "latest": T0 + DAY}], now=T0)
    m = bucketmap.plan_map(CacheState(available=False, reason="401 from the cluster manager"), plan, now=T0)
    assert not m["available"] and "401" in m["reason"]


# ------------------------------------------------------- run before/after


def _with_counts(state):
    for b in state.buckets:
        entry = state.per_index[b.index]
        entry.total_bytes += b.size
        if b.is_local:
            entry.local_buckets += 1
        else:
            entry.remote_buckets += 1
    return state


def test_run_pair_shares_rows_and_axis_and_shows_what_the_run_fetched():
    before = _with_counts(state_of([
        bucket(1, index="web", status="remote", day=0), bucket(2, index="web", status="remote", day=5),
        bucket(3, index="big", status="remote", day=0, days=30, size=99 * GB),
    ]))
    # The run read web's day 5 bucket; a new bucket also rolled in afterwards.
    after = _with_counts(state_of([
        bucket(1, index="web", status="remote", day=0), bucket(2, index="web", status="local", day=5),
        bucket(4, index="web", status="local", day=8),
        bucket(3, index="big", status="remote", day=0, days=30, size=99 * GB),
    ]))
    forecast = {
        "available": True,
        "per_index": {"web": {"bytes_total": 2 * GB, "spans": [[T0 + 4 * DAY, T0 + 6 * DAY]]}},
    }
    pair = bucketmap.run_pair(before, after, forecast, bins=10)
    assert pair["available"] and pair["source"] == "forecast"
    b, a = pair["before"], pair["after"]
    # Only what the run reads is drawn, on one axis for both sides.
    assert [r["index"] for r in b["indexes"]] == ["web"] == [r["index"] for r in a["indexes"]]
    assert (b["earliest"], b["latest"], b["bins"]) == (a["earliest"], a["latest"], a["bins"])
    assert (b["earliest"], b["latest"]) == (int(T0 + 4 * DAY), int(T0 + 6 * DAY))

    def local_bytes(doc):
        return sum(c["bytes"].get("local", 0) for c in doc["indexes"][0]["cells"])

    assert local_bytes(b) == 0 and local_bytes(a) == GB
    assert pair["overlay"] == [
        {"index": "web", "earliest": int(T0 + 4 * DAY), "latest": int(T0 + 6 * DAY), "clipped": False}
    ]


def test_run_pair_without_a_forecast_shows_the_indexes_that_changed():
    before = _with_counts(state_of([
        bucket(1, index="quiet", day=0, size=50 * GB), bucket(2, index="busy", day=0),
    ]))
    after = _with_counts(state_of([
        bucket(1, index="quiet", day=0, size=50 * GB), bucket(2, index="busy", status="local", day=0),
    ]))
    pair = bucketmap.run_pair(before, after, None, bins=10)
    assert pair["source"] == "changed"
    assert [r["index"] for r in pair["after"]["indexes"]] == ["busy"]
    assert pair["overlay"] == []


def test_run_pair_with_nothing_changed_falls_back_to_the_largest():
    state = _with_counts(state_of([bucket(1, index="small", day=0), bucket(2, index="large", day=0, size=9 * GB)]))
    pair = bucketmap.run_pair(state, state, {"available": False}, bins=10, max_indexes=1)
    assert pair["source"] == "largest"
    assert [r["index"] for r in pair["before"]["indexes"]] == ["large"]


def test_run_pair_is_unavailable_when_either_reading_failed():
    good = _with_counts(state_of([bucket(1)]))
    pair = bucketmap.run_pair(good, CacheState(available=False, reason="timed out"), None)
    assert not pair["available"] and pair["reason"] == "timed out"
