"""AIOutreachEvent — append-only log of every dispatch attempt.

The outreach dispatcher writes a row HERE BEFORE the provider call
(Gmail / SMS provider / portal post). On exception the row stays with
status='failed' + error text — that's the audit trail. On success the
row is updated with sent_at + provider message_id.

Separate from `ai_audit_events` because outreach has its own retention
story (TCPA / CAN-SPAM compliance), and because the cadence engine
queries this table on every pass to decide whether to back off.

There's no UPDATE path that mutates payload or assignment_id once
written. The only mutation is the status / sent_at / message_id /
error fields after the provider returns.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


from app.db import Base


class AIOutreachEvent(Base):
    __tablename__ = "ai_outreach_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_task_assignments.id", ondelete="CASCADE"),
        nullable=False,
    )

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    """OutreachChannel — portal | email | sms | voice."""

    direction: Mapped[str] = mapped_column(
        String(8), nullable=False, default="outbound", server_default="outbound",
    )
    """outbound | inbound | system. Inbound rows record borrower replies
    so the cadence engine can see them as one query."""

    template_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """Optional reference to the AICollectionRequirement.requirement_key
    or a named template the dispatcher used (e.g. "reminder_24h")."""

    message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    """Provider-side message reference: Gmail message id, SMS provider
    sid, AIChatMessage.id for portal events. NULL on failure."""

    ai_chat_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True,
    )
    """For portal sends — points at the AIChatMessage row that landed
    in the borrower's thread. Lets the UI link from the event log
    back to the actual chat bubble."""

    status: Mapped[str] = mapped_column(String(24), nullable=False)
    """OutreachEventStatus — scheduled | drafted | sent | delivered |
    responded | failed | blocked_by_consent | blocked_by_quiet_hours |
    blocked_by_policy | cancelled."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Provider error text. Populated on failed. Used by the workbench
    to surface "AI Blocked" badges with a specific reason."""

    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """Free-form per-event details. For portal: {"body": "..."}; for
    email: {"subject": "...", "body": "..."}; for SMS: {"text": "..."}."""

    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()",
    )
