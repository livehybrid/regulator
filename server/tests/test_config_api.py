"""The Configuration and scenario-archive endpoints, through the real app.

These drive the real FastAPI app and the real database, because the thing most
worth guarding is the round trip: what the Download button produces has to be
exactly what the Restore button and ``REG_CONFIG_IMPORT`` accept, and what a
scenario Download produces has to be what an Upload on another instance reads.
A unit test of either half alone would not catch the two drifting apart.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "worker"))
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO))

from regulator_server import db, scenariosource  # noqa: E402
from regulator_server.config import (  # noqa: E402
    ServerConfig,
    get_settings,
    new_master_key,
    set_settings,
)

CONF = """[Errors in the last hour]
search = index=main sourcetype=access_combined status>=500 | stats count by uri_path
dispatch.earliest_time = -1h
dispatch.latest_time = now
cron_schedule = */5 * * * *
enableSched = 1
"""


def make_client(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("REG_POLL_INITIAL_MS", "10")
    settings = dict(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        master_key=new_master_key(),
        master_key_generated=False,
        admin_password=None,
        session_ttl_s=3600,
        scenarios_dir=str(REPO / "scenarios"),
        user_scenarios_dir=str(tmp_path / "user-scenarios"),
        max_virtual_users=50,
        max_concurrent_runs=2,
        allow_unauthenticated=True,
    )
    settings.update(overrides)
    set_settings(ServerConfig(**settings))
    db.reset_engine()
    from regulator_server.app import create_app

    return TestClient(create_app())


@pytest.fixture
def client(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as test_client:
        yield test_client
    db.reset_engine()
    set_settings(None)


def make_target(client, name="prod-sh"):
    response = client.post(
        "/api/targets",
        json={
            "name": name,
            "mgmt_url": "https://sh.example:8089",
            "token": "a-secret-bearer-token",
            "verify_tls": False,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_scenario(client, name="imported"):
    response = client.post(
        "/api/scenarios",
        json={
            "name": name,
            "savedsearches": CONF,
            "load_model": "closed",
            "virtual_users": 2,
            "duration_s": 60,
            "index": "main",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Config download and restore
# --------------------------------------------------------------------------- #

def test_the_download_never_carries_a_credential_in_clear(client):
    make_target(client)
    response = client.get("/api/config/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].endswith('filename="regulator-config.json"')
    # A file full of splunkd credentials must not sit in a browser cache.
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-regulator-config-targets"] == "1"
    body = response.text
    assert "a-secret-bearer-token" not in body
    document = json.loads(body)
    assert document["targets"][0]["name"] == "prod-sh"
    # What travels is the ciphertext, which is useless without the master key.
    assert document["targets"][0]["token_encrypted"]
    assert document["targets"][0]["token_encrypted"] != "a-secret-bearer-token"


def test_the_sanitised_download_is_safe_to_commit(client):
    make_target(client)
    response = client.get("/api/config/export", params={"secrets": "exclude"})
    assert response.status_code == 200
    assert response.headers["x-regulator-config-secrets"] == "excluded"
    document = json.loads(response.text)
    assert document["includes_secrets"] is False
    assert "token_encrypted" not in document["targets"][0]
    assert document["targets"][0]["mgmt_url"] == "https://sh.example:8089"


def test_a_round_trip_restores_onto_a_second_instance(client, tmp_path, monkeypatch):
    """The property the whole feature exists for.

    Configure an instance, download the JSON, then bring a *different* instance
    up from it and have the same targets and scenarios there.
    """
    make_target(client)
    make_scenario(client, "imported")
    document = client.get("/api/config/export").json()
    assert len(document["targets"]) == 1
    assert [s["name"] for s in document["scenarios"]] == ["imported"]
    # The same master key, which is what a Kubernetes Secret gives you across
    # the volume teardown this exists for. Read before the second client
    # replaces the process-wide settings.
    key = get_settings().master_key

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    with make_client(second_dir, monkeypatch, master_key=key) as second:
        assert second.get("/api/targets").json() == []
        report = second.post("/api/config/import", json=document)
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["targets"] == 1 and body["scenarios"] == 1
        assert not body["warnings"], body["warnings"]
        names = [t["name"] for t in second.get("/api/targets").json()]
        assert names == ["prod-sh"]
        scenarios = [s["name"] for s in second.get("/api/scenarios").json()]
        assert "imported" in scenarios
    db.reset_engine()


def test_restoring_under_a_different_master_key_says_so(client, tmp_path, monkeypatch):
    make_target(client)
    document = client.get("/api/config/export").json()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    with make_client(second_dir, monkeypatch) as second:  # a fresh, different key
        body = second.post("/api/config/import", json=document).json()
        assert body["targets"] == 1
        assert any("cannot be decrypted" in w for w in body["warnings"])
    db.reset_engine()


def test_the_restore_is_idempotent_through_the_api(client):
    make_target(client)
    document = client.get("/api/config/export").json()
    first = client.post("/api/config/import", json=document).json()
    second = client.post("/api/config/import", json=document).json()
    assert first["targets_updated"] == 1  # the target it came from
    assert second["targets"] == 0 and second["targets_updated"] == 1
    assert len(client.get("/api/targets").json()) == 1


def test_an_export_appears_in_the_audit_log(client):
    make_target(client)
    client.get("/api/config/export")
    actions = [event["action"] for event in client.get("/api/audit").json()["events"]]
    assert "config_exported" in actions


# --------------------------------------------------------------------------- #
# Scenario archives
# --------------------------------------------------------------------------- #

def test_a_scenario_round_trips_through_download_and_upload(client, tmp_path, monkeypatch):
    make_scenario(client, "imported")
    download = client.get("/api/scenarios/imported/export")
    assert download.status_code == 200, download.text
    assert download.headers["content-type"] == "application/gzip"
    assert 'filename="imported.tar.gz"' in download.headers["content-disposition"]
    archive = download.content

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    with make_client(second_dir, monkeypatch) as second:
        assert "imported" not in [s["name"] for s in second.get("/api/scenarios").json()]
        upload = second.post(
            "/api/scenarios/upload",
            files={"file": ("imported.tar.gz", archive, "application/gzip")},
        )
        assert upload.status_code == 201, upload.text
        assert upload.json()["name"] == "imported"
        landed = {s["name"]: s for s in second.get("/api/scenarios").json()}
        assert landed["imported"]["origin"] == "user"
        # And it is a real scenario on the far side, not just files on disk.
        detail = second.get("/api/scenarios/imported").json()
        assert detail["steps"]
        assert "savedsearches.conf" in detail["files"]
    db.reset_engine()


def test_a_builtin_scenario_can_be_downloaded_too(client):
    """Copying a shipped scenario to another instance and editing it there is
    a reasonable thing to want."""
    builtin = [s for s in client.get("/api/scenarios").json() if s["origin"] == "builtin"]
    if not builtin:
        pytest.skip("this checkout ships no built-in scenarios")
    response = client.get(f"/api/scenarios/{builtin[0]['name']}/export")
    assert response.status_code == 200
    assert response.content[:2] == b"\x1f\x8b"


def test_uploading_over_an_existing_scenario_needs_replace(client):
    make_scenario(client, "imported")
    archive = client.get("/api/scenarios/imported/export").content

    refused = client.post(
        "/api/scenarios/upload", files={"file": ("imported.tar.gz", archive, "application/gzip")}
    )
    assert refused.status_code == 422
    assert "already exists" in refused.json()["detail"]

    allowed = client.post(
        "/api/scenarios/upload",
        files={"file": ("imported.tar.gz", archive, "application/gzip")},
        data={"replace": "true"},
    )
    assert allowed.status_code == 201, allowed.text


def test_an_archive_that_extracts_but_does_not_load_is_refused(client, tmp_path):
    """Better to fail at upload than to sit in the library and fail at launch."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        payload = b"name: broken\nthis is not a scenario at all\n"
        info = tarfile.TarInfo("broken/scenario.yaml")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    response = client.post(
        "/api/scenarios/upload",
        files={"file": ("broken.tar.gz", buffer.getvalue(), "application/gzip")},
    )
    assert response.status_code == 422
    assert "does not load" in response.json()["detail"] or "does not lint" in response.json()["detail"]
    # And nothing was left in the library to trip over later.
    assert "broken" not in [s["name"] for s in client.get("/api/scenarios").json()]


def test_an_oversized_upload_is_refused_before_it_is_parsed(client, tmp_path, monkeypatch):
    small_dir = tmp_path / "small"
    small_dir.mkdir()
    with make_client(small_dir, monkeypatch, scenario_upload_max_archive_bytes=1024) as small:
        response = small.post(
            "/api/scenarios/upload",
            files={"file": ("big.tar.gz", b"\x1f\x8b" + b"x" * 4096, "application/gzip")},
        )
        assert response.status_code == 413
        assert "upload limit" in response.json()["detail"]
    db.reset_engine()


def test_an_empty_upload_is_refused(client):
    response = client.post(
        "/api/scenarios/upload", files={"file": ("empty.tar.gz", b"", "application/gzip")}
    )
    assert response.status_code == 422


def test_an_upload_appears_in_the_audit_log(client):
    make_scenario(client, "imported")
    archive = client.get("/api/scenarios/imported/export").content
    client.post(
        "/api/scenarios/upload",
        files={"file": ("imported.tar.gz", archive, "application/gzip")},
        data={"replace": "true"},
    )
    actions = [event["action"] for event in client.get("/api/audit").json()["events"]]
    assert "scenario_uploaded" in actions


# --------------------------------------------------------------------------- #
# The scenario source
# --------------------------------------------------------------------------- #

def test_the_source_endpoint_reports_nothing_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv(scenariosource.SOURCE_ENV, raising=False)
    monkeypatch.delenv(scenariosource.WRITE_ENV, raising=False)
    body = client.get("/api/scenario-source").json()
    assert body == {
        "configured": False,
        "writable": False,
        "kind": None,
        "location": None,
        "error": None,
    }


def test_publish_refuses_a_missing_or_read_only_source(client, monkeypatch, tmp_path):
    make_scenario(client, "imported")
    monkeypatch.delenv(scenariosource.SOURCE_ENV, raising=False)
    monkeypatch.delenv(scenariosource.WRITE_ENV, raising=False)
    refused = client.post("/api/scenarios/imported/publish")
    assert refused.status_code == 409
    assert scenariosource.SOURCE_ENV in refused.json()["detail"]

    monkeypatch.setenv(scenariosource.SOURCE_ENV, str(tmp_path / "bucket"))
    read_only = client.post("/api/scenarios/imported/publish")
    assert read_only.status_code == 409
    assert "read-only" in read_only.json()["detail"]


def test_publish_writes_an_archive_the_sync_can_read(client, monkeypatch, tmp_path):
    bucket = tmp_path / "bucket"
    monkeypatch.setenv(scenariosource.SOURCE_ENV, str(bucket))
    monkeypatch.setenv(scenariosource.WRITE_ENV, "1")
    make_scenario(client, "imported")

    info = client.get("/api/scenario-source").json()
    assert info["configured"] and info["writable"] and info["kind"] == "directory"

    response = client.post("/api/scenarios/imported/publish")
    assert response.status_code == 200, response.text
    assert response.json()["key"] == "imported.tar.gz"
    assert (bucket / "imported.tar.gz").is_file()

    # The whole point: a second instance pointed at the same prefix comes up
    # with the scenario already present and no operator step at all.
    report = scenariosource.sync_from_store(
        scenariosource.DirectoryStore(str(bucket)), library=tmp_path / "second-library"
    )
    assert report["imported"] == ["imported"]


def test_creating_a_scenario_mirrors_it_when_writes_are_on(client, monkeypatch, tmp_path):
    bucket = tmp_path / "bucket"
    monkeypatch.setenv(scenariosource.SOURCE_ENV, str(bucket))
    monkeypatch.setenv(scenariosource.WRITE_ENV, "1")
    make_scenario(client, "auto-mirrored")
    assert (bucket / "auto-mirrored.tar.gz").is_file()


def test_publishing_an_unknown_scenario_is_a_404(client, monkeypatch, tmp_path):
    monkeypatch.setenv(scenariosource.SOURCE_ENV, str(tmp_path / "bucket"))
    monkeypatch.setenv(scenariosource.WRITE_ENV, "1")
    assert client.post("/api/scenarios/no-such-thing/publish").status_code == 404
