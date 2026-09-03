"""The control plane.

Serves the operator API, the single-page UI and nothing else. Routers register
themselves by being imported, so adding a feature never means editing this file
(a convention taken from Stoker, where it kept a 2000-line router file from
becoming a 2000-line app file as well).

**The UI is one self-contained HTML file with no build step.** That is a
deliberate choice rather than a stopgap: no npm, no node_modules in the image,
no build stage in CI, and nothing to go stale. It buys a page an operator can
read the source of. If the UI ever needs real charting libraries, that is the
point to reconsider, not before.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from . import auth
from .adapters import scenarios_dir, user_scenarios_dir
from .audit import record as audit_record
from .config import get_settings
from .crypto import encrypt
from .db import init_engine, session_scope
from .models import Target
from .routes import agent as agent_routes
from .routes import baselines as baselines_routes
from .routes import fleets as fleets_routes
from .routes import runs as runs_routes
from .routes import scenarios as scenarios_routes
from .routes import targets as targets_routes
from .runner import manager
from .schemas import AuthStatus, LoginRequest

log = logging.getLogger("regulator.server")

UI_DIR = Path(__file__).resolve().parents[1] / "ui"
UI_INDEX = UI_DIR / "index.html"

__version__ = "0.1.0"


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    app = FastAPI(
        title="Regulator",
        version=__version__,
        description=(
            "Search load generation for Splunk: simulate concurrent users, measure what "
            "the cluster gives back, and know whether the answer came from cache."
        ),
        # The API description is only served to a signed-in operator when
        # authentication is on: it is a map of everything the service can do
        # to a cluster, and the exemption list was letting anyone read it.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    init_engine()
    auth.warn_if_open()
    manager.reconcile_at_boot()
    if get_settings().fleet.swarm_available or get_settings().fleet.k8s_available:
        from .fleet import reconcile_fleets_at_boot

        try:
            reconcile_fleets_at_boot()
        except Exception:  # noqa: BLE001 - a backend hiccup must not stop the boot
            log.warning("could not sweep stray worker groups", exc_info=True)
    _seed_target_from_env()
    user_scenarios_dir().mkdir(parents=True, exist_ok=True)

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api") and not auth.path_is_exempt(path):
            if not auth.is_authenticated(request):
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return await call_next(request)

    # ------------------------------------------------------------------ auth

    @app.post("/api/auth/login")
    async def login(body: LoginRequest, request: Request) -> Response:
        if not get_settings().admin_password:
            # Nothing to log in to. Say so rather than accepting any password
            # and leaving the operator believing the door is locked.
            return JSONResponse(
                {"detail": "this control plane has no password configured"}, status_code=409
            )
        client = request.client.host if request.client else "unknown"
        if not auth.login_allowed(client):
            return JSONResponse(
                {"detail": "too many failed logins from this address; try again later"},
                status_code=429,
            )
        if not auth.password_matches(body.password):
            auth.record_login_failure(client)
            audit_record("login_failed", actor="anonymous", client=client)
            return JSONResponse({"detail": "wrong password"}, status_code=401)
        auth.record_login_success(client)
        response = JSONResponse({"ok": True})
        auth.issue_session(response, request)
        return response

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(request: Request) -> Response:
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Regulator API")

    @app.get("/api/openapi.json", include_in_schema=False)
    async def api_openapi(request: Request) -> Response:
        return JSONResponse(app.openapi())

    @app.post("/api/auth/logout")
    async def logout() -> Response:
        response = JSONResponse({"ok": True})
        auth.clear_session(response)
        return response

    @app.get("/api/auth/status", response_model=AuthStatus)
    async def auth_status(request: Request) -> AuthStatus:
        settings = get_settings()
        return AuthStatus(
            authenticated=auth.is_authenticated(request),
            setup_needed=not settings.auth_enabled,
        )

    # --------------------------------------------------------------- health

    @app.get("/api/audit")
    async def audit_log(limit: int = 200) -> Dict[str, Any]:
        """Who did what: logins, launches, stops, evictions, scenario changes."""
        from .audit import recent

        return {"events": recent(limit)}

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        # Deliberately says nothing about the deployment: a probe needs "up",
        # and an absolute path plus "no auth" was a beacon for the open ones.
        return {"ok": True, "version": __version__}

    app.include_router(targets_routes.router)
    app.include_router(runs_routes.router)
    app.include_router(baselines_routes.router)
    app.include_router(scenarios_routes.router)
    app.include_router(fleets_routes.router)
    app.include_router(agent_routes.router)

    # ------------------------------------------------------------------- ui

    @app.get("/")
    async def index() -> Response:
        if not UI_INDEX.is_file():
            return JSONResponse(
                {"detail": "the UI is not present in this build", "api": "/docs"},
                status_code=404,
            )
        # No-store rather than a cache header: the page is small, and an
        # operator reloading after an upgrade must never get the old one.
        return FileResponse(UI_INDEX, media_type="text/html", headers={"Cache-Control": "no-store"})

    return app


def _seed_target_from_env() -> None:
    """Register the target named in the environment, creating or updating it.

    What makes a nightly-rebuilt or CI-spawned control plane immediately
    usable: it comes up with a target already registered and can run with no
    web step and no stored state. The credential is encrypted on arrival
    like any other; env seeding is a bootstrap, not a bypass.
    """
    seed = get_settings().seed_target
    if seed is None:
        return
    with session_scope() as session:
        target = session.query(Target).filter(Target.name == seed.name).one_or_none()
        if target is None:
            target = Target(name=seed.name)
            session.add(target)
            log.info("seeding target %r from the environment", seed.name)
        else:
            log.info("updating the env-seeded target %r", seed.name)
        target.mgmt_url = seed.mgmt_url
        target.web_url = seed.web_url
        target.token_encrypted = encrypt(seed.token)
        target.username = seed.username
        target.password_encrypted = encrypt(seed.password)
        target.verify_tls = seed.verify_tls
        target.app = seed.app
        target.owner = seed.owner


app = create_app()
