"""A fleet member: claim a slot, warm up, wait for T0, run, report.

The managed protocol, adapted from Stoker where it has been through real
multi-node runs. Four calls against the control plane, all with a per-run
bearer token that authorises exactly one run:

``claim``
    Take the lowest free lease (or the hinted one). The response carries
    everything a standalone worker would have read from its environment: the
    target, the scenario's files, this worker's share of the load, where to
    ship telemetry. Nothing sensitive rides in the container environment.

``ready``
    The engine is started, the scenario validated, the parameters resolved.
    When every worker is ready the control plane sets one shared T0.

``heartbeat``
    Every couple of seconds, carrying the live aggregate. The response is the
    command channel: ``release`` with T0, ``stop`` to drain, ``superseded``
    when the lease was reissued to somebody else. A heartbeat that is
    acknowledged is the lease renewal; a lease that stops being renewed is
    marked lost and its share is not waited for.

``final``
    The run summary with the raw histograms, so the control plane computes the
    fleet's percentiles once over the merged whole rather than averaging
    percentiles, which is meaningless.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .config import Config, ConfigError, ManagedBoot, load_config
from .engines import BrowserUnavailable, get_engine
from .hec import HecEmitter
from .params import ParameterResolver
from .results import NdjsonEmitter, RunStats, RunSummary
from .scenario import ScenarioError, is_advice, lint, load_scenario
from .scheduler import Scheduler

log = logging.getLogger("regulator.managed")

PROTOCOL_VERSION = 1
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 20.0
# Heartbeats missed for longer than this pause nothing (a search generator has
# no equivalent of pausing delivery) but are logged loudly; the dead-man ends
# the run so a worker whose control plane vanished does not hammer a target
# nobody is watching.
DEFAULT_HEARTBEAT_S = 2.0

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_GUARD_RAIL = 2
EXIT_INVALID = 3
EXIT_LINT = 4
EXIT_SUPERSEDED = 5


class ControlError(RuntimeError):
    """The control plane refused or could not be reached."""


class Superseded(ControlError):
    """The lease was reissued to another holder: a fatal drain."""


class ControlClient:
    """The worker's side of the protocol."""

    def __init__(self, boot: ManagedBoot, timeout_s: float = 10.0) -> None:
        self._base = f"{boot.control_url.rstrip('/')}/api/agent/runs/{boot.run_id}"
        self._jwt = boot.jwt
        self._deadman_s = boot.deadman_s
        self._timeout = timeout_s
        self._client: Optional[httpx.AsyncClient] = None
        self._last_ack = time.monotonic()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        assert self._client is not None
        response = await self._client.post(
            f"{self._base}/{path}",
            json=body,
            headers={"Authorization": f"Bearer {self._jwt}"},
        )
        if response.status_code == 409 and path != "claim":
            # Fenced: our lease is not the slot holder any more.
            detail = ""
            with contextlib.suppress(ValueError):
                detail = str(response.json().get("detail", ""))
            if "superseded" in detail.lower() or "holder" in detail.lower():
                raise Superseded(detail or "lease superseded")
        if response.status_code >= 400:
            raise ControlError(f"control {path} returned HTTP {response.status_code}: {response.text[:200]}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ControlError(f"control {path} returned a body that is not JSON") from exc

    async def _post_with_backoff(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        started = time.monotonic()
        attempt = 0
        while True:
            try:
                document = await self._post(path, body)
                self._last_ack = time.monotonic()
                return document
            except Superseded:
                raise
            except (httpx.HTTPError, ControlError) as exc:
                elapsed = time.monotonic() - started
                if elapsed >= self._deadman_s:
                    raise ControlError(
                        f"control {path} failed for {elapsed:.0f}s (dead-man {self._deadman_s:.0f}s): {exc}"
                    ) from exc
                delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** attempt)) * random.uniform(0.5, 1.5)
                log.warning("control %s failed (%s); retrying in %.1fs", path, exc, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def claim(self, holder: str, hint_slot: Optional[int], engine: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"holder": holder, "protocol_version": PROTOCOL_VERSION}
        if hint_slot is not None:
            body["hint_slot"] = hint_slot
        if engine:
            body["engine"] = engine
        return await self._post_with_backoff("claim", body)

    async def ready(self, slot: int, lease_id: str) -> Dict[str, Any]:
        return await self._post_with_backoff("ready", {"slot": slot, "lease_id": lease_id})

    async def heartbeat(
        self,
        slot: int,
        lease_id: str,
        stats: Dict[str, Any],
        state: str,
        interval: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """One heartbeat. None on a missed acknowledgement."""
        body: Dict[str, Any] = {
            "slot": slot,
            "lease_id": lease_id,
            "protocol_version": PROTOCOL_VERSION,
            "state": state,
            "stats": stats,
        }
        if interval:
            # What happened since the last heartbeat, with the raw buckets, so
            # the control plane's time series has honest fleet-wide
            # percentiles rather than an average of averages.
            body["interval"] = interval
        try:
            document = await self._post("heartbeat", body)
        except Superseded:
            raise
        except (httpx.HTTPError, ControlError) as exc:
            log.warning("heartbeat missed: %s", exc)
            return None
        if document.get("command") == "superseded":
            raise Superseded("lease superseded by the control plane")
        self._last_ack = time.monotonic()
        return document

    async def final(self, slot: int, lease_id: str, summary: Dict[str, Any], log_tail: List[str]) -> bool:
        body = {"slot": slot, "lease_id": lease_id, "summary": summary, "log_tail": log_tail}
        for attempt in range(4):
            try:
                await self._post("final", body)
                return True
            except Superseded:
                return False
            except (httpx.HTTPError, ControlError) as exc:
                log.warning("final report attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(min(5.0, BACKOFF_BASE_S * (2 ** attempt)))
        return False

    def deadman_expired(self) -> bool:
        return (time.monotonic() - self._last_ack) > self._deadman_s


def _materialise_scenario(claim: Dict[str, Any], root: Path) -> Path:
    """Write the scenario files the claim carried, so the loader reads them as usual.

    The files travel in the claim because a worker image only ships the
    built-in library, and a scenario imported through the web interface exists
    nowhere else. Writing them out keeps one loader for both cases.
    """
    scenario = claim.get("scenario") or {}
    name = Path(str(scenario.get("name") or "claimed")).name or "claimed"  # never a traversal
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    files = scenario.get("files") or {}
    if "scenario.yaml" not in files:
        raise ControlError("the claim carried no scenario.yaml")
    for filename, text in files.items():
        safe = Path(filename).name  # never a path, never a traversal
        (directory / safe).write_text(str(text), encoding="utf-8")
    return directory


def _config_from_claim(boot: ManagedBoot, claim: Dict[str, Any], scenario_dir: Path) -> Config:
    """Turn the claim response into the same Config a standalone worker has.

    The claim's ``env`` block uses the standalone variable names on purpose:
    one parser, one set of validations, no second way for a value to be wrong.
    """
    env: Dict[str, str] = {
        key: value for key, value in os.environ.items()
        if key.startswith("REG_") and key not in MANAGED_ONLY_KEYS
    }
    env.update({str(k): str(v) for k, v in (claim.get("env") or {}).items() if v is not None})
    env["REG_STANDALONE"] = "1"
    env["REG_SCENARIO"] = str(scenario_dir)
    env["REG_SLOT"] = str(int(claim.get("slot", 0)))
    env["REG_TOTAL_WORKERS"] = str(int(claim.get("total_workers", 1)))
    # The group's own numbering, for dealing out work that lives in this
    # group's scenario (a mixed run's api and browser groups have different
    # step lists). The global slot stays what keeps virtual-user ids disjoint.
    share = claim.get("share") or {}
    if share.get("group_slot") is not None:
        env["REG_GROUP_SLOT"] = str(int(share["group_slot"]))
    if share.get("group_workers"):
        env["REG_GROUP_WORKERS"] = str(int(share["group_workers"]))
    env["REG_RUN_LABEL"] = str(claim.get("run_label") or f"run{boot.run_id}")
    return load_config(env)


MANAGED_ONLY_KEYS = (
    "REG_RUN_ID", "REG_CONTROL_URL", "REG_RUN_JWT", "REG_RUN_JWT_FILE", "REG_HINT_SLOT",
    "REG_SLOT_BASE", "REG_SWARM_TASK_SLOT", "REG_WORKER_ENGINE", "REG_HOLDER", "REG_DEADMAN_S",
)


def _parse_t0(document: Dict[str, Any]) -> Optional[float]:
    value = document.get("t0")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def run_managed(boot: ManagedBoot) -> int:
    """The whole life of a fleet member. Returns the process exit code.

    Heartbeats start the moment the claim is granted, not when the worker is
    ready: a browser engine's start plus a validation against the target can
    take longer than a lease, and a healthy worker declared lost while it
    warmed up was the failure mode that stranded a fleet.
    """
    client = ControlClient(boot)
    await client.start()
    workdir = Path(tempfile.mkdtemp(prefix="regulator-"))
    engine = None
    hec: Optional[HecEmitter] = None
    heartbeat_task: Optional[asyncio.Task[None]] = None
    scheduler: Optional[Scheduler] = None
    stats: Optional[RunStats] = None
    released = asyncio.Event()
    t0_holder: Dict[str, Optional[float]] = {"t0": None}
    phase = {"name": "starting"}
    superseded = {"flag": False}
    stopped: Dict[str, Any] = {"flag": False, "reason": ""}

    def request_stop(reason: str) -> None:
        """A stop from any source: the control plane, a signal, the dead-man."""
        if not stopped["flag"]:
            log.info("stop requested: %s", reason)
        stopped["flag"] = True
        stopped["reason"] = stopped["reason"] or reason
        if scheduler is not None:
            scheduler.request_stop(reason)
        released.set()  # a stop before the release means: do not wait for one

    # The orchestrator's stop is a SIGTERM with a grace period. Without a
    # handler the process dies at once and the slot is lost with its data.
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signum, request_stop, f"signal {signum.name}")

    async def heartbeats(slot: int, lease_id: str, heartbeat_s: float) -> None:
        while True:
            snapshot = stats.snapshot() if stats is not None else {}
            interval = stats.take_interval(with_buckets=True) if stats is not None else None
            try:
                document = await client.heartbeat(slot, lease_id, snapshot, phase["name"], interval=interval)
            except Superseded:
                superseded["flag"] = True
                request_stop("lease superseded by the control plane")
                return
            if document is not None:
                command = str(document.get("command") or "continue")
                # The cache epoch clock is the control plane's: it evicts, and
                # every worker tags its records from the same instant.
                epoch = document.get("epoch")
                if stats is not None and isinstance(epoch, int) and epoch > 0 and epoch != stats.epoch:
                    stats.set_epoch(epoch, float(document.get("epoch_started_at") or time.time()))
                if command == "release":
                    t0 = _parse_t0(document)
                    if t0 is not None:
                        t0_holder["t0"] = t0
                    if phase["name"] == "ready":
                        released.set()
                elif command == "stop":
                    request_stop("stopped by the control plane")
            elif client.deadman_expired():
                log.error("no control-plane contact for %.0fs: draining", boot.deadman_s)
                request_stop("control plane unreachable")
                return
            await asyncio.sleep(heartbeat_s)

    try:
        claim = await client.claim(boot.holder, boot.hint_slot, engine=boot.engine)
        slot = int(claim.get("slot", 0))
        lease_id = str(claim.get("lease_id") or "")
        heartbeat_s = float(claim.get("heartbeat_s") or DEFAULT_HEARTBEAT_S)
        log.info(
            "claimed slot %d of %d for run %s (%s)",
            slot, int(claim.get("total_workers", 1)), boot.run_id, boot.holder,
        )
        heartbeat_task = asyncio.create_task(heartbeats(slot, lease_id, heartbeat_s), name="heartbeat")

        scenario_dir = _materialise_scenario(claim, workdir)
        try:
            config = _config_from_claim(boot, claim, scenario_dir)
            scenario = load_scenario(scenario_dir)
        except (ConfigError, ScenarioError) as exc:
            log.error("the claim could not be turned into a configuration: %s", exc)
            await client.final(slot, lease_id, _failure_summary(boot, slot, str(exc)), [])
            return EXIT_FAILED

        blocking = [line for line in lint(scenario) if not is_advice(line)]
        if blocking and config.lint_strict:
            message = "the scenario does not lint: " + "; ".join(blocking[:3])
            log.error(message)
            await client.final(slot, lease_id, _failure_summary(boot, slot, message), [])
            return EXIT_LINT
        if stopped["flag"]:
            await client.final(slot, lease_id, _failure_summary(boot, slot, stopped["reason"], outcome="stopped"), [])
            return EXIT_SUPERSEDED if superseded["flag"] else EXIT_OK

        engine_name = str(claim.get("engine") or "api")
        try:
            engine = get_engine(engine_name, config)
            await engine.start()
        except (ValueError, BrowserUnavailable) as exc:
            log.error("%s", exc)
            await client.final(slot, lease_id, _failure_summary(boot, slot, str(exc)), [])
            return EXIT_FAILED

        capabilities = await engine.probe()
        online = [line for line in await engine.validate(scenario) if not is_advice(line)]
        if online and config.lint_strict:
            message = "the scenario does not match the target: " + "; ".join(online[:3])
            log.error(message)
            await client.final(slot, lease_id, _failure_summary(boot, slot, message), [])
            return EXIT_LINT

        resolver = ParameterResolver(scenario, seed=config.seed)
        if resolver.dynamic_parameters:
            await engine.resolve_parameters(resolver)

        stats = RunStats(run_id=config.run_id, slot=slot)
        emitters: List[Any] = [NdjsonEmitter(path=config.output_path)]
        if config.hec is not None:
            hec = HecEmitter(config.hec, run_id=config.run_id)
            await hec.start()
            emitters.append(hec)

        scheduler = Scheduler(
            scenario=scenario,
            config=config,
            engine=engine,
            resolver=resolver,
            stats=stats,
            emitters=emitters,
            capabilities=capabilities,
        )
        scheduler.wire_histograms = True
        if stopped["flag"]:
            scheduler.request_stop(stopped["reason"])

        # Ready, then wait to be released with the fleet's shared T0. The
        # release rides on the ready response when the fleet is already
        # released (a late worker), otherwise on a heartbeat.
        phase["name"] = "ready"
        ready_doc = await client.ready(slot, lease_id)
        t0 = _parse_t0(ready_doc)
        if t0 is not None:
            t0_holder["t0"] = t0
        if t0_holder["t0"] is not None:
            released.set()
        await released.wait()
        if superseded["flag"]:
            return EXIT_SUPERSEDED
        phase["name"] = "running"

        summary = await scheduler.run(start_at=t0_holder["t0"])
        phase["name"] = "finishing"
        payload = summary.to_dict()
        if hec is not None:
            await hec.flush()
            payload["telemetry"] = hec.stats()
            hec.emit_summary(payload)
            await hec.flush()

        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        delivered = await client.final(slot, lease_id, payload, [])
        if not delivered:
            log.error("the final report could not be delivered; the summary is on stdout")
        return _exit_code(summary, superseded["flag"])
    except ControlError as exc:
        log.error("control plane: %s", exc)
        return EXIT_FAILED
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
        if hec is not None:
            with contextlib.suppress(Exception):
                await hec.close()
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.close()
        await client.close()


def _failure_summary(boot: ManagedBoot, slot: int, reason: str, outcome: str = "failed") -> Dict[str, Any]:
    """A summary for a worker that never got as far as a run."""
    return {
        "run_id": boot.run_id,
        "slot": slot,
        "scenario": "",
        "outcome": outcome,
        "valid": False,
        "invalid_reason": reason,
        "started_at": time.time(),
        "ended_at": time.time(),
        "duration_s": 0.0,
        "stats": RunStats(run_id=boot.run_id, slot=slot).snapshot(include_histograms=True),
    }


def _exit_code(summary: RunSummary, superseded: bool) -> int:
    if superseded:
        return EXIT_SUPERSEDED
    if summary.outcome == "failed":
        return EXIT_FAILED
    if not summary.valid:
        return EXIT_INVALID
    if summary.outcome == "aborted":
        return EXIT_GUARD_RAIL
    return EXIT_OK
