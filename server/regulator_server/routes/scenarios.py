"""The scenario library, and building scenarios from Splunk's own search format.

Two libraries: the one that ships in the image, read only, and the operator's
own, written here. A scenario created through this API is a directory holding
the pasted ``savedsearches.conf`` verbatim and a generated ``scenario.yaml``
that points at it, so what runs is exactly what was pasted and a Splunk admin
can read both files.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from regulator_agent import savedsearches as ss
from regulator_agent.scenario import ScenarioError, lint, load_scenario

from ..adapters import list_scenarios, scenario_path, scenario_summary, user_scenarios_dir
from ..audit import record as audit_record
from ..schemas import SavedSearchPreview, ScenarioCreate, ScenarioOut

log = logging.getLogger("regulator.server.scenarios")

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.get("", response_model=List[ScenarioOut])
def get_scenarios() -> List[Dict[str, Any]]:
    return [scenario_summary(scenario, origin) for scenario, origin in list_scenarios()]


@router.get("/{name}")
def get_scenario(name: str) -> Dict[str, Any]:
    """One scenario in full: its summary, its steps and its files."""
    try:
        directory, origin = scenario_path(name)
        scenario = load_scenario(directory)
    except (FileNotFoundError, ScenarioError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    steps = [
        {
            "persona": persona.name,
            "persona_weight": persona.weight,
            "id": step.id,
            "type": step.type,
            "engine": step.engine,
            "class": step.step_class,
            "spl": step.spl,
            "saved": step.saved,
            "dispatch": step.dispatch,
            "cron": step.cron,
            "weight": step.weight,
            "app": step.app,
            "dashboard": step.dashboard,
        }
        for persona in scenario.personas
        for step in persona.steps
    ]
    files: Dict[str, str] = {}
    for candidate in ("scenario.yaml", scenario.searches.file if scenario.searches else ""):
        if candidate and (directory / candidate).is_file():
            files[candidate] = (directory / candidate).read_text(encoding="utf-8")
    return {
        **scenario_summary(scenario, origin),
        "steps": steps,
        "saved_selected": list(scenario.saved_selected),
        "saved_skipped": dict(scenario.saved_skipped),
        "files": files,
    }


@router.post("/preview", response_model=List[SavedSearchPreview])
def preview_savedsearches(body: Dict[str, Any]) -> List[SavedSearchPreview]:
    """What a pasted savedsearches.conf would turn into, before anything is saved."""
    text = str(body.get("savedsearches") or "")
    if not text.strip():
        raise HTTPException(status_code=422, detail="savedsearches is empty")
    searches = ss.parse_savedsearches(text, app_hint=body.get("app") or None)
    selection = ss.select_searches(
        searches,
        only_enabled=bool(body.get("only_enabled", True)),
        only_scheduled=bool(body.get("only_scheduled", False)),
        allow_side_effects=bool(body.get("allow_side_effects", False)),
    )
    previews = []
    for search in searches:
        firings = None
        if search.cron:
            try:
                firings = round(ss.cron_firings_per_day(search.cron), 3)
            except ss.CronError:
                firings = None
        previews.append(
            SavedSearchPreview(
                name=search.name,
                app=search.app,
                search=search.search,
                cron=search.cron,
                scheduled=search.scheduled,
                disabled=search.disabled,
                earliest=search.earliest,
                latest=search.latest,
                guessed_class=search.annotated_class or ss.classify(search.search, search.earliest),
                side_effects=ss.side_effects(search.search),
                firings_per_day=firings,
                skipped_reason=selection.skipped.get(search.name),
            )
        )
    return previews


def _scenario_yaml(body: ScenarioCreate, sourcetypes: List[str]) -> Dict[str, Any]:
    """The scenario.yaml for a set of saved searches, in the shape the loader reads."""
    load: Dict[str, Any]
    if body.load_model == "schedule":
        load = {"model": "schedule", "duration": f"{int(body.duration_s)}s"}
        if body.schedule_start:
            load["schedule_start"] = body.schedule_start
    else:
        load = {
            "model": "closed",
            "virtual_users": body.virtual_users,
            "duration": f"{int(body.duration_s)}s",
            "ramp": [
                {"to": body.virtual_users, "over_s": min(60, int(body.duration_s) // 4 or 1)},
            ],
        }
    seed = body.seed or (abs(hash(body.name)) % 900_000 + 100_000)
    return {
        "name": body.name,
        "engine": "api",
        "seed": seed,
        "description": body.description
        or f"Imported saved searches ({body.load_model} model), in Splunk's own format",
        "tags": ["imported", "savedsearches", body.load_model],
        "corpus": {"index": body.index, "sourcetypes": sourcetypes},
        "time_policy": {"mode": "rolling", "window": "24h", "jitter": "30m", "align": "1m"},
        "searches": {
            "file": "savedsearches.conf",
            **({"app": body.app} if body.app else {}),
            "only_enabled": body.only_enabled,
            "only_scheduled": body.only_scheduled or body.load_model == "schedule",
            "allow_side_effects": body.allow_side_effects,
            "time_from_saved": body.time_from_saved,
        },
        "personas": [
            {
                "name": "scheduler" if body.load_model == "schedule" else "analyst",
                "weight": 100,
                "think_time": {
                    "dist": "lognormal",
                    "median_s": body.think_median_s or 1,
                    "sigma": 0.5,
                    "min_s": 1,
                    "max_s": max(600.0, body.think_median_s * 4),
                },
                "steps_from": "saved",
                "weight_by": "cron",
                "walk": "sample",
            }
        ],
        "load": load,
        "abort_if": {"error_rate_pct": 25, "p95_ms": 300000, "generator_drift_ms": 3000},
    }


_SOURCETYPE_RE = __import__("re").compile(r"sourcetype\s*=\s*\"?([A-Za-z0-9_:.*-]+)\"?")


def _sourcetypes_in(searches: List[ss.SavedSearch]) -> List[str]:
    """The sourcetypes the searches name outright, for the corpus check."""
    found: List[str] = []
    for search in searches:
        for match in _SOURCETYPE_RE.finditer(search.search):
            value = match.group(1)
            if "*" in value or value in found:
                continue
            found.append(value)
    return found[:20]


@router.post("", status_code=201)
def create_scenario(body: ScenarioCreate, request: Request) -> Dict[str, Any]:
    """Create a scenario from a savedsearches.conf.

    The conf is stored verbatim. The scenario.yaml is generated, linted and
    the result reported before anything is committed, so a file with no
    usable searches never becomes a scenario that runs nothing.
    """
    try:
        scenario_path(body.name)
    except FileNotFoundError:
        pass
    else:
        raise HTTPException(status_code=409, detail=f"a scenario named {body.name!r} already exists")

    searches = ss.parse_savedsearches(body.savedsearches, app_hint=body.app)
    if not searches:
        raise HTTPException(status_code=422, detail="the file holds no stanzas")

    root = user_scenarios_dir()
    root.mkdir(parents=True, exist_ok=True)
    directory = root / body.name
    if directory.exists():
        raise HTTPException(status_code=409, detail=f"a scenario named {body.name!r} already exists")
    directory.mkdir()
    try:
        (directory / "savedsearches.conf").write_text(body.savedsearches, encoding="utf-8")
        document = _scenario_yaml(body, _sourcetypes_in(searches))
        (directory / "scenario.yaml").write_text(
            "# Generated by Regulator from an imported savedsearches.conf.\n"
            "# Edit freely: the searches live in savedsearches.conf next to this file.\n"
            + yaml.safe_dump(document, sort_keys=False),
            encoding="utf-8",
        )
        scenario = load_scenario(directory)
        problems = [line for line in lint(scenario) if not line.startswith("advice: ")]
        selected = list(scenario.saved_selected)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail=(
                    "no search was selected from the file: "
                    + "; ".join(f"{k}: {v}" for k, v in list(scenario.saved_skipped.items())[:5])
                ),
            )
        if problems and any("does not lint" in p or "needs" in p for p in problems):
            raise HTTPException(status_code=422, detail="the scenario does not lint: " + "; ".join(problems[:5]))
    except HTTPException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - never leave a half-written scenario behind
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc))

    audit_record("scenario_created", request=request, detail=f"{body.name}: {len(selected)} searches")
    summary = scenario_summary(scenario, "user")
    summary["saved_selected"] = selected
    summary["saved_skipped"] = dict(scenario.saved_skipped)
    return summary


@router.delete("/{name}", status_code=204)
def delete_scenario(name: str, request: Request) -> None:
    try:
        directory, origin = scenario_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if origin != "user":
        raise HTTPException(
            status_code=403, detail="built-in scenarios ship with the image and cannot be deleted"
        )
    shutil.rmtree(directory)
    audit_record("scenario_deleted", request=request, detail=name)


@router.get("/{name}/savedsearches.conf", response_class=PlainTextResponse)
def get_scenario_conf(name: str) -> str:
    try:
        directory, _ = scenario_path(name)
        scenario = load_scenario(directory)
    except (FileNotFoundError, ScenarioError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if scenario.searches is None:
        raise HTTPException(status_code=404, detail="this scenario has no savedsearches.conf")
    conf = directory / scenario.searches.file
    if not conf.is_file():
        raise HTTPException(status_code=404, detail="this scenario has no savedsearches.conf")
    return conf.read_text(encoding="utf-8")
