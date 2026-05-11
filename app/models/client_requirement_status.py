"""ClientRequirementStatus — per-client per-requirement state.

This is the source of truth for individual requirement state. The
plan builder rolls multiple rows here up into `client_ai_plan` for
the rendered/snapshot view; the AI updates a row here whenever it
asks, receives, verifies, or waives a requirement.

Partial unique indexes (created in alembic 0032) ensure one row per
(client_id, loan_id, requirement_key) — with `loan_id IS NULL` and
`loan_id IS NOT NULL` split into separate indexes because Postgres
treats NULL as distinct in plain UNIQUE constraints.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ClientRequirementStatus(TimestampMixin, Base):
    __tablename__ = "client_requirement_status"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=True,
    )

    requirement_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    """missing | asked | provided_unverified | uploaded | verified |
    not_applicable | waived | needed_later | conflicted | stalled."""

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    """platform | funding_required | agent_playbook | client_custom |
    ai_detected | underwriter_added."""

    evidence_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    """document_id or message_id backing this status, when relevant."""

    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    """AI confidence score when source=ai_detected."""

    due_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- AI Deal Secretary fields (alembic 0038) ----
    owner_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="human", server_default="human",
    )
    """TaskOwnerType — human | ai | shared | funding_locked. The
    Deal Secretary picker on the workbench writes this on drag. Cadence
    engine consults this BEFORE firing any borrower-visible action."""

    ai_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_task_assignments.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Live AITaskAssignment row when owner_type='ai'. NULL otherwise.
    Soft-deleting an assignment leaves this dangling momentarily — the
    same /unassign endpoint clears it in the same transaction."""

    last_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    """Last borrower reply timestamp. Cadence engine reads this to
    avoid re-asking inside the configured window."""
