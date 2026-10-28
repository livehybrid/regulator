"""One run as a self-contained JSON document, and back again.

The document is the one ``GET /api/runs/{id}/export`` downloads: the run row and
its summary, the cluster's own account, the time series, and the scenario that
produced it inline. The config source (:mod:`.configsource`) archives every
finished run in this form and pulls them back into a rebuilt instance, which is
what lets run history and the baselines that point into it survive a teardown.

Re-importing is the half that needs care. A row id belongs to one database, so
a run is matched by its ``uid``; the target by name, because a rebuilt
instance numbers its targets differently too. Only the aggregate series travels
(the per-slot rows are a fleet's working detail, and roughly multiply the size
by the worker count), and worker leases are listed in the document but not
recreated, since a lease is a claim on a slot of a run that is long over.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from regulator_agent.scenario import ScenarioError, load_scenario

from .adapters import scenario_path, scenario_summary
from .models import Run, RunSample, Target, is_terminal

log = logging.getLogger("regulator.server.runarchive")

EXPORT_VERSION = 1

# The run fields a document carries back into a row. Explicit for the same
# reason configio's lists are: a new column should not start travelling, or
# stop, by accident.
RUN_FIELDS = (
    "label", "scenario", "state", "virtual_users", "duration_s",
    "arrival_rate_per_min", "pacing_s", "evict_cache", "evict_cache_indexes",
    "evict_every_s", "cold_window_s", "seed", "scenario_digest", "fleet",
    "workers", "fleet_state", "created_at", "started_at", "ended_at", "t0", "error",
)


class RunArchiveError(Exception):
    """A run document that cannot be read or applied."""


def build_document(session: Session, run: Run, target: Optional[Target], points: int = 5000) -> Dict[str, Any]:
    """The run as one JSON-serialisable document. Never carries a credential."""
    from .routes.runs import _serialise
    from .samples import markers_for, samples_for

    document: Dict[str, Any] = {
        "regulator_run_export_version": EXPORT_VERSION,
        "exported_at": time.time(),
        "run": _serialise(run, target.name if target is not None else None),
        "target": None,
        "scenario": None,
        "samples": samples_for(session, run.id, points=max(10, min(points, 100_000))),
        "markers": markers_for(run),
        "workers": [
            {
                "slot": lease.slot,
                "engine": lease.engine,
                "state": lease.state,
                "holder": lease.holder,
                "restarts": lease.restarts,
                "claimed_at": lease.claimed_at,
                "last_heartbeat_at": lease.last_heartbeat_at,
            }
            for lease in sorted(run.leases, key=lambda candidate: candidate.slot)
        ],
    }
    if target is not None:
        # Named, never credentialed: a result has to say what it measured.
        document["target"] = {
            "name": target.name,
            "mgmt_url": target.mgmt_url,
            "web_url": target.web_url,
            "app": target.app,
            "owner": target.owner,
            "api_version": target.api_version,
            "verify_tls": target.verify_tls,
        }
    # The scenario inline, because it can be edited after the run that used it
    # and a result nobody can reproduce is a screenshot with extra steps. The
    # digest on the run row is what proves this is the version that ran.
    try:
        directory, origin = scenario_path(run.scenario)
        scenario = load_scenario(directory)
        files: Dict[str, str] = {}
        for candidate in ("scenario.yaml", scenario.searches.file if scenario.searches else ""):
            if candidate and (directory / candidate).is_file():
                files[candidate] = (directory / candidate).read_text(encoding="utf-8")
        document["scenario"] = {
            **scenario_summary(scenario, origin),
            "digest": run.scenario_digest,
            "files": files,
        }
    except (FileNotFoundError, ScenarioError, OSError) as exc:
        # A run outlives the scenario that made it, and that is not a reason to
        # refuse the export. Say which version is missing instead.
        document["scenario"] = {
            "name": run.scenario,
            "digest": run.scenario_digest,
            "unavailable": str(exc),
        }
    return document


def uid_of(document: Dict[str, Any]) -> Optional[str]:
    run = document.get("run") if isinstance(document, dict) else None
    uid = run.get("uid") if isinstance(run, dict) else None
    return str(uid) if uid else None


def import_document(session: Session, document: Dict[str, Any], uid: Optional[str] = None) -> Optional[Run]:
    """Recreate the run a document describes. Returns None if it is already here.

    ``uid`` is the fallback identity for a document exported before runs had
    one (the archive passes its object name). The caller commits.
    """
    if not isinstance(document, dict):
        raise RunArchiveError("a run document must be a JSON object")
    version = document.get("regulator_run_export_version")
    try:
        if version is not None and int(version) > EXPORT_VERSION:
            raise RunArchiveError(
                f"run exported by a newer Regulator (version {version}, this instance "
                f"understands {EXPORT_VERSION})"
            )
    except (TypeError, ValueError):
        raise RunArchiveError(f"regulator_run_export_version is not a number: {version!r}")
    row = document.get("run")
    if not isinstance(row, dict) or not row.get("scenario"):
        raise RunArchiveError("the document has no run in it")
    uid = uid_of(document) or uid
    if not uid:
        raise RunArchiveError("the run has no uid to match it by")
    if not is_terminal(str(row.get("state") or "")):
        # Only a finished run is history; an in-flight one belongs to the
        # instance that is running it.
        raise RunArchiveError(f"run {uid} is {row.get('state')!r}, not finished")
    if session.scalar(select(Run.id).where(Run.uid == uid)) is not None:
        return None

    target_id = None
    target_doc = document.get("target")
    if isinstance(target_doc, dict) and target_doc.get("name"):
        target_id = session.scalar(select(Target.id).where(Target.name == target_doc["name"]))

    run = Run(uid=uid, target_id=target_id, archived_at=time.time())
    for name in RUN_FIELDS:
        if name in row:
            setattr(run, name, row[name])
    run.stats_json = row.get("stats")
    run.summary_json = row.get("summary")
    session.add(run)
    session.flush()

    for sample in document.get("samples") or []:
        if not isinstance(sample, dict) or sample.get("at") is None:
            continue
        session.add(RunSample(
            run_id=run.id,
            slot=None,
            at=float(sample["at"]),
            elapsed_s=float(sample.get("elapsed_s") or 0.0),
            executions=int(sample.get("executions") or 0),
            errors=int(sample.get("errors") or 0),
            in_flight=int(sample.get("in_flight") or 0),
            searches_queued=int(sample.get("searches_queued") or 0),
            throughput_per_s=float(sample.get("throughput_per_s") or 0.0),
            interval_json=sample.get("interval") or None,
            cum_json=sample.get("cum") or None,
        ))
    session.flush()
    return run


__all__ = [
    "EXPORT_VERSION", "RUN_FIELDS", "RunArchiveError",
    "build_document", "import_document", "uid_of",
]
