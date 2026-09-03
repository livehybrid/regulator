"""Who did what.

One row per state-changing action, with the caller's address and how they
authenticated. Eviction has no undo and a run can saturate a production
cluster, so "who pressed that at 14:20" has to be answerable after the log
has rotated. Best effort: a failure to write the audit row is logged and
never turns into a failure of the action itself.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Request

from .db import session_scope
from .models import AuditEvent

log = logging.getLogger("regulator.server.audit")


def actor_of(request: Optional[Request]) -> str:
    """How the caller authenticated: token, session, or nothing."""
    if request is None:
        return "system"
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return "api-token"
    if request.cookies.get("regulator_session"):
        return "session"
    return "unauthenticated"


def record(
    action: str,
    *,
    request: Optional[Request] = None,
    actor: Optional[str] = None,
    client: Optional[str] = None,
    target_id: Optional[int] = None,
    detail: Optional[str] = None,
) -> None:
    try:
        with session_scope() as session:
            session.add(
                AuditEvent(
                    actor=actor or actor_of(request),
                    client=client or (request.client.host if request and request.client else None),
                    action=action,
                    target_id=target_id,
                    detail=(detail or "")[:2000] or None,
                )
            )
    except Exception:  # noqa: BLE001 - the audit trail must never break the action
        log.warning("could not write the audit event %s", action, exc_info=True)


def recent(limit: int = 200) -> List[Dict[str, Any]]:
    with session_scope() as session:
        rows = (
            session.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(max(1, min(limit, 1000))).all()
        )
        return [
            {
                "id": row.id,
                "at": row.at,
                "actor": row.actor,
                "client": row.client,
                "action": row.action,
                "target_id": row.target_id,
                "detail": row.detail,
            }
            for row in rows
        ]
