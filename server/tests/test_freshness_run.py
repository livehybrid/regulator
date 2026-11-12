"""A run that measures ingest-to-searchable latency, end to end through the API."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_api import client, wait_for_terminal  # noqa: E402,F401 - the shared app fixture
from test_warm_api import audit_rows  # noqa: E402

FakeSplunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present").FakeSplunk


@pytest.fixture
def splunkd():
    server = FakeSplunk(port=0, base_latency_ms=10.0, jitter_ms=1.0, dispatch_latency_ms=1.0, ingest_delay_s=1.0)
    try:
        yield server
    finally:
        server.close()


def target_with_hec(client, splunkd, **extra):  # noqa: F811
    body = {
        "name": "fake", "mgmt_url": splunkd.base_url, "token": "a-secret-bearer-token", "verify_tls": False,
        "hec_url": splunkd.base_url, "hec_token": "a-secret-hec-token", **extra,
    }
    response = client.post("/api/targets", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def launch(client, target, **extra):  # noqa: F811
    body = {"target_id": target["id"], "scenario": "smoke", "virtual_users": 1, "duration_s": 5,
            "freshness": True, "freshness_index": "main", "freshness_interval_s": 2, **extra}
    return client.post("/api/runs", json=body)


def test_the_hec_token_is_stored_write_only_and_can_be_changed_or_cleared(client, splunkd):  # noqa: F811
    target = target_with_hec(client, splunkd)
    assert target["hec_url"] == splunkd.base_url and target["has_hec_token"] is True
    assert "a-secret-hec-token" not in str(target) and "hec_token" not in target

    changed = client.patch(f"/api/targets/{target['id']}", json={"hec_token": "another"}).json()
    assert changed["has_hec_token"] is True
    cleared = client.patch(f"/api/targets/{target['id']}", json={"clear": ["hec_token"], "hec_url": ""}).json()
    assert cleared["has_hec_token"] is False and cleared["hec_url"] is None
    row = next(r for r in audit_rows(client) if r["action"] == "target_updated" and "hec_token" in r["detail"])
    assert "another" not in row["detail"]


def test_a_run_reports_how_long_its_probes_took_to_become_searchable(client, splunkd):  # noqa: F811
    target = target_with_hec(client, splunkd)
    response = launch(client, target)
    assert response.status_code == 201, response.text
    finished = wait_for_terminal(client, response.json()["id"])
    assert finished["state"] == "completed", finished.get("error")
    assert finished["freshness"] == {"index": "main", "interval_s": 2.0}

    fresh = finished["summary"]["freshness"]
    assert fresh["available"] is True, fresh
    assert fresh["during_run"] >= 2 and fresh["found"] == fresh["during_run"] and fresh["not_found"] == 0
    # The fake makes an event searchable a second after it arrives.
    assert 0.5 <= fresh["latency"]["p50_s"] <= 6.0
    assert fresh["source"] == f"regulator:r{finished['id']}"
    assert any("Events became searchable" in line for line in fresh["findings"])

    started = next(r for r in audit_rows(client) if r["action"] == "run_started")
    assert "measuring ingest-to-searchable latency with a probe every 2s into index main" in started["detail"]


def test_a_run_without_the_probe_sends_nothing(client, splunkd):  # noqa: F811
    target = target_with_hec(client, splunkd)
    response = launch(client, target, freshness=False)
    finished = wait_for_terminal(client, response.json()["id"])
    assert finished["freshness"] is None and finished["summary"].get("freshness") is None
    assert splunkd._hec_events == []


def test_the_probe_needs_a_hec_on_the_target_and_a_plain_index(client, splunkd):  # noqa: F811
    bare = client.post(
        "/api/targets",
        json={"name": "bare", "mgmt_url": splunkd.base_url, "token": "t", "verify_tls": False},
    ).json()
    refused = launch(client, bare)
    assert refused.status_code == 422 and "give the target a HEC address and token" in refused.text

    target = target_with_hec(client, splunkd, name="with-hec")
    for index in (None, "main OR index=*", ""):
        bad = launch(client, target, freshness_index=index)
        assert bad.status_code == 422, index
    assert launch(client, target, freshness_interval_s=1).status_code == 422
    assert client.get("/api/runs").json() == []


def test_a_refused_hec_token_is_reported_and_the_run_still_completes(client, splunkd):  # noqa: F811
    splunkd.config.hec_reject_token = "a-secret-hec-token"
    target = target_with_hec(client, splunkd)
    finished = wait_for_terminal(client, launch(client, target).json()["id"])
    assert finished["state"] == "completed", finished.get("error")
    fresh = finished["summary"]["freshness"]
    assert fresh["available"] is False and "HTTP 403" in fresh["reason"]
    assert finished["stats"]["executions"] > 0
