"""Append-only audit log writer for AI-behavior-changing events.

Centralizes the boilerplate around inserting `ai_audit_events` rows so
callers (cadence engine, plan builder, override handlers, document
contradiction resolver) don't each reinvent the same INSERT.

All events are append-only. Failures here MUST NOT block the calling
operation — audit is best-effort. Caller wraps in a try/except if
needed; the helper itself doesn't raise on database errors that would
disrupt the user-visible action.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_audit_event import AIAuditEvent

logger = logging.getLogger(__name__)


async def record_event(
    db: AsyncSession,
    *,
    event_type: str,
    actor_type: str,
    actor_id: UUID | None = None,
    client_id: UUID | None = None,
    loan_id: UUID | None = None,
    playbook_id: UUID | None = None,
    requirement_key: str | None = None,
    old_value: Any | None = None,
    new_value: Any | None = None,
    payload: dict[str, Any] | None = None,
) -> AIAuditEvent | None:
    """Insert one audit-event row. Returns the persisted model or None
    on failure. Does NOT commit — caller controls the transaction.

    `event_type` should match the canonical set documented in
    AIAuditEvent (playbook_edited, requirement_waived, etc.). Free-form
    strings are accepted but discouraged.

    `actor_type` is one of: user | ai | cadence_engine | system.
    `actor_id` should be set for user/ai actions, NULL for system."""
    try:
        evt = AIAuditEvent(
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            client_id=client_id,
            loan_id=loan_id,
            playbook_id=playbook_id,
            requirement_key=requirement_key,
            old_value=_as_jsonable(old_value),
            new_value=_as_jsonable(new_value),
            payload=payload,
        )
        db.add(evt)
        await db.flush()
        return evt
    except Exception:  # pragma: no cover — audit must never break callers
        logger.exception(
            "audit.record_event failed: event_type=%s actor_type=%s "
            "client_id=%s loan_id=%s",
            event_type, actor_type, client_id, loan_id,
        )
        return None


def _as_jsonable(value: Any) -> Any:
    """Wrap a primitive in a dict so JSONB stores it round-trippably."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    # Wrap scalars so we always have a dict shape coming back out.
    return {"value": value}
