"""What a run was asked to do, as the runs list and run page show it.

A run launched without overrides stores no virtual users or duration of its
own, so the list could not say that one run had one user and the next two,
and a running run could not say how long it had left.
"""

from __future__ import annotations

from test_api import client, make_target, splunkd, wait_for_terminal  # noqa: F401


def _launch(client, target, **load):
    body = {"target_id": target["id"], "scenario": "smoke", **load}
    response = client.post("/api/runs", json=body)
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _listed(client, run_id):
    return next(r for r in client.get("/api/runs").json() if r["id"] == run_id)


def test_an_override_is_what_the_list_shows(client, splunkd):
    target = make_target(client, splunkd)
    run_id = _launch(client, target, virtual_users=1, duration_s=2)
    wait_for_terminal(client, run_id)
    planned = _listed(client, run_id)["planned"]
    assert planned["virtual_users"] == 1
    assert planned["duration_s"] == 2
    assert planned["load_model"] == "closed"
    assert planned["workers"] == 1


def test_the_scenario_default_fills_in_what_the_launch_left_out(client, splunkd):
    target = make_target(client, splunkd)
    run_id = _launch(client, target, duration_s=2)   # smoke's own load: 2 users
    wait_for_terminal(client, run_id)
    listed = _listed(client, run_id)
    assert listed["virtual_users"] is None            # nothing stored on the run
    assert listed["planned"]["virtual_users"] == 2
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["planned"]["virtual_users"] == 2
    assert detail["planned"]["duration_s"] == 2


def test_an_open_model_run_reports_its_rate_not_users(client, splunkd):
    target = make_target(client, splunkd)
    run_id = _launch(client, target, arrival_rate_per_min=30, duration_s=2)
    wait_for_terminal(client, run_id)
    planned = _listed(client, run_id)["planned"]
    assert planned["load_model"] == "open"
    assert planned["arrival_rate_per_min"] == 30
    assert planned["virtual_users"] is None


def test_the_list_carries_the_latency_spread(client, splunkd):
    target = make_target(client, splunkd)
    run_id = _launch(client, target, virtual_users=1, duration_s=2)
    wait_for_terminal(client, run_id)
    headline = _listed(client, run_id)["headline"]
    for key in ("p50_ms", "p95_ms", "p99_ms", "elapsed_s", "peak_virtual_users"):
        assert key in headline
