"""Shipping results to Splunk over HEC.

Optional, and off unless configured, for a reason that is easy to miss: writing
your results into the cluster you are measuring adds load to it. The volume is
small next to the search load, but it is not zero, and a benchmark that
silently instruments its own subject is a benchmark with an asterisk nobody
reads. So the endpoint is separately configurable, the default is not to ship
at all, and when the telemetry host and the target host are the same the run
record carries a ``self_instrumented`` flag.

Everything here is best effort by design. If Splunk is unreachable, the run
carries on and the NDJSON output still has every record. Telemetry that can
take down the thing it is observing is worse than no telemetry.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import HecConfig
from .results import StepRecord

log = logging.getLogger("regulator.hec")

# Retry schedule for a 5xx or a timeout. Exponential with a cap, and a hard
# limit on attempts: a load generator that spends its time retrying telemetry
# is not generating load.
RETRY_BASE_S = 0.5
RETRY_CAP_S = 15.0
RETRY_ATTEMPTS = 4


class HecEmitter:
    """Batches step records and posts them to a HEC event endpoint.

    The scheduler calls :meth:`emit` synchronously from the hot path, so it
    does nothing but append to a list. A background task owns every network
    operation, which keeps HEC latency out of the measurement entirely. That
    separation matters more than it looks: a synchronous post here would add
    the telemetry round trip to the very latency the tool exists to measure.
    """

    def __init__(
        self,
        config: HecConfig,
        run_id: str = "local",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self.run_id = run_id
        # Injectable purely for tests. Everything in this module is best effort
        # and swallows its own failures by design, which makes it exactly the
        # kind of code that can be quietly broken for months, so it needs to be
        # testable without a Splunk.
        self._transport = transport
        self._pending: List[Dict[str, Any]] = []
        self._pending_bytes = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._closing = False
        self._lock = asyncio.Lock()
        # Counters, surfaced in the run summary so a run where telemetry
        # quietly failed is distinguishable from one where it worked.
        self.sent = 0
        self.dropped = 0
        self.http_2xx = 0
        self.http_4xx = 0
        self.http_5xx = 0
        self.timeouts = 0
        self.retries = 0

    # ------------------------------------------------------------------

    async def start(self) -> None:
        kwargs: Dict[str, Any] = {
            # A Splunk HEC endpoint on a self-signed certificate is the normal
            # case, not the exception: an on-premises indexer, a SmartStore
            # test rig or an in-cluster service name all present a certificate
            # nothing trusts. REG_HEC_VERIFY_TLS exists so telemetry does not
            # silently vanish into TLS failures the run never reports, because
            # this module swallows its own errors by design.
            "verify": self.config.verify_tls,
            "timeout": httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
            "headers": {"Authorization": f"Splunk {self.config.token}"},
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)
        self._task = asyncio.create_task(self._flush_loop(), name="hec-flush")

    async def close(self) -> None:
        """Flush what is left, then release the connection. Safe twice."""
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        try:
            await self._flush(force=True)
        except Exception:  # noqa: BLE001 - closing must not raise
            log.warning("final HEC flush failed", exc_info=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """Send everything queued, now.

        Called before the run summary reads the counters. Without it the
        summary reports whatever the background loop happened to have shipped
        at that instant, which undercounts by however many records were still
        in the queue and makes the telemetry block look like it lost events it
        had not tried to send yet.
        """
        await self._flush(force=True)

    def emit(self, record: StepRecord) -> None:
        """Queue one step record. Never blocks, never raises."""
        self._append(self._envelope(record.to_dict(), self.config.sourcetype_step, record.started_at))

    def emit_summary(self, summary: Dict[str, Any]) -> None:
        self._append(self._envelope(summary, self.config.sourcetype_run, time.time()))

    def _append(self, envelope: Dict[str, Any]) -> None:
        line = json.dumps(envelope, separators=(",", ":"))
        self._pending.append(envelope)
        self._pending_bytes += len(line)

    def _envelope(self, event: Dict[str, Any], sourcetype: str, when: float) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {
            "time": when or time.time(),
            "source": self.config.source,
            "sourcetype": sourcetype,
            "event": event,
        }
        # Omit a null index rather than sending one: Splunk applies the token's
        # default index when the key is absent, and sending an explicit null is
        # a 400.
        if self.config.index:
            envelope["index"] = self.config.index
        return envelope

    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        interval = self.config.batch_ms / 1000.0
        try:
            while not self._closing:
                await asyncio.sleep(interval)
                if self._pending_bytes >= self.config.batch_bytes or self._pending:
                    await self._flush()
        except asyncio.CancelledError:
            return

    async def _flush(self, force: bool = False) -> None:
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
            self._pending_bytes = 0

        body = "\n".join(json.dumps(e, separators=(",", ":")) for e in batch).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.gzip:
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"

        url = f"{self.config.url}/services/collector/event"
        delay = RETRY_BASE_S
        for attempt in range(RETRY_ATTEMPTS):
            if self._client is None:
                self.dropped += len(batch)
                return
            try:
                response = await self._client.post(url, content=body, headers=headers)
            except httpx.TimeoutException:
                self.timeouts += 1
            except httpx.HTTPError as exc:
                log.debug("HEC transport error: %s", exc)
                self.timeouts += 1
            else:
                if 200 <= response.status_code < 300:
                    self.http_2xx += 1
                    self.sent += len(batch)
                    return
                if 400 <= response.status_code < 500:
                    # A 4xx is a contract problem: a bad token, an index the
                    # token cannot write, a malformed event. Retrying cannot
                    # fix it and would just repeat the mistake at rate, so log
                    # once and drop.
                    self.http_4xx += 1
                    self.dropped += len(batch)
                    log.warning(
                        "HEC rejected %d events with %d: %s",
                        len(batch),
                        response.status_code,
                        response.text[:200],
                    )
                    return
                self.http_5xx += 1

            if attempt < RETRY_ATTEMPTS - 1:
                self.retries += 1
                await asyncio.sleep(min(delay, RETRY_CAP_S))
                delay *= 2

        self.dropped += len(batch)
        log.warning("dropped %d telemetry events after %d attempts", len(batch), RETRY_ATTEMPTS)

    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, int]:
        return {
            "sent": self.sent,
            "dropped": self.dropped,
            "http_2xx": self.http_2xx,
            "http_4xx": self.http_4xx,
            "http_5xx": self.http_5xx,
            "timeouts": self.timeouts,
            "retries": self.retries,
        }
