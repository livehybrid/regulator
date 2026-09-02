"""The control-plane API, end to end against the fake splunkd.

These drive the real FastAPI app, the real database, the real worker code and a
real socket. The only thing faked is Splunk itself, which is the point: if the
UI's buttons work here they work against a cluster, because nothing in between
is stubbed.
"""

from __future__ import annotations

import sys
import time
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


@pytest.fixture
def splunkd():
    server = FakeSplunk(
        port=0,
        base_latency_ms=10.0,
        jitter_ms=2.0,
        dispatch_latency_ms=1.0,
        smartstore_buckets=40,
        smartstore_local_pct=75,
        seed=7,
    )
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A fresh app with its own database and no password."""
    monkeypatch.setenv("REG_POLL_INITIAL_MS", "10")
    set_settings(
        ServerConfig(
            database_url=f"sqlite:///{tmp_path/'test.db'}",
            master_key=new_master_key(),
            master_key_generated=False,
            admin_password=None,
            session_ttl_s=3600,
            port=8080,
            scenarios_dir=str(REPO / "scenarios"),
            max_virtual_users=50,
            max_concurrent_runs=2,
        )
    )
    db.reset_engine()
    from regulator_server.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    db.reset_engine()
    set_settings(None)


def make_target(client, splunkd, name="fake"):
    response = client.post(
        "/api/targets",
        json={
            "name": name,
            "mgmt_url": splunkd.base_url,
            "token": "a-secret-bearer-token",
            "verify_tls": False,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ health


def test_healthz_reports_the_build(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["version"]


def test_the_ui_is_served_at_the_root(client):
    response = client.get("/")
    # The page may not exist in a partial checkout, but the route must not 500.
    assert response.status_code in (200, 404)


# ----------------------------------------------------------------- targets


def test_a_target_round_trips_without_ever_returning_its_credential(client, splunkd):
    created = make_target(client, splunkd)
    assert created["name"] == "fake"
    assert created["health"] == "unknown"

    listed = client.get("/api/targets").json()
    assert len(listed) == 1

    # The security property: no response body anywhere contains the token.
    for body in (created, listed[0], client.get("/api/targets").text):
        assert "a-secret-bearer-token" not in str(body)


def test_a_target_needs_a_credential(client, splunkd):
    response = client.post(
        "/api/targets",
        json={"name": "no-creds", "mgmt_url": splunkd.base_url, "verify_tls": False},
    )
    assert response.status_code == 422
    assert "token" in response.text


def test_duplicate_names_are_rejected(client, splunkd):
    make_target(client, splunkd, name="dup")
    response = client.post(
        "/api/targets",
        json={
            "name": "dup",
            "mgmt_url": splunkd.base_url,
            "token": "t",
            "verify_tls": False,
        },
    )
    assert response.status_code == 409


def test_a_non_http_url_is_rejected(client):
    response = client.post(
        "/api/targets",
        json={"name": "bad", "mgmt_url": "splunk.example:8089", "token": "t"},
    )
    assert response.status_code == 422


def test_testing_a_target_records_its_health(client, splunkd):
    target = make_target(client, splunkd)
    body = client.post(f"/api/targets/{target['id']}/test").json()
    assert body["ok"] is True
    assert body["version"] == "10.4.0"
    # 6 + (1 * 8): the ceiling the run detail draws its line at.
    assert body["max_hist_searches"] == 14

    assert client.get("/api/targets").json()[0]["health"] == "ok"


def test_testing_an_unreachable_target_is_a_result_not_a_crash(client):
    client.post(
        "/api/targets",
        json={
            "name": "gone",
            "mgmt_url": "http://127.0.0.1:9",
            "token": "t",
            "verify_tls": False,
        },
    )
    body = client.post("/api/targets/1/test").json()
    assert body["ok"] is False
    assert body["detail"]
    assert client.get("/api/targets").json()[0]["health"] == "error"


def test_deleting_a_target(client, splunkd):
    target = make_target(client, splunkd)
    assert client.delete(f"/api/targets/{target['id']}").status_code == 204
    assert client.get("/api/targets").json() == []


def test_a_missing_target_is_a_404(client):
    assert client.post("/api/targets/999/test").status_code == 404


# ------------------------------------------------------------- the buttons


def test_the_report_button_returns_a_full_picture(client, splunkd):
    target = make_target(client, splunkd)
    report = client.post(f"/api/targets/{target['id']}/report").json()

    assert report["reachable"] is True
    assert report["instance"]["version"] == "10.4.0"
    assert report["concurrency"]["max_hist_searches"] == 14
    assert report["can_dispatch"] is True
    assert "recommended_scenario" in report
    assert isinstance(report["notes"], list)
    assert report["smartstore_cache"]["available"] is True


def test_the_cache_button_shows_local_versus_remote(client, splunkd):
    target = make_target(client, splunkd)
    cache = client.get(f"/api/targets/{target['id']}/cache").json()
    assert cache["available"] is True
    assert cache["total_buckets"] == 40
    assert cache["local_buckets"] == 30  # 75% of 40
    assert cache["per_index"]


def test_the_evict_button_refuses_without_an_explicit_scope(client, splunkd):
    """No undo beyond re-download, so a bare request must not flush everything."""
    target = make_target(client, splunkd)
    response = client.post(
        f"/api/targets/{target['id']}/evict", json={"indexes": [], "all_indexes": False}
    )
    assert response.status_code == 422
    assert "at least one index" in response.text

    # Nothing was touched.
    assert client.get(f"/api/targets/{target['id']}/cache").json()["local_buckets"] == 30


def test_the_evict_button_drops_a_named_index(client, splunkd):
    target = make_target(client, splunkd)
    cache = client.get(f"/api/targets/{target['id']}/cache").json()
    index = sorted(cache["per_index"])[0]

    body = client.post(
        f"/api/targets/{target['id']}/evict", json={"indexes": [index], "all_indexes": False}
    ).json()

    assert body["eviction"]["evicted"] > 0
    assert body["after"]["local_buckets"] < body["before"]["local_buckets"]
    assert body["after"]["per_index"][index]["local_buckets"] == 0


def test_the_evict_button_can_flush_everything_when_told_to(client, splunkd):
    target = make_target(client, splunkd)
    body = client.post(
        f"/api/targets/{target['id']}/evict", json={"indexes": [], "all_indexes": True}
    ).json()
    assert body["after"]["local_buckets"] == 0


def test_evicting_a_non_smartstore_target_is_a_clear_409(client, tmp_path):
    plain = FakeSplunk(port=0, base_latency_ms=5.0, smartstore_buckets=0)
    try:
        client.post(
            "/api/targets",
            json={
                "name": "plain",
                "mgmt_url": plain.base_url,
                "token": "t",
                "verify_tls": False,
            },
        )
        response = client.post("/api/targets/1/evict", json={"all_indexes": True})
        assert response.status_code == 409
        assert "no SmartStore cache" in response.text
    finally:
        plain.close()


# --------------------------------------------------------------- scenarios


def test_the_shipped_scenarios_are_listed(client):
    scenarios = client.get("/api/scenarios").json()
    names = {s["name"] for s in scenarios}
    assert {"smoke", "search-classes", "soc-analyst-morning"} <= names
    smoke = next(s for s in scenarios if s["name"] == "smoke")
    assert smoke["steps"] == 2
    assert smoke["virtual_users"] >= 1
    assert smoke["engine"] == "api"


# -------------------------------------------------------------------- runs


def wait_for_terminal(client, run_id, timeout_s=90.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        if body["state"] in ("completed", "stopped", "aborted", "failed"):
            return body
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} never finished: {body['state']}")


def test_a_run_goes_all_the_way_through(client, splunkd):
    target = make_target(client, splunkd)
    created = client.post(
        "/api/runs",
        json={
            "target_id": target["id"],
            "scenario": "smoke",
            "label": "from-the-ui",
            "virtual_users": 2,
            "duration_s": 3,
        },
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]

    finished = wait_for_terminal(client, run_id)
    assert finished["state"] == "completed", finished.get("error")
    assert finished["label"] == "from-the-ui"
    assert finished["target_name"] == "fake"

    stats = finished["stats"]
    assert stats["executions"] > 0
    assert stats["latency"]["p95_ms"] >= 0
    assert "queueing" in stats

    summary = finished["summary"]
    assert summary["valid"] is True
    assert summary["outcome"] == "completed"
    # SmartStore provenance is recorded on every run, not only evicting ones.
    assert summary["cache"]["delta"]["provenance"] in ("warm", "cold", "mixed")


def test_a_run_appears_in_the_list_with_its_target(client, splunkd):
    target = make_target(client, splunkd)
    client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 1, "duration_s": 2},
    )
    runs = client.get("/api/runs").json()
    assert len(runs) == 1
    assert runs[0]["scenario"] == "smoke"
    assert runs[0]["target_name"] == "fake"
    wait_for_terminal(client, runs[0]["id"])


def test_a_run_can_be_stopped(client, splunkd):
    target = make_target(client, splunkd)
    run_id = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 2, "duration_s": 120},
    ).json()["id"]

    # Let it actually get going before stopping it.
    deadline = time.time() + 30
    while time.time() < deadline:
        if client.get(f"/api/runs/{run_id}").json()["state"] == "running":
            break
        time.sleep(0.2)

    assert client.post(f"/api/runs/{run_id}/stop").status_code == 200
    finished = wait_for_terminal(client, run_id)
    assert finished["state"] in ("stopped", "aborted", "completed")
    assert finished["ended_at"]


def test_the_virtual_user_ceiling_is_enforced(client, splunkd):
    """The control plane generates the load itself, so it has a limit and says so."""
    target = make_target(client, splunkd)
    response = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 5000},
    )
    assert response.status_code == 409
    assert "ceiling" in response.text


def test_an_unknown_scenario_is_a_404(client, splunkd):
    target = make_target(client, splunkd)
    response = client.post(
        "/api/runs", json={"target_id": target["id"], "scenario": "does-not-exist"}
    )
    assert response.status_code == 404


def test_both_load_models_at_once_is_rejected(client, splunkd):
    target = make_target(client, splunkd)
    response = client.post(
        "/api/runs",
        json={
            "target_id": target["id"],
            "scenario": "smoke",
            "virtual_users": 4,
            "arrival_rate_per_min": 60,
        },
    )
    assert response.status_code == 422


def test_a_run_against_a_deleted_target_fails_cleanly(client, splunkd):
    target = make_target(client, splunkd)
    client.delete(f"/api/targets/{target['id']}")
    response = client.post(
        "/api/runs", json={"target_id": target["id"], "scenario": "smoke"}
    )
    assert response.status_code == 404


# -------------------------------------------------------------------- auth


def test_with_a_password_set_the_api_is_closed(tmp_path, splunkd):
    set_settings(
        ServerConfig(
            database_url=f"sqlite:///{tmp_path/'auth.db'}",
            master_key=new_master_key(),
            master_key_generated=False,
            admin_password="letmein",
            session_ttl_s=3600,
            port=8080,
            scenarios_dir=str(REPO / "scenarios"),
            max_virtual_users=50,
            max_concurrent_runs=2,
        )
    )
    db.reset_engine()
    from regulator_server.app import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/targets").status_code == 401
        assert client.get("/api/auth/status").json() == {
            "authenticated": False,
            "setup_needed": False,
        }
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "letmein"}).status_code == 200
        assert client.get("/api/targets").status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/targets").status_code == 401

    db.reset_engine()
    set_settings(None)


def test_without_a_password_the_status_says_so_out_loud(client):
    """An open deployment must be visible, not merely undocumented."""
    assert client.get("/api/auth/status").json()["setup_needed"] is True


# ----------------------------------------------------------------- security
#
# Each of these is a regression test for a real defect found in review, not a
# hypothetical. The tool deliberately offers no way to dispatch arbitrary SPL
# (scenarios are files on disk, credentials are write-only), and each of these
# inputs defeated that.


@pytest.mark.parametrize(
    "label",
    [
        'x```|delete|search "',      # closes the SPL comment the marker sits in
        'a" | sendemail to=x@y.z',   # closes the quoted operand in the _audit query
        "has spaces",
        "-leading-dash",
        "x" * 200,
    ],
)
def test_a_run_label_cannot_carry_spl(client, splunkd, label):
    """The label is embedded in the comment appended to EVERY search a run runs."""
    target = make_target(client, splunkd)
    response = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": "smoke", "label": label},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "scenario",
    ["../../../tmp/evil", "/tmp/evil", "..", "smoke/../../etc", "with space"],
)
def test_a_scenario_name_cannot_escape_the_library(client, splunkd, scenario):
    """Otherwise anything that can drop a file on the box runs SPL on the target."""
    target = make_target(client, splunkd)
    response = client.post(
        "/api/runs", json={"target_id": target["id"], "scenario": scenario}
    )
    assert response.status_code in (404, 422), response.text


def test_the_library_boundary_is_enforced_below_the_schema_too(tmp_path):
    """The run row is re-read at launch, so the check cannot live only at the edge."""
    from regulator_server.adapters import load_named_scenario

    with pytest.raises(FileNotFoundError) as excinfo:
        load_named_scenario("../../etc")
    assert "outside the scenario library" in str(excinfo.value)


def test_a_credential_embedded_in_a_url_is_refused(client):
    """The base URL is echoed into transport errors, which reach health_detail."""
    response = client.post(
        "/api/targets",
        json={
            "name": "creds-in-url",
            "mgmt_url": "https://svc:hunter2@splunk.example:8089",
            "token": "t",
        },
    )
    assert response.status_code == 422
    assert "username or password" in response.text
