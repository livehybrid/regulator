"""Tests for the fake splunkd test double.

Standard library and pytest only: the tools tree stays dependency free, so
these use urllib.request rather than requests or httpx.

Run them with:

    cd /opt/aios/apps/regulator && python3 -m pytest tools/tests -q
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

import pytest

# Allow the file to be run from anywhere, not only the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.fake_splunk import FakeSplunk  # noqa: E402

TOKEN = "Bearer testing-token"

# Fast by default: the whole file has to stay well under 20 seconds.
FAST = {
    "base_latency_ms": 30.0,
    "jitter_ms": 5.0,
    "dispatch_latency_ms": 1.0,
    "seed": 20260902,
}


# ----------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------
@pytest.fixture
def make_server():
    """Build servers on port 0 and close them all at the end of the test."""
    built = []

    def _make(**overrides):
        options = dict(FAST)
        options.update(overrides)
        server = FakeSplunk(port=0, **options)
        built.append(server)
        return server

    yield _make

    for server in built:
        server.close()


@pytest.fixture
def server(make_server):
    return make_server()


def call(server, method, path, *, body=None, query=None, token=TOKEN, timeout=15.0):
    """Return (status, decoded json or None) for one request."""
    url = server.base_url + path
    if query:
        url += "?" + urlencode(query)
    data = urlencode(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("Authorization", token)
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, (json.loads(raw) if raw else None)


def dispatch(server, spl="search index=main | stats count by host", **extra):
    body = {"search": spl, "output_mode": "json"}
    body.update(extra)
    status, payload = call(server, "POST", "/services/search/v2/jobs", body=body)
    assert status == 201, payload
    return payload["sid"]


def job_content(server, sid, path="/services/search/v2/jobs"):
    status, payload = call(server, "GET", f"{path}/{sid}", query={"output_mode": "json"})
    assert status == 200, payload
    return payload["entry"][0]["content"]


def wait_for_done(server, sid, timeout=15.0, path="/services/search/v2/jobs"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        content = job_content(server, sid, path=path)
        if content["isDone"]:
            return content
        time.sleep(0.005)
    raise AssertionError(f"job {sid} never finished")


def stats(server):
    status, payload = call(server, "GET", "/__stats", token=None)
    assert status == 200
    return payload


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------
def test_starts_on_port_zero_and_closes_cleanly():
    server = FakeSplunk(port=0, **FAST)
    try:
        assert server.port > 0
        assert server.base_url == f"http://127.0.0.1:{server.port}"
        status, payload = call(server, "GET", "/services/server/info", token=None)
        assert status == 200
        assert payload["entry"][0]["content"]["version"] == "10.4.0"
    finally:
        server.close()

    # The socket is released, so a fresh server can take the same port.
    replacement = FakeSplunk(host="127.0.0.1", port=server.port, **FAST)
    try:
        assert replacement.port == server.port
    finally:
        replacement.close()

    server.close()  # closing twice is harmless


def test_context_manager_closes():
    with FakeSplunk(port=0, **FAST) as server:
        port = server.port
        assert call(server, "GET", "/__stats", token=None)[0] == 200
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/__stats", timeout=2)


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------
def test_auth_required_and_accepted(make_server):
    server = make_server(reject_token="revoked-token")

    status, payload = call(server, "GET", "/services/search/v2/jobs", token=None)
    assert status == 401
    assert payload["messages"][0]["text"] == "call not properly authenticated"
    assert payload["messages"][0]["type"] == "WARN"

    assert call(server, "GET", "/services/search/v2/jobs", token="Bearer abc")[0] == 200
    assert call(server, "GET", "/services/search/v2/jobs", token="Splunk abc")[0] == 200
    assert call(server, "GET", "/services/search/v2/jobs", token="Basic abc")[0] == 401
    assert call(server, "GET", "/services/search/v2/jobs", token="Bearer")[0] == 401
    assert call(server, "GET", "/services/search/v2/jobs", token="Bearer ")[0] == 401

    status, _ = call(server, "GET", "/services/search/v2/jobs", token="Bearer revoked-token")
    assert status == 401
    status, _ = call(server, "GET", "/services/search/v2/jobs", token="Splunk revoked-token")
    assert status == 401

    assert stats(server)["auth_failures"] >= 5


def test_login(server):
    status, payload = call(
        server,
        "POST",
        "/services/auth/login",
        body={"username": "admin", "password": "changeme", "output_mode": "json"},
        token=None,
    )
    assert status == 200
    assert len(payload["sessionKey"]) == 36

    status, payload = call(
        server,
        "POST",
        "/services/auth/login",
        body={"username": "admin", "password": "wrong", "output_mode": "json"},
        token=None,
    )
    assert status == 401
    assert payload["messages"][0]["type"] == "WARN"


def test_login_honours_custom_password(make_server):
    server = make_server(password="s3cret")
    status, _ = call(
        server,
        "POST",
        "/services/auth/login",
        body={"username": "admin", "password": "s3cret"},
        token=None,
    )
    assert status == 200


# ----------------------------------------------------------------------
# Probes
# ----------------------------------------------------------------------
def test_server_info_needs_no_auth(server):
    status, payload = call(server, "GET", "/services/server/info", token=None)
    assert status == 200
    content = payload["entry"][0]["content"]
    assert content["version"] == "10.4.0"
    assert content["build"] == "f798d4d49089"
    assert content["numberOfVirtualCores"] == 8
    assert "search_head" in content["server_roles"]


def test_conf_limits(server):
    status, payload = call(
        server,
        "GET",
        "/services/configs/conf-limits/search",
        query={"output_mode": "json"},
    )
    assert status == 200
    entry = payload["entry"][0]
    assert entry["name"] == "search"
    assert entry["content"] == {
        "base_max_searches": "6",
        "max_searches_per_cpu": "1",
        "max_rt_search_multiplier": "1",
        "max_searches_perc": "50",
    }


def test_content_type_is_json_without_output_mode(server):
    request = urllib.request.Request(server.base_url + "/services/server/info")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.headers["Content-Type"].startswith("application/json")
        json.loads(response.read())


# ----------------------------------------------------------------------
# Job lifecycle
# ----------------------------------------------------------------------
def test_full_job_lifecycle(server):
    spl = "search index=main sourcetype=access_combined | stats count by host"
    sid = dispatch(server, spl)
    assert len(sid) == 36

    content = wait_for_done(server, sid)
    assert content["sid"] == sid
    assert content["dispatchState"] == "DONE"
    assert content["isDone"] is True
    assert content["isFailed"] is False
    assert content["isFinalized"] is False
    assert content["doneProgress"] == 1.0
    assert 0.02 <= float(content["runDuration"]) <= 2.0
    assert int(content["scanCount"]) == 100000
    assert int(content["eventCount"]) > 0
    assert int(content["resultCount"]) == 1 + len(spl) % 500
    assert content["resultPreviewCount"] == content["resultCount"]
    assert content["earliestTime"] and content["latestTime"]
    assert content["eventSearch"].startswith("index=main")
    assert isinstance(content["searchProviders"], list)
    assert isinstance(content["performance"], dict)
    # splunkd hands the numeric fields back as strings, the flags as booleans.
    for key in ("scanCount", "eventCount", "resultCount", "resultPreviewCount", "runDuration"):
        assert isinstance(content[key], str)
    for key in ("isDone", "isFailed", "isFinalized"):
        assert isinstance(content[key], bool)
    assert isinstance(content["doneProgress"], float)

    status, payload = call(
        server,
        "GET",
        f"/services/search/v2/jobs/{sid}/results",
        query={"output_mode": "json", "count": "0"},
    )
    assert status == 200
    assert payload["preview"] is False
    assert payload["init_offset"] == 0
    assert len(payload["results"]) == int(content["resultCount"])
    row = payload["results"][0]
    assert set(row) == {"_time", "host", "sourcetype", "count"}
    assert row["host"].startswith("web-0")
    assert row["sourcetype"] == "access_combined"
    assert [field["name"] for field in payload["fields"]] == [
        "_time",
        "host",
        "sourcetype",
        "count",
    ]

    status, payload = call(
        server,
        "GET",
        f"/services/search/v2/jobs/{sid}/results",
        query={"count": "3"},
    )
    assert status == 200
    assert len(payload["results"]) == 3

    status, payload = call(server, "DELETE", f"/services/search/v2/jobs/{sid}")
    assert status == 200
    assert payload["messages"] == []

    status, payload = call(server, "GET", f"/services/search/v2/jobs/{sid}")
    assert status == 404
    assert payload["messages"][0]["text"] == "Unknown sid."
    assert payload["messages"][0]["type"] == "FATAL"

    assert call(server, "DELETE", f"/services/search/v2/jobs/{sid}")[0] == 404

    counters = stats(server)
    assert counters["jobs_created"] == 1
    assert counters["jobs_done"] == 1
    assert counters["jobs_deleted"] == 1


def test_results_are_204_before_the_job_is_done(make_server):
    server = make_server(base_latency_ms=600.0, jitter_ms=0.0)
    sid = dispatch(server)
    status, payload = call(server, "GET", f"/services/search/v2/jobs/{sid}/results")
    assert status == 204
    assert payload is None
    assert job_content(server, sid)["isDone"] is False


def test_results_preview_while_running(make_server):
    server = make_server(base_latency_ms=600.0, jitter_ms=0.0)
    spl = "search index=main | stats count by host"
    sid = dispatch(server, spl)

    deadline = time.monotonic() + 10.0
    seen_running = False
    while time.monotonic() < deadline:
        content = job_content(server, sid)
        if content["dispatchState"] == "RUNNING" and content["doneProgress"] > 0.35:
            seen_running = True
            break
        assert not content["isDone"], "job finished before the preview could be read"
        time.sleep(0.005)
    assert seen_running

    status, payload = call(
        server,
        "GET",
        f"/services/search/v2/jobs/{sid}/results_preview",
        query={"output_mode": "json", "count": "0"},
    )
    assert status == 200
    assert payload["preview"] is True
    total = int(content["resultCount"])
    # A preview holds part of the answer, never more than the final count.
    assert 0 < len(payload["results"]) <= total
    assert 0 < int(content["resultPreviewCount"]) <= total


def test_results_preview_is_204_before_running(make_server):
    server = make_server(base_latency_ms=1500.0, jitter_ms=0.0, dispatch_latency_ms=0.0)
    sid = dispatch(server)
    status, _ = call(server, "GET", f"/services/search/v2/jobs/{sid}/results_preview")
    assert status == 204


def test_oneshot_returns_results_and_creates_no_job(server):
    status, payload = call(
        server,
        "POST",
        "/services/search/v2/jobs",
        body={
            "search": "search index=main | head 5",
            "exec_mode": "oneshot",
            "output_mode": "json",
        },
    )
    assert status == 200
    assert "sid" not in payload
    assert payload["preview"] is False
    assert len(payload["results"]) > 0
    assert set(payload["results"][0]) == {"_time", "host", "sourcetype", "count"}

    counters = stats(server)
    assert counters["oneshots"] == 1
    assert counters["jobs_created"] == 0
    assert counters["searches"] == []


def test_v1_path_works_and_is_counted(server):
    status, payload = call(
        server,
        "POST",
        "/services/search/jobs",
        body={"search": "search index=main", "output_mode": "json"},
    )
    assert status == 201
    sid = payload["sid"]
    content = wait_for_done(server, sid, path="/services/search/jobs")
    assert content["isDone"] is True

    dispatch(server)  # a v2 dispatch for contrast

    counters = stats(server)
    assert counters["v1_dispatches"] == 1
    assert counters["jobs_created"] == 2
    assert counters["requests_by_path"]["POST /services/search/jobs"] == 1
    assert counters["requests_by_path"]["POST /services/search/v2/jobs"] == 1


def test_control_cancel_and_finalize(make_server):
    server = make_server(base_latency_ms=800.0, jitter_ms=0.0)

    sid = dispatch(server)
    status, payload = call(
        server,
        "POST",
        f"/services/search/v2/jobs/{sid}/control",
        body={"action": "cancel"},
    )
    assert status == 200
    assert call(server, "GET", f"/services/search/v2/jobs/{sid}")[0] == 404
    assert stats(server)["jobs_cancelled"] == 1

    other = dispatch(server)
    status, _ = call(
        server,
        "POST",
        f"/services/search/v2/jobs/{other}/control",
        body={"action": "finalize"},
    )
    assert status == 200
    content = job_content(server, other)
    assert content["isFinalized"] is True
    assert content["isDone"] is True
    assert content["dispatchState"] == "DONE"
    assert call(server, "GET", f"/services/search/v2/jobs/{other}/results")[0] == 200

    status, _ = call(
        server,
        "POST",
        f"/services/search/v2/jobs/{other}/control",
        body={"action": "nonsense"},
    )
    assert status == 400
    status, _ = call(
        server,
        "POST",
        "/services/search/v2/jobs/not-a-sid/control",
        body={"action": "cancel"},
    )
    assert status == 404


def test_strict_spl_rejects_a_bad_dispatch(make_server):
    strict = make_server(strict_spl=True)
    status, payload = call(
        strict,
        "POST",
        "/services/search/v2/jobs",
        body={"search": "stats count by host", "output_mode": "json"},
    )
    assert status == 400
    assert payload["messages"][0] == {"type": "ERROR", "text": "Unknown search command"}
    assert call(
        strict,
        "POST",
        "/services/search/v2/jobs",
        body={"search": "| tstats count where index=main"},
    )[0] == 201

    lax = make_server()
    assert call(
        lax,
        "POST",
        "/services/search/v2/jobs",
        body={"search": "stats count by host"},
    )[0] == 201


# ----------------------------------------------------------------------
# The simulation model
# ----------------------------------------------------------------------
def test_max_concurrent_queues_dispatches(make_server):
    server = make_server(max_concurrent=2, base_latency_ms=400.0, jitter_ms=0.0,
                         dispatch_latency_ms=0.0)
    sids = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def fire():
        barrier.wait(timeout=10)
        sid = dispatch(server)
        with lock:
            sids.append(sid)

    threads = [threading.Thread(target=fire) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(sids) == 5
    counters = stats(server)
    assert counters["jobs_created"] == 5
    assert counters["queued_total"] > 0
    assert counters["peak_concurrent"] <= 2
    assert counters["current_concurrent"] <= 2

    states = [job_content(server, sid)["dispatchState"] for sid in sids]
    assert "QUEUED" in states

    for sid in sids:
        wait_for_done(server, sid)
    assert stats(server)["current_concurrent"] == 0


def test_per_concurrent_penalty_slows_a_loaded_dispatch(make_server):
    server = make_server(
        base_latency_ms=100.0,
        jitter_ms=0.0,
        per_concurrent_penalty_ms=50.0,
        dispatch_latency_ms=0.0,
    )

    solo = dispatch(server)
    solo_duration = float(wait_for_done(server, solo)["runDuration"])

    background = [dispatch(server) for _ in range(6)]
    loaded = dispatch(server)
    loaded_duration = float(wait_for_done(server, loaded)["runDuration"])

    assert loaded_duration > solo_duration * 2, (solo_duration, loaded_duration)
    for sid in background:
        wait_for_done(server, sid)


def test_tstats_is_faster_and_scans_less(make_server):
    server = make_server(base_latency_ms=300.0, jitter_ms=0.0, dispatch_latency_ms=0.0)
    raw_sid = dispatch(server, "search index=main sourcetype=access_combined")
    tstats_sid = dispatch(server, "| tstats count where index=main by host")

    tstats_content = wait_for_done(server, tstats_sid)
    raw_content = wait_for_done(server, raw_sid)

    assert float(tstats_content["runDuration"]) < float(raw_content["runDuration"])
    assert int(tstats_content["scanCount"]) < 1500
    assert int(raw_content["scanCount"]) == 100000
    assert int(tstats_content["scanCount"]) * 10 < int(raw_content["scanCount"])


def test_long_window_scans_ten_times_as_much(make_server):
    server = make_server()
    narrow = wait_for_done(server, dispatch(server, "search index=main earliest=-1h"))
    wide = wait_for_done(server, dispatch(server, "search index=main earliest=-7d"))
    assert int(narrow["scanCount"]) == 100000
    assert int(wide["scanCount"]) == 1000000


def test_scan_count_base_is_configurable(make_server):
    server = make_server(scan_count_base=42)
    content = wait_for_done(server, dispatch(server, "search index=main"))
    assert int(content["scanCount"]) == 42


def test_fail_rate_one_fails_every_job(make_server):
    server = make_server(fail_rate=1.0)
    content = wait_for_done(server, dispatch(server))
    assert content["isFailed"] is True
    assert content["dispatchState"] == "FAILED"
    assert content["isDone"] is True
    assert int(content["resultCount"]) == 0
    assert content["messages"][0]["type"] == "FATAL"
    assert stats(server)["jobs_done"] == 0


def test_seed_makes_durations_repeatable(make_server):
    durations = []
    for _ in range(2):
        server = make_server(seed=99, jitter_ms=200.0, base_latency_ms=10.0)
        durations.append(float(wait_for_done(server, dispatch(server))["runDuration"]))
    assert abs(durations[0] - durations[1]) < 0.02


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------
def test_parser_accepts_and_rejects(server):
    for good in (
        "search index=main | stats count",
        "| tstats count where index=main",
        "index=main error",
    ):
        status, payload = call(
            server,
            "POST",
            "/services/search/v2/parser",
            body={"q": good, "output_mode": "json"},
        )
        assert status == 200, (good, payload)
        assert payload["messages"] == []

    for bad in ("search index=main SYNTAX_ERROR", "| tstats SYNTAX_ERROR", "", ") broken"):
        status, payload = call(
            server,
            "POST",
            "/services/search/v2/parser",
            body={"q": bad, "output_mode": "json"},
        )
        assert status == 400, (bad, payload)
        assert payload["messages"][0]["type"] == "ERROR"


# ----------------------------------------------------------------------
# Test hooks
# ----------------------------------------------------------------------
def test_stats_and_reset(server):
    spl = "search index=main | stats count"
    sid = dispatch(server, spl)
    wait_for_done(server, sid)
    call(server, "GET", f"/services/search/v2/jobs/{sid}/results")

    counters = stats(server)
    assert counters["jobs_created"] == 1
    assert counters["searches"] == [spl]
    assert counters["peak_concurrent"] >= 1
    assert counters["requests_by_path"]["POST /services/search/v2/jobs"] == 1
    assert counters["requests_by_path"]["GET /services/search/v2/jobs/<sid>"] >= 1
    assert counters["requests_by_path"]["GET /services/search/v2/jobs/<sid>/results"] == 1
    # The introspection hooks stay out of the map, so reading it is side effect free.
    assert not any(key.endswith("__stats") for key in counters["requests_by_path"])
    assert set(counters) == {
        "jobs_created",
        "jobs_done",
        "jobs_deleted",
        "jobs_cancelled",
        "peak_concurrent",
        "current_concurrent",
        "queued_total",
        "requests_by_path",
        "auth_failures",
        "v1_dispatches",
        "oneshots",
        "searches",
    }

    status, _ = call(server, "POST", "/__reset", token=None)
    assert status == 200
    counters = stats(server)
    assert counters["jobs_created"] == 0
    assert counters["jobs_done"] == 0
    assert counters["searches"] == []
    assert counters["requests_by_path"] == {}
    assert counters["peak_concurrent"] == 0
    assert call(server, "GET", f"/services/search/v2/jobs/{sid}")[0] == 404


# ----------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------
def test_hundred_concurrent_dispatches(make_server):
    server = make_server(base_latency_ms=20.0, jitter_ms=5.0, dispatch_latency_ms=0.0)

    def fire(index):
        return dispatch(server, f"search index=main idx={index}")

    with ThreadPoolExecutor(max_workers=20) as pool:
        sids = list(pool.map(fire, range(100)))

    assert len(set(sids)) == 100
    counters = stats(server)
    assert counters["jobs_created"] == 100
    assert counters["peak_concurrent"] > 1
