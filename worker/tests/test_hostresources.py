"""CPU, memory and disk on the target's hosts across a run, by tier."""

from __future__ import annotations

import asyncio

import pytest

from regulator_agent import hostresources as hr
from regulator_agent.splunk import SplunkError

START, END = 10_020.0, 10_620.0


class StubClient:
    """Answers the three searches and the three REST reads from canned data."""

    def __init__(self, hostwide=(), io=(), mounts=(), peers=(), members=(), info=None, fail=None):
        self.hostwide, self.io, self.mounts = list(hostwide), list(io), list(mounts)
        self.peers, self.members = list(peers), list(members)
        self.info = info if info is not None else {"serverName": "sh1"}
        self.fail = fail or {}
        self.asked = []

    async def oneshot(self, spl, window, count=100):
        self.asked.append((spl, window, count))
        for needle, exc in self.fail.items():
            if needle in spl:
                raise exc
        if "component=Hostwide" in spl:
            return self.hostwide, 0
        if "bin _time" in spl:
            return self.io, 0
        return self.mounts, 0

    async def entries(self, path, context, params=None):
        if path.endswith("/distributed/peers"):
            return self.peers
        return self.members

    async def server_info(self):
        return self.info


def run(coro):
    return asyncio.run(coro)


def cpu_rows(host, value, times, **extra):
    return [
        {"host": host, "t": str(t), "cpu_pct": str(value), "cpu_peak_pct": str(value + 3), "mem_pct": "40",
         "mem_peak_pct": "42", "load": "0.5", "load_peak": "0.6", "mem_mb": "65536", "cores": "8", "vcpus": "16", **extra}
        for t in times
    ]


DURING = range(10_020, 10_620, 30)
BEFORE = range(9_840, 10_020, 30)
# One bucket across the start of the run: partly the idle machine.
STRADDLE = [10_005]
PEERS = [
    {"name": "idx1.corp:8089", "peerName": "idx1", "server_roles": ["indexer", "cluster_peer", "search_peer"]},
    {"name": "idx2.corp:8089", "peerName": "idx2", "server_roles": ["indexer", "cluster_peer"]},
]


# ------------------------------------------------------------ the small parts


def test_the_bucket_width_keeps_the_point_count_about_the_same():
    assert hr.span_for(600) == 30
    assert hr.span_for(3600) == 60
    assert hr.span_for(8 * 3600) == 600
    assert hr.span_for(7 * 86400) == 3600


def test_the_searches_follow_the_quoting_that_works_on_a_live_instance():
    spl = hr.hostwide_spl(30)
    # eval names a dotted field in single quotes; stats takes it bare.
    assert "'data.cpu_system_pct' + 'data.cpu_user_pct'" in spl
    assert "avg(data.normalized_load_avg_1min)" in spl and "span=30s" in spl
    assert "by _time, host, data.mount_point" in hr.io_series_spl(60)
    assert "bin _time" not in hr.io_mounts_spl()


@pytest.mark.parametrize(
    "roles, expected",
    [
        (["indexer", "search_peer", "cluster_peer"], hr.ROLE_INDEXER),
        (["indexer", "cluster_slave", "search_head"], hr.ROLE_INDEXER),
        (["search_head", "kv_store", "shc_member"], hr.ROLE_SEARCH_HEAD),
        (["indexer", "search_head"], hr.ROLE_INDEXER),
        ([], hr.ROLE_INDEXER),
        (["cluster_master", "license_master"], hr.ROLE_OTHER),
    ],
)
def test_a_search_peers_tier_is_read_from_its_roles(roles, expected):
    assert hr.peer_role(roles) == expected


def test_a_host_is_matched_by_its_long_or_short_name_and_an_address_only_whole():
    assert hr._names("IDX1.example.com:8089") == ["idx1.example.com", "idx1"]
    assert hr._names("https://sh2.corp:8089") == ["sh2.corp", "sh2"]
    assert hr._names("10.0.0.5:8089") == ["10.0.0.5"]
    roles = {"idx1": hr.ROLE_INDEXER, "10.0.0.5": hr.ROLE_INDEXER}
    assert hr.role_of("idx1.other.domain", roles) == hr.ROLE_INDEXER
    assert hr.role_of("10.0.0.50", roles) == hr.ROLE_UNKNOWN


def test_the_instance_the_run_talked_to_is_a_search_head_whatever_else_it_is():
    client = StubClient(
        peers=PEERS + [{"name": "solo:8089", "peerName": "solo", "server_roles": ["indexer"]}],
        members=[{"label": "sh2", "mgmt_uri": "https://sh2.corp:8089"}],
        info={"serverName": "solo", "host_fqdn": "solo.corp"},
    )
    roles, notes, indexers = run(hr.host_roles(client))
    assert roles["solo"] == hr.ROLE_SEARCH_HEAD and roles["solo.corp"] == hr.ROLE_SEARCH_HEAD
    assert roles["sh2"] == hr.ROLE_SEARCH_HEAD
    assert roles["idx1"] == hr.ROLE_INDEXER and roles["idx2.corp"] == hr.ROLE_INDEXER
    assert indexers == 3 and notes == []


# --------------------------------------------------------------- the whole way


def test_hosts_are_sorted_into_tiers_and_summarised_over_the_run_only():
    client = StubClient(
        hostwide=(
            cpu_rows("sh1", 35, DURING) + cpu_rows("sh1", 10, BEFORE)
            + cpu_rows("idx1.corp", 96, DURING) + cpu_rows("idx1.corp", 12, BEFORE)
            + cpu_rows("idx1.corp", 50, STRADDLE)
            + cpu_rows("idx2.corp", 40, DURING)
        ),
        io=[{"host": "idx1.corp", "t": str(t), "read_kb_ps": "204800", "write_kb_ps": "1024",
             "reads_ps": "900", "writes_ps": "100", "wait_ms": "80"} for t in DURING],
        mounts=[{"host": "idx1.corp", "mount": "/opt/splunk/var", "device": "sdb", "fs_type": "xfs",
                 "avg_wait_ms": "80", "max_wait_ms": "140", "read_kb_ps": "204800", "write_kb_ps": "1024",
                 "reads_ps": "900", "writes_ps": "100"}],
        peers=PEERS,
    )
    result = run(hr.host_resources(client, START, END))
    assert result["available"] is True and result["span_s"] == 30
    assert [(h["host"], h["role"]) for h in result["hosts"]] == [
        ("sh1", "search head"), ("idx1.corp", "indexer"), ("idx2.corp", "indexer"),
    ]
    idx1 = result["hosts"][1]
    # The run's figures, with what the host was doing before it kept apart
    # and the bucket that straddles the start counted as neither.
    assert idx1["cpu"]["avg_pct"] == 96.0 and idx1["cpu"]["before_pct"] == 12.0
    assert idx1["cpu"]["peak_pct"] == 99.0 and idx1["cpu"]["peak_at"] in DURING
    assert idx1["cpu"]["hot_s"] == len(DURING) * 30
    assert idx1["cores"] == 16 and idx1["mem_mb"] == 65536
    assert idx1["io"]["read_mb_s"] == 200.0 and idx1["io"]["wait_ms"] == 80.0
    assert "io" not in result["hosts"][0]

    # A tier is its mean and its busiest host: one hot indexer is not averaged away.
    tier = result["roles"]["indexer"]
    assert tier["hosts"] == 2
    during = tier["t"].index(10_020.0)
    assert tier["cpu_avg"][during] == 68.0 and tier["cpu_max"][during] == 96.0
    assert tier["read_mb_s"][during] == 200.0 and tier["io_wait_max"][during] == 80.0
    assert "read_mb_s" not in result["roles"]["search head"]

    assert result["mounts"][0] == {
        "host": "idx1.corp", "role": "indexer", "mount": "/opt/splunk/var", "device": "sdb", "fs_type": "xfs",
        "avg_wait_ms": 80.0, "max_wait_ms": 140.0, "read_mb_s": 200.0, "write_mb_s": 1.0, "iops": 1000.0,
    }

    said = " ".join(result["findings"])
    assert "1 search head: CPU averaged 35% during the run (10% before it)" in said
    assert "2 indexers: CPU averaged 68% during the run" in said and "peaking at 99% on idx1.corp" in said
    assert "idx1.corp (indexer) was at or above 90% CPU for 10m of the 10m run" in said
    assert "idx1.corp (indexer) averaged 80 ms per disk request" in said
    assert "idx2.corp (indexer) was at or above" not in said

    # Three searches, over the run with a lead and a tail, and nothing else.
    assert len(client.asked) == 3 and result["self_load"]["queries"] == 3
    window = client.asked[0][1]
    assert window.earliest == START - hr.LEAD_S and window.latest == END + hr.TAIL_S


def test_memory_and_load_are_called_out_only_past_the_line():
    rows = cpu_rows("sh1", 50, DURING, mem_peak_pct="97", swap_used_mb="512", load="1.4", load_peak="2.1")
    result = run(hr.host_resources(StubClient(hostwide=rows), START, END))
    said = " ".join(result["findings"])
    assert "sh1 (search head) reached 97% of memory used, with 512 MB of swap in use" in said
    assert "more runnable work than cores for 10m (load per core peaked at 2.1)" in said
    assert "no disk figures" in " ".join(result["notes"])


def test_missing_indexers_and_hosts_of_no_known_tier_are_said_not_guessed():
    client = StubClient(hostwide=cpu_rows("sh1", 20, DURING) + cpu_rows("mystery", 20, DURING), peers=PEERS)
    result = run(hr.host_resources(client, START, END))
    said = " ".join(result["findings"])
    assert "None of this search head's 2 search peers reported resource usage" in said
    assert "tier is not known: mystery" in said
    assert [h["role"] for h in result["hosts"]] == ["search head", "unknown"]


def test_an_unreadable_introspection_is_a_reason_and_never_an_exception():
    refused = StubClient(fail={"component=Hostwide": SplunkError("HTTP 403: no access to _introspection")})
    result = run(hr.host_resources(refused, START, END))
    assert result["available"] is False and "403" in result["reason"]
    assert len(refused.asked) == 1  # nothing more is asked once the first answer is no

    empty = run(hr.host_resources(StubClient(), START, END))
    assert empty["available"] is False and "no splunk_resource_usage events" in empty["reason"]

    class Broken(StubClient):
        async def entries(self, path, context, params=None):
            raise RuntimeError("boom")

        async def server_info(self):
            raise RuntimeError("boom")

    partial = run(hr.host_resources(Broken(hostwide=cpu_rows("sh1", 20, DURING)), START, END))
    assert partial["available"] is True
    assert partial["hosts"][0]["role"] == "unknown"
    assert any("could not be" in note for note in partial["notes"])


def test_disk_figures_missing_do_not_cost_the_cpu_figures():
    client = StubClient(hostwide=cpu_rows("sh1", 20, DURING), fail={"component=IOStats": SplunkError("HTTP 500")})
    result = run(hr.host_resources(client, START, END))
    assert result["available"] is True and result["mounts"] == []
    assert [q["available"] for q in result["queries"]] == [True, False, False]
