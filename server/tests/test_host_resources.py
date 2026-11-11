"""A run carries what the target's machines were doing while it ran."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_api import client, make_target, splunkd, wait_for_terminal  # noqa: E402,F401 - the shared fixtures


def test_a_finished_run_says_what_its_hosts_were_doing(client, splunkd):  # noqa: F811
    target = make_target(client, splunkd)
    created = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 2, "duration_s": 3},
    )
    assert created.status_code == 201, created.text
    finished = wait_for_terminal(client, created.json()["id"])
    assert finished["state"] == "completed", finished.get("error")

    resources = finished["summary"]["resources"]
    assert resources["available"] is True, resources.get("reason")
    # Read after the run, in three searches, over the run with a lead and a tail.
    assert [q["name"] for q in resources["queries"]] == ["hostwide", "io_series", "io_mounts"]
    assert all(q["available"] for q in resources["queries"])
    assert resources["window"]["earliest"] < finished["started_at"] < resources["window"]["latest"]

    # The instance the run talked to is the search head, by its own name.
    host = resources["hosts"][0]
    assert host["host"] == "fake-splunk" and host["role"] == "search head"
    assert host["cores"] == 16 and host["cpu"]["avg_pct"] > 0
    assert host["io"]["read_mb_s"] == 20.0
    tier = resources["roles"]["search head"]
    assert tier["hosts"] == 1 and len(tier["t"]) == len(tier["cpu_avg"]) >= 2
    assert resources["mounts"][0]["mount"] == "/opt/splunk"
    assert any("1 search head: CPU averaged" in line for line in resources["findings"])


def test_a_target_that_will_not_say_does_not_fail_the_run(client, splunkd, monkeypatch):  # noqa: F811
    from regulator_agent.splunk import SplunkClient, SplunkError

    real = SplunkClient.oneshot

    async def oneshot(self, spl, window, count=100):
        if "splunk_resource_usage" in spl and "bin _time" in spl:
            raise SplunkError("HTTP 403: this account cannot search _introspection")
        return await real(self, spl, window, count=count)

    monkeypatch.setattr(SplunkClient, "oneshot", oneshot)
    target = make_target(client, splunkd)
    created = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 1, "duration_s": 2},
    )
    finished = wait_for_terminal(client, created.json()["id"])
    assert finished["state"] == "completed", finished.get("error")
    resources = finished["summary"]["resources"]
    assert resources["available"] is False and "403" in resources["reason"]
    assert finished["stats"]["executions"] > 0
