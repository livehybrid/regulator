"""Download this instance's configuration, and restore one.

Exists so that a Regulator deployment is disposable: configure it, download the
JSON, tear the environment down including its volumes, and bring it back from
the file. ``REG_CONFIG_IMPORT`` is the automatic path for a rebuilt
environment and needs no UI at all; these two endpoints are how an operator
obtains the file to mount in the first place, and how one gets applied to a
running instance without reaching for curl.

Both are ordinary authenticated operator endpoints. There is no finer role
model here than "signed in", so the export carries encrypted credentials to
whoever is already able to read and change every target - but it is still worth
saying what the download contains, which the response headers and the console
both do.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from fastapi import Depends

from .. import configio, configsource
from ..audit import record as audit_record
from ..config import get_settings
from ..db import get_session

log = logging.getLogger("regulator.server.config")

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/export")
def export_config(
    request: Request,
    secrets: str = Query(
        default="include",
        pattern="^(include|exclude)$",
        description="'exclude' for a copy safe to commit: the configuration "
        "restores with every credential blank",
    ),
    scenarios: str = Query(
        default="include",
        pattern="^(include|exclude)$",
        description="'exclude' to leave the operator's scenario library out, "
        "for a file that only carries targets",
    ),
    session: Session = Depends(get_session),
) -> Response:
    """The instance's configuration as a downloadable JSON document."""
    include_secrets = secrets == "include"
    document = configio.export_config(
        session,
        include_secrets=include_secrets,
        include_scenarios=scenarios == "include",
        settings=get_settings(),
    )
    audit_record(
        "config_exported",
        request=request,
        detail=(
            f"{len(document['targets'])} target(s), "
            f"{len(document['scenarios'])} scenario(s), "
            f"secrets {'included' if include_secrets else 'excluded'}"
        ),
    )
    body = json.dumps(document, indent=2, sort_keys=False, default=str) + "\n"
    suffix = "" if include_secrets else "-nosecrets"
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="regulator-config{suffix}.json"',
            # A browser must never cache a file that carries credentials.
            "Cache-Control": "no-store",
            "X-Regulator-Config-Targets": str(len(document["targets"])),
            "X-Regulator-Config-Scenarios": str(len(document["scenarios"])),
            "X-Regulator-Config-Secrets": "included" if include_secrets else "excluded",
        },
    )


@router.post("/import")
def import_config(
    body: Dict[str, Any],
    request: Request,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Apply a configuration document to this instance.

    An idempotent upsert: re-applying the same file changes nothing, and a
    partial restore can simply be run again. Returns a report of what was
    created, updated and skipped rather than failing the whole restore over one
    bad entry, because a restore that got most of the way is worth keeping.
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="the config must be a JSON object")
    try:
        report = configio.import_config(session, body, settings=get_settings())
    except configio.ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    audit_record(
        "config_imported",
        request=request,
        detail=(
            f"{report['targets']} target(s) and {report['scenarios']} scenario(s) "
            f"created, {report['targets_updated']} and {report['scenarios_updated']} "
            f"updated, {len(report['skipped'])} skipped"
        ),
    )
    return report


# --------------------------------------------------------------------------- #
# The shared copy and run history (REG_CONFIG_SOURCE)
# --------------------------------------------------------------------------- #

def _require_source() -> configsource.Store:
    try:
        store = configsource.store_from_env()
    except configsource.ConfigSourceError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if store is None:
        raise HTTPException(status_code=409, detail=f"{configsource.SOURCE_ENV} is not set")
    return store


@router.get("/source")
def config_source() -> Dict[str, Any]:
    """Where the shared copy lives, whether this instance writes it, and what
    the last pull and push did."""
    return configsource.describe()


@router.post("/source/pull")
def pull_config_source(request: Request, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Apply the shared copy and import any archived runs this instance lacks."""
    store = _require_source()
    try:
        report = configsource.pull(session, settings=get_settings(), store=store)
    except configsource.ConfigSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    session.commit()
    audit_record(
        "config_pulled",
        request=request,
        detail=(
            f"{report['targets']} target(s), {report['scenarios']} scenario(s), "
            f"{report['baselines']} baseline(s) and {report['runs']['imported']} run(s) "
            "created from the config source"
        ),
    )
    return report


@router.post("/source/push")
def push_config_source(request: Request, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Archive finished runs and write the configuration to the shared copy now.

    409 unless ``REG_CONFIG_SOURCE_WRITE`` is on: the switch exists so an
    instance cannot overwrite a copy it was only meant to read, and a button
    that went round it would make the switch meaningless.
    """
    store = _require_source()
    if not configsource.writes_enabled():
        raise HTTPException(
            status_code=409,
            detail=f"set {configsource.WRITE_ENV}=1 to let this instance write the shared copy",
        )
    try:
        result = configsource.push(session, settings=get_settings(), store=store)
    except configsource.ConfigSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    session.commit()
    audit_record(
        "config_pushed",
        request=request,
        detail=f"{result['targets']} target(s), {result['runs']} run(s) archived",
    )
    return result
