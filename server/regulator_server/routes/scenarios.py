"""The scenario library, and building scenarios from Splunk's own search format.

Two libraries: the one that ships in the image, read only, and the operator's
own, written here. A scenario created through this API is a directory holding
the pasted ``savedsearches.conf`` verbatim and a generated ``scenario.yaml``
that points at it, so what runs is exactly what was pasted and a Splunk admin
can read both files.

A scenario also moves between instances as a single ``.tar.gz``: Download and
Upload are the two directions, and a configured scenario source (S3 or a
mounted directory, see :mod:`..scenariosource`) is the same artefact pulled at
boot and pushed on save, so a second control plane needs no shell access to
end up with the same library.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

from regulator_agent import savedsearches as ss
from regulator_agent.scenario import ScenarioError, lint, load_scenario

from .. import scenariosource
from ..adapters import list_scenarios, scenario_path, scenario_summary, user_scenarios_dir
from ..audit import record as audit_record
from ..config import get_settings
from ..scenarioarchive import (
    ArchiveLimits,
    ScenarioArchiveError,
    archive_name,
    export_scenario_bytes,
    extract_scenario,
)
from ..schemas import SavedSearchPreview, ScenarioCreate, ScenarioOut

log = logging.getLogger("regulator.server.scenarios")

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

# A second router because the scenario SOURCE is not a scenario: hanging it off
# /api/scenarios would put a literal path in the same space as /{name} and
# leave the two depending on declaration order.
source_router = APIRouter(prefix="/api", tags=["scenarios"])


@source_router.get("/scenario-source")
def get_scenario_source() -> Dict[str, Any]:
    """Where scenarios are pulled from and pushed to, so the console can say so.

    Reported because the feature is otherwise invisible: the library would be
    synced at boot and mirrored on save because two environment variables
    happened to be set, with nothing in the product to confirm it. Read only
    and credential free (see :func:`..scenariosource.describe`).
    """
    return scenariosource.describe()


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
    # Mirror to the scenario source when one is configured for writing, so a
    # scenario written here reaches the other instances pulling from it. Best
    # effort: a refused write must never fail the save that produced it.
    scenariosource.publish_scenario(body.name, directory)
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


# --------------------------------------------------------------------------- #
# Archives: one scenario in, one scenario out
# --------------------------------------------------------------------------- #

@router.get("/{name}/export")
def export_scenario(name: str) -> Response:
    """Download a scenario as the ``.tar.gz`` that Upload accepts elsewhere.

    Works for a built-in scenario as well as an operator's own: copying one of
    the shipped scenarios to another instance and editing it there is a
    reasonable thing to want, and the archive is the same shape either way.
    Reproducible, so re-exporting an unchanged scenario gives identical bytes
    and a checksum is worth comparing.
    """
    try:
        directory, _origin = scenario_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        payload = export_scenario_bytes(directory, name)
    except ScenarioArchiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    filename = archive_name(name)
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Regulator-Scenario": name,
        },
    )


@router.post("/upload", status_code=201)
async def upload_scenario(
    request: Request,
    file: UploadFile = File(..., description="a .tar.gz/.tgz/.tar or .zip of one scenario"),
    name: Optional[str] = Form(default=None, description="override the scenario's name"),
    replace: bool = Form(default=False, description="overwrite a scenario of the same name"),
) -> Dict[str, Any]:
    """Upload a scenario archive into the operator's library.

    The mirror of Download, and the no-git, no-shell path onto an instance. The
    archive is untrusted input, so extraction is the careful part and lives in
    :mod:`..scenarioarchive`; what lands here is a scenario that has been
    loaded and linted, because an archive that extracts but does not parse is a
    scenario that would fail at launch instead of at upload.
    """
    settings = get_settings()
    cap = settings.scenario_upload_max_archive_bytes
    data = await file.read(cap + 1)
    if len(data) > cap:
        raise HTTPException(
            status_code=413,
            detail=f"the archive is larger than the {cap}-byte upload limit",
        )
    if not data:
        raise HTTPException(status_code=422, detail="the upload is empty")

    library = user_scenarios_dir()
    try:
        directory = extract_scenario(
            data,
            library,
            limits=ArchiveLimits.from_settings(settings),
            name_hint=name or None,
            overwrite=bool(replace),
        )
    except ScenarioArchiveError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Loaded and linted AFTER extraction, with the directory removed again on
    # failure: a scenario that does not parse must not sit in the library
    # waiting to fail at launch.
    try:
        scenario = load_scenario(directory)
        problems = [line for line in lint(scenario) if not line.startswith("advice: ")]
    except Exception as exc:  # noqa: BLE001 - any parse failure at all is a 422
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(
            status_code=422, detail=f"the archive extracted but does not load: {exc}"
        )
    if problems and any("does not lint" in p or "needs" in p for p in problems):
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(
            status_code=422, detail="the scenario does not lint: " + "; ".join(problems[:5])
        )

    audit_record(
        "scenario_uploaded",
        request=request,
        detail=f"{directory.name} ({len(data)} bytes, {len(scenario.steps)} step(s))",
    )
    scenariosource.publish_scenario(directory.name, directory)
    summary = scenario_summary(scenario, "user")
    summary["advice"] = [line for line in lint(scenario) if line.startswith("advice: ")]
    return summary


@router.post("/{name}/publish")
def publish_scenario(name: str, request: Request) -> Dict[str, Any]:
    """Push one scenario to the configured source, now.

    Scenarios created here are mirrored automatically, so this exists for the
    two cases that is not enough for: confirming that a push actually landed,
    and mirroring a scenario that predates the source being configured. A
    missing or read-only source is a 409, because each is configuration the
    operator must change rather than a transient failure, and saying so is
    more use than a silent success.
    """
    try:
        directory, _origin = scenario_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    info = scenariosource.describe()
    if not info["configured"]:
        raise HTTPException(
            status_code=409,
            detail=f"no scenario source is configured (set {scenariosource.SOURCE_ENV})",
        )
    if info.get("error"):
        raise HTTPException(status_code=409, detail=info["error"])
    if not info["writable"]:
        raise HTTPException(
            status_code=409,
            detail=f"the scenario source is read-only (set {scenariosource.WRITE_ENV}=1)",
        )
    key = scenariosource.publish_scenario(name, directory)
    if key is None:
        # publish_scenario never raises, so an operator-initiated push has to
        # turn its None back into something the console can show.
        raise HTTPException(
            status_code=502,
            detail="the scenario source refused the write; see the control-plane log",
        )
    audit_record("scenario_published", request=request, detail=f"{name} -> {key}")
    return {"published": True, "key": key, "location": info["location"]}
