"""Ingest-to-searchable latency, against the fake splunkd's HEC and search."""

from __future__ import annotations

import asyncio

import pytest

from regulator_agent import freshness as fr
from regulator_agent.config import TargetConfig
from regulator_agent.splunk import SplunkClient

FakeSplunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present").FakeSplunk


def client_for(server):
    return SplunkClient(TargetConfig(url=server.base_url, token="tok-en", verify_tls=False))


def measure(server, *, run_s=4.0, interval_s=2.0, tail_s=10.0, token="hec-token", index="main"):
    async def go():
        client = client_for(server)
        await client.start()
        try:
            probe = fr.FreshnessProbe(
                hec_url=server.base_url, hec_token=token, client=client, index=index,
                run_label="r7-label", interval_s=interval_s, verify_tls=False, tail_s=tail_s,
            )
            task = asyncio.create_task(probe.run())
            await asyncio.sleep(run_s)
            probe.stop()
            await task
            return probe
        finally:
            await client.close()

    return asyncio.run(go())


def test_probes_are_sent_found_and_timed_on_one_clock():
    server = FakeSplunk(port=0, ingest_delay_s=1.5)
    try:
        probe = measure(server)
    finally:
        server.close()
    result = probe.result()
    assert result["available"] is True
    assert result["sent"] >= 2 and result["send_failed"] == 0
    assert result["found"] == result["during_run"] >= 2 and result["not_found"] == 0
    latency = result["latency"]
    # Searchable 1.5 s after arrival; a probe is placed between the last
    # search that missed it and the first that found it.
    assert 1.0 <= latency["p50_s"] <= 1.5 + probe.poll_s + 0.5
    # Never wider than two polls: one before the first miss, one between the
    # miss and the find.
    assert result["resolution_s"] <= 2 * probe.poll_s + 0.5
    # The fake indexes on arrival, so the split puts the delay after indexing,
    # to the second (_indextime is whole seconds).
    # (_indextime is whole seconds; its middle is the estimate.)
    assert result["index_delay"]["max_s"] <= 1.5
    assert result["after_index"]["p50_s"] >= 0.0
    assert result["skew_evident"] is False
    assert len(result["points"]) == result["sent"] and result["points"][0][1] is not None
    said = " ".join(result["findings"])
    assert "Events became searchable" in said and "at the median" in said


def test_a_probe_that_never_becomes_searchable_is_counted_not_dropped():
    server = FakeSplunk(port=0, ingest_delay_s=600.0)
    try:
        probe = measure(server, run_s=2.5, tail_s=2.0)
    finally:
        server.close()
    result = probe.result()
    assert result["found"] == 0 and result["not_found"] == result["during_run"] >= 1
    assert result["latency"] == {"count": 0}
    assert "were not searchable 2s after the run ended" in " ".join(result["findings"])
    assert all(point[1] is None for point in result["points"])


def test_a_refused_token_is_reported_and_nothing_is_searched_for():
    server = FakeSplunk(port=0, hec_reject_token="bad")
    try:
        probe = measure(server, token="bad", run_s=2.5, tail_s=1.0)
    finally:
        server.close()
    result = probe.result()
    assert result["available"] is False and result["send_failed"] == result["sent"] >= 1
    assert "HTTP 403" in result["reason"]
    assert result["searches"] == 0
    assert result["findings"][0].startswith("No ingest-to-searchable figure: sending probe 1: HTTP 403")


def test_probes_before_the_load_are_the_baseline_not_the_result():
    server = FakeSplunk(port=0, ingest_delay_s=0.5)
    try:
        probe = measure(server, run_s=5.0, interval_s=2.0)
    finally:
        server.close()
    sent = sorted(p.sent_wall for p in probe.probes)
    result = probe.result(run_started_at=sent[1] - 0.01)
    assert result["before_run"]["count"] == 1
    assert result["during_run"] == len(sent) - 1
    assert "Before the load started the median was" in " ".join(result["findings"])


def test_the_search_names_the_run_and_nothing_else():
    probe = fr.FreshnessProbe(hec_url="https://h:8088", hec_token="t", client=None, index="probe_idx", run_label="r9 my label")
    spl = probe.search_spl()
    assert spl.startswith('search index=probe_idx sourcetype="regulator:freshness" source="regulator:r9_my_label"')
    assert "stats min(indextime) as indextime by probe" in spl


@pytest.mark.parametrize("name, ok", [("main", True), ("probe_idx-2", True), ("_internal", True),
                                      ("main OR index=*", False), ("", False), ('a"b', False)])
def test_only_a_plain_index_name_is_accepted(name, ok):
    assert fr.valid_index(name) is ok


def test_skew_is_flagged_and_the_split_withheld():
    probe = fr.FreshnessProbe(hec_url="h", hec_token="t", client=None, index="main", run_label="r")
    for n, (lo, hi, indexed_early_by) in enumerate([(1.0, 3.0, 5.0), (1.0, 2.0, 0.0)]):
        p = fr.Probe(probe_id=str(n), seq=n, sent_wall=1000.0 + n, sent_mono=float(n), sent_ok=True)
        p.found_lo_s, p.found_hi_s, p.found_wall = lo, hi, 1000.0 + n + hi
        p.indextime = 1000.0 + n - indexed_early_by + 1
        probe.probes.append(p)
    result = probe.result(run_started_at=999.0, run_ended_at=1010.0)
    assert result["skew_evident"] is True
    said = " ".join(result["findings"])
    assert "only possible if the clocks disagree" in said and "Of that, indexing took" not in said
    assert result["latency"]["p50_s"] == 1.5 and result["latency"]["max_s"] == 2.0


def test_a_delay_shorter_than_the_second_indextime_counts_in_is_said_as_such():
    probe = fr.FreshnessProbe(hec_url="h", hec_token="t", client=None, index="main", run_label="r")
    p = fr.Probe(probe_id="a", seq=1, sent_wall=1000.7, sent_mono=0.0, sent_ok=True)
    # Indexed 0.2 s after sending, in second 1000: read as exact, that was
    # "indexed 0.7 s before it was sent".
    p.found_lo_s, p.found_hi_s, p.found_wall, p.indextime = 1.0, 2.0, 1002.2, 1000.0
    probe.probes.append(p)
    result = probe.result(run_started_at=1000.0, run_ended_at=1010.0)
    assert result["index_delay"]["p50_s"] == -0.2 and result["skew_evident"] is False
    said = " ".join(result["findings"])
    assert "indexing took under a second at the median" in said and "-0." not in said
