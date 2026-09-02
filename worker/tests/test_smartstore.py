"""SmartStore cache state, provenance and eviction.

The property under test throughout is the one that decides whether a benchmark
means anything on a SmartStore cluster: **you must know whether the searches
read local disk or paid for an object-storage fetch**. A fast run and a slow
run of the same scenario are not comparable without it, and after the fact the
question cannot be answered at all.

Eviction is tested only against the fake. Pointing it at a live cluster throws
away a warm cache and makes somebody's afternoon slow, so that stays a
deliberate, human decision.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

import pytest

from conftest import run_async
from regulator_agent.config import load_config
from regulator_agent.smartstore import (
    CacheState,
    cache_size_gb,
    cache_state,
    delta,
    evict_all,
    render,
)
from regulator_agent.splunk import SplunkClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk


def server(**overrides):
    defaults = dict(port=0, base_latency_ms=5.0, jitter_ms=1.0, dispatch_latency_ms=1.0)
    defaults.update(overrides)
    return FakeSplunk(**defaults)


def with_client(splunkd, env, coro_factory):
    config = load_config(
        env(REG_TARGET_URL=splunkd.base_url, REG_TARGET_VERIFY_TLS="0")
    )

    async def go():
        client = SplunkClient(config.target)
        await client.start()
        try:
            return await coro_factory(client)
        finally:
            await client.close()

    return run_async(go())


# ------------------------------------------------------------------- units


def test_the_configured_ceiling_is_converted_from_mebibytes():
    """max_cache_size is MiB and bucket sizes are decimal GB.

    Mixing them made the summary read "78.8 GB of 75 GB, 98% full", which looks
    like arithmetic nobody should trust.
    """
    assert cache_size_gb(76800) == pytest.approx(80.53, abs=0.01)
    assert cache_size_gb(None) == 0.0


# --------------------------------------------------------------- reading


def test_a_non_smartstore_instance_says_so_rather_than_reporting_zeroes(env):
    """Absence is an answer, and it must not look like an empty cache."""
    splunkd = server(smartstore_buckets=0)
    try:
        state = with_client(splunkd, env, cache_state)
    finally:
        splunkd.close()

    assert state.available is False
    assert "not using SmartStore" in state.reason
    assert state.local_buckets == 0


def test_it_counts_local_and_remote_buckets(env):
    splunkd = server(smartstore_buckets=100, smartstore_local_pct=40)
    try:
        state = with_client(splunkd, env, cache_state)
    finally:
        splunkd.close()

    assert state.available is True
    assert state.total_buckets == 100
    assert state.local_buckets == 40
    assert state.remote_buckets == 60
    assert state.local_pct == pytest.approx(40.0)


def test_it_computes_how_full_the_cache_is(env):
    """The number that decides whether a wide search evicts somebody else's data."""
    splunkd = server(
        smartstore_buckets=100,
        smartstore_local_pct=50,
        smartstore_bucket_bytes=1024 * 1024 * 1024,
        smartstore_max_cache_size_mb=100 * 1024,
    )
    try:
        state = with_client(splunkd, env, cache_state)
    finally:
        splunkd.close()

    # 50 buckets of 1 GiB against a 100 GiB ceiling.
    assert state.fill_pct == pytest.approx(50.0, abs=0.5)
    assert state.eviction_policy == "lru"
    assert state.max_cache_size_mb == 100 * 1024


def test_it_breaks_the_cache_down_per_index(env):
    splunkd = server(smartstore_buckets=60, smartstore_local_pct=50)
    try:
        state = with_client(splunkd, env, cache_state)
    finally:
        splunkd.close()

    assert state.per_index
    assert sum(i.local_buckets for i in state.per_index.values()) == state.local_buckets
    for index in state.per_index.values():
        assert index.local_buckets + index.remote_buckets > 0


def test_render_survives_an_unavailable_cache():
    assert "not available" in render(CacheState(reason="not SmartStore"))


# ------------------------------------------------------------ provenance


def test_a_run_that_downloads_nothing_is_warm():
    before = CacheState(available=True, local_buckets=100, remote_buckets=50, local_bytes=1000)
    after = CacheState(available=True, local_buckets=100, remote_buckets=50, local_bytes=1000)
    change = delta(before, after)
    assert change.provenance == "warm"
    assert change.buckets_downloaded == 0


def test_a_run_starting_from_an_empty_cache_is_cold():
    before = CacheState(available=True, local_buckets=0, remote_buckets=150, local_bytes=0)
    after = CacheState(available=True, local_buckets=30, remote_buckets=120, local_bytes=3000)
    change = delta(before, after)
    assert change.provenance == "cold"
    assert change.buckets_downloaded == 30
    assert change.bytes_downloaded == 3000


def test_a_run_that_downloads_some_of_what_it_needed_is_mixed():
    before = CacheState(available=True, local_buckets=100, remote_buckets=50, local_bytes=1000)
    after = CacheState(available=True, local_buckets=120, remote_buckets=30, local_bytes=1500)
    change = delta(before, after)
    assert change.provenance == "mixed"
    assert change.buckets_downloaded == 20


def test_provenance_is_unknown_when_the_cache_could_not_be_read():
    change = delta(CacheState(), CacheState())
    assert change.provenance == "unknown"
    assert change.available is False


def test_eviction_during_a_run_is_recorded_separately_from_downloads():
    """A cache at its ceiling evicts while the run is in flight.

    That is churn, and it is a different phenomenon from a cold start, so it
    gets its own counter rather than showing up as a negative download.
    """
    before = CacheState(available=True, local_buckets=100, remote_buckets=50)
    after = CacheState(available=True, local_buckets=80, remote_buckets=70)
    change = delta(before, after)
    assert change.buckets_evicted_during == 20
    assert change.buckets_downloaded == 0


# -------------------------------------------------------------- eviction


def test_eviction_empties_the_local_cache(env):
    splunkd = server(smartstore_buckets=40, smartstore_local_pct=100)
    try:
        result = with_client(splunkd, env, lambda c: evict_all(c))
        after = with_client(splunkd, env, cache_state)
    finally:
        splunkd.close()

    assert result.attempted == 40
    assert result.evicted == 40
    assert result.failed == 0
    assert result.bytes_evicted > 0
    assert after.local_buckets == 0
    assert after.remote_buckets == 40


def test_eviction_can_be_restricted_to_named_indexes(env):
    """On a shared cluster the rest of the cache belongs to other people.

    Flushing everything to measure your own cold path makes their afternoon
    slow for no benefit to your result.
    """
    splunkd = server(smartstore_buckets=40, smartstore_local_pct=100)
    try:
        before = with_client(splunkd, env, cache_state)
        target_index = sorted(before.per_index)[0]
        result = with_client(splunkd, env, lambda c: evict_all(c, indexes=[target_index]))
        after = with_client(splunkd, env, cache_state)
    finally:
        splunkd.close()

    assert 0 < result.evicted < 40
    assert after.per_index[target_index].local_buckets == 0
    assert after.local_buckets == before.local_buckets - result.evicted


def test_evicting_an_already_remote_cache_is_a_no_op(env):
    splunkd = server(smartstore_buckets=20, smartstore_local_pct=0)
    try:
        result = with_client(splunkd, env, lambda c: evict_all(c))
    finally:
        splunkd.close()

    assert result.attempted == 0
    assert result.evicted == 0


def test_eviction_on_a_non_smartstore_instance_does_nothing(env):
    splunkd = server(smartstore_buckets=0)
    try:
        result = with_client(splunkd, env, lambda c: evict_all(c))
    finally:
        splunkd.close()

    assert result.attempted == 0
    assert result.errors == []


def test_the_evict_command_refuses_to_flush_the_estate_by_default(monkeypatch, capsys, env):
    """There is no undo beyond waiting for everything to re-download.

    On a shared cluster most of the cache belongs to other people's
    dashboards, so flushing all of it has to be an explicit act.
    """
    from regulator_agent.__main__ import main

    splunkd = server(smartstore_buckets=20, smartstore_local_pct=100)
    try:
        for key, value in {
            "REG_STANDALONE": "1",
            "REG_TARGET_URL": splunkd.base_url,
            "REG_TARGET_TOKEN": "t",
            "REG_TARGET_VERIFY_TLS": "0",
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("REG_SCENARIO", raising=False)
        monkeypatch.delenv("REG_EVICT_CACHE_INDEXES", raising=False)

        assert main(["--evict-cache"]) == 1
        # Nothing was touched: the refusal happens before any request.
        assert with_client(splunkd, env, cache_state).local_buckets == 20
    finally:
        splunkd.close()


def test_the_evict_command_drops_one_named_index(monkeypatch, capsys):
    from regulator_agent.__main__ import main

    splunkd = server(smartstore_buckets=20, smartstore_local_pct=100)
    try:
        for key, value in {
            "REG_STANDALONE": "1",
            "REG_TARGET_URL": splunkd.base_url,
            "REG_TARGET_TOKEN": "t",
            "REG_TARGET_VERIFY_TLS": "0",
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("REG_SCENARIO", raising=False)

        index = sorted({b.split("|")[1].split("~")[0] for b in splunkd._buckets})[0]
        assert main(["--evict-cache", "--index", index]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["indexes"] == [index]
        assert payload["eviction"]["evicted"] > 0
        assert payload["after"]["local_buckets"] < payload["before"]["local_buckets"]
        assert payload["after"]["per_index"][index]["local_buckets"] == 0
    finally:
        splunkd.close()


def test_the_evict_endpoint_refuses_a_get(env):
    """Matches real splunkd, which answers "custom actions require POST".

    That behaviour is how the endpoint was confirmed to exist in the first
    place, without evicting anything on a live cluster.
    """
    import urllib.error
    import urllib.request

    splunkd = server(smartstore_buckets=4, smartstore_local_pct=100)
    try:
        bid = sorted(splunkd._buckets)[0]
        url = (
            f"{splunkd.base_url}/services/admin/cacheman/"
            f"{urllib.parse.quote(bid, safe='')}/evict?output_mode=json"
        )
        request = urllib.request.Request(url)
        request.add_header("Authorization", "Bearer x")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 400
        assert "require POST" in excinfo.value.read().decode()
    finally:
        splunkd.close()

