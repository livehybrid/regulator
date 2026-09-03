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
    # Workers authenticate with the per-run token, checked by the route.
    "/api/agent",
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


def bearer_matches(request: Request) -> bool:
    """An ``Authorization: Bearer`` header naming one of the configured API tokens.

    This is what a pipeline uses. The GitHub Action presents one, and so can
    curl. Compared in constant time against every configured token, so the
    endpoint does not leak which prefix was right.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    presented = header[7:].strip().encode()
    matched = False
    for token in get_settings().api_tokens:
        # Every candidate is compared so the timing does not depend on which
        # token, if any, was the right one.
        if hmac.compare_digest(presented, token.encode()):
            matched = True
    return matched


def is_authenticated(request: Request) -> bool:
    settings = get_settings()
    if not settings.auth_enabled:
        # Nothing configured: everything is permitted, and /api/auth/status
        # reports setup_needed so the UI can say so out loud.
        return True
    if bearer_matches(request):
        return True
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    return verify_session(token, settings.session_ttl_s) is not None


# A small, honest throttle on the one password. Not a substitute for putting
# the control plane behind something that rate-limits properly, but enough
# that a script cannot try a wordlist against a service that can evict a
# production cache. In-memory and per process, which is the deployment shape.
_FAILURES: dict[str, list[float]] = {}
LOGIN_WINDOW_S = 300.0
LOGIN_MAX_FAILURES = 8


def login_allowed(client: str) -> bool:
    now = time.time()
    recent = [t for t in _FAILURES.get(client, []) if now - t < LOGIN_WINDOW_S]
    _FAILURES[client] = recent
    return len(recent) < LOGIN_MAX_FAILURES


def record_login_failure(client: str) -> None:
    _FAILURES.setdefault(client, []).append(time.time())


def record_login_success(client: str) -> None:
    _FAILURES.pop(client, None)


def path_is_exempt(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in EXEMPT_PREFIXES)


def warn_if_open() -> None:
    settings = get_settings()
    if not settings.auth_enabled:
        if not settings.allow_unauthenticated:
            # Refusing is the safe default. An unauthenticated control plane
            # on a network interface is an unauthenticated HTTP client into
            # that network with the response echoed back, plus the buttons.
            raise RuntimeError(
                "refusing to start with no authentication: set REG_ADMIN_PASSWORD (or "
                "REG_API_TOKENS), or set REG_ALLOW_UNAUTHENTICATED=1 if this really is a "
                "laptop on a network you control"
            )
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
