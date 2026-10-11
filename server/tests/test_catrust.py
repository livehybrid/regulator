"""The CA bundle that lets a customer's private CA be verified rather than ignored.

Before this, a Splunk behind an internal CA left an operator two options and no
good one: verify and fail, or turn verification off and ship a splunkd admin
credential over a connection nobody checked. The certificate was fine; there
was nowhere to put it.

Two things are worth pinning. The truth table (``verify_tls`` x bundle), and
the fleet path: a worker is its own container, so it is handed the PEM text
rather than a path that would resolve to nothing there.
"""
from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

import certifi
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "worker"))
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO))

from regulator_agent import catrust  # noqa: E402
from regulator_agent.config import ConfigError, TargetConfig  # noqa: E402
from regulator_server.config import ServerConfigError, load_server_config  # noqa: E402


@pytest.fixture()
def bundle(tmp_path):
    """A real PEM, so the SSLContext path is genuinely exercised."""
    path = tmp_path / "customer-ca.pem"
    with open(certifi.where(), encoding="utf-8") as handle:
        path.write_text(handle.read(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Resolving
# --------------------------------------------------------------------------- #

def test_unset_means_the_public_trust_store():
    assert catrust.resolve_bundle({}) is None
    assert catrust.verify_arg(True, None) is True


def test_a_configured_path_resolves(bundle):
    assert catrust.resolve_bundle({catrust.CA_BUNDLE_ENV: str(bundle)}) == str(bundle)


def test_a_directory_of_certificates_is_accepted(tmp_path):
    directory = tmp_path / "certs"
    directory.mkdir()
    assert catrust.resolve_bundle({catrust.CA_BUNDLE_ENV: str(directory)}) == str(directory)


def test_a_missing_path_is_loud_not_silent():
    with pytest.raises(catrust.CaBundleError) as caught:
        catrust.resolve_bundle({catrust.CA_BUNDLE_ENV: "/no/such/ca.pem"})
    message = str(caught.value)
    assert "does not exist" in message
    # And it names the way a fleet worker is meant to receive it, because that
    # is the usual reason a path is right in one container and wrong in another.
    assert catrust.CA_BUNDLE_PEM_ENV in message


def test_a_file_that_is_not_pem_is_refused_with_the_fix(tmp_path):
    der = tmp_path / "ca.der"
    der.write_bytes(b"\x30\x82\x03\x0a definitely not pem")
    with pytest.raises(catrust.CaBundleError) as caught:
        catrust.resolve_bundle({catrust.CA_BUNDLE_ENV: str(der)})
    assert "openssl x509 -inform der" in str(caught.value)


# --------------------------------------------------------------------------- #
# The truth table
# --------------------------------------------------------------------------- #

def test_the_bundle_is_used_only_where_verification_is_on(bundle):
    """It composes with verify_tls rather than overriding it."""
    assert catrust.verify_arg(False, str(bundle)) is False


def test_httpx_gets_an_ssl_context_not_a_path(bundle):
    """httpx 0.28 deprecated the bare path its verify= used to accept, so
    passing one through would warn now and break later."""
    assert isinstance(catrust.verify_arg(True, str(bundle)), ssl.SSLContext)


def test_an_unloadable_bundle_falls_back_rather_than_failing_the_request(tmp_path):
    broken = tmp_path / "broken.pem"
    broken.write_text("-----BEGIN CERTIFICATE-----\nnot base64 at all\n", encoding="utf-8")
    assert catrust.verify_arg(True, str(broken)) is True


# --------------------------------------------------------------------------- #
# The fleet path: PEM text, not a path
# --------------------------------------------------------------------------- #

def test_inline_pem_is_written_to_a_file_the_client_can_open(bundle):
    pem = bundle.read_text(encoding="utf-8")
    resolved = catrust.resolve_bundle({catrust.CA_BUNDLE_PEM_ENV: pem})
    assert resolved and os.path.isfile(resolved)
    assert "-----BEGIN CERTIFICATE-----" in Path(resolved).read_text(encoding="utf-8")
    # And it is usable, which is the only thing that matters about it.
    assert isinstance(catrust.verify_arg(True, resolved), ssl.SSLContext)


def test_inline_pem_wins_over_a_path(bundle, tmp_path):
    """The control plane sent the inline one deliberately for this run.

    A path may be a leftover baked into the image, so preferring it would let
    stale configuration beat the thing the launch actually chose.
    """
    pem = bundle.read_text(encoding="utf-8")
    stale = tmp_path / "stale.pem"
    stale.write_text(pem, encoding="utf-8")
    resolved = catrust.resolve_bundle(
        {catrust.CA_BUNDLE_PEM_ENV: pem, catrust.CA_BUNDLE_ENV: str(stale)}
    )
    assert resolved != str(stale)


def test_inline_text_that_is_not_pem_is_refused():
    with pytest.raises(catrust.CaBundleError) as caught:
        catrust.resolve_bundle({catrust.CA_BUNDLE_PEM_ENV: "oops, a log line"})
    assert catrust.CA_BUNDLE_PEM_ENV in str(caught.value)


def test_read_pem_returns_the_text_for_projection(bundle):
    assert "-----BEGIN CERTIFICATE-----" in (catrust.read_pem(str(bundle)) or "")


def test_a_directory_cannot_be_projected_and_says_so(tmp_path, caplog):
    directory = tmp_path / "certs"
    directory.mkdir()
    with caplog.at_level("WARNING"):
        assert catrust.read_pem(str(directory)) is None
    assert "cannot be projected" in caplog.text


def test_projection_never_raises_after_boot(caplog):
    """A run must not fail to launch because the bundle became unreadable."""
    with caplog.at_level("WARNING"):
        assert catrust.read_pem("/gone/ca.pem") is None


# --------------------------------------------------------------------------- #
# Config wiring, both sides
# --------------------------------------------------------------------------- #

def test_the_worker_target_carries_the_bundle(bundle):
    target = TargetConfig(url="https://sh:8089", token="t", ca_bundle=str(bundle))
    assert isinstance(catrust.verify_arg(target.verify_tls, target.ca_bundle), ssl.SSLContext)


def test_a_bad_bundle_stops_the_worker_loading_its_config(monkeypatch, tmp_path):
    monkeypatch.setenv("REG_SCENARIO", str(tmp_path))
    monkeypatch.setenv("REG_TARGET_URL", "https://sh:8089")
    monkeypatch.setenv("REG_TARGET_TOKEN", "tok")
    monkeypatch.setenv(catrust.CA_BUNDLE_ENV, "/no/such/ca.pem")
    from regulator_agent.config import load_config

    with pytest.raises(ConfigError):
        load_config()


def test_the_control_plane_setting_is_validated_at_boot(monkeypatch, bundle):
    monkeypatch.setenv("REG_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv(catrust.CA_BUNDLE_ENV, str(bundle))
    assert load_server_config().ca_bundle == str(bundle)

    monkeypatch.setenv(catrust.CA_BUNDLE_ENV, "/no/such/ca.pem")
    with pytest.raises(ServerConfigError):
        load_server_config()


def test_the_control_plane_defaults_to_no_bundle(monkeypatch):
    monkeypatch.setenv("REG_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.delenv(catrust.CA_BUNDLE_ENV, raising=False)
    monkeypatch.delenv(catrust.CA_BUNDLE_PEM_ENV, raising=False)
    assert load_server_config().ca_bundle is None
