"""The target report.

This is the thing you point at a cluster nobody has benchmarked before, so its
job is to be useful with whatever access it is given and honest about the rest.
Every one of these tests is really the same test: a permission the account
lacks, or an endpoint the node does not have, must become a note rather than a
traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import run_async
from regulator_agent.config import load_config
from regulator_agent.report import _recommend, render, target_report
from regulator_agent.splunk import SplunkClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk


@pytest.fixture
def splunkd():
    server = FakeSplunk(port=0, base_latency_ms=5.0, jitter_ms=1.0, dispatch_latency_ms=1.0)
    try:
        yield server
    finally:
        server.close()


def report_for(splunkd, env, **overrides):
    config = load_config(
        env(REG_TARGET_URL=splunkd.base_url, REG_TARGET_VERIFY_TLS="0", **overrides)
    )

    async def go():
        client = SplunkClient(config.target)
        await client.start()
        try:
            return await target_report(client)
        finally:
            await client.close()

    return run_async(go())


def test_it_describes_the_instance(splunkd, env):
    report = report_for(splunkd, env)
    assert report["reachable"] is True
    assert report["instance"]["version"] == "10.4.0"
    assert report["instance"]["cores"] == 8
    assert "indexer" in report["instance"]["roles"]
    assert report["auth_method"] == "token"


def test_it_computes_the_concurrency_ceiling(splunkd, env):
    """base_max_searches + (max_searches_per_cpu x cores), which is 6 + 8."""
    report = report_for(splunkd, env)
    assert report["concurrency"]["max_hist_searches"] == 14
    assert report["concurrency"]["base_max_searches"] == 6


def test_it_proves_the_account_can_actually_dispatch(splunkd, env):
    """Logging in, reading configuration and running a search are three permissions."""
    report = report_for(splunkd, env)
    assert report["can_dispatch"] is True


def test_a_missing_endpoint_becomes_a_note_not_a_traceback(splunkd, env):
    """The fake has no distributed-peers or search-head-cluster endpoint.

    Real Splunk answers 503 for a search head cluster endpoint on a node that
    is not in one, which is a statement about configuration rather than load.
    Either way the report must carry on.
    """
    report = report_for(splunkd, env)
    assert report["search_peers"] == []
    assert report["search_head_cluster"] is None
    assert any("single instance" in note for note in report["notes"])


def test_an_empty_corpus_is_called_out(splunkd, env):
    """A search against an empty index returns in milliseconds and looks fast.

    That is the single most misleading result this tool could produce, so it is
    reported before anyone runs a benchmark rather than after.
    """
    report = report_for(splunkd, env)
    assert any("no non-internal index holds any events" in n for n in report["notes"])
    assert report["recommended_scenario"].startswith("smoke")


def test_an_unreachable_target_reports_rather_than_raises(env):
    config = load_config(
        env(REG_TARGET_URL="http://127.0.0.1:9", REG_TARGET_VERIFY_TLS="0")
    )

    async def go():
        client = SplunkClient(config.target, connect_timeout_s=0.5, read_timeout_s=1.0)
        await client.start()
        try:
            return await target_report(client)
        finally:
            await client.close()

    report = run_async(go())
    assert report["reachable"] is False
    assert report["notes"]


def test_render_is_readable_and_survives_missing_pieces(splunkd, env):
    text = render(report_for(splunkd, env))
    assert "target" in text
    assert "concurrency" in text
    assert "max_hist_searches=14" in text
    assert "recommended" in text


def test_render_handles_an_unreachable_target():
    assert "UNREACHABLE" in render(
        {"reachable": False, "target_url": "http://x", "notes": ["nope"]}
    )


@pytest.mark.parametrize(
    "report, expected",
    [
        ({"can_dispatch": False}, "none"),
        ({"can_dispatch": True, "indexes": [], "search_peers": []}, "smoke"),
        (
            {
                "can_dispatch": True,
                "indexes": [{"name": "main", "events": 10, "internal": False}],
                "search_peers": [],
            },
            "harness check",
        ),
        (
            {
                "can_dispatch": True,
                "indexes": [{"name": "main", "events": 10, "internal": False}],
                "search_peers": [{"name": "idx1"}],
            },
            "search-classes first",
        ),
    ],
)
def test_the_recommendation_matches_what_the_cluster_can_actually_show(report, expected):
    assert expected in _recommend(report)


def test_internal_indexes_are_distinguished_from_a_real_corpus():
    """_internal always has data, so it can make an empty cluster look stocked.

    A benchmark built on it measures Splunk logging about itself, not the
    workload anybody cares about.
    """
    report = {
        "can_dispatch": True,
        "search_peers": [{"name": "idx1"}],
        "indexes": [{"name": "_internal", "events": 5_000_000, "internal": True}],
    }
    assert "smoke" in _recommend(report)
