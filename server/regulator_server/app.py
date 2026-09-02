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
from .adapters import scenarios_dir
from .config import get_settings
from .db import init_engine
from .routes import runs as runs_routes
from .routes import targets as targets_routes
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
    )

    init_engine()
    auth.warn_if_open()

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
        if not get_settings().auth_enabled:
            # Nothing to log in to. Say so rather than accepting any password
            # and leaving the operator believing the door is locked.
            return JSONResponse(
                {"detail": "this control plane has no password configured"}, status_code=409
            )
        if not auth.password_matches(body.password):
            return JSONResponse({"detail": "wrong password"}, status_code=401)
        response = JSONResponse({"ok": True})
        auth.issue_session(response, request)
        return response

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

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "scenarios_dir": str(scenarios_dir()),
            "auth": get_settings().auth_enabled,
        }

    app.include_router(targets_routes.router)
    app.include_router(runs_routes.router)

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


app = create_app()
