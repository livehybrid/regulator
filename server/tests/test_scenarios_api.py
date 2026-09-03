"""Scenarios from saved searches, through the API, against the fake splunkd."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "worker"))
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO))

from regulator_server import db  # noqa: E402
from regulator_server.config import ServerConfig, new_master_key, set_settings  # noqa: E402

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk

CONF = """[Errors in the last hour]
search = index=main sourcetype=access_combined status>=500 | stats count by uri_path
dispatch.earliest_time = -1h
dispatch.latest_time = now
cron_schedule = */5 * * * *
enableSched = 1

[Summary writer]
search = index=main | stats count | collect index=summary
dispatch.earliest_time = -24h
dispatch.latest_time = now
cron_schedule = 0 1 * * *
enableSched = 1
"""


@pytest.fixture
def splunkd():
    server = FakeSplunk(port=0, base_latency_ms=10.0, jitter_ms=2.0, dispatch_latency_ms=1.0, seed=7)
    try:
        yield server
    finally:
        server.close()


def make_client(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("REG_POLL_INITIAL_MS", "10")
    settings = dict(
        database_url=f"sqlite:///{tmp_path/'test.db'}",
        master_key=new_master_key(),
        master_key_generated=False,
        admin_password=None,
        session_ttl_s=3600,
        scenarios_dir=str(REPO / "scenarios"),
        user_scenarios_dir=str(tmp_path / "user-scenarios"),
        max_virtual_users=50,
        max_concurrent_runs=2,
        allow_unauthenticated=True,
    )
    settings.update(overrides)
    set_settings(ServerConfig(**settings))
    db.reset_engine()
    from regulator_server.app import create_app

    return TestClient(create_app())


@pytest.fixture
def client(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as test_client:
        yield test_client
    db.reset_engine()
    set_settings(None)


def make_target(client, splunkd, name="fake"):
    response = client.post(
        "/api/targets",
        json={"name": name, "mgmt_url": splunkd.base_url, "token": "tok-en", "verify_tls": False},
    )
    assert response.status_code == 201, response.text
    return response.json()


# -------------------------------------------------------------- previews


def test_a_preview_explains_what_would_be_left_out(client):
    response = client.post("/api/scenarios/preview", json={"savedsearches": CONF})
    assert response.status_code == 200, response.text
    rows = {row["name"]: row for row in response.json()}
    assert rows["Errors in the last hour"]["skipped_reason"] is None
    assert rows["Errors in the last hour"]["firings_per_day"] == 288.0
    assert rows["Summary writer"]["side_effects"] == ["collect"]
    assert "collect" in rows["Summary writer"]["skipped_reason"]


# ------------------------------------------------------- create and run


def test_a_scenario_is_created_from_a_conf_and_runs(client, splunkd):
    created = client.post(
        "/api/scenarios",
        json={
            "name": "imported-demo",
            "savedsearches": CONF,
            "virtual_users": 2,
            "duration_s": 3,
            "think_median_s": 0.1,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["origin"] == "user"
    assert body["saved_searches"] == 1
    assert body["saved_selected"] == ["Errors in the last hour"]
    assert "Summary writer" in body["saved_skipped"]

    listed = {s["name"]: s for s in client.get("/api/scenarios").json()}
    assert "imported-demo" in listed
    assert listed["imported-demo"]["origin"] == "user"
    assert listed["smoke"]["origin"] == "builtin"

    detail = client.get("/api/scenarios/imported-demo").json()
    assert "savedsearches.conf" in detail["files"]
    assert detail["steps"][0]["saved"] == "Errors in the last hour"
    conf = client.get("/api/scenarios/imported-demo/savedsearches.conf")
    assert conf.status_code == 200
    assert "[Errors in the last hour]" in conf.text

    target = make_target(client, splunkd)
    run = client.post(
        "/api/runs", json={"target_id": target["id"], "scenario": "imported-demo", "duration_s": 2}
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    import time

    deadline = time.time() + 40
    while time.time() < deadline:
        current = client.get(f"/api/runs/{run_id}").json()
        if current["state"] in ("completed", "stopped", "aborted", "failed"):
            break
        time.sleep(0.3)
    assert current["state"] == "completed", current.get("error")
    assert current["summary"]["scenario_digest"]
    assert current["scenario_digest"] == current["summary"]["scenario_digest"]
    step_ids = {s["step_id"] for s in current["stats"]["steps"]}
    assert step_ids == {"errors-in-the-last-hour"}
    # The fake dispatched the normalised search.
    assert any(spl.startswith("search index=main sourcetype=access_combined") for spl in splunkd.stats.searches)


def test_a_conf_with_nothing_usable_is_refused_and_leaves_no_directory(client, tmp_path):
    only_writer = CONF.split("[Summary writer]")[1]
    response = client.post(
        "/api/scenarios",
        json={"name": "nothing-here", "savedsearches": "[Summary writer]" + only_writer},
    )
    assert response.status_code == 422
    assert "collect" in response.text
    assert not (tmp_path / "user-scenarios" / "nothing-here").exists()


def test_a_builtin_scenario_cannot_be_deleted_and_a_user_one_can(client):
    assert client.delete("/api/scenarios/smoke").status_code == 403
    client.post("/api/scenarios", json={"name": "temp", "savedsearches": CONF})
    assert client.delete("/api/scenarios/temp").status_code == 204
    assert client.get("/api/scenarios/temp").status_code == 404


def test_a_schedule_scenario_needs_scheduled_searches(client):
    response = client.post(
        "/api/scenarios",
        json={"name": "sched", "savedsearches": CONF, "load_model": "schedule", "duration_s": 60},
    )
    assert response.status_code == 201, response.text
    assert response.json()["load_model"] == "schedule"


# ---------------------------------------------------- from the target


def test_saved_searches_are_listed_and_exported_from_a_target(client, splunkd):
    target = make_target(client, splunkd)
    listed = client.get(f"/api/targets/{target['id']}/savedsearches")
    assert listed.status_code == 200, listed.text
    rows = {row["name"]: row for row in listed.json()}
    assert "Errors in the last hour" in rows
    assert rows["Nightly summary writer"]["side_effects"] == ["collect"]
    assert rows["Disabled report"]["skipped_reason"] == "disabled"

    exported = client.get(f"/api/targets/{target['id']}/savedsearches.conf")
    assert exported.status_code == 200
    assert "[Errors in the last hour]" in exported.text
    assert "cron_schedule = */5 * * * *" in exported.text

    # And straight back in as a scenario, dispatched by name on the target.
    created = client.post(
        "/api/scenarios",
        json={"name": "from-target", "savedsearches": exported.text, "duration_s": 2, "virtual_users": 1},
    )
    assert created.status_code == 201, created.text


def test_dispatch_by_name_runs_the_targets_own_copy(client, splunkd, tmp_path):
    import yaml

    directory = tmp_path / "user-scenarios" / "by-name"
    directory.mkdir(parents=True)
    (directory / "scenario.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "by-name",
                "engine": "api",
                "seed": 5,
                "corpus": {"index": "main"},
                "time_policy": {"mode": "rolling", "window": "1h", "jitter": "5m"},
                "personas": [
                    {
                        "name": "s",
                        "weight": 1,
                        "think_time": {"dist": "fixed", "value_s": 0.1},
                        "steps": [
                            {
                                "id": "errors",
                                "type": "search",
                                "spl": "search index=main | head 1",
                                "saved": "Errors in the last hour",
                                "dispatch": "saved",
                                "app": "search",
                            }
                        ],
                    }
                ],
                "load": {"model": "closed", "virtual_users": 1, "duration": "2s"},
                "abort_if": {"error_rate_pct": 50, "p95_ms": 60000},
            }
        ),
        encoding="utf-8",
    )
    target = make_target(client, splunkd)
    run = client.post("/api/runs", json={"target_id": target["id"], "scenario": "by-name"})
    assert run.status_code == 201, run.text
    import time

    deadline = time.time() + 40
    while time.time() < deadline:
        current = client.get(f"/api/runs/{run.json()['id']}").json()
        if current["state"] in ("completed", "stopped", "aborted", "failed"):
            break
        time.sleep(0.3)
    assert current["state"] == "completed", current.get("error")
    assert splunkd.stats.saved_dispatches > 0
    assert current["stats"]["errors"] == 0


# ------------------------------------------------------------- auth tokens


def test_an_api_token_authenticates_a_bearer_and_the_ui_still_logs_in(tmp_path, monkeypatch):
    with make_client(
        tmp_path, monkeypatch, admin_password="pw", api_tokens=("ci-token-0123456789",), allow_unauthenticated=False
    ) as client:
        assert client.get("/api/targets").status_code == 401
        assert client.get("/api/targets", headers={"Authorization": "Bearer wrong-token-000000"}).status_code == 401
        ok = client.get("/api/targets", headers={"Authorization": "Bearer ci-token-0123456789"})
        assert ok.status_code == 200
        # The password path still works, and issues a cookie.
        login = client.post("/api/auth/login", json={"password": "pw"})
        assert login.status_code == 200
        assert client.get("/api/targets").status_code == 200
        # The API description is behind the same door.
        assert client.get("/api/openapi.json").status_code == 200
    db.reset_engine()
    set_settings(None)


def test_repeated_wrong_passwords_are_throttled(tmp_path, monkeypatch):
    from regulator_server import auth

    auth._FAILURES.clear()
    with make_client(tmp_path, monkeypatch, admin_password="pw", allow_unauthenticated=False) as client:
        for _ in range(auth.LOGIN_MAX_FAILURES):
            assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "pw"}).status_code == 429
    auth._FAILURES.clear()
    db.reset_engine()
    set_settings(None)


def test_no_auth_is_refused_unless_allowed(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError):
        with make_client(tmp_path, monkeypatch, allow_unauthenticated=False):
            pass
    db.reset_engine()
    set_settings(None)
