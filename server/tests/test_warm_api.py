"""The warm-up buttons: suggest a plan, let the operator edit it, warm exactly that."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_api import client, make_target  # noqa: E402,F401 - the shared app fixture

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk

SCENARIO = {
    "name": "warmable",
    "engine": "api",
    "seed": 7,
    "description": "reads an hour of main, and a day of main in a subsearch",
    "corpus": {"index": "main"},
    "time_policy": {"mode": "rolling", "window": "1h", "jitter": "0s", "align": "1m"},
    "personas": [{
        "name": "user", "weight": 100, "think_time": {"dist": "fixed", "value_s": 0},
        "steps": [{
            "id": "one", "type": "search", "engine": "api", "class": "dense",
            "spl": "search index=main | append [search index=main earliest=-3h | stats count]",
        }],
    }],
    "load": {"model": "closed", "virtual_users": 1, "duration": "60s"},
    "abort_if": {"error_rate_pct": 50, "p95_ms": 60000},
}


@pytest.fixture
def cold():
    """Forty cold buckets, an hour each, alternating main and stoker_metrics."""
    server = FakeSplunk(
        port=0, base_latency_ms=2.0, jitter_ms=0.0, dispatch_latency_ms=1.0,
        smartstore_buckets=40, smartstore_local_pct=0, smartstore_bucket_span_s=3600.0,
    )
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def scenario(client, tmp_path):  # noqa: F811 - pytest fixture injection
    from regulator_server.config import get_settings

    directory = Path(get_settings().user_scenarios_dir) / "warmable"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "scenario.yaml").write_text(yaml.safe_dump(SCENARIO), encoding="utf-8")
    return "warmable"


def audit_rows(client):  # noqa: F811
    audit = client.get("/api/audit").json()
    return audit if isinstance(audit, list) else audit.get("events") or audit.get("items") or []


def test_the_suggestion_reads_the_subsearch_and_is_only_a_suggestion(client, cold, scenario):  # noqa: F811
    target = make_target(client, cold)
    body = client.post(f"/api/targets/{target['id']}/warm/suggest", json={"scenario": scenario}).json()
    assert body["scenario"] == "warmable"
    assert [line["index"] for line in body["plan"]] == ["main"]
    line = body["plan"][0]
    # The subsearch's earliest=-3h widens the hour the dispatch window reads.
    assert line["latest"] - line["earliest"] >= 3 * 3600
    assert line["earliest_iso"].endswith("Z")
    assert body["forecast"]["available"] is True
    # Suggesting warms nothing.
    assert cold.stats.buckets_localised == 0


def test_warming_needs_the_operator_to_confirm(client, cold):  # noqa: F811
    target = make_target(client, cold)
    response = client.post(
        f"/api/targets/{target['id']}/warm",
        json={"plan": [{"index": "main", "earliest": "-3h", "latest": "now"}]},
    )
    assert response.status_code == 422
    assert "confirm" in response.text
    assert cold.stats.buckets_localised == 0


@pytest.mark.parametrize(
    "line",
    [
        {"index": "*", "earliest": "-3h"},
        {"index": "main", "earliest": "-1h", "latest": "-2h"},
        {"index": "main | delete", "earliest": "-1h"},
        {"index": "main", "earliest": "whenever"},
    ],
)
def test_a_plan_that_cannot_be_warmed_as_written_is_refused(client, cold, line):  # noqa: F811
    target = make_target(client, cold)
    response = client.post(f"/api/targets/{target['id']}/warm", json={"plan": [line], "confirmed": True})
    assert response.status_code == 422
    assert cold.stats.oneshots == 0


def test_an_edited_plan_is_warmed_exactly_and_audited(client, cold):  # noqa: F811
    target = make_target(client, cold)
    # The operator narrowed the suggestion to two hours and switched to full.
    response = client.post(
        f"/api/targets/{target['id']}/warm",
        json={
            "plan": [{"index": "main", "earliest": "-2h", "latest": "now"}],
            "mode": "full", "confirmed": True, "scenario": "warmable",
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["plan"][0]["index"] == "main"

    status = client.get(f"/api/targets/{target['id']}/warm").json()
    assert status["state"] == "done", status
    result = status["result"]
    assert result["mode"] == "full" and result["failed"] == 0
    after = result["after"]
    assert after["buckets"] == cold.stats.buckets_localised  # nothing outside the plan
    assert after["buckets"] <= 3
    assert after["residency"]["counts"]["full"] == after["buckets"]

    actions = {row["action"]: row for row in audit_rows(client)}
    assert "main" in actions["cache_warm_started"]["detail"]
    assert "confirmed plan" in actions["cache_warm_started"]["detail"]
    assert "warmable" in actions["cache_warm_started"]["detail"]
    assert "cache_warmed" in actions

    samples = client.get(f"/api/targets/{target['id']}/samples").json()["samples"]
    assert any(s["kind"] == "warm" for s in samples)


def test_one_warm_up_at_a_time(client, cold):  # noqa: F811
    from regulator_server.routes import targets as routes

    target = make_target(client, cold)
    routes._warm_jobs[target["id"]] = {"state": "running"}
    try:
        response = client.post(
            f"/api/targets/{target['id']}/warm",
            json={"plan": [{"index": "main", "earliest": "-1h"}], "confirmed": True},
        )
        assert response.status_code == 409
    finally:
        routes._warm_jobs.pop(target["id"], None)


def test_status_is_idle_before_any_warm_up(client, cold):  # noqa: F811
    target = make_target(client, cold)
    assert client.get(f"/api/targets/{target['id']}/warm").json() == {"state": "idle"}


# ------------------------------------------------------------------ bucket map


def test_the_bucket_map_lays_buckets_out_by_index_and_time(client, cold):  # noqa: F811
    target = make_target(client, cold)
    body = client.get(f"/api/targets/{target['id']}/buckets", params={"bins": 20}).json()
    assert body["available"] is True and body["detail"] == "cache"
    assert body["states"] == ["remote"]
    assert sorted(row["index"] for row in body["indexes"]) == ["main", "stoker_metrics"]
    main = next(row for row in body["indexes"] if row["index"] == "main")
    assert main["buckets"] == 20 and main["cells"]
    # Reading the map warms nothing.
    assert cold.stats.buckets_localised == 0


def test_the_bucket_map_reads_on_disk_detail_for_named_indexes(client, cold):  # noqa: F811
    target = make_target(client, cold)
    base = f"/api/targets/{target['id']}"
    # A tstats warm-up of two hours leaves index files only; the map must say so.
    client.post(f"{base}/warm", json={
        "plan": [{"index": "main", "earliest": "-2h", "latest": "now"}], "confirmed": True,
    })
    plain = client.get(f"{base}/buckets", params={"indexes": "main"}).json()
    assert plain["states"] == ["remote", "local"]
    detailed = client.get(f"{base}/buckets", params={"indexes": "main", "detail": "true"}).json()
    assert detailed["detail"] == "disk"
    assert detailed["states"] == ["remote", "index_only"]
    by_state = detailed["indexes"][0]["by_state"]
    assert by_state["index_only"]["buckets"] == plain["indexes"][0]["by_state"]["local"]["buckets"]


def test_on_disk_detail_needs_named_indexes(client, cold):  # noqa: F811
    target = make_target(client, cold)
    response = client.get(f"/api/targets/{target['id']}/buckets", params={"detail": "true"})
    assert response.status_code == 422
    assert cold.stats.oneshots == 0


def test_the_bucket_map_overlays_what_a_scenario_reads(client, cold, scenario):  # noqa: F811
    target = make_target(client, cold)
    body = client.get(
        f"/api/targets/{target['id']}/buckets", params={"scenario": scenario, "earliest": "-12h"}
    ).json()
    spans = body["overlay"]["spans"]
    assert [s["index"] for s in spans] == ["main"]
    # The subsearch's earliest=-3h, not just the 1h dispatch window.
    assert spans[0]["latest"] - spans[0]["earliest"] >= 3 * 3600
    assert body["overlay"]["scenario"] == "warmable"


def test_a_bad_range_is_refused(client, cold):  # noqa: F811
    target = make_target(client, cold)
    base = f"/api/targets/{target['id']}/buckets"
    assert client.get(base, params={"earliest": "-1h", "latest": "-2h"}).status_code == 422
    assert client.get(base, params={"earliest": "whenever"}).status_code == 422


# ------------------------------------------------------------------ at launch


from test_api import wait_for_terminal  # noqa: E402


def _launch(client, target, scenario, **extra):  # noqa: F811
    body = {"target_id": target["id"], "scenario": scenario, "virtual_users": 1, "duration_s": 2}
    body.update(extra)
    return client.post("/api/runs", json=body)


def test_a_run_launched_with_a_confirmed_plan_starts_warm_and_records_it(client, cold, scenario):  # noqa: F811
    target = make_target(client, cold)
    created = _launch(
        client, target, scenario,
        warm_plan=[{"index": "main", "earliest": "-3h", "latest": "now"}],
        warm_mode="full", warm_confirmed=True,
    )
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["warm"]["plan"] == [{"index": "main", "earliest": "-3h", "latest": "now"}]
    assert run["warm"]["mode"] == "full"

    finished = wait_for_terminal(client, run["id"])
    assert finished["state"] == "completed", finished.get("error")
    cache = finished["summary"]["cache"]
    warm = cache["warm"]
    assert warm["before"]["local"] == 0
    assert warm["after"]["local"] == warm["after"]["buckets"] > 0
    assert warm["failed"] == 0
    # The forecast is taken after the warm-up, so the run is forecast warm.
    assert cache["forecast"]["available"] is True
    assert cache["forecast"]["local_pct"] == 100.0
    assert cache["forecast"]["residency"]["counts"]["full"] == cache["forecast"]["buckets"]

    started = next(row for row in audit_rows(client) if row["action"] == "run_started")
    assert "cache warm-up of a confirmed plan" in started["detail"] and "main -3h..now" in started["detail"]
    samples = client.get(f"/api/targets/{target['id']}/samples").json()["samples"]
    assert [s["kind"] for s in samples if s["run_id"] == run["id"]][:2] == ["warm", "before"]


def test_every_run_carries_a_forecast_even_without_a_warm_up(client, cold, scenario):  # noqa: F811
    target = make_target(client, cold)
    run_id = _launch(client, target, scenario).json()["id"]
    cache = wait_for_terminal(client, run_id)["summary"]["cache"]
    assert cache["forecast"]["available"] is True
    assert cache["forecast"]["local"] == 0
    assert "warm" not in cache


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"warm_plan": [{"index": "main", "earliest": "-3h"}]}, "confirm the warm-up plan"),
        ({"warm_plan": [{"index": "main", "earliest": "-3h"}], "warm_confirmed": True,
          "evict_cache": True, "evict_cache_indexes": ["main"]}, "not both"),
        ({"warm_plan": [{"index": "*", "earliest": "-3h"}], "warm_confirmed": True}, "bare wildcard"),
        ({"warm_plan": [{"index": "main", "earliest": "-1h", "latest": "-2h"}], "warm_confirmed": True}, "not after"),
    ],
)
def test_a_launch_with_a_bad_or_unconfirmed_plan_is_refused(client, cold, scenario, extra, message):  # noqa: F811
    target = make_target(client, cold)
    response = _launch(client, target, scenario, **extra)
    assert response.status_code == 422
    assert message in response.text
    assert cold.stats.oneshots == 0 and cold.stats.jobs_created == 0


def test_a_warm_up_that_fails_is_recorded_and_does_not_raise():
    import asyncio

    from regulator_server import warmup

    class Broken:
        async def oneshot(self, *args, **kwargs):
            raise RuntimeError("search head unavailable")

        async def entries(self, *args, **kwargs):
            raise RuntimeError("search head unavailable")

    spec = {"plan": [{"index": "main", "earliest": "-1h", "latest": "now"}], "mode": "tsidx"}
    outcome = asyncio.run(warmup.warm_before_run(Broken(), spec, where={}, target_id=1))
    result = outcome["result"]
    assert outcome["cache_after"] is None or isinstance(outcome["cache_after"], dict)
    assert result["plan"][0]["index"] == "main"
    assert result.get("error") or result.get("failed")


def test_a_stop_before_the_warm_up_starts_no_searches():
    import asyncio

    from regulator_agent import cachewarm
    from regulator_agent.timepolicy import TimeWindow
    from regulator_server.warmup import StopFlag

    class Owner:
        stop_requested = True

    class Client:
        calls = 0

        async def oneshot(self, *args, **kwargs):
            Client.calls += 1
            return [], 0

    planned = [("| makeresults", TimeWindow(0.0, 1.0))] * 3
    result = asyncio.run(cachewarm.warm(Client(), planned, mode="tsidx", abort=StopFlag(Owner())))
    assert Client.calls == 0 and result.not_started == 3
