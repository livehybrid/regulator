"""Config download and restore, and scenarios as portable archives.

The behaviour that matters is what survives a teardown. A restore has to work
onto an *empty* instance, under a *different* master key without pretending the
credentials still work, and it has to be safe to run twice. The archive half is
tested for the things an untrusted archive tries: escaping the library, being a
link, lying about its size.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "worker"))
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO))

from regulator_server import configio, db, scenarioarchive, scenariosource  # noqa: E402
from regulator_server.config import ServerConfig, new_master_key, set_settings  # noqa: E402
from regulator_server.models import Base, Target  # noqa: E402


@pytest.fixture
def user_library(tmp_path):
    """Settings pointed at an empty operator library and a throwaway database.

    The built-in library is pointed at an empty directory too: these tests are
    about what the operator authored, and a shipped scenario turning up in an
    export would make every count here depend on the contents of scenarios/.
    """
    builtin = tmp_path / "builtin-scenarios"
    builtin.mkdir()
    library = tmp_path / "user-scenarios"
    library.mkdir()
    set_settings(
        ServerConfig(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            master_key=new_master_key(),
            master_key_generated=False,
            admin_password=None,
            session_ttl_s=3600,
            scenarios_dir=str(builtin),
            user_scenarios_dir=str(library),
            max_virtual_users=50,
            max_concurrent_runs=2,
            allow_unauthenticated=True,
        )
    )
    db.reset_engine()
    try:
        yield library
    finally:
        db.reset_engine()
        set_settings(None)


@pytest.fixture
def session(user_library):
    engine = db.init_engine()
    Base.metadata.create_all(engine)
    with db.session_scope() as active:
        yield active

SCENARIO_YAML = """\
name: {name}
engine: api
seed: 424242
description: "a scenario for the tests"
corpus:
  index: main
  sourcetypes: [access_combined]
time_policy:
  mode: rolling
  window: 24h
personas:
  - name: analyst
    weight: 100
    think_time: {{dist: lognormal, median_s: 1, sigma: 0.5, min_s: 1, max_s: 60}}
    steps:
      - id: one
        type: search
        spl: "search index=main | head 1"
load:
  model: closed
  virtual_users: 1
  duration: 10s
"""


def _write_scenario(root, name="demo", extra=None):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "scenario.yaml").write_text(SCENARIO_YAML.format(name=name), encoding="utf-8")
    for filename, text in (extra or {}).items():
        (directory / filename).write_text(text, encoding="utf-8")
    return directory


# --------------------------------------------------------------------------- #
# Export / import
# --------------------------------------------------------------------------- #

def test_export_carries_targets_and_scenarios(session, user_library):
    session.add(
        Target(
            name="prod-sh",
            mgmt_url="https://sh.example:8089",
            username="svc",
            token_encrypted="gAAAAA-not-a-real-token",
            password_encrypted="gAAAAA-not-a-real-password",
            verify_tls=False,
            app="search",
        )
    )
    session.flush()
    _write_scenario(user_library, "demo", {"savedsearches.conf": "[one]\nsearch = index=main\n"})

    doc = configio.export_config(session)

    assert doc["regulator_config_version"] == configio.CONFIG_VERSION
    assert doc["includes_secrets"] is True
    assert [t["name"] for t in doc["targets"]] == ["prod-sh"]
    target = doc["targets"][0]
    assert target["mgmt_url"] == "https://sh.example:8089"
    assert target["verify_tls"] is False
    # The credential travels as the ciphertext it is stored as, never in clear.
    assert target["token_encrypted"] == "gAAAAA-not-a-real-token"
    assert "password" not in target  # only the *_encrypted column exists
    assert [s["name"] for s in doc["scenarios"]] == ["demo"]
    assert "savedsearches.conf" in doc["scenarios"][0]["files"]


def test_export_without_secrets_omits_every_credential(session, user_library):
    session.add(
        Target(
            name="prod-sh",
            mgmt_url="https://sh.example:8089",
            token_encrypted="gAAAAA-secret",
            password_encrypted="gAAAAA-secret",
            indexer_token_encrypted="gAAAAA-secret",
            indexer_password_encrypted="gAAAAA-secret",
        )
    )
    session.flush()

    doc = configio.export_config(session, include_secrets=False)

    assert doc["includes_secrets"] is False
    serialised = json.dumps(doc)
    assert "gAAAAA-secret" not in serialised
    for field_name in configio.TARGET_SECRETS:
        assert field_name not in doc["targets"][0]
    # ...but what is left still describes the target well enough to restore it.
    assert doc["targets"][0]["mgmt_url"] == "https://sh.example:8089"


def test_the_builtin_library_is_not_exported(session, user_library):
    """Built-in scenarios ship in the image.

    Exporting them would write the shipped copies into a restore and shadow
    the newer ones after an upgrade, which is the opposite of what a config
    backup is for.
    """
    doc = configio.export_config(session)
    assert doc["scenarios"] == []


def test_import_restores_onto_an_empty_instance(session, user_library):
    doc = {
        "regulator_config_version": 1,
        "includes_secrets": True,
        "targets": [
            {
                "name": "prod-sh",
                "mgmt_url": "https://sh.example:8089",
                "username": "svc",
                "token_encrypted": "gAAAAA-token",
                "verify_tls": False,
            }
        ],
        "scenarios": [{"name": "demo", "files": {"scenario.yaml": SCENARIO_YAML.format(name="demo")}}],
    }

    report = configio.import_config(session, doc)
    session.flush()

    assert report["targets"] == 1 and report["scenarios"] == 1
    assert not report["skipped"]
    target = session.query(Target).filter(Target.name == "prod-sh").one()
    assert target.mgmt_url == "https://sh.example:8089"
    assert target.token_encrypted == "gAAAAA-token"
    assert target.verify_tls is False
    assert (user_library / "demo" / "scenario.yaml").is_file()


def test_import_is_idempotent(session, user_library):
    doc = {
        "targets": [{"name": "prod-sh", "mgmt_url": "https://sh.example:8089"}],
        "scenarios": [{"name": "demo", "files": {"scenario.yaml": SCENARIO_YAML.format(name="demo")}}],
    }
    first = configio.import_config(session, doc)
    session.flush()
    second = configio.import_config(session, doc)
    session.flush()

    assert first["targets"] == 1 and first["scenarios"] == 1
    # Second time round nothing is CREATED; both are recognised and updated.
    assert second["targets"] == 0 and second["scenarios"] == 0
    assert second["targets_updated"] == 1 and second["scenarios_updated"] == 1
    assert session.query(Target).count() == 1


def test_a_different_master_key_warns_rather_than_failing(session, user_library):
    """The configuration still restores; the credentials in it cannot work.

    Saying so once, loudly, beats letting it surface later as a pile of auth
    failures against targets that look correctly configured.
    """
    doc = {
        "master_key_fingerprint": "deadbeef",
        "includes_secrets": True,
        "targets": [{"name": "prod-sh", "mgmt_url": "https://sh.example:8089"}],
    }
    report = configio.import_config(session, doc)
    assert report["targets"] == 1
    assert any("cannot be decrypted" in w for w in report["warnings"])


def test_an_export_without_secrets_says_so_on_restore(session, user_library):
    report = configio.import_config(
        session, {"includes_secrets": False, "targets": [{"name": "t", "mgmt_url": "https://x:8089"}]}
    )
    assert any("no credential" in w for w in report["warnings"])


@pytest.mark.parametrize("name", ["../escape", "a/b", "..", "", ".hidden"])
def test_import_refuses_a_scenario_name_that_escapes_the_library(session, user_library, name):
    """A config document may have come from a bucket or a git repo.

    So a scenario name in it is untrusted input, and `../../etc` would write
    wherever the control plane can reach.
    """
    report = configio.import_config(
        session,
        {"scenarios": [{"name": name, "files": {"scenario.yaml": "name: x\n"}}]},
    )
    assert report["scenarios"] == 0
    assert report["skipped"]
    assert not list(user_library.glob("**/escape"))


def test_import_refuses_a_file_a_scenario_is_not_made_of(session, user_library):
    report = configio.import_config(
        session,
        {
            "scenarios": [
                {
                    "name": "demo",
                    "files": {
                        "scenario.yaml": SCENARIO_YAML.format(name="demo"),
                        "run.sh": "#!/bin/sh\nrm -rf /\n",
                    },
                }
            ]
        },
    )
    assert report["scenarios"] == 1
    assert (user_library / "demo" / "scenario.yaml").is_file()
    assert not (user_library / "demo" / "run.sh").exists()
    assert any("run.sh" in line for line in report["skipped"])


def test_a_newer_document_version_is_refused():
    with pytest.raises(configio.ConfigError) as caught:
        configio.load_document(json.dumps({"regulator_config_version": 99}))
    assert "newer Regulator" in str(caught.value)


def test_load_document_accepts_a_file_or_inline_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"targets": []}), encoding="utf-8")
    assert configio.load_document(str(path)) == {"targets": []}
    assert configio.load_document('{"targets": []}') == {"targets": []}
    # A path that does not exist is reported as a path, not as bad JSON.
    with pytest.raises(configio.ConfigError) as caught:
        configio.load_document("/no/such/file.json")
    assert "neither a readable file nor inline JSON" in str(caught.value)


def test_import_from_env_never_raises_on_a_bad_document(session, monkeypatch):
    """A typo in a ConfigMap must not stop the control plane starting."""
    monkeypatch.setenv(configio.IMPORT_ENV, "{not json at all")
    report = configio.import_from_env(session)
    assert report and "error" in report


def test_import_from_env_does_nothing_when_unset(session, monkeypatch):
    monkeypatch.delenv(configio.IMPORT_ENV, raising=False)
    assert configio.import_from_env(session) is None


# --------------------------------------------------------------------------- #
# Archives
# --------------------------------------------------------------------------- #

def _tar_bytes(members, compress=True):
    buffer = io.BytesIO()
    mode = "w:gz" if compress else "w"
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_export_then_extract_round_trips(user_library, tmp_path):
    directory = _write_scenario(
        user_library, "demo", {"savedsearches.conf": "[one]\nsearch = index=main\n"}
    )
    payload = scenarioarchive.export_scenario_bytes(directory, "demo")

    other = tmp_path / "other-library"
    landed = scenarioarchive.extract_scenario(payload, other)

    assert landed.name == "demo"
    assert (landed / "scenario.yaml").read_text(encoding="utf-8") == (
        directory / "scenario.yaml"
    ).read_text(encoding="utf-8")
    assert (landed / "savedsearches.conf").is_file()


def test_the_export_is_reproducible(user_library):
    directory = _write_scenario(user_library, "demo")
    first = scenarioarchive.export_scenario_bytes(directory, "demo")
    # Touching the files must not change the bytes: the mtimes are fixed, so a
    # checksum is worth comparing between two instances.
    os.utime(directory / "scenario.yaml", (0, 0))
    second = scenarioarchive.export_scenario_bytes(directory, "demo")
    assert first == second


def test_extraction_refuses_a_traversal_member(tmp_path):
    data = _tar_bytes({"../../escaped.yaml": b"name: x\n", "demo/scenario.yaml": b"name: x\n"})
    with pytest.raises(scenarioarchive.ScenarioArchiveError) as caught:
        scenarioarchive.extract_scenario(data, tmp_path / "library")
    assert "escapes the extraction directory" in str(caught.value)
    assert not (tmp_path / "escaped.yaml").exists()


def test_extraction_refuses_a_symlink_member(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        payload = b"name: x\n"
        info = tarfile.TarInfo("demo/scenario.yaml")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo("demo/secrets")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    with pytest.raises(scenarioarchive.ScenarioArchiveError) as caught:
        scenarioarchive.extract_scenario(buffer.getvalue(), tmp_path / "library")
    assert "symlink" in str(caught.value)


def test_extraction_caps_bind_on_real_output_not_declared_size(tmp_path):
    """The classic bomb trick is a header that lies about the size."""
    limits = scenarioarchive.ArchiveLimits(
        max_members=10, max_member_bytes=64, max_total_bytes=1024
    )
    data = _tar_bytes({"demo/scenario.yaml": b"x" * 4096})
    with pytest.raises(scenarioarchive.ScenarioArchiveError) as caught:
        scenarioarchive.extract_scenario(data, tmp_path / "library", limits=limits)
    assert "per-file limit" in str(caught.value)


def test_extraction_refuses_an_archive_of_several_scenarios(tmp_path):
    data = _tar_bytes({"one/scenario.yaml": b"name: one\n", "two/scenario.yaml": b"name: two\n"})
    with pytest.raises(scenarioarchive.ScenarioArchiveError) as caught:
        scenarioarchive.extract_scenario(data, tmp_path / "library")
    assert "one scenario at a time" in str(caught.value)


def test_extraction_keeps_only_the_files_a_scenario_is_made_of(tmp_path):
    data = _tar_bytes(
        {
            "demo/scenario.yaml": b"name: demo\n",
            "demo/evil.sh": b"#!/bin/sh\n",
            "demo/nested/deep.yaml": b"name: nope\n",
        }
    )
    landed = scenarioarchive.extract_scenario(data, tmp_path / "library")
    assert sorted(p.name for p in landed.iterdir()) == ["scenario.yaml"]


def test_extraction_refuses_to_overwrite_unless_asked(tmp_path):
    library = tmp_path / "library"
    data = _tar_bytes({"demo/scenario.yaml": b"name: demo\n"})
    scenarioarchive.extract_scenario(data, library)
    with pytest.raises(scenarioarchive.ScenarioArchiveError) as caught:
        scenarioarchive.extract_scenario(data, library)
    assert "already exists" in str(caught.value)
    # And with replace, it lands.
    second = _tar_bytes({"demo/scenario.yaml": b"name: demo-v2\n"})
    landed = scenarioarchive.extract_scenario(second, library, overwrite=True)
    assert "demo-v2" in (landed / "scenario.yaml").read_text(encoding="utf-8")


def test_a_zip_works_too_and_the_format_comes_from_the_content(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/scenario.yaml", "name: demo\n")
    landed = scenarioarchive.extract_scenario(buffer.getvalue(), tmp_path / "library")
    assert (landed / "scenario.yaml").is_file()
    # A plain file is not an archive, whatever it is called.
    with pytest.raises(scenarioarchive.ScenarioArchiveError) as caught:
        scenarioarchive.extract_scenario(b"name: demo\n", tmp_path / "other")
    assert "unrecognised archive" in str(caught.value)


# --------------------------------------------------------------------------- #
# Scenario source
# --------------------------------------------------------------------------- #

def test_describe_reports_nothing_when_unconfigured(monkeypatch):
    monkeypatch.delenv(scenariosource.SOURCE_ENV, raising=False)
    monkeypatch.delenv(scenariosource.WRITE_ENV, raising=False)
    assert scenariosource.describe() == {
        "configured": False,
        "writable": False,
        "kind": None,
        "location": None,
        "error": None,
    }


def test_describe_names_the_destination(monkeypatch, tmp_path):
    monkeypatch.setenv(scenariosource.SOURCE_ENV, "s3://my-bucket/my-scenarios")
    monkeypatch.setenv(scenariosource.WRITE_ENV, "1")
    info = scenariosource.describe()
    assert info["kind"] == "s3"
    assert info["location"] == "s3://my-bucket/my-scenarios"
    assert info["configured"] and info["writable"] and info["error"] is None
    # Read-only is the default, and is a distinct state from unconfigured: the
    # Push button must not appear for it.
    monkeypatch.delenv(scenariosource.WRITE_ENV)
    monkeypatch.setenv(scenariosource.SOURCE_ENV, str(tmp_path))
    info = scenariosource.describe()
    assert info["configured"] and not info["writable"] and info["kind"] == "directory"


def test_describe_reports_a_bad_source_instead_of_raising(monkeypatch):
    monkeypatch.setenv(scenariosource.SOURCE_ENV, "ftp://nope/scenarios")
    info = scenariosource.describe()
    assert info["configured"] and info["error"] and info["kind"] is None


def test_sync_imports_what_is_missing_and_leaves_local_edits_alone(user_library, tmp_path):
    bucket = scenariosource.DirectoryStore(str(tmp_path / "bucket"))
    bucket.put("fresh.tar.gz", _tar_bytes({"fresh/scenario.yaml": b"name: fresh\n"}))
    bucket.put("mine.tar.gz", _tar_bytes({"mine/scenario.yaml": b"name: from-the-bucket\n"}))
    # An existing local scenario of the same name must survive the sync.
    _write_scenario(user_library, "mine")

    report = scenariosource.sync_from_store(bucket, library=user_library)

    assert report["imported"] == ["fresh"]
    assert report["skipped"] == ["mine"]
    assert not report["failed"]
    assert "from-the-bucket" not in (user_library / "mine" / "scenario.yaml").read_text(
        encoding="utf-8"
    )


def test_one_bad_object_does_not_abort_the_sync(user_library, tmp_path):
    bucket = scenariosource.DirectoryStore(str(tmp_path / "bucket"))
    bucket.put("good.tar.gz", _tar_bytes({"good/scenario.yaml": b"name: good\n"}))
    bucket.put("broken.tar.gz", b"this is not an archive at all")

    report = scenariosource.sync_from_store(bucket, library=user_library)

    assert report["imported"] == ["good"]
    assert "broken.tar.gz" in report["failed"]


def test_publish_writes_the_archive_the_sync_reads(user_library, tmp_path):
    directory = _write_scenario(user_library, "demo")
    bucket = scenariosource.DirectoryStore(str(tmp_path / "bucket"))

    key = scenariosource.publish_scenario("demo", directory, store=bucket)

    assert key == "demo.tar.gz"
    assert bucket.list_archives() == ["demo.tar.gz"]
    # Round trip: what was pushed imports cleanly somewhere else.
    other = tmp_path / "other"
    report = scenariosource.sync_from_store(bucket, library=other)
    assert report["imported"] == ["demo"]


def test_publish_is_a_no_op_without_the_write_switch(user_library, monkeypatch, tmp_path):
    directory = _write_scenario(user_library, "demo")
    monkeypatch.setenv(scenariosource.SOURCE_ENV, str(tmp_path / "bucket"))
    monkeypatch.delenv(scenariosource.WRITE_ENV, raising=False)
    assert scenariosource.publish_scenario("demo", directory) is None
    assert not (tmp_path / "bucket").exists()


def test_a_failed_publish_never_fails_the_save(user_library):
    """Losing a mirror copy is annoying; losing the operator's work is not ok."""
    directory = _write_scenario(user_library, "demo")

    class Refusing(scenariosource.Store):
        def put(self, key, data):
            raise RuntimeError("AccessDenied")

    assert scenariosource.publish_scenario("demo", directory, store=Refusing()) is None


def test_load_from_env_never_raises_on_a_bad_source(monkeypatch):
    monkeypatch.setenv(scenariosource.SOURCE_ENV, "ftp://nope/scenarios")
    out = scenariosource.load_from_env()
    assert out and "error" in out


def test_load_from_env_does_nothing_when_unset(monkeypatch):
    monkeypatch.delenv(scenariosource.SOURCE_ENV, raising=False)
    assert scenariosource.load_from_env() is None
