"""Configuration parsing, including the properties that are security ones."""

from __future__ import annotations

import pytest

from regulator_agent.config import Config, ConfigError, TargetConfig, load_config


def test_a_minimal_environment_parses(env):
    config = load_config(env())
    assert config.standalone is True
    assert config.target.url == "https://splunk.example:8089"
    assert config.target.token == "s3cr3t-token-value"
    assert config.hec is None


def test_a_trailing_slash_is_stripped_from_urls(env):
    config = load_config(env(REG_TARGET_URL="https://splunk.example:8089/"))
    assert config.target.url == "https://splunk.example:8089"


@pytest.mark.parametrize(
    "overrides, expected_fragment",
    [
        ({"REG_STANDALONE": None}, "REG_STANDALONE"),
        ({"REG_SCENARIO": None}, "REG_SCENARIO"),
        ({"REG_TARGET_URL": None}, "REG_TARGET_URL"),
        ({"REG_TARGET_URL": "splunk.example:8089"}, "http://"),
        ({"REG_TARGET_TOKEN": None}, "REG_TARGET_TOKEN"),
        ({"REG_TARGET_VERIFY_TLS": "maybe"}, "REG_TARGET_VERIFY_TLS"),
        ({"REG_VUS": "lots"}, "REG_VUS"),
        ({"REG_HEC_URL": "http://hec.example:8088"}, "REG_HEC_TOKEN"),
        ({"REG_HEC_TOKEN": "abc"}, "REG_HEC_URL"),
        ({"REG_VUS": "10", "REG_ARRIVAL_RATE_PER_MIN": "60"}, "not both"),
        ({"REG_POLL_INITIAL_MS": "500", "REG_POLL_MAX_MS": "100"}, "REG_POLL_MAX_MS"),
        ({"REG_LOG_LEVEL": "CHATTY"}, "REG_LOG_LEVEL"),
        ({"REG_TARGET_API_VERSION": "v3"}, "REG_TARGET_API_VERSION"),
    ],
)
def test_bad_values_are_fatal_and_name_the_variable(env, overrides, expected_fragment):
    """A malformed value must never fall back to a default.

    The failure mode this prevents is a typo in a verify-TLS flag quietly
    widening the trust boundary, or a bad virtual-user count silently running a
    much smaller test than the operator believes they asked for.
    """
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(**overrides))
    assert expected_fragment in str(excinfo.value)


def test_managed_mode_variables_are_rejected_until_phase_one(env):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(REG_STANDALONE=None, REG_RUN_ID="42", REG_CONTROL_URL="http://cp"))
    assert "Phase 1" in str(excinfo.value)


def test_username_and_password_are_an_acceptable_alternative_to_a_token(env):
    config = load_config(
        env(REG_TARGET_TOKEN=None, REG_TARGET_USERNAME="loadtest", REG_TARGET_PASSWORD="pw")
    )
    assert config.target.username == "loadtest"


@pytest.mark.parametrize(
    "raw, expected", [("1", True), ("true", True), ("YES", True), ("on", True),
                      ("0", False), ("false", False), ("No", False), ("off", False)]
)
def test_boolean_spellings(env, raw, expected):
    assert load_config(env(REG_TARGET_VERIFY_TLS=raw)).target.verify_tls is expected


def test_jobs_path_follows_the_api_version():
    v2 = TargetConfig(url="https://x:8089", token="t")
    v1 = TargetConfig(url="https://x:8089", token="t", api_version="v1")
    assert v2.jobs_path == "/services/search/v2/jobs"
    assert v1.jobs_path == "/services/search/jobs"


def test_self_instrumented_is_true_when_telemetry_goes_to_the_target(env):
    """Same host on a different port still counts.

    HEC is 8088 and management is 8089 on the same box, which is exactly the
    common homelab case, so comparing the whole URL would miss it.
    """
    config = load_config(
        env(REG_HEC_URL="http://splunk.example:8088", REG_HEC_TOKEN="hec-token")
    )
    assert config.self_instrumented is True


def test_self_instrumented_is_false_for_a_separate_telemetry_host(env):
    config = load_config(
        env(REG_HEC_URL="http://telemetry.example:8088", REG_HEC_TOKEN="hec-token")
    )
    assert config.self_instrumented is False


def test_secrets_never_appear_in_a_repr(env):
    """A security property, not a nicety.

    Config objects end up in log lines, exception context and pytest fixture
    dumps. A token that survives repr() is a token in someone's CI log.
    """
    config = load_config(
        env(REG_HEC_URL="http://telemetry.example:8088", REG_HEC_TOKEN="hec-token-value")
    )
    rendered = repr(config)
    assert "s3cr3t-token-value" not in rendered
    assert "hec-token-value" not in rendered
    assert repr(config.target).count("s3cr3t") == 0
    assert repr(config.hec).count("hec-token-value") == 0


def test_password_is_also_redacted(env):
    config = load_config(
        env(REG_TARGET_TOKEN=None, REG_TARGET_USERNAME="u", REG_TARGET_PASSWORD="hunter2")
    )
    assert "hunter2" not in repr(config)


def test_defaults_are_sensible(env):
    config = load_config(env())
    assert config.target.verify_tls is True
    assert config.delete_jobs is True
    assert config.cache_bust is True
    assert config.poll_initial_ms == 250
    assert config.poll_max_ms == 1000
    assert config.total_workers == 1
    assert config.slot == 0
    assert isinstance(config, Config)
