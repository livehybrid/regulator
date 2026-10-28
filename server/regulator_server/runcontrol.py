"""Changing a run after it has launched: label and note, pause, resume, load.

A run is a measurement, so each change is recorded on it (``controls_json``)
and drawn on its charts: a p95 that moved when the load doubled has to say the
load doubled. Every control works on both kinds of run. An in-process run's
scheduler is told directly; a fleet run's workers read the run row in their
next heartbeat reply, which carries ``paused`` and their share of the load as
state rather than one-shot commands.

Pausing stops issuing searches and stops the run clock (searches in flight
finish and are recorded), so the run still delivers its full duration of load;
resuming re-anchors the schedules, so the pause is not reported as latency.
A run paused for longer than ``REG_MAX_PAUSE_S`` in total is stopped, not
resumed, because whoever paused it may have done so to protect the cluster.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from .config import get_settings
from .fleet import STATE_RUNNING, _leases
from .models import Run, is_terminal
from .runner import manager


class ControlRefused(Exception):
    """The change cannot be made to this run as it stands."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def _record(run: Run, kind: str, **detail: Any) -> None:
    entries: List[Dict[str, Any]] = list(run.controls_json or [])
    entries.append({"at": time.time(), "kind": kind, **detail})
    # A new list, so the JSON column registers the change.
    run.controls_json = entries


def _require_running(run: Run) -> Optional[Any]:
    """The in-process scheduler, if it exists yet, or None.

    An in-process run reports running a moment before its scheduler exists
    (the target is probed and the scenario validated first). A control that
    arrives in that gap is recorded on the run and applied by the runner as
    soon as the scheduler is built, rather than refused.
    """
    if is_terminal(run.state):
        raise ControlRefused(f"the run has already {run.state}")
    if (run.fleet or "inprocess") == "inprocess":
        entry = manager.active_entry(run.id)
        if entry is None:
            raise ControlRefused("the run is not in flight on this control plane")
        return entry.scheduler
    if run.state != "running" or run.fleet_state != STATE_RUNNING:
        raise ControlRefused("the fleet is not running yet (or is draining)")
    return None


def _fleet_model(session, run: Run) -> Tuple[str, str, int]:
    """(model, engine, workers) of a fleet run's single group."""
    shares = [lease.share_json or {} for lease in _leases(session, run)]
    groups = {share.get("group") for share in shares}
    if len(groups) > 1:
        raise ControlRefused(
            "this run mixes api and browser workers, whose loads are not one figure: "
            "change it by launching again"
        )
    share = shares[0] if shares else {}
    return str(share.get("model") or ""), str(share.get("engine") or "api"), len(shares)


def edit(run: Run, body: Dict[str, Any]) -> None:
    """Label and note. Allowed at any time, finished or not."""
    if "label" in body:
        label = (body.get("label") or "").strip()
        run.label = label[:128] or None
    if "note" in body:
        note = (body.get("note") or "").strip()
        run.note = note or None


def pause(session, run: Run) -> None:
    scheduler = _require_running(run)
    if run.paused_at is not None:
        return
    if scheduler is not None:
        scheduler.pause()
    run.paused_at = time.time()
    _record(run, "pause")


def resume(session, run: Run) -> None:
    if run.paused_at is None:
        raise ControlRefused("the run is not paused")
    scheduler = _require_running(run)
    if scheduler is not None:
        scheduler.resume()
    now = time.time()
    run.paused_s = float(run.paused_s or 0.0) + max(0.0, now - run.paused_at)
    run.paused_at = None
    _record(run, "resume")


def set_load(session, run: Run, value: float) -> str:
    """Change the total load. Returns which figure it set."""
    scheduler = _require_running(run)
    settings = get_settings()
    if value < 0:
        raise ControlRefused("the load cannot be negative", status=422)
    in_process = (run.fleet or "inprocess") == "inprocess"
    if in_process:
        if scheduler is None:
            raise ControlRefused("the run is still starting; try again in a moment")
        model = scheduler.load.model
        if model == "closed" and value > settings.max_virtual_users:
            raise ControlRefused(
                f"{value:.0f} virtual users exceeds this control plane's ceiling of "
                f"{settings.max_virtual_users}; launch a fleet run for more", status=422)
        if model == "open" and value > settings.max_virtual_users * 60:
            raise ControlRefused(
                f"an arrival rate of {value:.0f}/min exceeds what this control plane can "
                "honestly generate; launch a fleet run for more", status=422)
        try:
            scheduler.set_load(value)
        except ValueError as exc:
            raise ControlRefused(str(exc))
    else:
        model, engine, workers = _fleet_model(session, run)
        if model not in ("closed", "open"):
            raise ControlRefused("the schedule model fires by cron and has no load figure to change")
        density = (settings.fleet.browser_contexts_per_worker if engine == "browser"
                   else settings.fleet.vus_per_worker)
        if model == "closed" and value > workers * density:
            raise ControlRefused(
                f"{value:.0f} virtual users is more than this fleet's {workers} worker(s) were "
                f"sized for ({workers * density}); launch again with more workers", status=422)
    run.load_override = float(value)
    figure = "virtual_users" if model == "closed" else "arrival_rate_per_min"
    _record(run, "load", **{figure: value})
    return figure


__all__ = ["ControlRefused", "edit", "pause", "resume", "set_load"]
