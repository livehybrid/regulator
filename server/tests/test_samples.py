"""The time series, the control plane's HEC telemetry, and the purge action."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "worker"))
sys.path.insert(0, str(REPO / "server"))

from regulator_server import db  # noqa: E402
from regulator_server.config import HecSettings, set_settings  # noqa: E402
from regulator_server.telemetry import telemetry  # noqa: E402
from test_fleet import FakeSplunk, make_settings, make_target  # noqa: E402 - helpers shared with the fleet tests


@pytest.fixture
def splunkd():
    """A fake splunkd with a SmartStore cache, so evictions have something to evict."""
    server = FakeSplunk(port=0, base_latency_ms=10.0, jitter_ms=2.0, dispatch_latency_ms=1.0, smartstore_buckets=40, smartstore_local_pct=75, seed=7)
    try:
        yield server
    finally:
        server.close()


class Collector:
    """Receives the emitter's HEC batches and keeps every event."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        for line in request.content.decode("utf-8").splitlines():
            line = line.strip()
            if line:
                self.events.append(json.loads(line))
        return httpx.Response(200, json={"text": "Success", "code": 0})

    def by_sourcetype(self, sourcetype: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e.get("sourcetype") == sourcetype]


@pytest.fixture
def collector():
    box = Collector()
    telemetry.stop()
    telemetry.transport = httpx.MockTransport(box.handler)
    yield box
    telemetry.stop()
    telemetry.transport = None


@pytest.fixture
def client(tmp_path, monkeypatch, collector):
    monkeypatch.setenv("REG_POLL_INITIAL_MS", "10")
    set_settings(
        make_settings(
            tmp_path,
            sample_interval_s=1.0,
            hec=HecSettings(url="http://hec.invalid:8088", token="hec-token", index="regulator", verify_tls=False, gzip=False, batch_ms=50),
        )
    )
    db.reset_engine()
    from regulator_server.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    from regulator_server.runner import manager

    manager.drain_fleets(20.0)
    db.reset_engine()
    set_settings(None)


def _wait_terminal(client, run_id: int, timeout_s: float = 90.0) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        if body["state"] in ("completed", "stopped", "aborted", "failed"):
            return body
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} never finished: {body['state']}")


def test_a_run_keeps_a_time_series_and_ships_it_over_hec(client, splunkd, collector):
    target = make_target(client, splunkd)
    created = client.post("/api/runs", json={"target_id": target["id"], "scenario": "smoke", "label": "sampled", "virtual_users": 2, "duration_s": 4})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    finished = _wait_terminal(client, run_id)
    assert finished["state"] == "completed", finished.get("error")

    # Samples: one a second, interval and cumulative figures, thinned on request.
    doc = client.get(f"/api/runs/{run_id}/samples").json()
    samples = doc["samples"]
    assert len(samples) >= 2, samples
    assert all(s["slot"] is None for s in samples)
    assert samples[-1]["executions"] >= samples[0]["executions"]
    assert any((s["interval"] or {}).get("executions", 0) > 0 for s in samples)
    assert any((s["cum"] or {}).get("p95_ms") is not None for s in samples)
    newer = client.get(f"/api/runs/{run_id}/samples?since={samples[0]['at']}").json()["samples"]
    assert len(newer) == len(samples) - 1
    thinned = client.get(f"/api/runs/{run_id}/samples?points=10").json()["samples"]
    assert len(thinned) <= 11
    # The run document carries its markers, and the list its sparklines.
    assert isinstance(finished["markers"], list)
    sparks = client.get(f"/api/runs/sparklines?ids={run_id},999").json()
    assert str(run_id) in sparks and len(sparks[str(run_id)]) >= 2
    # Cache readings were taken around the run.
    readings = client.get(f"/api/targets/{target['id']}/samples").json()["samples"]
    assert [r["kind"] for r in readings][:2] == ["before", "after"]
    assert all(r["run_id"] == run_id for r in readings[:2])

    telemetry.flush()
    kinds = [e["event"]["kind"] for e in collector.by_sourcetype("regulator:lifecycle")]
    for expected in ("run_created", "run_started", "cache_before", "cache_after", "correlation", "run_completed"):
        assert expected in kinds, kinds
    shipped = collector.by_sourcetype("regulator:sample")
    assert len(shipped) >= 2
    first = shipped[0]
    assert first["index"] == "regulator" and first["source"] == "regulator"
    assert first["event"]["run_no"] == run_id and first["fields"] == {"emitter": "control"}
    assert first["event"]["run_label"].startswith(f"r{run_id}")
    assert "interval" in first["event"] and "cum" in first["event"]
    # The aggregate row has no slot key at all (a JSON null would read as a value in Splunk).
    assert "slot" not in first["event"]
    finals = [e for e in collector.by_sourcetype("regulator:run") if e["event"].get("scope") == "inprocess"]
    assert len(finals) == 1 and finals[0]["event"]["outcome"] == "completed"
    # The worker's own step records take the worker's emitter (covered in
    # worker/tests/test_hec.py); only the control plane's half is captured here.


def test_the_purge_action_evicts_everything_and_writes_the_counts_to_the_audit(client, splunkd, collector):
    target = make_target(client, splunkd)
    assert client.get(f"/api/targets/{target['id']}/cache").status_code == 200
    response = client.post(f"/api/targets/{target['id']}/evict", json={"all_indexes": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["indexes"] == "all"
    assert "duration_s" in body and body["in_flight_runs"] == []
    assert body["eviction"]["attempted"] >= 1
    audit = client.get("/api/audit").json()
    rows = audit if isinstance(audit, list) else audit.get("events") or audit.get("items") or []
    evicted = [row for row in rows if row.get("action") == "cache_evicted"]
    assert evicted, rows
    assert "confirmed evicted" in evicted[0]["detail"]
    readings = client.get(f"/api/targets/{target['id']}/samples").json()["samples"]
    assert [r["kind"] for r in readings] == ["read", "evict"]
    telemetry.flush()
    events = [e for e in collector.by_sourcetype("regulator:lifecycle") if e["event"]["kind"] == "cache_evicted"]
    assert events and events[0]["event"]["target"] == "fake" and events[0]["event"]["scope"] == "all"


def test_the_health_event_describes_the_control_plane(client, collector):
    from regulator_server.telemetry import HEALTH_INTERVAL_S  # noqa: F401 - the loop fires once at start

    assert telemetry.lifecycle("probe", None, None, note="hello") is True
    telemetry.flush()
    health = collector.by_sourcetype("regulator:health")
    assert health, "the health loop did not fire at start"
    event = health[0]["event"]
    assert event["kind"] == "health" and "fleets" in event and "hec" in event
    probe = [e for e in collector.by_sourcetype("regulator:lifecycle") if e["event"]["kind"] == "probe"]
    assert probe and probe[0]["event"]["note"] == "hello"


def test_a_purge_during_a_run_starts_a_cache_epoch_and_the_summary_shows_it(client, splunkd, collector):
    target = make_target(client, splunkd)
    created = client.post("/api/runs", json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 2, "duration_s": 6})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    deadline = time.time() + 20
    while time.time() < deadline and client.get(f"/api/runs/{run_id}").json()["state"] != "running":
        time.sleep(0.2)
    time.sleep(1.5)
    purged = client.post(f"/api/targets/{target['id']}/evict", json={"all_indexes": True})
    assert purged.status_code == 200, purged.text
    assert purged.json()["in_flight_runs"] == [run_id]
    # The live series marks the epoch at once.
    live = client.get(f"/api/runs/{run_id}/samples").json()
    assert any(m["kind"] == "evict" and m["label"] == "E1" for m in live["markers"])
    finished = _wait_terminal(client, run_id)
    assert finished["state"] == "completed", finished.get("error")
    epochs = finished["summary"]["cache"]["epochs"]
    assert len(epochs) == 1 and epochs[0]["source"] == "button" and epochs[0]["epoch"] == 1
    assert any(m["kind"] == "evict" for m in finished["markers"])
    # Records after the purge carry the epoch, so the summary can split by it.
    stats = finished["summary"]["stats"]
    assert stats["cache_epoch"] == 1
    assert stats["epochs"] and stats["epochs"][0]["epoch"] == 1 and stats["epochs"][0]["executions"] > 0
    telemetry.flush()
    marked = [e["event"] for e in collector.by_sourcetype("regulator:lifecycle") if e["event"]["kind"] == "cache_evicted"]
    assert marked and marked[0]["in_flight_runs"] == [run_id]


def test_a_run_can_evict_on_a_clock_and_reports_cold_and_warm(client, splunkd):
    target = make_target(client, splunkd)
    refused = client.post("/api/runs", json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 2, "duration_s": 30, "evict_every_s": 10})
    assert refused.status_code == 422 and "scope" in refused.text
    created = client.post("/api/runs", json={
        "target_id": target["id"], "scenario": "smoke", "virtual_users": 2, "duration_s": 24,
        "evict_all_indexes": True, "evict_every_s": 10, "cold_window_s": 5,
    })
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    assert created.json()["evict_every_s"] == 10 and created.json()["cold_window_s"] == 5
    finished = _wait_terminal(client, run_id, timeout_s=120)
    assert finished["state"] == "completed", finished.get("error")
    epochs = finished["summary"]["cache"]["epochs"]
    assert len(epochs) >= 2, epochs
    assert all(e["source"] == "timer" and e.get("attempted", 0) >= 0 for e in epochs)
    markers = [m for m in finished["markers"] if m["kind"] == "evict"]
    assert len(markers) == len(epochs)
    # The per-step split: something ran cold, something ran warm.
    steps = finished["summary"]["stats"]["steps"]
    assert any(s.get("cold") for s in steps) and any(s.get("warm") for s in steps), steps
    readings = client.get(f"/api/targets/{target['id']}/samples").json()["samples"]
    assert [r["kind"] for r in readings].count("epoch") == len(epochs)
