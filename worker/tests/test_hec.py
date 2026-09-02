"""Telemetry shipping.

This module is best effort by design: if Splunk is unreachable the run carries
on and the NDJSON output still has every record. That is the right behaviour
and it is also exactly what makes this code dangerous to leave untested, because
a broken shipper reports nothing and looks identical to one that had nothing to
ship.

The TLS test at the bottom is the one that earns its place. Stoker shipped a
verify-TLS flag that was never projected into the worker, so the control plane
showed a green target while every worker failed TLS against the same
self-signed endpoint. Here the equivalent failure would be silent, since this
module swallows its own errors, so the flag is asserted to actually reach httpx
rather than merely to exist.
"""

from __future__ import annotations

import gzip
import json
from typing import Any, Dict, List

import httpx
import pytest

from conftest import run_async
from regulator_agent.config import HecConfig
from regulator_agent.hec import HecEmitter
from regulator_agent.results import StepRecord


def config(**overrides: Any) -> HecConfig:
    base: Dict[str, Any] = {
        "url": "https://splunk.example:8088",
        "token": "hec-token",
        "index": None,
        "gzip": False,
        "batch_ms": 10,
    }
    base.update(overrides)
    return HecConfig(**base)


def record(step_id: str = "one") -> StepRecord:
    return StepRecord(
        run_id="r",
        slot=0,
        vu_id=1,
        iteration=0,
        persona="p",
        step_id=step_id,
        latency_ms=12.5,
        service_time_ms=12.5,
        started_at=1_756_800_000.0,
    )


class Capture:
    """A transport that records requests and replies with a scripted status."""

    def __init__(self, statuses: List[int] | None = None) -> None:
        self.requests: List[httpx.Request] = []
        self.statuses = list(statuses or [200])
        self.raise_timeout = False

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.raise_timeout:
                raise httpx.ReadTimeout("simulated", request=request)
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(status, json={"text": "Success", "code": 0})

        return httpx.MockTransport(handler)

    def bodies(self) -> List[List[Dict[str, Any]]]:
        parsed = []
        for request in self.requests:
            raw = request.content
            if request.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            parsed.append([json.loads(line) for line in raw.decode().splitlines() if line])
        return parsed


def drive(emitter: HecEmitter, actions) -> None:
    async def go():
        await emitter.start()
        try:
            actions()
            await emitter._flush(force=True)
        finally:
            await emitter.close()

    run_async(go())


# ------------------------------------------------------------------ posting


def test_records_are_posted_as_ndjson_to_the_collector():
    capture = Capture()
    emitter = HecEmitter(config(), run_id="r7", transport=capture.transport())
    drive(emitter, lambda: [emitter.emit(record("a")), emitter.emit(record("b"))])

    assert len(capture.requests) == 1
    request = capture.requests[0]
    assert str(request.url) == "https://splunk.example:8088/services/collector/event"
    assert request.headers["Authorization"] == "Splunk hec-token"

    envelopes = capture.bodies()[0]
    assert [e["event"]["step_id"] for e in envelopes] == ["a", "b"]
    assert envelopes[0]["sourcetype"] == "regulator:step"
    assert envelopes[0]["source"] == "regulator"
    assert envelopes[0]["time"] == 1_756_800_000.0
    assert emitter.sent == 2
    assert emitter.http_2xx == 1


def test_a_null_index_is_omitted_rather_than_sent():
    """Splunk applies the token's default index when the key is absent.

    Sending an explicit null is a 400, and a 400 is dropped without retry, so
    this would silently lose every event.
    """
    capture = Capture()
    emitter = HecEmitter(config(index=None), transport=capture.transport())
    drive(emitter, lambda: emitter.emit(record()))
    assert "index" not in capture.bodies()[0][0]


def test_an_explicit_index_is_sent():
    capture = Capture()
    emitter = HecEmitter(config(index="regulator"), transport=capture.transport())
    drive(emitter, lambda: emitter.emit(record()))
    assert capture.bodies()[0][0]["index"] == "regulator"


def test_gzip_is_applied_when_enabled():
    capture = Capture()
    emitter = HecEmitter(config(gzip=True), transport=capture.transport())
    drive(emitter, lambda: emitter.emit(record()))
    assert capture.requests[0].headers["Content-Encoding"] == "gzip"
    assert capture.bodies()[0][0]["event"]["step_id"] == "one"


def test_the_run_summary_goes_with_its_own_sourcetype():
    capture = Capture()
    emitter = HecEmitter(config(), transport=capture.transport())
    drive(emitter, lambda: emitter.emit_summary({"outcome": "completed", "valid": True}))
    envelope = capture.bodies()[0][0]
    assert envelope["sourcetype"] == "regulator:run"
    assert envelope["event"]["outcome"] == "completed"


# ----------------------------------------------------------------- failures


def test_a_4xx_is_dropped_without_retry():
    """A bad token or an index the token cannot write cannot be fixed by trying again.

    Retrying would just repeat the mistake at rate, against a target we are
    supposed to be measuring rather than hammering.
    """
    capture = Capture(statuses=[403])
    emitter = HecEmitter(config(), transport=capture.transport())
    drive(emitter, lambda: emitter.emit(record()))

    assert len(capture.requests) == 1
    assert emitter.http_4xx == 1
    assert emitter.dropped == 1
    assert emitter.sent == 0


def test_a_5xx_is_retried_then_dropped():
    capture = Capture(statuses=[503])
    emitter = HecEmitter(config(), transport=capture.transport())
    drive(emitter, lambda: emitter.emit(record()))

    assert len(capture.requests) == 4  # RETRY_ATTEMPTS
    assert emitter.http_5xx == 4
    assert emitter.retries == 3
    assert emitter.dropped == 1


def test_a_5xx_that_recovers_is_counted_as_sent():
    capture = Capture(statuses=[503, 200])
    emitter = HecEmitter(config(), transport=capture.transport())
    drive(emitter, lambda: emitter.emit(record()))

    assert emitter.sent == 1
    assert emitter.dropped == 0
    assert emitter.retries == 1


def test_a_timeout_is_counted_and_never_raises():
    capture = Capture()
    capture.raise_timeout = True
    emitter = HecEmitter(config(), transport=capture.transport())
    drive(emitter, lambda: emitter.emit(record()))

    assert emitter.timeouts >= 1
    assert emitter.dropped == 1


def test_emitting_without_starting_never_raises():
    """The scheduler calls emit from the hot path and must never be interrupted."""
    emitter = HecEmitter(config())
    emitter.emit(record())
    emitter.emit_summary({"outcome": "completed"})
    assert emitter.sent == 0


def test_close_is_safe_twice_and_flushes_what_is_left():
    capture = Capture()
    emitter = HecEmitter(config(), transport=capture.transport())

    async def go():
        await emitter.start()
        emitter.emit(record("late"))
        await emitter.close()
        await emitter.close()

    run_async(go())
    assert emitter.sent == 1
    assert capture.bodies()[0][0]["event"]["step_id"] == "late"


def test_stats_reports_the_counters():
    emitter = HecEmitter(config())
    assert set(emitter.stats()) == {
        "sent", "dropped", "http_2xx", "http_4xx", "http_5xx", "timeouts", "retries"
    }


# ----------------------------------------------------------------------- TLS


@pytest.mark.parametrize("verify", [True, False])
def test_the_verify_flag_actually_reaches_httpx(monkeypatch, verify):
    """The flag must be projected, not merely accepted.

    Stoker had exactly this bug in reverse: a verify-TLS setting that existed on
    the target and was never passed to the worker, so every worker failed TLS
    against a self-signed HEC while the UI showed the target green. Here the
    failure would be silent, because this module swallows its own errors, so
    assert the value lands on the client rather than trusting that it does.
    """
    seen: Dict[str, Any] = {}
    real_client = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any):
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)

    emitter = HecEmitter(config(verify_tls=verify))

    async def go():
        await emitter.start()
        await emitter.close()

    run_async(go())
    assert seen["verify"] is verify
