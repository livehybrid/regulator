"""Renaming a scenario: its directory, its scenario.yaml, its history, its bucket copy.

The name is the scenario's identity everywhere: the directory, the ``name``
key, the scenario-source key and the string every run and baseline stores.
A rename has to move all four or something is left pointing at nothing.
"""

from __future__ import annotations

import time

import pytest
import yaml

from test_scenarios_api import CONF, client  # noqa: F401 - fixture; sets sys.path

from regulator_server import db, scenariosource  # noqa: E402
from regulator_server.models import Baseline, Run, Target  # noqa: E402


def _create(client, name="before"):
    response = client.post("/api/scenarios", json={"name": name, "savedsearches": CONF})
    assert response.status_code == 201, response.text
    return response.json()


def _run(scenario, state="completed"):
    with db.session_scope() as session:
        target = session.query(Target).first()
        if target is None:
            target = Target(name="t", mgmt_url="https://x:8089", token_encrypted=None)
            session.add(target)
            session.flush()
        run = Run(target_id=target.id, scenario=scenario, state=state,
                  started_at=time.time() - 10, ended_at=time.time())
        session.add(run)
        session.flush()
        return run.id


def _rename(client, old, new):
    return client.post(f"/api/scenarios/{old}/rename", json={"name": new})


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    path = tmp_path / "bucket"
    path.mkdir()
    monkeypatch.setenv(scenariosource.SOURCE_ENV, str(path))
    monkeypatch.setenv(scenariosource.WRITE_ENV, "1")
    return path


def test_a_rename_moves_the_directory_and_the_yaml_name(client, tmp_path):
    _create(client)
    response = _rename(client, "before", "after")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "after" and body["old_name"] == "before"

    library = tmp_path / "user-scenarios"
    assert not (library / "before").exists()
    assert yaml.safe_load((library / "after" / "scenario.yaml").read_text())["name"] == "after"
    assert client.get("/api/scenarios/before").status_code == 404
    assert client.get("/api/scenarios/after").status_code == 200
    # No staging directory is left behind.
    assert [p.name for p in library.iterdir() if p.name.startswith(".")] == []


def test_runs_and_baselines_follow_the_rename(client):
    _create(client)
    run_id = _run("before")
    other = _run("someone-else")
    with db.session_scope() as session:
        session.add(Baseline(label="green", run_id=run_id, scenario="before"))

    body = _rename(client, "before", "after").json()
    assert body["runs_updated"] == 1 and body["baselines_updated"] == 1
    with db.session_scope() as session:
        assert session.get(Run, run_id).scenario == "after"
        assert session.get(Run, other).scenario == "someone-else"
        assert session.get(Baseline, "green").scenario == "after"


def test_a_scenario_with_an_unfinished_run_is_not_renamed(client, tmp_path):
    _create(client)
    _run("before", state="running")
    response = _rename(client, "before", "after")
    assert response.status_code == 409, response.text
    assert (tmp_path / "user-scenarios" / "before").is_dir()


def test_a_taken_name_is_refused(client):
    _create(client, "first")
    _create(client, "second")
    assert _rename(client, "first", "second").status_code == 409


def test_a_built_in_name_is_refused_because_it_would_shadow(client):
    _create(client)
    builtin = next(s["name"] for s in client.get("/api/scenarios").json() if s["origin"] == "builtin")
    assert _rename(client, "before", builtin).status_code == 409


def test_a_built_in_cannot_be_renamed(client):
    builtin = next(s["name"] for s in client.get("/api/scenarios").json() if s["origin"] == "builtin")
    response = _rename(client, builtin, "mine-now")
    assert response.status_code == 403 and "duplicate" in response.json()["detail"]


@pytest.mark.parametrize("name", ["", "Upper", "has space", "../escape", "x" * 65])
def test_unsafe_names_are_refused(client, name):
    _create(client)
    assert _rename(client, "before", name).status_code == 422


def test_the_same_name_is_refused(client):
    _create(client)
    assert _rename(client, "before", "before").status_code == 422


def test_unknown_scenario_is_404(client):
    assert _rename(client, "nope", "still-nope").status_code == 404


def test_the_bucket_copy_moves_with_it(client, bucket):
    _create(client)
    assert (bucket / "before.tar.gz").exists()
    body = _rename(client, "before", "after").json()
    assert body["remote_key"] == "after.tar.gz"
    assert body["old_remote_key"] == "before.tar.gz" and body["old_remote_removed"] is True
    assert body["warning"] is None
    assert (bucket / "after.tar.gz").exists()
    assert not (bucket / "before.tar.gz").exists()


def test_the_boot_sync_does_not_bring_the_old_name_back(client, bucket):
    _create(client)
    _rename(client, "before", "after")
    scenariosource.load_from_env()
    names = {s["name"] for s in client.get("/api/scenarios").json() if s["origin"] == "user"}
    assert names == {"after"}


def test_a_failed_old_key_delete_is_a_warning(client, bucket, monkeypatch):
    _create(client)

    def refuse(self, key):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(scenariosource.DirectoryStore, "delete", refuse)
    response = _rename(client, "before", "after")
    assert response.status_code == 200, response.text
    assert "before.tar.gz" in response.json()["warning"]
    assert client.get("/api/scenarios/after").status_code == 200


def test_a_read_only_source_warns(client, bucket, monkeypatch):
    _create(client)
    monkeypatch.delenv(scenariosource.WRITE_ENV)
    response = _rename(client, "before", "after")
    assert response.status_code == 200
    assert "read-only" in response.json()["warning"]
