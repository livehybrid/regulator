"""Editing a target in place.

The case that motivated it: a target restored from yesterday's config whose
token has expired. Replacing the token must keep the target's id, so runs,
re-runs and baselines against it keep working, and must never echo a secret.
"""

from __future__ import annotations

import pytest

from test_api import FakeSplunk, client, make_target, splunkd, wait_for_terminal  # noqa: F401


@pytest.fixture
def expired_splunkd():
    """A splunkd on which the token the target was created with no longer works."""
    server = FakeSplunk(port=0, base_latency_ms=5.0, jitter_ms=1.0, dispatch_latency_ms=1.0,
                        seed=7, reject_token="a-secret-bearer-token")
    try:
        yield server
    finally:
        server.close()


def _patch(client, target_id, **body):
    return client.patch(f"/api/targets/{target_id}", json=body)


def test_an_expired_token_is_replaced_and_the_target_works_again(client, expired_splunkd):
    target = make_target(client, expired_splunkd)
    client.post(f"/api/targets/{target['id']}/test")
    assert expired_splunkd.stats.auth_failures > 0   # the stored token is refused

    response = _patch(client, target["id"], token="a-fresh-token")
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["id"] == target["id"]
    assert edited["health"] == "unknown"          # the old Test no longer applies
    assert edited["has_token"] is True
    assert "a-fresh-token" not in response.text

    expired_splunkd.stats.auth_failures = 0
    assert client.post(f"/api/targets/{target['id']}/test").json()["ok"] is True
    assert expired_splunkd.stats.auth_failures == 0  # the new token is the one sent


def test_a_run_launched_before_the_edit_still_names_the_target(client, splunkd):
    target = make_target(client, splunkd)
    run_id = client.post(
        "/api/runs",
        json={"target_id": target["id"], "scenario": "smoke", "virtual_users": 1, "duration_s": 2},
    ).json()["id"]
    wait_for_terminal(client, run_id)

    assert _patch(client, target["id"], name="renamed", token="another-token").status_code == 200

    run = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)
    assert run["target_name"] == "renamed"


def test_only_the_fields_sent_change(client, splunkd):
    target = make_target(client, splunkd)
    before = client.get("/api/targets").json()[0]

    edited = _patch(client, target["id"], app="itsi", web_url="https://web.example:8000").json()

    assert edited["app"] == "itsi"
    assert edited["web_url"] == "https://web.example:8000"
    for unchanged in ("name", "mgmt_url", "owner", "verify_tls", "api_version", "has_token"):
        assert edited[unchanged] == before[unchanged], unchanged
    # Not a connection field, so the health a Test recorded still stands.
    assert edited["health"] == before["health"]

    assert _patch(client, target["id"], web_url="").json()["web_url"] is None


def test_an_empty_secret_keeps_the_stored_one(client, splunkd):
    target = make_target(client, splunkd)
    edited = _patch(client, target["id"], token="", owner="admin").json()
    assert edited["has_token"] is True
    assert client.post(f"/api/targets/{target['id']}/test").json()["ok"] is True


def test_switching_to_a_username_and_password_and_clearing_the_token(client, splunkd):
    target = make_target(client, splunkd)
    edited = _patch(client, target["id"], username="admin", password="changeme",
                    clear=["token"]).json()
    assert edited["has_token"] is False
    assert edited["has_username"] is True and edited["has_password"] is True
    assert "changeme" not in str(client.get("/api/targets").json())


def test_an_edit_that_leaves_no_credential_is_refused(client, splunkd):
    target = make_target(client, splunkd)
    response = _patch(client, target["id"], clear=["token"])
    assert response.status_code == 422
    assert "no credential" in response.json()["detail"]
    assert client.get("/api/targets").json()[0]["has_token"] is True


def test_renaming_onto_another_target_is_a_409(client, splunkd):
    make_target(client, splunkd, name="a")
    b = make_target(client, splunkd, name="b")
    assert _patch(client, b["id"], name="a").status_code == 409


@pytest.mark.parametrize("body", [
    {"mgmt_url": "splunk.example:8089"},
    {"mgmt_url": "https://admin:pw@splunk.example:8089"},
    {"api_version": "v3"},
    {"clear": ["mgmt_url"]},
    {"token_encrypted": "x"},
])
def test_a_bad_edit_is_a_422(client, splunkd, body):
    target = make_target(client, splunkd)
    assert _patch(client, target["id"], **body).status_code == 422


def test_editing_a_missing_target_is_a_404(client):
    assert _patch(client, 999, token="t").status_code == 404


def test_the_audit_log_names_the_fields_not_the_secret(client, splunkd):
    target = make_target(client, splunkd)
    _patch(client, target["id"], token="sekrit-new-token", owner="admin")
    audit = client.get("/api/audit").text
    assert "target_updated" in audit
    assert "owner, token" in audit
    assert "sekrit-new-token" not in audit
