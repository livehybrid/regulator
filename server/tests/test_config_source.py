"""The shared config copy and run history in REG_CONFIG_SOURCE.

The trip that matters is an engineer's: configure an instance and run a test,
have both land in the bucket, then bring up a rebuilt instance and find it
configured, with the run history and a baseline the CI gate can still compare
against. The failure that matters most is quieter: a push that deletes from the
shared copy something this instance merely could not apply.
"""

from __future__ import annotations

import json
import time

import pytest

from test_config_api import _finished_run, make_client, make_scenario, make_target

from regulator_server import configsource, db  # noqa: E402
from regulator_server.config import set_settings  # noqa: E402
from regulator_server.models import Run, RunSample  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    configsource._reset_for_tests()
    # Never start the background publisher in a test: the tests push by hand.
    monkeypatch.setattr(configsource, "start_publisher", lambda settings=None: False)
    yield
    configsource._reset_for_tests()


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    path = tmp_path / "bucket"
    monkeypatch.setenv(configsource.SOURCE_ENV, str(path))
    monkeypatch.setenv(configsource.WRITE_ENV, "1")
    return path


def _instance(tmp_path, monkeypatch, name):
    """A control plane with its own database and library: a rebuild is a new one."""
    root = tmp_path / name
    root.mkdir()
    return make_client(root, monkeypatch)


def _close(client):
    client.__exit__(None, None, None)
    db.reset_engine()
    set_settings(None)


def _with_samples(run_id):
    with db.session_scope() as session:
        for second in range(3):
            session.add(RunSample(
                run_id=run_id, slot=None, at=1_700_000_000.0 + second * 5,
                elapsed_s=second * 5.0, executions=10 * (second + 1), errors=0,
                interval_json={"p95_ms": 1200 + second}, cum_json={"p95_ms": 1300},
            ))


def test_a_rebuilt_instance_comes_back_with_its_config_history_and_baselines(
        tmp_path, monkeypatch, bucket):
    first = _instance(tmp_path, monkeypatch, "first").__enter__()
    try:
        run_id = _finished_run(first)
        _with_samples(run_id)
        assert first.post("/api/baselines", json={"run_id": run_id, "label": "main-green"}).status_code == 201
        pushed = first.post("/api/config/source/push")
        assert pushed.status_code == 200, pushed.text
        assert pushed.json()["runs"] == 1
        uid = first.get(f"/api/runs/{run_id}").json()["uid"]
    finally:
        _close(first)

    doc = json.loads((bucket / configsource.CONFIG_OBJECT).read_text())
    assert doc["baselines"] == [{
        "label": "main-green", "run_uid": uid, "scenario": "exported",
        "target": "export-target", "note": None, "created_at": doc["baselines"][0]["created_at"],
    }]
    assert (bucket / "runs" / f"{uid}.json").is_file()

    # A new database, a new library, the same bucket: pulled at boot.
    rebuilt = _instance(tmp_path, monkeypatch, "rebuilt").__enter__()
    try:
        assert [t["name"] for t in rebuilt.get("/api/targets").json()] == ["export-target"]
        assert "exported" in {s["name"] for s in rebuilt.get("/api/scenarios").json()}
        runs = rebuilt.get("/api/runs").json()
        assert len(runs) == 1 and runs[0]["uid"] == uid
        new_id = runs[0]["id"]
        detail = rebuilt.get(f"/api/runs/{new_id}").json()
        assert detail["summary"]["steps"][0]["executions"] == 120
        assert detail["target_name"] == "export-target"
        samples = rebuilt.get(f"/api/runs/{new_id}/samples").json()["samples"]
        assert [s["executions"] for s in samples] == [10, 20, 30]
        baselines = rebuilt.get("/api/baselines").json()
        assert baselines == [{
            "label": "main-green", "run_id": new_id, "scenario": "exported",
            "target_id": detail["target_id"], "created_at": baselines[0]["created_at"],
            "note": None,
        }]
        # The CI gate still has something to compare against.
        compare = rebuilt.post(f"/api/runs/{new_id}/compare", json={"baseline_label": "main-green"})
        assert compare.status_code == 200, compare.text

        # Pulling again imports nothing twice.
        again = rebuilt.post("/api/config/source/pull").json()
        assert again["runs"]["imported"] == 0 and again["runs"]["present"] == 1
        assert len(rebuilt.get("/api/runs").json()) == 1
    finally:
        _close(rebuilt)


def test_nothing_is_pushed_until_something_changes(tmp_path, monkeypatch, bucket):
    client = _instance(tmp_path, monkeypatch, "one").__enter__()
    try:
        make_target(client)
        with db.session_scope() as session:
            assert configsource.maybe_push(session) is True
        stamp = json.loads((bucket / configsource.CONFIG_OBJECT).read_text())["exported_at"]
        with db.session_scope() as session:
            assert configsource.maybe_push(session) is False
        assert json.loads((bucket / configsource.CONFIG_OBJECT).read_text())["exported_at"] == stamp

        make_target(client, "second-sh")
        with db.session_scope() as session:
            assert configsource.maybe_push(session) is True
        names = {t["name"] for t in json.loads((bucket / configsource.CONFIG_OBJECT).read_text())["targets"]}
        assert names == {"prod-sh", "second-sh"}
    finally:
        _close(client)


def test_a_run_is_archived_once_it_has_settled(tmp_path, monkeypatch, bucket):
    client = _instance(tmp_path, monkeypatch, "one").__enter__()
    try:
        run_id = _finished_run(client)
        with db.session_scope() as session:
            run = session.get(Run, run_id)
            # Just finished: the last samples and the cluster's account may
            # still be arriving.
            run.ended_at = time.time()
        with db.session_scope() as session:
            configsource.maybe_push(session)
        assert not (bucket / "runs").exists() or not list((bucket / "runs").iterdir())

        with db.session_scope() as session:
            session.get(Run, run_id).ended_at = 1_700_000_060.0
        with db.session_scope() as session:
            assert configsource.maybe_push(session) is True
        assert len(list((bucket / "runs").iterdir())) == 1
    finally:
        _close(client)


def test_a_read_only_instance_never_writes(tmp_path, monkeypatch, bucket):
    monkeypatch.delenv(configsource.WRITE_ENV)
    client = _instance(tmp_path, monkeypatch, "one").__enter__()
    try:
        make_target(client)
        info = client.get("/api/config/source").json()
        assert info["configured"] is True and info["writable"] is False
        assert client.post("/api/config/source/push").status_code == 409
        with db.session_scope() as session:
            assert configsource.maybe_push(session) is False
        assert not (bucket / configsource.CONFIG_OBJECT).exists()
    finally:
        _close(client)


def test_deleting_a_run_removes_its_archive(tmp_path, monkeypatch, bucket):
    client = _instance(tmp_path, monkeypatch, "one").__enter__()
    try:
        run_id = _finished_run(client)
        client.post("/api/config/source/push")
        assert len(list((bucket / "runs").iterdir())) == 1
        assert client.delete(f"/api/runs/{run_id}").status_code == 204
        # Otherwise the next pull would bring it straight back.
        assert list((bucket / "runs").iterdir()) == []
    finally:
        _close(client)


def test_a_baseline_whose_run_is_not_here_is_carried_not_dropped(tmp_path, monkeypatch, bucket):
    bucket.mkdir()
    (bucket / configsource.CONFIG_OBJECT).write_text(json.dumps({
        "regulator_config_version": 1,
        "targets": [{"name": "prod-sh", "mgmt_url": "https://sh.example:8089"}],
        "scenarios": [],
        "baselines": [{"label": "main-green", "run_uid": "f" * 32, "scenario": "x"}],
    }))
    client = _instance(tmp_path, monkeypatch, "one").__enter__()
    try:
        info = client.get("/api/config/source").json()
        assert info["carried"] == 1
        assert client.get("/api/baselines").json() == []

        make_target(client, "an-unrelated-edit")
        with db.session_scope() as session:
            assert configsource.maybe_push(session) is True
        doc = json.loads((bucket / configsource.CONFIG_OBJECT).read_text())
        assert [b["label"] for b in doc["baselines"]] == ["main-green"]
    finally:
        _close(client)


def test_an_empty_bucket_is_not_an_error(tmp_path, monkeypatch, bucket):
    client = _instance(tmp_path, monkeypatch, "one").__enter__()
    try:
        pulled = client.post("/api/config/source/pull")
        assert pulled.status_code == 200, pulled.text
        assert pulled.json()["found"] is False
        assert client.get("/api/config/source").json()["last_pull"]["ok"] is True
    finally:
        _close(client)


def test_an_unusable_source_never_stops_boot(tmp_path, monkeypatch):
    monkeypatch.setenv(configsource.SOURCE_ENV, "ftp://nowhere/at-all")
    client = _instance(tmp_path, monkeypatch, "one").__enter__()
    try:
        info = client.get("/api/config/source").json()
        assert info["error"]
        assert client.post("/api/config/source/pull").status_code == 409
    finally:
        _close(client)


def test_runs_from_before_uids_get_one(tmp_path, monkeypatch):
    client = _instance(tmp_path, monkeypatch, "one").__enter__()
    try:
        run_id = _finished_run(client)
        with db.session_scope() as session:
            session.get(Run, run_id).uid = None
        db.reset_engine()
        db.init_engine()
        with db.session_scope() as session:
            assert session.get(Run, run_id).uid
    finally:
        _close(client)


def test_a_runs_note_and_controls_survive_the_trip(tmp_path, monkeypatch, bucket):
    first = _instance(tmp_path, monkeypatch, "first").__enter__()
    try:
        run_id = _finished_run(first)
        with db.session_scope() as session:
            run = session.get(Run, run_id)
            run.note = "indexer 3 replaced"
            run.paused_s = 42.0
            run.controls_json = [
                {"at": 1_700_000_010.0, "kind": "pause"},
                {"at": 1_700_000_052.0, "kind": "resume"},
                {"at": 1_700_000_055.0, "kind": "load", "virtual_users": 8},
            ]
        assert first.post("/api/config/source/push").status_code == 200
    finally:
        _close(first)

    rebuilt = _instance(tmp_path, monkeypatch, "rebuilt").__enter__()
    try:
        pulled = rebuilt.get("/api/runs").json()[0]
        detail = rebuilt.get(f"/api/runs/{pulled['id']}").json()
        assert detail["note"] == "indexer 3 replaced"
        assert detail["paused_s"] == 42.0
        labels = [m["label"] for m in detail["markers"] if m["kind"] in ("pause", "resume", "load")]
        assert labels == ["paused", "resumed", "8 users"]
    finally:
        _close(rebuilt)
