"""Audit trail.

``record()`` is append-only and defensive: an audit failure must never roll back a
business transaction, but it is always logged.  Callers pass the *action* and the
affected entity; the helper resolves the actor, IP address and user agent from the
request when one is active.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.extensions import db
from app.models import AuditEvent

logger = logging.getLogger(__name__)


def _current_actor():
    try:
        from flask_login import current_user

        if current_user and getattr(current_user, "is_authenticated", False):
            return current_user
    except Exception:  # pragma: no cover - outside a request
        pass
    return None


def _request_meta() -> tuple[str | None, str | None]:
    try:
        from flask import has_request_context, request

        if has_request_context():
            return request.remote_addr, (request.user_agent.string or "")[:255]
    except Exception:  # pragma: no cover
        pass
    return None, None


def record(
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    summary: str | None = None,
    details: Any = None,
    actor=None,
) -> AuditEvent | None:
    """Append an audit event.  Returns the event, or ``None`` on failure."""
    try:
        actor = actor or _current_actor()
        ip, agent = _request_meta()
        if details is not None and not isinstance(details, str):
            try:
                details = json.dumps(details, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                details = str(details)
        event = AuditEvent(
            actor_id=getattr(actor, "id", None),
            actor_name=getattr(actor, "username", None) if actor else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=(summary or "")[:255] or None,
            details=details,
            ip_address=ip,
            user_agent=agent,
        )
        db.session.add(event)
        db.session.flush()
        return event
    except Exception:  # pragma: no cover - auditing must not break the app
        logger.exception("Failed to write audit event %s/%s", action, entity_type)
        return None


def record_login_event(username: str, success: bool, reason: str | None = None) -> None:
    action = "login_success" if success else "login_failed"
    record(
        action=action,
        entity_type="user",
        summary=f"{username}" + (f" ({reason})" if reason else ""),
        actor=None,
    )
