"""SmartStore: what the cache manager and dbinspect report, and what a tstats downloads.

Rows 1 to 3 of issue 16's table. cachewarm.py's residency classes (full,
index files only, partial, stub) rest on all three.
"""

from __future__ import annotations

import time

from conftest import INDEX, INDEXER_URLS, SH_URL, oneshot, run, wait_for, with_client


def cacheman_rows(index):
    rows = run(oneshot(
        SH_URL,
        "| rest splunk_server=* /services/admin/cacheman count=0 "
        f'| search title="bid|{index}~*" '
        "| eval status='cm:bucket.status', earliest='cm:bucket.earliest_time', "
        "latest='cm:bucket.latest_time', journal='cm:bucket.journal_size', "
        "journal_local='cm:bucket.file_type.journal_gz', tsidx_local='cm:bucket.file_type.tsidx' "
        "| fields splunk_server, title, status, earliest, latest, journal, journal_local, tsidx_local",
    ))
    return rows


def dbinspect(index, cached):
    rows = run(oneshot(
        SH_URL,
        f"| dbinspect index={index} cached={'true' if cached else 'false'} timeformat=%s "
        "| eval bytes=sizeOnDiskMB*1048576 | fields splunk_server, bucketId, state, bytes",
    ))
    return {(r["splunk_server"], f"bid|{r['bucketId']}|"): float(r["bytes"]) for r in rows if r.get("bucketId")}


def uploaded(index):
    """Warm buckets that are in remote storage, by (server, bid)."""
    return {(r["splunk_server"], r["title"]): r for r in cacheman_rows(index) if r.get("status") in ("local", "remote")}


def test_the_cache_manager_reports_the_fields_the_forecast_reads(corpus):
    rows = wait_for(lambda: cacheman_rows(INDEX), what="cache manager entries for the contract index")
    for row in rows:
        assert row.get("earliest") and row.get("latest"), row
        assert int(row["earliest"]) <= int(row["latest"])
        assert int(row["journal"]) > 0, row
    # And Regulator's own reading of them, through the search head.
    from regulator_agent.smartstore import cache_state

    state = run(with_client(SH_URL, lambda c: cache_state(c)))
    assert state.available, state.reason
    ours = [b for b in state.buckets if b.index == INDEX]
    assert ours and all(b.earliest and b.latest and b.journal_size for b in ours)


def settled(index):
    """No copy uploading or downloading: a bucket still uploading cannot be evicted.

    The copy that did not do the upload reports ``canceled``, which is as
    finished as ``idle`` (seen on 10.6: one peer uploads, and the other
    copy's upload is cancelled once it has).
    """
    rows = run(oneshot(
        SH_URL,
        "| rest splunk_server=* /services/admin/cacheman count=0 "
        f'| search title="bid|{index}~*" '
        "| eval up='cm:bucket.upload_status', down='cm:bucket.download_status' "
        '| eval busy=if(in(up, "idle", "canceled") AND in(down, "idle", "canceled"), 0, 1) '
        "| stats sum(busy) as busy, count as copies",
    ))
    return rows and int(rows[0]["copies"]) > 0 and int(rows[0]["busy"]) == 0


def evict_contract():
    """Evict every copy of every bucket in the index, and wait until they are gone.

    Through the indexers by the addresses this host reaches them on: the
    search head knows them by their container addresses, which it does not.
    """
    from regulator_agent.smartstore import evict_all

    wait_for(lambda: settled(INDEX), what="every bucket to finish uploading")
    results = []

    def all_gone():
        results.append(run(with_client(SH_URL, lambda c: evict_all(c, indexes=[INDEX], indexer_urls=INDEXER_URLS))))
        local = dbinspect(INDEX, cached=False)
        return local and all(size < 64 * 1024 for size in local.values())

    wait_for(all_gone, timeout_s=180, every_s=5, what="every copy to be evicted")
    return results


def test_dbinspect_cached_false_is_local_and_cached_true_is_the_whole_bucket(corpus):
    results = evict_contract()
    assert sum(r.confirmed for r in results) > 0, [r.to_dict() for r in results]
    local, full = dbinspect(INDEX, cached=False), dbinspect(INDEX, cached=True)
    evicted = [k for k in local if k in full and full[k] > 0 and local[k] < 0.5 * full[k]]
    # Every copy is evicted, and cached=true still reports the whole bucket.
    assert evicted and len(evicted) == len(local), {"local": local, "full": full}
    from regulator_agent.cachewarm import classify

    journals = {(r["splunk_server"], r["title"]): int(r["journal"]) for r in cacheman_rows(INDEX)}
    classes = {k: classify(local[k], full[k], journals.get(k)) for k in evicted}
    assert set(classes.values()) == {"stub"}, {"classes": classes, "local": local, "full": full}


def test_a_tstats_downloads_the_index_files_and_not_the_journal(corpus):
    """What cachewarm's tsidx mode and its "index files only" class rest on.

    Checked on the bytes rather than through classify(): its 2 MB stub floor
    is right for production buckets (hundreds of MB) and larger than every
    bucket this small cluster makes.
    """
    evict_contract()
    run(oneshot(SH_URL, f"| tstats count where index={INDEX} TERM({corpus['tag']}) by _time span=1m",
                earliest=corpus["earliest"], latest=corpus["latest"]))
    time.sleep(3)
    local, full = dbinspect(INDEX, cached=False), dbinspect(INDEX, cached=True)
    rows = {(r["splunk_server"], r["title"]): r for r in cacheman_rows(INDEX)}
    fetched = {k: rows[k] for k in local if k in rows and rows[k]["tsidx_local"] == "1"}
    print("after tstats:", {k: (local[k], full.get(k), r["journal"], r["journal_local"]) for k, r in fetched.items()})
    assert fetched, "the tstats fetched no index files at all"
    for key, row in fetched.items():
        # The cache manager says the journal stayed remote...
        assert row["journal_local"] == "0", (key, row)
        # ...and the bytes on disk are the bucket less its journal.
        without_journal = full[key] - int(row["journal"])
        assert abs(local[key] - without_journal) <= 0.15 * full[key], (key, local[key], full[key], row["journal"])

    # Then a raw-event search over the same range brings the journal too.
    run(oneshot(SH_URL, f"search index={INDEX} {corpus['tag']} | stats count",
                earliest=corpus["earliest"], latest=corpus["latest"]))
    time.sleep(3)
    local = dbinspect(INDEX, cached=False)
    rows = {(r["splunk_server"], r["title"]): r for r in cacheman_rows(INDEX)}
    after = {k: (local[k], full[k], rows[k]["journal_local"]) for k in fetched}
    print("after a raw search:", after)
    assert all(rows[k]["journal_local"] == "1" and local[k] >= 0.95 * full[k] for k in fetched), after
