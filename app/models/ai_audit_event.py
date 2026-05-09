"""AIAuditEvent — append-only audit log for AI-behavior-changing events.

Every event that modifies a playbook, waives a requirement, suggests
or approves an AI action, resolves a document conflict, or moves a
deal across the realtor↔lending boundary writes a row here. Cheap to
append, invaluable for compliance + debugging + funding-side review.

Events are filtered by visibility at the route layer (Phase 7) — agents
see their own + client-scoped events; super-admin sees everything.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


from app.db import Base


class AIAuditEvent(Base):
    __tablename__ = "ai_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    """playbook_edited | playbook_published | requirement_added |
    requirement_waived | ai_action_suggested | ai_action_approved |
    ai_action_dismissed | document_conflict_detected |
    document_conflict_resolved | handoff_created | handoff_accepted |
    client_override_added | cadence_action_fired."""

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    """user | ai | cadence_engine | system."""

    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True,
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="SET NULL"),
        nullable=True,
    )
    playbook_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_playbook_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    requirement_key: Mapped[str | None] = mapped_column(String(120), nullable=True)

    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
