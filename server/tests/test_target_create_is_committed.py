"""A 201 from POST /api/targets must name a row that a concurrent read can see.

This is a real server on a real socket, deliberately, because the failure it
guards against cannot happen through FastAPI's TestClient. That client is
single threaded: a request's dependency teardown always finishes before the
next call begins, so a handler that returns before committing still looks
correct. Under uvicorn with concurrent callers the response goes out first and
the commit races whatever the caller does next.

Measured on this suite's own harness, with the commit left to teardown:
15 of 120 create-then-read pairs returned 201 and then 404 for the id they had
just been handed. With the commit inside the handler: 0 of 120.

The rows were never lost — they arrive a moment later — so this is a
read-after-write visibility window rather than data loss, which is exactly why
it presented as an intermittent 404 that nobody could reproduce by hand.
"""

from __future__ import annotations

import concurrent.futures
import json
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
ROUNDS = 60
CONCURRENCY = 16


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:  # not up yet
        return 0, None


@pytest.fixture
def live_server(tmp_path):
    """uvicorn on a real port, with its own on-disk SQLite database."""
    port = _free_port()
    env = {
        **dict(__import__("os").environ),
        "REG_ALLOW_UNAUTHENTICATED": "1",
        "REG_DATABASE_URL": f"sqlite:///{tmp_path/'targets.db'}",
        "REG_BUILTIN_SCENARIOS_DIR": str(REPO / "scenarios"),
        "PYTHONPATH": f"{REPO/'server'}:{REPO/'worker'}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "regulator_server.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if _call(base, "GET", "/healthz")[0] == 200:
                break
            time.sleep(0.5)
        else:
            pytest.skip("the control plane did not start")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_201_is_never_followed_by_a_404_for_the_same_id(live_server):
    base = live_server

    def create_then_read(i):
        status, body = _call(base, "POST", "/api/targets", {
            "name": f"concurrent-{i}",
            "mgmt_url": "https://splunk.example:8089",
            "token": "t",
        })
        if status != 201:
            return f"create returned {status}"
        target_id = body["id"]
        read, _ = _call(base, "GET", f"/api/targets/{target_id}/samples")
        if read == 404:
            return f"201 then 404 for id {target_id}"
        if read != 200:
            return f"read returned {read} for id {target_id}"
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        problems = [p for p in pool.map(create_then_read, range(ROUNDS)) if p]

    assert not problems, (
        f"{len(problems)} of {ROUNDS} create-then-read pairs failed: {problems[:5]}"
    )
