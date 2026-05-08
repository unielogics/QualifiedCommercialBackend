from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import ClientStage
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.broker import Broker
    from app.models.loan import Loan
    from app.models.user import User


class Client(TimestampMixin, Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    broker_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    since: Mapped[date | None] = mapped_column(Date, nullable=True)

    tier: Mapped[str] = mapped_column(String(16), default="standard")
    fico: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avatar_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    referral_source: Mapped[str | None] = mapped_column(String(160), nullable=True)

    funded_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    funded_count: Mapped[int] = mapped_column(Integer, default=0)

    # Lead-funnel state (alembic 0024).
    #
    # `stage` drives the LeadsPipelineView kanban + the agent funnel
    # metrics. Backfilled at migration time from existing Loan state
    # (see 0024 for the strict order). New clients default to 'lead'.
    stage: Mapped[ClientStage] = mapped_column(
        String(32), nullable=False, default=ClientStage.LEAD,
        server_default="lead",
    )
    # Buyer or seller side of the transaction. Mirrors the per-loan
    # `Loan.side` (alembic 0023) but at the client level — a single
    # client could have multiple loans on different sides over time.
    client_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Stage transition timestamps. `contacted_at` is the activity
    # baseline for stale-lead detection (NOT updated_at, which ticks
    # on any field edit and would silently un-stale leads).
    contacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    intake_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    intake_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Investor profile (Profile → Investor Profile dialog). Free-text for
    # v1 — operators see what the borrower wrote and can fold into the AI's
    # context. Structured (per-property table) lands when we wire REO.
    properties: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 8 — account-wide AI aggregator (alembic 0013).
    # client_summarizer reads + writes these:
    #   living_profile  — JSONB with cross-loan next_actions, blockers,
    #                     suggested-next-loan, etc. Mirrors the per-loan
    #                     LivingLoanProfile shape.
    #   living_summary  — human-readable narrative
    #   living_refreshed_at — last successful aggregator run
    living_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    living_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    living_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="client")
    broker: Mapped[Broker] = relationship(back_populates="clients")
    loans: Mapped[list[Loan]] = relationship(back_populates="client")
