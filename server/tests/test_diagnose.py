"""Diagnosing a finished run: which searches had a problem, and why.

Two halves. Regulator's own record, which costs the target nothing and says
whether a "cancelled" search was cut off by Regulator itself; and three small
searches on the target, about the search ids in the run's ledger and nothing
else, made only once the run is over.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_api import client, make_target, wait_for_terminal  # noqa: E402,F401 - the shared app fixture
from test_warm_api import audit_rows  # noqa: E402

from regulator_server import diagnose  # noqa: E402

fake_splunk = pytest.importorskip("tools.fake_splunk", reason="the fake splunkd is not present")
FakeSplunk = fake_splunk.FakeSplunk

SCENARIO = {
    "name": "failing",
    "engine": "api",
    "seed": 7,
    "description": "one search, against a target that fails every one",
    "corpus": {"index": "main"},
    "time_policy": {"mode": "rolling", "window": "1h", "jitter": "0s", "align": "1m"},
    "personas": [{
        "name": "user", "weight": 100, "think_time": {"dist": "fixed", "value_s": 0.2},
        "steps": [{"id": "one", "type": "search", "engine": "api", "class": "dense", "spl": "search index=main"}],
    }],
    "load": {"model": "closed", "virtual_users": 1, "duration": "60s"},
}


@pytest.fixture
def failing():
    server = FakeSplunk(port=0, base_latency_ms=5.0, jitter_ms=0.0, dispatch_latency_ms=1.0, fail_rate=1.0)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def scenario(client):  # noqa: F811 - pytest fixture injection
    from regulator_server.config import get_settings

    directory = Path(get_settings().user_scenarios_dir) / "failing"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "scenario.yaml").write_text(yaml.safe_dump(SCENARIO), encoding="utf-8")
    return "failing"


def finished_run(client, target, scenario, duration_s=1):  # noqa: F811
    response = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": scenario, "virtual_users": 1, "duration_s": duration_s},
    )
    assert response.status_code == 201, response.text
    return wait_for_terminal(client, response.json()["id"])


# ----------------------------------------------------- Regulator's own record


def cut_off(sid, age_s, outcome="still_running"):
    return {"sid": sid, "outcome": outcome, "error_class": "cancelled", "service_time_ms": age_s * 1000.0}


def test_a_search_cut_off_at_the_end_is_named_as_regulators_own_limit():
    """The case that prompted this: 'searches over 200 seconds were cancelled'."""
    stats = {
        "unfinished": {"count": 3, "finished_after_end": 1, "still_running": 2, "run_duration": {"max_ms": 251_000}},
        "errors_by_class": {},
        "searches": {"problems": [
            cut_off("a", 200, "finished_after_end"), cut_off("b", 204), cut_off("c", 199),
        ]},
    }
    said = " ".join(diagnose.own_findings(
        diagnose.ledger_of(stats), stats, state="completed", duration_s=140.0,
        drain_budget_s=60.0, harvest_s=240.0,
    ))
    assert "3 search(es) were still running" in said
    assert "a 140s run and the 60s allowed" in said and "REG_DRAIN_BUDGET_S" in said
    assert "between 199s and 204s" in said
    assert "Regulator's, not Splunk's" in said
    assert "1 of them finished" in said and "longest ran 251s" in said
    assert "2 were still running 240s after the run and were deleted" in said
    assert "REG_HARVEST_S" in said


def test_a_stopped_run_says_its_searches_were_deleted_at_once():
    stats = {"unfinished": {"count": 1}, "searches": {"problems": [cut_off("a", 30, "unfinished")]}}
    said = " ".join(diagnose.own_findings(
        diagnose.ledger_of(stats), stats, state="stopped", duration_s=600.0, drain_budget_s=60.0, harvest_s=240.0,
    ))
    assert "ended as stopped" in said and "deleted at once" in said
    assert "kept following" not in said


def test_failures_are_reported_by_class_with_what_they_most_often_said():
    problems = [
        {"sid": f"s{n}", "outcome": "failed", "error_class": "timeout", "error_detail": "read timed out after 300s",
         "service_time_ms": 300_000.0}
        for n in range(3)
    ] + [{"sid": "q", "outcome": "failed", "error_class": "quota", "error_detail": "HTTP 503", "service_time_ms": 20.0}]
    stats = {"errors_by_class": {"timeout": 3, "quota": 1}, "partial": 2, "searches": {"problems": problems, "dropped": {"failed:timeout": 5}}}
    said = diagnose.own_findings(
        diagnose.ledger_of(stats), stats, state="completed", duration_s=600.0, drain_budget_s=60.0, harvest_s=240.0,
    )
    assert said[0].startswith("3 search(es) failed as timeout after 300s.")
    assert '"read timed out after 300s"' in said[0]
    assert said[1].startswith("1 search(es) failed as quota")
    assert any("2 search(es) finished but said their results were incomplete" in line for line in said)
    assert any("5 more search(es)" in line for line in said)


def test_a_clean_run_says_so():
    said = diagnose.own_findings({}, {}, state="completed", duration_s=60.0, drain_budget_s=60.0, harvest_s=240.0)
    assert said == ["Regulator recorded no failed, unfinished or partial searches in this run."]


# ------------------------------------------------------- the search ids asked


def test_only_search_ids_that_are_safe_inside_a_quoted_operand_are_looked_up():
    ledger = {
        "problems": [{"sid": "reg_r1_0_ab12cd_3"}, {"sid": 'x" OR index=*'}, {"sid": "1700000000.123"}, {}, {"sid": "reg_r1_0_ab12cd_3"}],
        "slowest": [{"sid": "reg_r1_0_ab12cd_9"}],
    }
    assert diagnose.ledger_sids(ledger) == ["reg_r1_0_ab12cd_3", "1700000000.123", "reg_r1_0_ab12cd_9"]
    assert diagnose.ledger_sids(ledger, limit=1) == ["reg_r1_0_ab12cd_3"]
    spl = diagnose.audit_spl(["reg_r1_0_ab12cd_3"])
    assert 'search_id="*reg_r1_0_ab12cd_3*"' in spl and "index=_audit" in spl


def test_what_the_target_said_is_folded_by_search_id():
    sids = ["reg_r1_0_ab_1", "reg_r1_0_ab_12"]
    by_sid = diagnose.fold(
        sids,
        [
            {"sid": "reg_r1_0_ab_12", "info": "granted,canceled", "user": "svc_reg", "total_run_time": "203.4", "scan_count": "9000"},
            {"sid": "reg_r1_0_ab_1", "info": ["granted", "completed"], "total_run_time": "1.5"},
            {"sid": "somebody_elses", "info": "completed"},
        ],
        [
            {"search_id": "remote_sh1_reg_r1_0_ab_12", "host": "idx1", "elapsed_s": "180.2", "scan_count": "8000"},
            {"search_id": "remote_sh1_reg_r1_0_ab_12", "host": "idx2", "elapsed_s": "20"},
        ],
        [{"message": "search reg_r1_0_ab_12 hit a limit", "component": "DispatchManager", "log_level": "WARN", "count": "4", "host": "sh1"}],
    )
    # The longer id wins where one id is the start of another.
    assert by_sid["reg_r1_0_ab_12"]["audit"]["ended"] == "canceled"
    assert by_sid["reg_r1_0_ab_12"]["audit"]["total_run_time"] == 203.4
    assert by_sid["reg_r1_0_ab_12"]["audit"]["scan_count"] == 9000
    assert [row["host"] for row in by_sid["reg_r1_0_ab_12"]["indexers"]] == ["idx1", "idx2"]
    assert by_sid["reg_r1_0_ab_12"]["logs"][0]["component"] == "DispatchManager"
    assert by_sid["reg_r1_0_ab_1"]["audit"]["ended"] == "completed"
    assert "somebody_elses" not in by_sid


def test_a_cancel_is_attributed_to_regulator_only_when_regulator_deleted_the_search():
    ledger = {"problems": [{"sid": "a", "outcome": "still_running"}, {"sid": "b", "outcome": "partial"}]}
    ours = {"a": {"audit": {"ended": "canceled", "total_run_time": 301.0, "user": "svc_reg"}}}
    said = " ".join(diagnose.target_findings(ledger, ["a", "b"], ours, {"audit": True}, []))
    assert "1 canceled, 1 not found" in said
    assert "after 301s of run time, as svc_reg" in said
    assert "Regulator's delete and not a limit on the target" in said

    theirs = {"b": {"audit": {"ended": "canceled", "total_run_time": 200.0}}}
    said = " ".join(diagnose.target_findings(ledger, ["a", "b"], theirs, {"audit": True}, []))
    assert "Regulator did not delete them: something on the target did" in said


def test_an_indexer_far_slower_than_the_rest_is_named():
    by_sid = {"a": {"indexers": [
        {"host": "idx1", "elapsed_s": 10.0}, {"host": "idx2", "elapsed_s": 12.0}, {"host": "idx3", "elapsed_s": 190.0},
    ]}}
    said = " ".join(diagnose.target_findings({"slowest": [{"sid": "a", "outcome": "slow"}]}, ["a"], by_sid, {"indexers": True}, []))
    assert "Indexer idx3 spent 190s" in said and "median of 12s across 3 indexers" in said


# ------------------------------------------------------------- the whole way


def test_a_finished_run_keeps_a_ledger_and_is_diagnosed_from_it(client, failing, scenario, monkeypatch):  # noqa: F811
    target = make_target(client, failing)
    run = finished_run(client, target, scenario)
    assert run["state"] in ("completed", "aborted"), run.get("error")
    ledger = run["stats"]["searches"]
    assert ledger["problems"], "every search failed, so the ledger has them"
    entry = ledger["problems"][0]
    assert entry["outcome"] == "failed" and entry["error_class"] == "search_failed"
    assert entry["sid"].startswith("reg_") and entry["dispatch_state"] == "FAILED"
    sids = [e["sid"] for e in ledger["problems"]]

    from regulator_agent.splunk import SplunkClient

    asked = []

    async def oneshot(self, spl, window, count=100):
        asked.append((spl, window, count))
        if "index=_audit" in spl:
            return [{"sid": sids[0], "info": "granted,failed", "user": "admin", "total_run_time": "0.4"}], 0
        if "splunkd_remote_searches" in spl:
            return [{"search_id": f"remote_sh_{sids[0]}", "host": "idx1", "elapsed_s": "0.3", "scan_count": "10"}], 0
        return [{"message": f"{sids[0]} died", "component": "SearchProcess", "log_level": "ERROR", "count": "1"}], 0

    monkeypatch.setattr(SplunkClient, "oneshot", oneshot)
    response = client.post(f"/api/runs/{run['id']}/diagnose")
    assert response.status_code == 200, response.text
    body = response.json()

    # Three questions, each about the ledger's search ids and nothing else,
    # over the run's own window.
    assert [q["name"] for q in body["queries"]] == ["audit", "indexers", "logs"]
    assert all(q["available"] for q in body["queries"])
    assert len(asked) == 3 and all(sids[0] in spl for spl, _, _ in asked)
    assert all(window.earliest <= run["started_at"] and window.latest >= run["ended_at"] for _, window, _ in asked)
    assert body["self_load"]["queries"] == 3

    assert any("failed as search_failed" in line for line in body["own"])
    detail = body["searches"][sids[0]]
    assert detail["audit"]["ended"] == "failed"
    assert detail["indexers"][0]["host"] == "idx1"
    assert detail["logs"][0]["component"] == "SearchProcess"
    assert any("ended as failed on the target itself" in line for line in body["target"])

    # Kept on the run, so the page shows it again without asking again.
    again = client.get(f"/api/runs/{run['id']}").json()
    assert again["summary"]["diagnosis"]["searches"][sids[0]]["audit"]["ended"] == "failed"
    assert "target_web_url" in again
    row = next(r for r in audit_rows(client) if r["action"] == "run_diagnosed")
    assert f"run {run['id']}" in row["detail"]


def test_a_question_the_target_cannot_answer_is_recorded_not_raised(client, failing, scenario, monkeypatch):  # noqa: F811
    target = make_target(client, failing)
    run = finished_run(client, target, scenario)

    from regulator_agent.splunk import SplunkClient, SplunkError

    async def oneshot(self, spl, window, count=100):
        if "index=_audit" in spl:
            raise SplunkError("HTTP 403: not allowed to read _audit")
        return [], 0

    monkeypatch.setattr(SplunkClient, "oneshot", oneshot)
    body = client.post(f"/api/runs/{run['id']}/diagnose").json()
    audit = body["queries"][0]
    assert audit["available"] is False and "403" in audit["reason"]
    assert body["queries"][1]["available"] is True
    # Regulator's own record is still there.
    assert body["own"]


def test_a_run_is_not_diagnosed_while_anything_is_running_on_its_target(client, failing, scenario):  # noqa: F811
    target = make_target(client, failing)
    done = finished_run(client, target, scenario)

    live = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 1, "duration_s": 60},
    )
    assert live.status_code == 201, live.text
    try:
        own = client.post(f"/api/runs/{live.json()['id']}/diagnose")
        assert own.status_code == 409 and "still going" in own.text
        other = client.post(f"/api/runs/{done['id']}/diagnose")
        assert other.status_code == 409 and "in flight on this target" in other.text
    finally:
        client.post(f"/api/runs/{live.json()['id']}/stop")
        wait_for_terminal(client, live.json()["id"])
    assert client.post("/api/runs/999999/diagnose").status_code == 404


def test_a_run_from_before_the_ledger_is_told_so(client, failing, scenario):  # noqa: F811
    from regulator_server.db import session_scope
    from regulator_server.models import Run

    target = make_target(client, failing)
    run = finished_run(client, target, scenario)
    with session_scope() as session:
        row = session.get(Run, run["id"])
        row.stats_json = {k: v for k, v in (row.stats_json or {}).items() if k != "searches"}
        session.add(row)
    before = failing.stats.oneshots
    body = client.post(f"/api/runs/{run['id']}/diagnose").json()
    assert "kept no search ledger" in body["note"] and body["queries"] == []
    assert failing.stats.oneshots == before


def test_the_drain_and_follow_up_limits_in_force_are_the_ones_reported(client, failing, scenario):  # noqa: F811
    from regulator_server.config import get_settings, set_settings

    set_settings(dataclasses.replace(get_settings(), drain_budget_s=600.0, harvest_s=900.0))
    stats = {"unfinished": {"count": 1, "still_running": 1}, "searches": {"problems": [cut_off("reg_a_1", 700)]}}
    from regulator_server.db import session_scope
    from regulator_server.models import Run

    target = make_target(client, failing)
    run = finished_run(client, target, scenario)
    with session_scope() as session:
        row = session.get(Run, run["id"])
        row.stats_json = stats
        row.state = "completed"
        session.add(row)
    said = " ".join(client.post(f"/api/runs/{run['id']}/diagnose").json()["own"])
    assert "the 600s allowed for work in flight" in said and "900s after the run" in said
