"""The fleet: planning, the lease protocol, merging, and real workers on a real socket."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "worker"))
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO))

from regulator_agent.histogram import LatencyHistogram  # noqa: E402
from regulator_server import db  # noqa: E402
from regulator_server import fleet  # noqa: E402
from regulator_server.config import FleetSettings, ServerConfig, new_master_key, set_settings  # noqa: E402
from regulator_server.crypto import mint_run_token  # noqa: E402
from regulator_server.drivers import clear_cache, register_driver  # noqa: E402
from regulator_server.drivers.fake import FakeDriver  # noqa: E402
from regulator_server.models import LEASE_DONE, LEASE_LOST, LEASE_READY, LEASE_RUNNING, Run, WorkerLease  # noqa: E402

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk


def make_settings(tmp_path, **overrides) -> ServerConfig:
    values = dict(
        database_url=f"sqlite:///{tmp_path/'fleet.db'}",
        master_key=new_master_key(),
        master_key_generated=False,
        admin_password=None,
        session_ttl_s=3600,
        scenarios_dir=str(REPO / "scenarios"),
        user_scenarios_dir=str(tmp_path / "user-scenarios"),
        max_virtual_users=50,
        max_concurrent_runs=2,
        allow_unauthenticated=True,
        fleet=FleetSettings(default_fleet="inprocess", vus_per_worker=2, heartbeat_s=1.0, lease_s=6.0, provision_timeout_s=20.0),
    )
    values.update(overrides)
    return ServerConfig(**values)


@pytest.fixture
def splunkd():
    server = FakeSplunk(port=0, base_latency_ms=10.0, jitter_ms=2.0, dispatch_latency_ms=1.0, seed=7)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def fake_driver():
    driver = FakeDriver(spawn=False)
    register_driver("fake", driver)
    yield driver
    clear_cache()


@pytest.fixture
def client(tmp_path, monkeypatch, fake_driver):
    monkeypatch.setenv("REG_POLL_INITIAL_MS", "10")
    set_settings(make_settings(tmp_path))
    db.reset_engine()
    from regulator_server.app import create_app

    with TestClient(create_app()) as test_client:
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


# ------------------------------------------------------------------ planning


def test_the_plan_deals_users_out_evenly_and_sizes_the_fleet(tmp_path):
    from regulator_agent.scenario import load_scenario

    directory = REPO / "scenarios" / "search-classes"
    scenario = load_scenario(directory)
    run = Run(id=7, scenario="search-classes", virtual_users=7, fleet="swarm")
    groups = fleet.plan(run, scenario, directory, FleetSettings(vus_per_worker=3))
    assert len(groups) == 1
    group = groups[0]
    assert group.engine == "api"
    assert group.workers == 3
    assert [slot.share["virtual_users"] for slot in group.slots] == [3, 2, 2]
    assert "scenario.yaml" in group.scenario_files
    assert "savedsearches.conf" not in group.scenario_files


def test_an_explicit_worker_count_wins_and_a_conf_travels(tmp_path):
    from regulator_agent.scenario import load_scenario

    directory = REPO / "scenarios" / "pack-web-access"
    scenario = load_scenario(directory)
    run = Run(id=8, scenario="pack-web-access", virtual_users=10, workers=4, fleet="k8s")
    groups = fleet.plan(run, scenario, directory, FleetSettings(vus_per_worker=200))
    assert groups[0].workers == 4
    assert [slot.share["virtual_users"] for slot in groups[0].slots] == [3, 3, 2, 2]
    assert "savedsearches.conf" in groups[0].scenario_files


def test_a_mixed_scenario_becomes_two_groups_on_two_images(tmp_path):
    from regulator_agent.scenario import parse_scenario
    import yaml

    document = {
        "name": "mixed",
        "engine": "mixed",
        "seed": 3,
        "corpus": {"index": "main"},
        "time_policy": {"mode": "rolling", "window": "1h", "jitter": "5m"},
        "personas": [
            {"name": "api", "weight": 75, "think_time": {"dist": "fixed", "value_s": 1},
             "steps": [{"id": "s", "type": "search", "spl": "search index=main | head 1"}]},
            {"name": "web", "weight": 25, "think_time": {"dist": "fixed", "value_s": 1},
             "steps": [{"id": "d", "type": "dashboard", "engine": "browser", "app": "search", "dashboard": "x"}]},
        ],
        "load": {"model": "closed", "virtual_users": 40, "duration": "10s"},
        "abort_if": {"error_rate_pct": 50},
    }
    directory = tmp_path / "mixed"
    directory.mkdir()
    (directory / "scenario.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    scenario = parse_scenario(document, source_path=directory / "scenario.yaml")
    run = Run(id=9, scenario="mixed", fleet="swarm")
    groups = fleet.plan(run, scenario, directory, FleetSettings(vus_per_worker=20, browser_contexts_per_worker=5, worker_image="api-img", browser_worker_image="browser-img"))
    by_engine = {group.engine: group for group in groups}
    assert set(by_engine) == {"api", "browser"}
    assert by_engine["api"].image == "api-img"
    assert by_engine["browser"].image == "browser-img"
    # 75% of 40 users on the api image at 20 per worker, 25% on browsers at 5.
    assert sum(s.share["virtual_users"] for s in by_engine["api"].slots) == 30
    assert by_engine["api"].workers == 2
    assert sum(s.share["virtual_users"] for s in by_engine["browser"].slots) == 10
    assert by_engine["browser"].workers == 2
    # Each group's scenario carries only its own personas.
    api_doc = yaml.safe_load(by_engine["api"].scenario_files["scenario.yaml"])
    assert [p["name"] for p in api_doc["personas"]] == ["api"]
    assert api_doc["engine"] == "api"
    browser_doc = yaml.safe_load(by_engine["browser"].scenario_files["scenario.yaml"])
    assert [p["name"] for p in browser_doc["personas"]] == ["web"]
    # Slots never collide across groups.
    slots = [s.slot for g in groups for s in g.slots]
    assert slots == sorted(set(slots))


# ------------------------------------------------------------------- merging


def _summary(slot: int, latencies: List[float], executions: int, errors: int = 0, valid: bool = True) -> Dict[str, Any]:
    hist = LatencyHistogram()
    for value in latencies:
        hist.record_ms(value)
    step_hist = hist.copy()
    empty = LatencyHistogram().to_dict()
    return {
        "run_id": "r1", "slot": slot, "scenario": "s", "outcome": "completed", "valid": valid,
        "invalid_reason": None if valid else "starved", "started_at": 1000.0 + slot, "ended_at": 1100.0 + slot,
        "duration_s": 100.0, "target_url": "https://x", "self_instrumented": False, "co_corrected": True,
        "load_model": "closed", "peak_virtual_users": 2, "scenario_seed": 7, "effective_seed": 7,
        "configured_load": {"model": "closed", "virtual_users": 2, "slot": slot, "total_workers": 2},
        "stats": {
            "slot": slot, "executions": executions, "errors": errors, "iterations_completed": executions,
            "errors_by_class": {"timeout": errors} if errors else {}, "partial": 0, "abandoned": 0,
            "peak_in_flight": 2, "latency": hist.summary(),
            "queueing": {"searches_queued": 1, "job_executions": executions - errors},
            "generator": {"max_loop_lag_ms": 5.0 * slot, "max_schedule_debt_ms": 0.0},
            "steps": [{"step_id": "one", "class": "dense", "executions": executions, "successes": executions - errors,
                       "errors": errors, "partial": 0, "scan_count_total": 1000 * executions, "mean_events_per_s": 1000.0,
                       "latency": hist.summary()}],
            "histograms": {"latency": hist.to_dict(), "failure_latency": empty, "queued": empty, "loop_lag": empty,
                           "drift": empty, "steps": {"one": {"latency": step_hist.to_dict(), "service_time": empty,
                                                             "dispatch": empty, "ttfr": empty, "failure_latency": empty}}},
        },
    }


def test_merging_computes_percentiles_over_the_union_not_an_average():
    fast = _summary(0, [100.0] * 90 + [110.0] * 10, 100)
    slow = _summary(1, [1000.0] * 100, 100)
    merged = fleet.merge_summaries("r1", "s", [fast, slow], lost_slots=[])
    stats = merged["stats"]
    assert stats["executions"] == 200
    assert stats["workers"] == 2
    # The merged p95 sits in the slow worker's mass, far from the average of
    # the two workers' p95 values (110 and 1000 -> 555 would be the average).
    assert stats["latency"]["p95_ms"] == pytest.approx(1000.0, rel=0.05)
    assert stats["latency"]["p50_ms"] > 100.0
    assert stats["steps"][0]["executions"] == 200
    assert stats["steps"][0]["scan_count_per_search"] == 1000.0
    assert stats["queueing"]["searches_queued"] == 2
    assert merged["valid"] is True
    assert merged["configured_load"]["virtual_users"] == 4
    assert merged["configured_load"]["workers"] == 2
    assert merged["duration_s"] == pytest.approx(101.0)
    assert [w["slot"] for w in merged["workers"]] == [0, 1]


def test_a_lost_worker_makes_the_run_invalid_and_says_why():
    merged = fleet.merge_summaries("r1", "s", [_summary(0, [50.0], 1)], lost_slots=[1, 2])
    assert merged["valid"] is False
    assert "2 worker(s) were lost" in merged["invalid_reason"]
    assert merged["configured_load"]["workers"] == 3
    assert [w["outcome"] for w in merged["workers"]] == ["completed", "lost", "lost"]


def test_an_invalid_worker_taints_the_fleet():
    merged = fleet.merge_summaries("r1", "s", [_summary(0, [50.0], 1), _summary(1, [50.0], 1, valid=False)], lost_slots=[])
    assert merged["valid"] is False
    assert "starved" in merged["invalid_reason"]


# ------------------------------------------------------- the lease protocol


def _bearer(run_id: int) -> Dict[str, str]:
    return {"Authorization": f"Bearer {mint_run_token(run_id)}"}


def _launch(client, splunkd, fake_driver, **body) -> int:
    target = make_target(client, splunkd)
    payload = {"target_id": target["id"], "scenario": "smoke", "fleet": "k8s", "duration_s": 5, "virtual_users": 3}
    payload.update(body)
    # The k8s kind is bound to the fake driver for the test.
    register_driver("k8s", fake_driver)
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_workers_claim_slots_ready_up_and_are_released_together(client, splunkd, fake_driver):
    run_id = _launch(client, splunkd, fake_driver)
    headers = _bearer(run_id)
    # The plan: 3 users at 2 per worker is 2 workers.
    deadline = time.time() + 10
    while time.time() < deadline and not fake_driver.created_groups():
        time.sleep(0.1)
    assert [g.workers for g in fake_driver.created_groups()] == [2]

    first = client.post(f"/api/agent/runs/{run_id}/claim", json={"holder": "w-a"}, headers=headers).json()
    second = client.post(f"/api/agent/runs/{run_id}/claim", json={"holder": "w-b"}, headers=headers).json()
    assert {first["slot"], second["slot"]} == {0, 1}
    assert first["lease_id"] != second["lease_id"]
    assert first["env"]["REG_TARGET_URL"] == splunkd.base_url
    assert first["env"]["REG_TARGET_TOKEN"] == "tok-en"
    assert first["env"]["REG_VUS"] in ("1", "2")
    assert "scenario.yaml" in first["scenario"]["files"]
    assert first["total_workers"] == 2
    # A third claimant finds no slot.
    assert client.post(f"/api/agent/runs/{run_id}/claim", json={"holder": "w-c"}, headers=headers).status_code == 409
    # A retried claim by a holder that already has one is idempotent.
    again = client.post(f"/api/agent/runs/{run_id}/claim", json={"holder": "w-a"}, headers=headers).json()
    assert again["slot"] == first["slot"] and again["lease_id"] == first["lease_id"]

    # Ready one: no release yet. Ready both: T0 set.
    assert client.post(f"/api/agent/runs/{run_id}/ready", json={"slot": first["slot"], "lease_id": first["lease_id"]}, headers=headers).status_code == 200
    beat = client.post(f"/api/agent/runs/{run_id}/heartbeat", json={"slot": first["slot"], "lease_id": first["lease_id"], "state": "ready"}, headers=headers).json()
    assert beat["command"] == "continue"
    assert client.post(f"/api/agent/runs/{run_id}/ready", json={"slot": second["slot"], "lease_id": second["lease_id"]}, headers=headers).status_code == 200
    beat = client.post(f"/api/agent/runs/{run_id}/heartbeat", json={"slot": first["slot"], "lease_id": first["lease_id"], "state": "ready"}, headers=headers).json()
    assert beat["command"] == "release"
    assert beat["t0"] > time.time() - 1
    # Once a worker reports running, the run is running and the command settles.
    beat = client.post(f"/api/agent/runs/{run_id}/heartbeat", json={"slot": first["slot"], "lease_id": first["lease_id"], "state": "running", "stats": {"executions": 5, "errors": 0, "latency": {"p95_ms": 12.0}}}, headers=headers).json()
    assert beat["command"] == "continue"
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["state"] == "running"
    assert run["fleet"] == "k8s"
    assert run["stats"]["executions"] == 5
    workers = client.get(f"/api/runs/{run_id}/workers").json()
    assert {w["state"] for w in workers} <= {LEASE_RUNNING, LEASE_READY}

    # A wrong lease id is fenced.
    assert client.post(f"/api/agent/runs/{run_id}/ready", json={"slot": first["slot"], "lease_id": "nope"}, headers=headers).status_code == 409
    fenced = client.post(f"/api/agent/runs/{run_id}/heartbeat", json={"slot": first["slot"], "lease_id": "nope", "state": "running"}, headers=headers).json()
    assert fenced["command"] == "superseded"
    # A token for another run is refused outright.
    assert client.post(f"/api/agent/runs/{run_id}/heartbeat", json={"slot": 0, "lease_id": first["lease_id"]}, headers=_bearer(run_id + 99)).status_code == 401

    # Finals from both workers finish the run with a merged summary.
    for doc in (first, second):
        summary = _summary(doc["slot"], [20.0, 30.0], 2)
        assert client.post(f"/api/agent/runs/{run_id}/final", json={"slot": doc["slot"], "lease_id": doc["lease_id"], "summary": summary}, headers=headers).status_code == 200
    deadline = time.time() + 15
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["state"] in ("completed", "stopped", "aborted", "failed"):
            break
        time.sleep(0.2)
    assert run["state"] == "completed", run.get("error")
    assert run["summary"]["stats"]["executions"] == 4
    assert run["summary"]["stats"]["workers"] == 2
    assert run["summary"]["cache"] is not None
    assert run["fleet_state"] == "finished"
    # The driver was told to remove the group.
    assert all(fake_driver.is_destroyed(ref) for ref in _refs(fake_driver))


def _refs(fake_driver):
    from regulator_server.drivers.base import DriverRef

    return [DriverRef(kind="fake", id=group_id) for group_id in list(fake_driver._groups)]


def test_a_worker_that_stops_heartbeating_is_lost_and_the_run_still_finishes(client, splunkd, fake_driver):
    run_id = _launch(client, splunkd, fake_driver, virtual_users=2, workers=2)
    headers = _bearer(run_id)
    time.sleep(0.5)
    first = client.post(f"/api/agent/runs/{run_id}/claim", json={"holder": "w-a"}, headers=headers).json()
    # Only one of the two slots ever claims; the other is lost at the
    # provisioning timeout and the fleet is released without it.
    client.post(f"/api/agent/runs/{run_id}/ready", json={"slot": first["slot"], "lease_id": first["lease_id"]}, headers=headers)
    deadline = time.time() + 30
    released = False
    while time.time() < deadline:
        beat = client.post(f"/api/agent/runs/{run_id}/heartbeat", json={"slot": first["slot"], "lease_id": first["lease_id"], "state": "ready"}, headers=headers).json()
        if beat["command"] == "release":
            released = True
            break
        time.sleep(1.0)
    assert released
    client.post(f"/api/agent/runs/{run_id}/final", json={"slot": first["slot"], "lease_id": first["lease_id"], "summary": _summary(first["slot"], [10.0], 1)}, headers=headers)
    deadline = time.time() + 15
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["state"] in ("completed", "stopped", "aborted", "failed"):
            break
        time.sleep(0.2)
    assert run["state"] == "completed"
    assert run["summary"]["valid"] is False
    assert "lost" in run["summary"]["invalid_reason"]
    workers = client.get(f"/api/runs/{run_id}/workers").json()
    assert sorted(w["state"] for w in workers) == [LEASE_DONE, LEASE_LOST]


def test_a_stop_from_the_interface_reaches_the_workers_as_a_command(client, splunkd, fake_driver):
    run_id = _launch(client, splunkd, fake_driver, virtual_users=1)
    headers = _bearer(run_id)
    time.sleep(0.5)
    lease = client.post(f"/api/agent/runs/{run_id}/claim", json={"holder": "w-a"}, headers=headers).json()
    client.post(f"/api/agent/runs/{run_id}/ready", json={"slot": lease["slot"], "lease_id": lease["lease_id"]}, headers=headers)
    assert client.post(f"/api/runs/{run_id}/stop").status_code == 200
    beat = client.post(f"/api/agent/runs/{run_id}/heartbeat", json={"slot": lease["slot"], "lease_id": lease["lease_id"], "state": "running"}, headers=headers).json()
    assert beat["command"] == "stop"


def test_the_fleet_list_says_what_is_available(client):
    fleets = {f["kind"]: f for f in client.get("/api/fleets").json()}
    assert fleets["inprocess"]["available"] is True
    assert fleets["swarm"]["available"] is False


# ------------------------------------------- real workers on a real socket


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def served(tmp_path, monkeypatch):
    """The real app on a real port, with a spawning fake driver bound to 'swarm'."""
    import uvicorn

    port = _free_port()
    driver = FakeDriver(spawn=True, cwd=str(REPO), env_overrides={"PYTHONPATH": str(REPO / "worker"), "REG_POLL_INITIAL_MS": "10", "REG_POLL_MAX_MS": "50"})
    register_driver("swarm", driver)
    set_settings(
        make_settings(
            tmp_path,
            fleet=FleetSettings(
                default_fleet="inprocess", vus_per_worker=2, heartbeat_s=1.0, lease_s=8.0,
                provision_timeout_s=60.0, public_base_url=f"http://127.0.0.1:{port}",
                portainer_host="portainer.invalid", portainer_token="not-used-the-driver-is-fake",
            ),
        )
    )
    db.reset_engine()
    from regulator_server.app import create_app

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline and not server.started:
        time.sleep(0.1)
    assert server.started
    import httpx

    yield httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30), driver
    server.should_exit = True
    thread.join(timeout=10)
    clear_cache()
    db.reset_engine()
    set_settings(None)


def test_real_workers_claim_run_and_report_over_the_wire(served, splunkd):
    """The whole protocol with the real agent as subprocesses.

    Two workers claim, warm up, are released together, run their share against
    the fake splunkd, and post finals with histograms that the control plane
    merges into one run.
    """
    http, driver = served
    target = http.post("/api/targets", json={"name": "fake", "mgmt_url": splunkd.base_url, "token": "tok-en", "verify_tls": False})
    assert target.status_code == 201, target.text
    launched = http.post("/api/runs", json={"target_id": target.json()["id"], "scenario": "smoke", "fleet": "swarm", "virtual_users": 4, "duration_s": 6, "label": "fleet-e2e"})
    assert launched.status_code == 201, launched.text
    run_id = launched.json()["id"]

    deadline = time.time() + 150
    run: Dict[str, Any] = {}
    while time.time() < deadline:
        run = http.get(f"/api/runs/{run_id}").json()
        if run["state"] in ("completed", "stopped", "aborted", "failed"):
            break
        time.sleep(1.0)
    workers = http.get(f"/api/runs/{run_id}/workers").json()
    logs = http.get(f"/api/runs/{run_id}/logs").json()
    assert run["state"] == "completed", (run.get("error"), workers, json.dumps(logs)[:3000])
    summary = run["summary"]
    assert summary["stats"]["workers"] == 2
    assert summary["stats"]["executions"] >= 4
    assert summary["stats"]["errors"] == 0
    assert summary["valid"] is True, summary["invalid_reason"]
    assert summary["configured_load"]["virtual_users"] == 4
    assert summary["configured_load"]["workers"] == 2
    assert {w["state"] for w in workers} == {LEASE_DONE}
    assert all(w["outcome"] == "completed" for w in workers)
    # The fake splunkd saw searches from both slots (slot-disjoint virtual users).
    markers = {spl.split("```")[-2] for spl in splunkd.stats.searches if "```" in spl}
    assert any(":vu0:" in m or ":vu1:" in m for m in markers)
    assert any(":vu1000000:" in m or ":vu1000001:" in m for m in markers)
    assert summary["cache"] is not None
    assert summary["sut"] is not None
