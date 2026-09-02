"""Baselines, and judging a run against one.

A baseline is a label pointing at a run. A pipeline says "compare against
main-green" and does not need to know which run that is this week, which is the
whole reason this is a label rather than a run id in a config file somewhere.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from regulator_agent.compare import GateError, compare_runs

from ..db import get_session
from ..models import Baseline, Run, is_terminal

log = logging.getLogger("regulator.server.baselines")

router = APIRouter(prefix="/api", tags=["baselines"])


class BaselineCreate(BaseModel):
    run_id: int
    label: str = Field(min_length=1, max_length=128)
    note: Optional[str] = None


class CompareRequest(BaseModel):
    baseline_run_id: Optional[int] = None
    baseline_label: Optional[str] = None
    gates: List[str] = Field(default_factory=list)
    allow_invalid: bool = False


def _summary_of(session: Session, run_id: int, what: str) -> Dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no {what} run with id {run_id}")
    if not run.summary_json:
        raise HTTPException(
            status_code=409,
            detail=(
                f"the {what} run {run_id} has no summary yet: it is {run.state}. A run "
                "that has not finished cannot be compared"
            ),
        )
    return run.summary_json


@router.get("/baselines")
def list_baselines(session: Session = Depends(get_session)) -> List[Dict[str, Any]]:
    rows = session.scalars(select(Baseline).order_by(Baseline.label)).all()
    return [
        {
            "label": b.label,
            "run_id": b.run_id,
            "scenario": b.scenario,
            "target_id": b.target_id,
            "created_at": b.created_at,
            "note": b.note,
        }
        for b in rows
    ]


@router.post("/baselines", status_code=201)
def create_baseline(
    body: BaselineCreate, session: Session = Depends(get_session)
) -> Dict[str, Any]:
    """Promote a run to a named baseline, replacing whatever held the label."""
    run = session.get(Run, body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run with id {body.run_id}")
    if not is_terminal(run.state):
        raise HTTPException(status_code=409, detail="the run has not finished yet")
    summary = run.summary_json or {}
    if not summary.get("valid", False):
        # A baseline everything else is judged against must not itself be a
        # measurement of the load generator.
        raise HTTPException(
            status_code=409,
            detail=(
                "refusing to make an invalid run a baseline: "
                f"{summary.get('invalid_reason') or 'the run did not measure the target'}"
            ),
        )

    existing = session.get(Baseline, body.label)
    if existing is not None:
        session.delete(existing)
        session.flush()

    baseline = Baseline(
        label=body.label,
        run_id=run.id,
        scenario=run.scenario,
        target_id=run.target_id,
        note=body.note,
    )
    session.add(baseline)
    log.info("baseline %r now points at run %s", body.label, run.id)
    return {"label": baseline.label, "run_id": baseline.run_id, "scenario": baseline.scenario}


@router.delete("/baselines/{label}", status_code=204)
def delete_baseline(label: str, session: Session = Depends(get_session)) -> None:
    baseline = session.get(Baseline, label)
    if baseline is None:
        raise HTTPException(status_code=404, detail=f"no baseline labelled {label!r}")
    session.delete(baseline)


@router.post("/runs/{run_id}/compare")
def compare(
    run_id: int, body: CompareRequest, session: Session = Depends(get_session)
) -> Dict[str, Any]:
    candidate = _summary_of(session, run_id, "candidate")

    baseline_summary = None
    baseline_run_id = body.baseline_run_id
    if body.baseline_label:
        baseline = session.get(Baseline, body.baseline_label)
        if baseline is None:
            raise HTTPException(
                status_code=404, detail=f"no baseline labelled {body.baseline_label!r}"
            )
        baseline_run_id = baseline.run_id
    if baseline_run_id is not None:
        baseline_summary = _summary_of(session, baseline_run_id, "baseline")

    try:
        result = compare_runs(
            candidate,
            baseline_summary,
            gates=body.gates,
            allow_invalid=body.allow_invalid,
        )
    except GateError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    payload = result.to_dict()
    payload["candidate_run_id"] = run_id
    payload["baseline_run_id"] = baseline_run_id
    payload["report"] = result.explain()
    return payload
