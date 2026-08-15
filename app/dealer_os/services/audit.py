"""Audit trail — Phase 3 Wave 1.

One append-only choke point (log_action) every human-correction endpoint
calls: target overrides, cash-event recategorization, add-back decisions,
account role edits, rule creation, dealer profile edits. Rows carry
before/after JSON snapshots so the precedence story ("AI drafted X, admin
corrected to Y") is reconstructable. Flushes, never commits — callers own the
transaction boundary.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

from ..models import DealerAuditLog


def _jsonable(value: Any) -> Any:
    """Coerce simple non-JSON scalars (UUID, date, Decimal, ...) so callers can
    pass model-field dicts without ceremony."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def log_action(
    db: AsyncSession,
    dealer_id: UUID,
    user: User | None,
    action: str,
    entity_kind: str,
    entity_id: UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> DealerAuditLog:
    row = DealerAuditLog(
        dealer_id=dealer_id,
        actor_user_id=user.id if user is not None else None,
        actor_name=((user.name or user.email) if user is not None else "system")[:120],
        action=action[:48],
        entity_kind=entity_kind[:24],
        entity_id=entity_id,
        before=_jsonable(before) if before is not None else None,
        after=_jsonable(after) if after is not None else None,
    )
    db.add(row)
    await db.flush()
    return row
