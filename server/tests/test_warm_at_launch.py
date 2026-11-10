"""Warming a confirmed plan as part of launching a run.

The contract is the Warm cache button's: nothing is warmed that the operator
has not checked, and what is warmed is their plan and nothing else. The run
adds: it is done once, by the control plane, before any load; and it is kept
on the run, as written, so "Run again" shows the same lines to check again.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_api import client, make_target, wait_for_terminal  # noqa: E402,F401 - the shared app fixture
from test_warm_api import audit_rows, cold, scenario  # noqa: E402,F401 - a cold fake and a scenario that reads it

PLAN = [{"index": "main", "earliest": "-2h", "latest": "now"}]


def launch(client, target, scenario, **extra):  # noqa: F811
    body = {
        "target_id": target["id"], "scenario": scenario, "virtual_users": 1, "duration_s": 2,
        "warm_cache": True, "warm_plan": PLAN, "warm_confirmed": True,
    }
    body.update(extra)
    return client.post("/api/runs", json=body)


def test_a_run_does_not_warm_a_plan_nobody_confirmed(client, cold, scenario):  # noqa: F811
    target = make_target(client, cold)
    response = launch(client, target, scenario, warm_confirmed=False)
    assert response.status_code == 422
    assert "confirm" in response.text
    assert cold.stats.oneshots == 0
    # And no run row was left behind to look as though one had been launched.
    assert client.get("/api/runs").json() == []


def test_a_warm_run_needs_a_plan(client, cold, scenario):  # noqa: F811
    target = make_target(client, cold)
    response = launch(client, target, scenario, warm_plan=[])
    assert response.status_code == 422
    assert "plan" in response.text
    assert cold.stats.oneshots == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"evict_cache": True, "evict_cache_indexes": ["main"]},
        {"evict_cache": True, "evict_all_indexes": True},
        {"evict_cache_indexes": ["main"], "evict_every_s": 60, "duration_s": 600},
    ],
)
def test_a_run_warms_or_evicts_never_both(client, cold, scenario, extra):  # noqa: F811
    target = make_target(client, cold)
    response = launch(client, target, scenario, **extra)
    assert response.status_code == 422
    assert "mutually exclusive" in response.text
    assert cold.stats.oneshots == 0 and cold.stats.buckets_localised == 0


@pytest.mark.parametrize(
    "line",
    [
        {"index": "*", "earliest": "-3h"},
        {"index": "main", "earliest": "-1h", "latest": "-2h"},
        {"index": "main | delete", "earliest": "-1h"},
        {"index": "main", "earliest": "whenever"},
    ],
)
def test_a_plan_that_cannot_be_warmed_is_refused_at_launch(client, cold, scenario, line):  # noqa: F811
    target = make_target(client, cold)
    response = launch(client, target, scenario, warm_plan=[line])
    assert response.status_code == 422
    assert cold.stats.oneshots == 0
    assert client.get("/api/runs").json() == []


def test_a_run_waits_its_turn_for_the_targets_cache(client, cold, scenario):  # noqa: F811
    from regulator_server.cachelock import evict_lock

    target = make_target(client, cold)
    lock = evict_lock(target["id"])
    asyncio.run(lock.acquire())
    try:
        response = launch(client, target, scenario)
        assert response.status_code == 409
        assert "already running" in response.text
    finally:
        lock.release()
    assert cold.stats.oneshots == 0


def test_the_confirmed_plan_is_warmed_once_before_the_load(client, cold, scenario):  # noqa: F811
    target = make_target(client, cold)
    response = launch(client, target, scenario, warm_mode="full", label="warm-start")
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]

    finished = wait_for_terminal(client, run_id)
    assert finished["state"] == "completed", finished.get("error")

    warm = finished["summary"]["cache"]["warm"]
    assert warm["mode"] == "full" and warm["failed"] == 0 and warm["searches"] >= 1
    # The plan as resolved, and the forecast of exactly that plan either side.
    assert [line["index"] for line in warm["plan"]] == ["main"]
    assert warm["before"]["local"] == 0
    assert warm["after"]["local"] == warm["after"]["buckets"] > 0
    assert warm["after"]["buckets"] <= 3  # two hours of hourly buckets, nothing outside the plan

    # The run keeps the lines as written, not as resolved: "Run again" shows
    # "-2h" to be checked again, not two hours before the first launch.
    assert finished["warm_cache"] is True
    assert finished["warm_plan"]["plan"] == PLAN
    assert finished["warm_plan"]["mode"] == "full"
    assert finished["warming_since"] is None

    # The run's own cache reading starts after the warm-up, so the warm-up's
    # downloads are not counted as the run's.
    assert finished["summary"]["cache"]["before"]["local_buckets"] >= warm["after"]["local"]

    actions = [row for row in audit_rows(client)]
    started = next(row for row in actions if row["action"] == "run_started")
    assert "confirmed plan" in started["detail"] and "main" in started["detail"]
    warmed = next(row for row in actions if row["action"] == "cache_warmed")
    assert f"run {run_id}" in warmed["detail"]

    samples = client.get(f"/api/targets/{target['id']}/samples").json()["samples"]
    assert any(s["kind"] == "warm" for s in samples)


def test_a_run_without_a_warm_up_is_unchanged(client, cold, scenario):  # noqa: F811
    target = make_target(client, cold)
    response = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": scenario, "virtual_users": 1, "duration_s": 2},
    )
    assert response.status_code == 201, response.text
    finished = wait_for_terminal(client, response.json()["id"])
    assert finished["state"] == "completed", finished.get("error")
    assert finished["warm_cache"] is False and finished["warm_plan"] is None
    assert "warm" not in finished["summary"]["cache"]
    assert not any(row["action"] == "cache_warmed" for row in audit_rows(client))


def test_a_stop_during_the_warm_up_starts_no_more_searches(cold):  # noqa: F811
    """The run's warm-up polls for a stop, and a stopped run fetches nothing more."""
    from regulator_agent.config import load_config
    from regulator_agent.splunk import SplunkClient
    from regulator_server import warmup

    async def go():
        config = load_config({
            "REG_STANDALONE": "1", "REG_SCENARIO": "/scenarios/smoke",
            "REG_TARGET_URL": cold.base_url, "REG_TARGET_TOKEN": "tok-en",
            "REG_TARGET_VERIFY_TLS": "0",
        })
        splunk = SplunkClient(config.target)
        await splunk.start()
        try:
            return await warmup.warm_before_run(
                1, splunk, {"plan": PLAN, "mode": "full"},
                where={}, lock=asyncio.Lock(), should_stop=lambda: True,
            )
        finally:
            await splunk.close()

    result, _ = asyncio.run(go())
    assert result["searches"] == 0 and result["not_started"] >= 1
    assert cold.stats.buckets_localised == 0
    assert warmup.warming_since(1) is None
