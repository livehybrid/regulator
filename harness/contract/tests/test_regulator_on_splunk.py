"""Regulator's own reads of the target, against a real one.

Not in issue 16's table because they post-date it: the field names the host
resources panel and Diagnose read, and the ingest-to-searchable probe. Each
was written from documentation and not confirmed until now.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from conftest import HEC_TOKEN, HEC_URL, INDEX, SH_URL, client_for, oneshot, run, wait_for, with_client


def test_host_resources_reads_cpu_memory_load_and_roles():
    from regulator_agent.hostresources import host_resources

    def read():
        now = time.time()
        return run(with_client(SH_URL, lambda c: host_resources(c, now - 900, now - 60)))

    result = wait_for(lambda: (lambda r: r if r.get("available") else None)(read()),
                      timeout_s=300, every_s=30, what="host resources")
    print({k: v for k, v in result.items() if k not in ("roles",)})
    assert result["available"], result.get("reason")
    hosts = {h["host"]: h for h in result["hosts"]}
    roles = {name: h["role"] for name, h in hosts.items()}
    print("roles:", roles)
    # The tiers are told apart by the search head's own lists.
    assert {"idx1", "idx2"} <= {n for n, r in roles.items() if r == "indexer"}, roles
    assert "sh1" in roles and roles["sh1"] == "search head", roles
    for name in ("idx1", "sh1"):
        host = hosts[name]
        assert host["cpu"].get("avg_pct") is not None, host
        # The documented-but-unconfirmed fields: memory and load.
        assert host["mem"].get("avg_pct") is not None, ("data.mem / data.mem_used", host)
        assert host["load"].get("avg") is not None, ("data.normalized_load_avg_1min", host)
        assert host.get("cores"), ("data.cpu_count / virtual_cpu_count", host)


def test_disk_figures_are_read_wherever_splunkd_writes_them():
    """IOStats, where the hosts have it.

    On GitHub's hosted runners splunkd did write IOStats, and Regulator read
    none of them: its queries grouped by a mount point those events did not
    carry. Skipped only where splunkd writes no IOStats at all.
    """
    from regulator_agent.hostresources import host_resources

    def written():
        rows = run(oneshot(SH_URL, "search index=_introspection sourcetype=splunk_resource_usage "
                                   "component=IOStats | head 1", earliest=time.time() - 1800))
        return bool(rows)

    try:
        wait_for(written, timeout_s=240, every_s=30, what="any IOStats event")
    except AssertionError:
        pytest.skip("splunkd wrote no IOStats events on these hosts, so there is nothing to read")
    sample = run(oneshot(SH_URL, "search index=_introspection sourcetype=splunk_resource_usage "
                                 "component=IOStats | head 2 | table host _raw", earliest=time.time() - 1800))
    print("IOStats as splunkd writes it here:", sample)
    now = time.time()
    result = run(with_client(SH_URL, lambda c: host_resources(c, now - 1800, now - 60)))
    io = [h for h in result["hosts"] if (h.get("io") or {}).get("read_mb_s") is not None]
    print("disk figures for:", [h["host"] for h in io], "mounts:", result["mounts"][:3])
    assert io and result["mounts"], ("IOStats", result["notes"], sample)


def test_diagnose_finds_a_search_in_audit_and_on_each_indexer(corpus):
    from regulator_agent.timepolicy import TimeWindow
    from regulator_server import diagnose

    sid = f"reg_contract_diag_{uuid.uuid4().hex[:8]}"

    async def dispatch(c):
        await c.create_job(f"search index={INDEX} {corpus['tag']} | stats count",
                           TimeWindow(earliest=corpus["earliest"], latest=corpus["latest"]), sid=sid)
        for _ in range(120):
            status = await c.job_status(sid)
            if status.is_terminal:
                return status
            await asyncio.sleep(0.5)

    run(with_client(SH_URL, dispatch))
    started = time.time() - 120

    def asked():
        queries, rows = run(with_client(SH_URL, lambda c: diagnose.ask_target(c, [sid], started, time.time() + 60)))
        found = diagnose.fold([sid], rows["audit"], rows["indexers"], rows["logs"]).get(sid) or {}
        # "completed" is written when the search ends and can lag the
        # "granted" record by a while on a busy cluster (seen on CI).
        ended = (found.get("audit") or {}).get("ended")
        return (queries, found) if ended == "completed" and found.get("indexers") else None

    queries, found = wait_for(asked, timeout_s=240, every_s=15, what="audit and remote search records of the search")
    print("queries:", queries)
    print("found:", found)
    assert all(q["available"] for q in queries), queries
    assert found["audit"]["ended"] == "completed", found["audit"]
    assert found["audit"].get("total_run_time") is not None and found["audit"].get("scan_count") is not None
    hosts = {row.get("host") for row in found["indexers"]}
    assert hosts & {"idx1", "idx2"}, found["indexers"]
    # The fields read from remote_searches.log, unconfirmed until now.
    for row in found["indexers"]:
        assert row.get("elapsed_s") is not None and row.get("scan_count") is not None, row


@pytest.mark.skipif(not HEC_URL, reason="needs a HEC address")
def test_the_freshness_probe_finds_its_events_on_a_real_cluster():
    # The probe lands with issue 21; until both are in, this is skipped.
    FreshnessProbe = pytest.importorskip("regulator_agent.freshness").FreshnessProbe

    async def go():
        client = client_for(SH_URL)
        await client.start()
        try:
            probe = FreshnessProbe(hec_url=HEC_URL, hec_token=HEC_TOKEN, client=client, index=INDEX,
                                   run_label=f"contract-{uuid.uuid4().hex[:6]}", interval_s=3.0,
                                   verify_tls=False, tail_s=90.0)
            task = asyncio.create_task(probe.run())
            await asyncio.sleep(20)
            probe.stop()
            await task
            return probe.result()
        finally:
            await client.close()

    result = run(go())
    print({k: v for k, v in result.items() if k != "points"})
    assert result["available"] and result["send_failed"] == 0, result
    assert result["found"] == result["during_run"] >= 5, result
    assert 0 < result["latency"]["p50_s"] < 60
    # Same host, so the clocks agree: no probe indexed before it was sent.
    assert result["skew_evident"] is False
    assert result["index_delay"]["count"] == result["found"]
