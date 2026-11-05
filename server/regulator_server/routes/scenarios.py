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
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from regulator_agent import savedsearches as ss
from regulator_agent.report import scenario_fit
from regulator_agent.scenario import ScenarioError, lint, load_scenario
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import scenariosource
from ..adapters import (
    list_scenarios,
    scenario_path,
    scenario_summary,
    set_overlay_tags,
    user_scenarios_dir,
)
from ..audit import record as audit_record
from ..config import get_settings
from ..db import get_session
from ..models import TERMINAL_STATES, Baseline, Run, Target
from ..scenarioarchive import (
    ArchiveLimits,
    ScenarioArchiveError,
    archive_name,
    export_scenario_bytes,
    extract_scenario,
)
from ..schemas import (
    SavedSearchPreview,
    ScenarioCreate,
    ScenarioDuplicate,
    ScenarioOut,
    ScenarioRename,
    ScenarioTags,
    ScenarioUpdate,
)

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


@source_router.get("/scenario-fit")
def get_scenario_fit(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Which scenarios each target holds data for, from its last stored Report.

    Worked out again from the stored sourcetype census against the library as
    it is now, rather than read back from the report, so a scenario imported
    or edited since the Report was taken still gets a fit. A target with no
    report yet is listed as untested: nothing is known about it.
    """
    library = [scenario_summary(sc, origin) for sc, origin in list_scenarios()]
    targets = []
    for target in session.scalars(select(Target).order_by(Target.id)):
        report = target.last_report_json or None
        if not report or not report.get("reachable", True) or report.get("sourcetypes") is None:
            targets.append(
                {"id": target.id, "name": target.name, "reported_at": None, "fit": None}
            )
            continue
        fits = scenario_fit(report, library)
        targets.append(
            {
                "id": target.id,
                "name": target.name,
                "reported_at": report.get("generated_at"),
                "fit": {f["name"]: {"fit": f["fit"], "missing": f["missing"]} for f in fits},
            }
        )
    return {"targets": targets}


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
        "searches_file": scenario.searches.file if scenario.searches else None,
        "saved_searches_rows": _search_rows(directory, scenario),
        "files": files,
    }


def _search_rows(directory: Path, scenario: Any) -> List[Dict[str, Any]]:
    """The scenario's savedsearches.conf as editable rows, in file order."""
    if scenario.searches is None:
        return []
    conf = directory / scenario.searches.file
    if not conf.is_file():
        return []
    rows = []
    for search in ss.parse_savedsearches(conf.read_text(encoding="utf-8")):
        rows.append(
            {
                "name": search.name,
                "search": search.raw_search or search.search,
                "earliest": search.earliest,
                "latest": search.latest,
                "cron": search.cron or "",
                "scheduled": search.scheduled,
                "disabled": search.disabled,
            }
        )
    return rows


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


# --------------------------------------------------------------------------- #
# Editing: in place for the operator's own, by copy for the built-in ones
# --------------------------------------------------------------------------- #

def _user_scenario(name: str) -> Path:
    """The directory of one of the operator's own scenarios, or the reason not."""
    try:
        directory, origin = scenario_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if origin != "user":
        raise HTTPException(
            status_code=403,
            detail="built-in scenarios ship with the image and are read only: duplicate it to edit",
        )
    return directory


def _set_top_level(text: str, key: str, value: Any) -> Optional[str]:
    """Replace one top-level ``key`` in YAML text, keeping everything else.

    The scenario files carry long explanatory comments, and a full dump would
    throw every one of them away to change a tag. A key written on one line,
    or as a block list under it, is swapped in place; anything else (a nested
    mapping, a folded string) returns None and the caller rewrites the
    document instead.
    """
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if re.match(rf"^{re.escape(key)}:(\s|$)", line)]
    if len(hits) != 1:
        return None
    first = hits[0]
    inline = lines[first][len(key) + 1:].strip()
    last = first
    if not inline or inline.startswith("#"):
        # A block list: the "- item" lines (at column 0, as PyYAML writes
        # them, or indented) that follow, and nothing else.
        j = first + 1
        while j < len(lines) and re.match(r"^\s*- ", lines[j]):
            last = j
            j += 1
        if last == first:
            return None
    elif inline[0] in "|>":
        return None
    rendered = yaml.safe_dump({key: value}, default_flow_style=True, width=10_000, allow_unicode=True)
    rendered = rendered.strip()
    if rendered.startswith("{") and rendered.endswith("}"):
        rendered = rendered[1:-1].strip()
    if "\n" in rendered:
        return None
    return "\n".join(lines[:first] + [rendered] + lines[last + 1:])


def _apply_yaml_fields(text: str, fields: Dict[str, Any], corpus: Dict[str, Any]) -> str:
    """scenario.yaml with the form's fields changed, comments kept where possible."""
    out = text
    pending: Dict[str, Any] = {}
    for key, value in fields.items():
        replaced = _set_top_level(out, key, value)
        if replaced is None:
            pending[key] = value
        else:
            out = replaced
    if not pending and not corpus:
        return out
    document = yaml.safe_load(out) or {}
    if not isinstance(document, dict):
        raise HTTPException(status_code=422, detail="scenario.yaml is not a mapping")
    document.update(pending)
    if corpus:
        merged = dict(document.get("corpus") or {})
        merged.update(corpus)
        document["corpus"] = merged
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _render_rows(existing_text: str, rows: List[Dict[str, Any]]) -> str:
    """A savedsearches.conf from edited rows, keeping each stanza's other keys."""
    existing = ss.parse_conf(existing_text) if existing_text else {}
    stanzas: Dict[str, Dict[str, str]] = {}
    for row in rows:
        name = row["name"]
        if name in stanzas:
            raise HTTPException(status_code=422, detail=f"two searches are named {name!r}")
        base = dict(existing.get(row.get("original_name") or name, {}))
        base[ss.KEY_SEARCH] = row["search"]
        for key, value in (
            (ss.KEY_EARLIEST, row.get("earliest") or ""),
            (ss.KEY_LATEST, row.get("latest") or ""),
            (ss.KEY_CRON, row.get("cron") or ""),
        ):
            if value:
                base[key] = value
            else:
                base.pop(key, None)
        base[ss.KEY_ENABLE_SCHED] = "1" if row.get("scheduled") else "0"
        if row.get("disabled"):
            base[ss.KEY_DISABLED] = "1"
        else:
            base.pop(ss.KEY_DISABLED, None)
        # The search goes first, as a person would write the stanza.
        stanzas[name] = {ss.KEY_SEARCH: base.pop(ss.KEY_SEARCH), **base}
    return ss.render_conf(stanzas, header="Edited in Regulator.")


def _check_and_swap(directory: Path, files: Dict[str, str]) -> Any:
    """Write ``files`` over a copy of the scenario, load and lint it, then swap.

    Nothing touches the live directory until the edited copy loads and lints,
    so a bad edit is a 422 and the scenario that was there still runs.
    """
    staging = directory.parent / f".edit-{directory.name}-{uuid.uuid4().hex[:8]}"
    shutil.copytree(directory, staging)
    try:
        for rel, content in files.items():
            target = (staging / rel).resolve()
            if staging.resolve() not in target.parents:
                raise HTTPException(status_code=422, detail=f"{rel!r} is outside the scenario")
            target.write_text(content, encoding="utf-8")
        try:
            scenario = load_scenario(staging)
        except Exception as exc:  # noqa: BLE001 - any parse failure is the operator's to fix
            raise HTTPException(status_code=422, detail=f"the edited scenario does not load: {exc}")
        if scenario.name != directory.name:
            raise HTTPException(
                status_code=422,
                detail=f"the scenario's name must stay {directory.name!r}: duplicate it to rename",
            )
        problems = [line for line in lint(scenario) if not line.startswith("advice: ")]
        if problems and any("does not lint" in p or "needs" in p for p in problems):
            raise HTTPException(
                status_code=422, detail="the edited scenario does not lint: " + "; ".join(problems[:5])
            )
        if scenario.searches is not None and not scenario.saved_selected:
            raise HTTPException(
                status_code=422,
                detail=(
                    "no search would be selected after this edit: "
                    + "; ".join(f"{k}: {v}" for k, v in list(scenario.saved_skipped.items())[:5])
                ),
            )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    retired = directory.parent / f".old-{directory.name}-{uuid.uuid4().hex[:8]}"
    directory.rename(retired)
    staging.rename(directory)
    shutil.rmtree(retired, ignore_errors=True)
    return load_scenario(directory)


@router.put("/{name}")
def update_scenario(name: str, body: ScenarioUpdate, request: Request) -> Dict[str, Any]:
    """Edit one of the operator's own scenarios.

    Either the raw files (``files``) or the form fields; raw wins when both
    are sent, because it is the more specific statement of intent.
    """
    directory = _user_scenario(name)
    scenario = load_scenario(directory)
    files: Dict[str, str] = {}
    if body.files:
        allowed = {"scenario.yaml"} | ({scenario.searches.file} if scenario.searches else set())
        unknown = set(body.files) - allowed
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"only {', '.join(sorted(allowed))} can be edited here"
            )
        files = dict(body.files)
    else:
        yaml_text = (directory / "scenario.yaml").read_text(encoding="utf-8")
        fields: Dict[str, Any] = {}
        corpus: Dict[str, Any] = {}
        if body.description is not None:
            fields["description"] = body.description
        if body.tags is not None:
            fields["tags"] = list(body.tags)
        if body.index is not None:
            corpus["index"] = body.index
        if body.searches is not None:
            if scenario.searches is None:
                raise HTTPException(
                    status_code=422,
                    detail="this scenario defines its steps in scenario.yaml: edit the raw file",
                )
            conf_path = directory / scenario.searches.file
            current = conf_path.read_text(encoding="utf-8") if conf_path.is_file() else ""
            conf_text = _render_rows(current, [r.model_dump() for r in body.searches])
            files[scenario.searches.file] = conf_text
            if body.sourcetypes is None:
                corpus["sourcetypes"] = _sourcetypes_in(ss.parse_savedsearches(conf_text))
        if body.sourcetypes is not None:
            corpus["sourcetypes"] = [st for st in body.sourcetypes if st]
        if fields or corpus:
            files["scenario.yaml"] = _apply_yaml_fields(yaml_text, fields, corpus)
    if not files:
        raise HTTPException(status_code=422, detail="nothing to change")
    updated = _check_and_swap(directory, files)
    audit_record("scenario_edited", request=request, detail=f"{name}: {', '.join(sorted(files))}")
    scenariosource.publish_scenario(name, directory)
    return scenario_summary(updated, "user")


@router.put("/{name}/tags")
def set_scenario_tags(name: str, body: ScenarioTags, request: Request) -> Dict[str, Any]:
    """Retag a scenario: in its scenario.yaml if it is the operator's, else in the overlay."""
    try:
        directory, origin = scenario_path(name)
        scenario = load_scenario(directory)
    except (FileNotFoundError, ScenarioError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    tags = body.tags
    if origin == "builtin":
        # Setting a built-in's tags back to what it ships with clears the
        # override, so a later image that retags it is not shadowed.
        set_overlay_tags(name, None if tags == list(scenario.tags) else tags)
        updated = scenario
    else:
        text = (directory / "scenario.yaml").read_text(encoding="utf-8")
        updated = _check_and_swap(directory, {"scenario.yaml": _apply_yaml_fields(text, {"tags": tags}, {})})
        scenariosource.publish_scenario(name, directory)
    audit_record("scenario_tagged", request=request, detail=f"{name}: {', '.join(tags) or '(none)'}")
    return scenario_summary(updated, origin)


@router.post("/{name}/duplicate", status_code=201)
def duplicate_scenario(name: str, body: ScenarioDuplicate, request: Request) -> Dict[str, Any]:
    """Copy any scenario into the operator's library under a new name, to edit it."""
    try:
        source, origin = scenario_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        scenario_path(body.name)
    except FileNotFoundError:
        pass
    else:
        raise HTTPException(status_code=409, detail=f"a scenario named {body.name!r} already exists")
    root = user_scenarios_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / body.name
    if target.exists():
        raise HTTPException(status_code=409, detail=f"a scenario named {body.name!r} already exists")
    shutil.copytree(source, target)
    try:
        text = (target / "scenario.yaml").read_text(encoding="utf-8")
        fields: Dict[str, Any] = {"name": body.name}
        if origin == "builtin":
            summary_tags = scenario_summary(load_scenario(source), origin)["tags"]
            fields["tags"] = list(summary_tags)
        (target / "scenario.yaml").write_text(_apply_yaml_fields(text, fields, {}), encoding="utf-8")
        scenario = load_scenario(target)
    except HTTPException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - never leave a half-written copy behind
        shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"the copy does not load: {exc}")
    audit_record("scenario_duplicated", request=request, detail=f"{name} -> {body.name}")
    scenariosource.publish_scenario(body.name, target)
    return scenario_summary(scenario, "user")


@router.post("/{name}/rename")
def rename_scenario(
    name: str,
    body: ScenarioRename,
    request: Request,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Rename one of the operator's own scenarios, and carry its history with it.

    A scenario's name is its directory, the ``name`` in its ``scenario.yaml``,
    its key in the scenario source, and the string every run and baseline row
    stores. All four move together:

    * the directory is copied to the new name with ``scenario.yaml`` rewritten,
      loaded to prove it still parses, and only then swapped in;
    * runs and baselines that name the old scenario are updated in the same
      transaction, so the Runs page, comparisons and baselines follow it;
    * with a writable source the archive is published under the new name and
      only then is the old key removed, so the scenario is never missing from
      the bucket. A bucket that cannot follow does not undo the rename; the
      response's ``warning`` says what is left behind (an old archive the
      boot sync would import again as a second scenario).

    Refused for a built-in (403: it ships with the image, duplicate it
    instead), for a name already taken in either library (409; a built-in
    would shadow it), and while a run of the scenario has not finished (409:
    the runner resolves the scenario by name while it runs).
    """
    try:
        directory, origin = scenario_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if origin != "user":
        raise HTTPException(
            status_code=403,
            detail="built-in scenarios ship with the image and cannot be renamed: duplicate it",
        )
    new = body.name
    if new == name:
        raise HTTPException(status_code=422, detail="that is already the scenario's name")
    try:
        scenario_path(new)
    except FileNotFoundError:
        pass
    else:
        raise HTTPException(status_code=409, detail=f"a scenario named {new!r} already exists")
    target = directory.parent / new
    if target.exists():
        raise HTTPException(status_code=409, detail=f"a scenario named {new!r} already exists")
    live = session.scalar(
        select(Run.id)
        .where(Run.scenario == name, Run.state.not_in(TERMINAL_STATES))
        .limit(1)
    )
    if live is not None:
        raise HTTPException(
            status_code=409,
            detail=f"run {live} of {name!r} has not finished; rename it once the run ends",
        )

    staging = directory.parent / f".rename-{new}-{uuid.uuid4().hex[:8]}"
    shutil.copytree(directory, staging)
    try:
        text = (staging / "scenario.yaml").read_text(encoding="utf-8")
        (staging / "scenario.yaml").write_text(
            _apply_yaml_fields(text, {"name": new}, {}), encoding="utf-8"
        )
        renamed = load_scenario(staging)
        if renamed.name != new:
            raise HTTPException(
                status_code=422, detail=f"the renamed scenario loads as {renamed.name!r}"
            )
    except HTTPException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - never leave a half-written copy behind
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"the renamed scenario does not load: {exc}")

    runs = session.query(Run).filter(Run.scenario == name).update(
        {Run.scenario: new}, synchronize_session=False
    )
    baselines = session.query(Baseline).filter(Baseline.scenario == name).update(
        {Baseline.scenario: new}, synchronize_session=False
    )
    staging.rename(target)
    try:
        session.commit()
    except Exception:
        # The rows still say the old name, so the old directory must stay.
        shutil.rmtree(target, ignore_errors=True)
        raise
    shutil.rmtree(directory, ignore_errors=True)
    scenario = load_scenario(target)
    audit_record(
        "scenario_renamed",
        request=request,
        detail=f"{name} -> {new} ({runs} run(s), {baselines} baseline(s))",
    )
    remote = _swap_remote(name, new, target)
    summary = scenario_summary(scenario, "user")
    summary.update(
        {"old_name": name, "runs_updated": runs, "baselines_updated": baselines, **remote}
    )
    return summary


def _swap_remote(old: str, new: str, directory: Path) -> Dict[str, Any]:
    """Publish the renamed scenario, then remove the old archive. Never raises."""
    out: Dict[str, Any] = {
        "remote_key": None,
        "old_remote_key": None,
        "old_remote_removed": False,
        "warning": None,
    }
    if not scenariosource.writes_enabled():
        if scenariosource.describe().get("configured"):
            out["warning"] = (
                f"the scenario source is read-only, so its copy is still named {old!r} "
                "and will be imported again at the next restart"
            )
        return out
    store = scenariosource.store_from_env()
    if store is None:
        return out
    key = scenariosource.publish_scenario(new, directory, store=store)
    out["remote_key"] = key
    if key is None:
        out["warning"] = f"the scenario source refused the new archive; its copy is still named {old!r}"
        return out
    try:
        old_key, existed = scenariosource.unpublish_scenario(old, store=store)
        out["old_remote_key"] = old_key
        out["old_remote_removed"] = existed
    except scenariosource.ScenarioSourceError as exc:
        out["old_remote_key"] = archive_name(old)
        out["warning"] = (
            f"{exc}; delete it from the scenario source or it returns at the next restart"
        )
    return out


@router.delete("/{name}", status_code=204)
def delete_scenario(name: str, request: Request, remote: bool = False) -> Response:
    """Delete a user scenario; ``?remote=true`` removes it from the source too.

    Without ``remote`` a scenario that is also in the scenario source comes
    straight back at the next boot, because the boot sync imports whatever the
    local library lacks. The remote delete runs after the built-in guard and
    BEFORE the local one, and a remote failure (502) keeps the local copy, so
    the two cannot drift apart in the direction that resurrects it. A source
    that is unset or read-only is a 409. The response is then 200 with
    ``{"remote_key", "remote_existed"}`` so the console can say whether the
    archive was actually there.
    """
    try:
        directory, origin = scenario_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if origin != "user":
        raise HTTPException(
            status_code=403, detail="built-in scenarios ship with the image and cannot be deleted"
        )
    result: Optional[Dict[str, Any]] = None
    if remote:
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
        try:
            key, existed = scenariosource.unpublish_scenario(directory.name)
        except scenariosource.ScenarioSourceError as exc:
            raise HTTPException(status_code=502, detail=f"{exc}; the local scenario was kept")
        result = {"remote_key": key, "remote_existed": existed}
    shutil.rmtree(directory)
    audit_record(
        "scenario_deleted",
        request=request,
        detail=name if result is None else f"{name} (and {result['remote_key']} from the source)",
    )
    if result is not None:
        return JSONResponse(status_code=200, content=result)
    return Response(status_code=204)


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
