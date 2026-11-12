"""Running the ingest-to-searchable probe alongside a run, in either fleet mode.

The probe itself is ``regulator_agent.freshness``. This is the control plane's
half: building it from the run and its target, starting it with the load, and
collecting what it found. The control plane owns it rather than the workers,
for the same reason it owns periodic eviction: one probe a run, however many
workers there are.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from regulator_agent.freshness import FreshnessProbe
from regulator_agent.splunk import SplunkClient

from .config import get_settings
from .crypto import decrypt
from .db import session_scope
from .models import Run

log = logging.getLogger("regulator.server.freshness")


def build(run_id: int, client: SplunkClient) -> Tuple[Optional[FreshnessProbe], Optional[str]]:
    """The run's probe, or None and why not. Never raises."""
    try:
        with session_scope() as session:
            run = session.get(Run, run_id)
            spec = run.freshness_json if run is not None else None
            if not spec:
                return None, None
            target = run.target
            if target is None or not target.hec_url or not target.hec_token_encrypted:
                return None, "the target has no HEC address and token any more"
            return (
                FreshnessProbe(
                    hec_url=target.hec_url,
                    hec_token=decrypt(target.hec_token_encrypted) or "",
                    client=client,
                    index=str(spec.get("index") or ""),
                    run_label=f"r{run_id}",
                    interval_s=float(spec.get("interval_s") or 10.0),
                    verify_tls=bool(target.verify_tls),
                    ca_bundle=get_settings().ca_bundle,
                ),
                None,
            )
    except Exception as exc:  # noqa: BLE001 - a probe never stops a run starting
        log.warning("could not set up the freshness probe for run %s: %s", run_id, exc)
        return None, f"{type(exc).__name__}: {exc}"[:300]


def start(probe: Optional[FreshnessProbe]) -> Optional["asyncio.Task[None]"]:
    return asyncio.create_task(probe.run()) if probe is not None else None


async def finish(
    probe: Optional[FreshnessProbe],
    task: Optional["asyncio.Task[None]"],
    started_at: Optional[float],
    ended_at: Optional[float],
    problem: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Wait out the probe's follow-up and return what it measured.

    ``stop`` should already have been called when the load ended, so the
    follow-up overlaps whatever else happens after a run.
    """
    if probe is None:
        if problem:
            return {"available": False, "reason": problem, "findings": [f"No ingest-to-searchable figure: {problem}."]}
        return None
    probe.stop()
    if task is not None:
        try:
            await task
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal
            log.warning("the freshness probe failed: %s", exc)
    try:
        return probe.result(started_at, ended_at)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not summarise the freshness probe: %s", exc, exc_info=True)
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}
