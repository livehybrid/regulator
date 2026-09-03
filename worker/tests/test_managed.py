"""Managed mode: the bootstrap, and turning a claim into a configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from regulator_agent.config import ConfigError, load_config, load_managed_boot
from regulator_agent.managed import _config_from_claim, _materialise_scenario


def test_the_bootstrap_needs_all_three_variables():
    assert load_managed_boot({}) is None
    with pytest.raises(ConfigError):
        load_managed_boot({"REG_RUN_ID": "7", "REG_CONTROL_URL": "http://cp:8080"})
    boot = load_managed_boot(
        {
            "REG_RUN_ID": "7",
            "REG_CONTROL_URL": "http://cp:8080/",
            "REG_RUN_JWT": "tok.en",
            "JOB_COMPLETION_INDEX": "3",
            "REG_HOLDER": "pod-3",
        }
    )
    assert boot is not None
    assert boot.run_id == "7"
    assert boot.control_url == "http://cp:8080"
    assert boot.hint_slot == 3
    assert boot.holder == "pod-3"
    assert "tok.en" not in repr(boot)


def test_a_managed_worker_is_refused_by_the_standalone_parser(env):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(REG_STANDALONE=None, REG_RUN_ID="7", REG_CONTROL_URL="http://cp", REG_RUN_JWT="t"))
    assert "claim" in str(excinfo.value)


def test_a_claim_becomes_the_same_config_a_standalone_worker_has(tmp_path, monkeypatch):
    monkeypatch.setenv("REG_RUN_ID", "7")
    monkeypatch.setenv("REG_CONTROL_URL", "http://cp:8080")
    monkeypatch.setenv("REG_RUN_JWT", "tok.en")
    monkeypatch.setenv("REG_POLL_INITIAL_MS", "25")  # a worker-image default survives
    boot = load_managed_boot()
    assert boot is not None
    claim = {
        "slot": 2,
        "lease_id": "abc",
        "total_workers": 4,
        "engine": "api",
        "run_label": "r7-nightly",
        "scenario": {
            "name": "smoke",
            "files": {"scenario.yaml": (Path(__file__).resolve().parents[2] / "scenarios" / "smoke" / "scenario.yaml").read_text()},
        },
        "env": {
            "REG_TARGET_URL": "https://splunk.example:8089",
            "REG_TARGET_TOKEN": "target-token",
            "REG_TARGET_VERIFY_TLS": "0",
            "REG_VUS": "3",
            "REG_DURATION_S": "12",
            "REG_HEC_URL": "https://hec.example:8088",
            "REG_HEC_TOKEN": "hec-token",
            "REG_HEC_VERIFY_TLS": "0",
        },
    }
    directory = _materialise_scenario(claim, tmp_path)
    assert (directory / "scenario.yaml").is_file()
    config = _config_from_claim(boot, claim, directory)
    assert config.standalone is True
    assert config.slot == 2
    assert config.total_workers == 4
    assert config.run_id == "r7-nightly"
    assert config.virtual_users == 3
    assert config.duration_s == 12.0
    assert config.target.url == "https://splunk.example:8089"
    assert config.target.token == "target-token"
    assert config.target.verify_tls is False
    assert config.hec is not None and config.hec.verify_tls is False
    assert config.poll_initial_ms == 25
    assert str(directory) == config.scenario_path


def test_scenario_files_from_a_claim_can_never_escape_their_directory(tmp_path):
    claim = {"scenario": {"name": "x", "files": {"scenario.yaml": "name: x\n", "../../evil.yaml": "boom"}}}
    directory = _materialise_scenario(claim, tmp_path)
    assert (directory / "evil.yaml").is_file()
    assert not (tmp_path.parent / "evil.yaml").exists()
