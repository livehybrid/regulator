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
