"""Baselines and comparison, through the API.

The behaviour worth protecting is that a baseline is a promise: everything else
gets judged against it, so it must never be a run that measured the load
generator rather than Splunk.
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
from regulator_server.models import Run, Target  # noqa: E402

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")


@pytest.fixture
def client(tmp_path):
    set_settings(
        ServerConfig(
            database_url=f"sqlite:///{tmp_path/'base.db'}",
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


def seed_run(summary_valid=True, p95=1000.0, scenario="smoke", label="seed") -> int:
    """Insert a finished run directly.

    Comparison is arithmetic over two stored summaries, so driving a real run
    for each case would add minutes to the suite and test nothing extra.
    """
    with db.session_scope() as session:
        target = session.query(Target).first()
        if target is None:
            target = Target(name="t", mgmt_url="https://x:8089", token_encrypted=None)
            session.add(target)
            session.flush()
        summary = {
            "scenario": scenario,
            "scenario_seed": 7,
            "outcome": "completed",
            "valid": summary_valid,
            "invalid_reason": None if summary_valid else "the generator fell behind",
            "co_corrected": True,
            "peak_virtual_users": 4,
            "target_url": "https://x:8089",
            "cache": {"delta": {"provenance": "warm"}},
            "stats": {
                "executions": 100,
                "error_rate_pct": 0.0,
                "throughput_per_s": 5.0,
                "latency": {"p50_ms": p95 / 2, "p95_ms": p95, "p99_ms": p95 * 1.1,
                            "mean_ms": p95 / 2, "max_ms": p95 * 2, "min_ms": 1.0, "count": 100},
                "queueing": {"searches_queued": 0, "queued_pct": 0.0},
                "generator": {"max_drift_ms": 1.0},
                "steps": [],
            },
        }
        run = Run(
            label=label,
            target_id=target.id,
            scenario=scenario,
            state="completed",
            started_at=time.time() - 10,
            ended_at=time.time(),
            summary_json=summary,
            stats_json=summary["stats"],
        )
        session.add(run)
        session.flush()
        return run.id


def test_a_run_can_be_promoted_to_a_baseline(client):
    run_id = seed_run()
    created = client.post("/api/baselines", json={"run_id": run_id, "label": "main-green"})
    assert created.status_code == 201
    assert created.json()["run_id"] == run_id

    listed = client.get("/api/baselines").json()
    assert [b["label"] for b in listed] == ["main-green"]


def test_an_invalid_run_cannot_become_a_baseline(client):
    """Everything else is judged against it, so it must have measured Splunk."""
    run_id = seed_run(summary_valid=False)
    response = client.post("/api/baselines", json={"run_id": run_id, "label": "bad"})
    assert response.status_code == 409
    assert "invalid" in response.text
    assert client.get("/api/baselines").json() == []


def test_promoting_again_moves_the_label(client):
    """A label is how a pipeline says 'the current good one'."""
    first = seed_run(p95=1000.0)
    second = seed_run(p95=900.0)
    client.post("/api/baselines", json={"run_id": first, "label": "main-green"})
    client.post("/api/baselines", json={"run_id": second, "label": "main-green"})

    listed = client.get("/api/baselines").json()
    assert len(listed) == 1
    assert listed[0]["run_id"] == second


def test_a_baseline_can_be_deleted(client):
    run_id = seed_run()
    client.post("/api/baselines", json={"run_id": run_id, "label": "gone"})
    assert client.delete("/api/baselines/gone").status_code == 204
    assert client.get("/api/baselines").json() == []


def test_comparing_against_a_label(client):
    baseline_run = seed_run(p95=1000.0)
    client.post("/api/baselines", json={"run_id": baseline_run, "label": "main-green"})
    candidate = seed_run(p95=1050.0)

    result = client.post(
        f"/api/runs/{candidate}/compare",
        json={"baseline_label": "main-green", "gates": ["p95 <= baseline + 15%"]},
    ).json()

    assert result["ok"] is True
    assert result["baseline_run_id"] == baseline_run
    assert result["candidate_run_id"] == candidate
    assert "report" in result


def test_a_regression_fails_the_comparison(client):
    baseline_run = seed_run(p95=1000.0)
    client.post("/api/baselines", json={"run_id": baseline_run, "label": "main-green"})
    candidate = seed_run(p95=2000.0)

    result = client.post(
        f"/api/runs/{candidate}/compare",
        json={"baseline_label": "main-green", "gates": ["p95 <= baseline + 15%"]},
    ).json()

    assert result["ok"] is False
    assert any(not g["passed"] for g in result["gates"])
    assert "FAIL" in result["report"]


def test_comparing_an_invalid_candidate_is_blocked(client):
    baseline_run = seed_run(p95=1000.0)
    client.post("/api/baselines", json={"run_id": baseline_run, "label": "main-green"})
    candidate = seed_run(summary_valid=False)

    result = client.post(
        f"/api/runs/{candidate}/compare",
        json={"baseline_label": "main-green", "gates": ["p95 <= baseline + 15%"]},
    ).json()

    assert result["ok"] is False
    assert result["blocked"]
    assert result["gates"] == []


def test_comparing_without_a_baseline_uses_absolute_gates(client):
    candidate = seed_run(p95=900.0)
    result = client.post(
        f"/api/runs/{candidate}/compare", json={"gates": ["p95 <= 1000ms", "valid"]}
    ).json()
    assert result["ok"] is True


def test_an_unknown_baseline_label_is_a_404(client):
    candidate = seed_run()
    response = client.post(
        f"/api/runs/{candidate}/compare", json={"baseline_label": "nope", "gates": []}
    )
    assert response.status_code == 404


def test_a_nonsense_gate_is_rejected_with_an_explanation(client):
    candidate = seed_run()
    response = client.post(
        f"/api/runs/{candidate}/compare", json={"gates": ["p95 <= soon"]}
    )
    assert response.status_code == 422
    assert "cannot parse" in response.text


def test_a_run_that_has_not_finished_cannot_be_compared(client):
    with db.session_scope() as session:
        target = Target(name="pending-target", mgmt_url="https://x:8089")
        session.add(target)
        session.flush()
        run = Run(target_id=target.id, scenario="smoke", state="running")
        session.add(run)
        session.flush()
        run_id = run.id

    response = client.post(f"/api/runs/{run_id}/compare", json={"gates": []})
    assert response.status_code == 409
    assert "has not finished" in response.text
