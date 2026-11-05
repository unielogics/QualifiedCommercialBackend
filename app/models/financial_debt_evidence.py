"""Published document associations for a stable financial obligation."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class FinancialDebtEvidence(TimestampMixin, Base):
    __tablename__ = "financial_debt_evidence"
    __table_args__ = (
        UniqueConstraint("debt_id", "file_id", "role", name="uq_financial_debt_evidence"),
        CheckConstraint("role in ('agreement','payoff','supporting')", name="ck_financial_debt_evidence_role"),
        CheckConstraint("not is_current or role = 'payoff'", name="ck_financial_debt_evidence_current_role"),
        Index("uq_financial_debt_current_payoff", "debt_id", unique=True,
              postgresql_where=text("is_current = true")),
    )
    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), index=True)
    debt_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("dos_debts.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    payoff_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    payoff_date: Mapped[date | None] = mapped_column(Date)

