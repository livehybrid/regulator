"""Authentication.

One password, from ``REG_ADMIN_PASSWORD``, and a signed session cookie. That is
deliberately modest: this control plane can evict a production cache and drive a
cluster to saturation, so it must not be wide open, but a full user model is
Phase 1 work and pretending otherwise would be worse than saying so.

When no password is configured the server runs unauthenticated and **says so on
every start and in the API**, so an operator cannot mistake "nobody asked me for
a password" for "there is no password to ask for".
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Optional

from fastapi import Request, Response

from .config import get_settings
from .crypto import sign_session, verify_session

log = logging.getLogger("regulator.server.auth")

SESSION_COOKIE = "regulator_session"

# Paths that answer without a session. Everything else under /api needs one.
EXEMPT_PREFIXES = (
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
    "/healthz",
)


def password_matches(candidate: str) -> bool:
    """Constant-time comparison, so the endpoint does not leak the password by timing."""
    configured = get_settings().admin_password
    if not configured:
        return False
    return hmac.compare_digest(candidate.encode(), configured.encode())


def issue_session(response: Response, request: Request) -> None:
    token = sign_session(f"admin:{int(time.time())}")
    settings = get_settings()
    # Secure follows the scheme the client actually used, including through a
    # reverse proxy. Setting it unconditionally would break a plain-HTTP LAN
    # deployment by making the cookie unusable, silently.
    forwarded = request.headers.get("x-forwarded-proto", "")
    secure = request.url.scheme == "https" or forwarded == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_s,
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def is_authenticated(request: Request) -> bool:
    settings = get_settings()
    if not settings.auth_enabled:
        # No password configured: everything is permitted, and /api/auth/status
        # reports setup_needed so the UI can say so out loud.
        return True
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    return verify_session(token, settings.session_ttl_s) is not None


def path_is_exempt(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in EXEMPT_PREFIXES)


def warn_if_open() -> None:
    settings = get_settings()
    if not settings.auth_enabled:
        log.warning(
            "SECURITY: REG_ADMIN_PASSWORD is not set, so this control plane is "
            "unauthenticated. It can evict a SmartStore cache and drive a cluster to "
            "saturation. Set a password, or keep it off any network you do not control"
        )
    if settings.master_key_generated:
        log.warning(
            "SECURITY: REG_MASTER_KEY is not set, so a throwaway key was generated. "
            "Every target credential stored now becomes unreadable at the next restart"
        )


def optional_password(request: Request) -> Optional[str]:
    return request.cookies.get(SESSION_COOKIE)
