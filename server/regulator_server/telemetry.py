"""The control plane's own telemetry over HEC.

Workers ship a record per step and their own summary. Everything the control
plane knows and they do not goes from here: the run's lifecycle (created,
started, released, stopped, finished), the workers' lease events (claimed,
ready, lost, done), the periodic samples behind the graphs, the cache
readings and evictions, the correlation findings, the merged fleet summary,
and a heartbeat about this process. Same index and token as the workers, so
one Splunk app answers "what happened to run 12" from both halves.

One emitter on its own thread and event loop. Runner threads and fleet
supervisors each own an asyncio loop of their own, so the only safe hand-over
is ``call_soon_threadsafe`` into this one. Every call here is fire and
forget: telemetry never blocks a request and never fails a run, exactly as
the worker's emitter promises.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

from regulator_agent.hec import HecEmitter

from .adapters import hec_config
from .config import get_settings

log = logging.getLogger("regulator.server.telemetry")

HEALTH_INTERVAL_S = 60.0
VERSION = "0.1.0"


def run_fields(run: Any, target_name: Optional[str] = None) -> Dict[str, Any]:
    """The join keys every event about a run carries."""
    if run is None:
        return {}
    label = f"r{run.id}" + (f"-{run.label}" if getattr(run, "label", None) else "")
    return {
        "run_no": run.id,
        "run_label": label,
        "scenario": run.scenario,
        "target": target_name,
        "fleet": run.fleet or "inprocess",
    }


class ControlTelemetry:
    """A process-wide, lazily started HEC emitter for the control plane."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._emitter: Optional[HecEmitter] = None
        self._config_key: Optional[tuple] = None
        self._ready = threading.Event()
        self._started_at = time.time()
        self.transport: Any = None  # test seam: an httpx transport for the emitter
        self.emitted = 0

    # ------------------------------------------------------------ lifecycle

    def _ensure(self) -> Optional[HecEmitter]:
        try:
            cfg = hec_config(get_settings())
        except Exception:  # noqa: BLE001 - no settings means no telemetry
            return None
        if cfg is None:
            return None
        key = (cfg.url, cfg.token, cfg.index, cfg.source)
        with self._lock:
            if self._emitter is not None and self._config_key == key:
                return self._emitter
            self._stop_locked()
            self._config_key = key
            self._ready.clear()
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._serve, name="regulator-telemetry", daemon=True)
            self._thread.start()
            if not self._ready.wait(5.0):
                log.warning("telemetry loop did not start")
                return None
            future = asyncio.run_coroutine_threadsafe(self._make(cfg), self._loop)
            try:
                self._emitter = future.result(10.0)
            except Exception:  # noqa: BLE001 - never fail a caller
                log.warning("telemetry emitter could not start", exc_info=True)
                self._emitter = None
            return self._emitter

    async def _make(self, cfg: Any) -> HecEmitter:
        emitter = HecEmitter(cfg, run_id="control", transport=self.transport, fields={"emitter": "control"})
        await emitter.start()
        asyncio.get_running_loop().create_task(self._health_loop(emitter), name="telemetry-health")
        return emitter

    def _serve(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _stop_locked(self) -> None:
        emitter, loop, thread = self._emitter, self._loop, self._thread
        self._emitter = None
        if emitter is not None and loop is not None:
            future = asyncio.run_coroutine_threadsafe(emitter.close(), loop)
            try:
                future.result(10.0)
            except Exception:  # noqa: BLE001
                pass
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)
        self._loop = None
        self._thread = None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def flush(self, timeout_s: float = 10.0) -> None:
        """Send what is queued now. For shutdown and tests."""
        with self._lock:
            emitter, loop = self._emitter, self._loop
        if emitter is None or loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(emitter.flush(), loop)
        try:
            future.result(timeout_s)
        except Exception:  # noqa: BLE001
            log.warning("telemetry flush failed", exc_info=True)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            emitter = self._emitter
        return emitter.stats() if emitter is not None else {}

    # --------------------------------------------------------------- events

    def emit(self, sourcetype_key: str, event: Dict[str, Any], when: Optional[float] = None, fields: Optional[Dict[str, Any]] = None) -> bool:
        emitter = self._ensure()
        if emitter is None or self._loop is None:
            return False
        sourcetype = getattr(emitter.config, sourcetype_key, None) or sourcetype_key
        self._loop.call_soon_threadsafe(emitter.emit_event, event, sourcetype, when, fields)
        self.emitted += 1
        return True

    def lifecycle(self, kind: str, run: Any = None, target_name: Optional[str] = None, when: Optional[float] = None, **detail: Any) -> bool:
        """One lifecycle event: ``kind`` plus the run's join keys plus detail."""
        keys = run_fields(run, target_name)
        event = {"kind": kind, **keys, **{k: v for k, v in detail.items() if v is not None}}
        indexed = {k: keys.get(k) for k in ("run_no", "run_label", "scenario", "target", "fleet")}
        for key in ("slot", "worker"):
            if detail.get(key) is not None:
                indexed[key] = detail[key]
        return self.emit("sourcetype_lifecycle", event, when, indexed)

    def sample(self, run: Any, sample: Dict[str, Any], target_name: Optional[str] = None, slot: Optional[int] = None) -> bool:
        keys = run_fields(run, target_name)
        event = {**keys, **sample}
        if slot is not None:
            event["slot"] = slot
        indexed = {**{k: keys.get(k) for k in ("run_no", "run_label", "scenario", "target", "fleet")}, "slot": slot}
        return self.emit("sourcetype_sample", event, sample.get("at"), indexed)

    def run_final(self, run: Any, summary: Dict[str, Any], scope: str, target_name: Optional[str] = None) -> bool:
        keys = run_fields(run, target_name)
        event = {**summary, **keys, "scope": scope}
        return self.emit("sourcetype_run", event, summary.get("ended_at"), {**keys, "scope": scope})

    async def _health_loop(self, emitter: HecEmitter) -> None:
        while True:
            try:
                emitter.emit_event(self._health_event(emitter), emitter.config.sourcetype_health)
            except Exception:  # noqa: BLE001
                log.debug("health event failed", exc_info=True)
            await asyncio.sleep(HEALTH_INTERVAL_S)

    def _health_event(self, emitter: HecEmitter) -> Dict[str, Any]:
        settings = get_settings()
        try:
            from .runner import manager

            active = manager.active_count()
        except Exception:  # noqa: BLE001
            active = None
        return {
            "kind": "health",
            "version": VERSION,
            "uptime_s": round(time.time() - self._started_at, 1),
            "active_runs": active,
            "max_concurrent_runs": settings.max_concurrent_runs,
            "max_virtual_users": settings.max_virtual_users,
            "fleets": {
                "swarm": settings.fleet.swarm_available,
                "k8s": settings.fleet.k8s_available,
                "default": settings.fleet.default_fleet,
            },
            "hec": emitter.stats(),
        }


telemetry = ControlTelemetry()
