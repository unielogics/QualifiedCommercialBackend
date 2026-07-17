"""AICadenceRule — event-driven follow-up rule.

Replaces the old gentle/standard/aggressive presets. Each rule fires
on a trigger event (`requirement_missing`, `closing_date_near`, etc.)
when its condition is met + the wait period has elapsed.

Default behavior is **draft-first**: `approval_required=True` means
the action shows up in Elara Inbox for the agent to review, not as an
auto-sent message. Auto-send is opt-in per rule (`approval_required=False`)
and only safe for unambiguous internal recipients.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class AICadenceRule(TimestampMixin, Base):
    __tablename__ = "ai_cadence_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    playbook_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_playbook_templates.id", ondelete="CASCADE"),
        nullable=True,
    )
    """NULL means an unattached rule (rare; mostly used by tests)."""

    trigger_event: Mapped[str] = mapped_column(String(64), nullable=False)
    """requirement_missing | document_uploaded | closing_date_near |
    borrower_unresponsive | agreement_unsigned | etc."""

    applies_to_requirement_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """Optional narrowing — fire only for this specific requirement."""

    condition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """Free-form condition block. e.g. {"hours_since_request": 24,
    "stage": "prequalification"}."""

    wait_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    """draft_message | create_task | escalate | mark_stalled | auto_send_reminder."""

    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    """Default TRUE — draft-first. FALSE only for the narrow set of
    rules that should auto-send."""

    message_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="agent", server_default="agent")
    """internal | agent | borrower | broker.

    broker = real-estate-agent lane: the action notifies the loan's realtor
    (not the borrower). Auto-send (approval_required=False) emails the realtor
    from the loan owner's Gmail, gated by the re_agent_email automation toggle;
    otherwise it falls back to an operator-facing AITask. Bypasses the borrower
    OutreachMode kill switch + follow-up windowing (both borrower-only)."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    # AI Deal Secretary integration (alembic 0038). When True, this rule
    # only fires for (client, loan) pairs where the associated CRS row
    # has owner_type='ai'. The cadence engine downgrades non-AI matches
    # to no-op so a global rule can't accidentally walk every deal.
    # Legacy rules backfill to False to preserve today's behavior.
    requires_ai_owner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
