"""AgentTask — internal agent CRM task (alembic 0051).

Distinct from ClientRequirementStatus — that table tracks borrower-
facing AI requirements (proof of funds, bank statements, etc.). This
table tracks the agent's own workflow: open house scheduled, listing
photos booked, CMA prep, etc.

Visibility tiers drive what flows through the funding handoff:
- agent_private: only visible to the assigned agent.
- team_visible: brokerage / team members.
- funding_visible: transferred into baseline_profile_snapshot on
  Ready-for-Lending — these notes / tasks are safe for the underwriting
  team to see.
- client_visible: shown to the borrower in their portal / AI flow.

AI promotion (see services/agent_task_promote): when owner_type='ai',
a synthetic ClientRequirementStatus row with requirement_key prefix
'agent_task:' is created and an AITaskAssignment bound to it, so the
existing DealSecretaryPicker renders the task in its AI column.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class AgentTask(TimestampMixin, Base):
    __tablename__ = "agent_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("deals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="other", server_default="other",
    )
    """AgentTaskCategory enum string."""

    visibility: Mapped[str] = mapped_column(
        String(24), nullable=False, default="team_visible", server_default="team_visible",
    )
    """AgentTaskVisibility — agent_private | team_visible | funding_visible | client_visible."""

    owner_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="human", server_default="human",
    )
    """TaskOwnerType reused: human | ai | shared | funding_locked.
    Flipped to 'ai' via the promote-to-ai endpoint (which also creates
    the backing CRS row + AITaskAssignment)."""

    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    ai_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_task_assignments.id", ondelete="SET NULL"),
        nullable=True,
    )

    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open",
    )
    priority: Mapped[str] = mapped_column(
        String(8), nullable=False, default="medium", server_default="medium",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client = relationship("Client", foreign_keys=[client_id])
    deal = relationship("Deal", foreign_keys=[deal_id])
    loan = relationship("Loan", foreign_keys=[loan_id])
    assigned_user = relationship("User", foreign_keys=[assigned_user_id])

    __table_args__ = (
        Index("ix_agent_tasks_client_status", "client_id", "status"),
    )
