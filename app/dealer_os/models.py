"""Dealer OS domain models. Every table is prefixed dos_ (isolation contract).

Monthly-grain time series now (uploads-first, no Plaid keys yet); the same rows
gain daily grain when connected feeds land. JSONB is used for open-ended
sub-structures (liquidity buckets, flags, credit history) so the schema stays
stable while engines evolve.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class DealerBusiness(TimestampMixin, Base):
    """The durable monitored business — distinct from intake leads and Clients."""

    __tablename__ = "dos_dealers"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(180))
    ein: Mapped[str | None] = mapped_column(String(24))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(48))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(8))
    industry: Mapped[str] = mapped_column(String(48), default="auto_dealer", server_default="auto_dealer")
    status: Mapped[str] = mapped_column(String(24), default="active", server_default="active")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    dealer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)


class DealerFinancialPeriod(TimestampMixin, Base):
    """One normalized month of financials, source-agnostic."""

    __tablename__ = "dos_financial_periods"
    __table_args__ = (UniqueConstraint("dealer_id", "period", name="uq_dos_period"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)  # first of month
    revenue: Mapped[float | None] = mapped_column(Numeric(14, 2))
    net_income: Mapped[float | None] = mapped_column(Numeric(14, 2))
    ebitda_reported: Mapped[float | None] = mapped_column(Numeric(14, 2))
    ebitda_adjusted: Mapped[float | None] = mapped_column(Numeric(14, 2))
    ebitda_bankable: Mapped[float | None] = mapped_column(Numeric(14, 2))
    debt_service: Mapped[float | None] = mapped_column(Numeric(14, 2))
    deposits: Mapped[float | None] = mapped_column(Numeric(14, 2))
    withdrawals: Mapped[float | None] = mapped_column(Numeric(14, 2))
    ending_balance: Mapped[float | None] = mapped_column(Numeric(14, 2))
    low_balance: Mapped[float | None] = mapped_column(Numeric(14, 2))
    avg_daily_balance: Mapped[float | None] = mapped_column(Numeric(14, 2))
    nsf_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    liquidity: Mapped[dict | None] = mapped_column(JSONB)  # {operating, banking, debt_reserve, strategic}
    source: Mapped[str] = mapped_column(String(24), default="upload", server_default="upload")
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class DealerCashEvent(TimestampMixin, Base):
    """Transaction-level cash line (statement/CSV now; Plaid/QBO later)."""

    __tablename__ = "dos_cash_events"

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(320), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    category: Mapped[str] = mapped_column(String(48), default="uncategorized", server_default="uncategorized")
    flags: Mapped[dict | None] = mapped_column(JSONB)  # {irregular, addback_candidate, early_payment, fixed, ai_suggested}
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    categorized_by: Mapped[str | None] = mapped_column(String(24))  # ai|admin|dealer|rule
    source: Mapped[str] = mapped_column(String(24), default="upload", server_default="upload")


class DealerAddback(TimestampMixin, Base):
    __tablename__ = "dos_addbacks"

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    monthly_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    annual_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(16), default="candidate", server_default="candidate")  # verified|candidate|review|excluded
    evidence: Mapped[str | None] = mapped_column(Text)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_cash_events.id", ondelete="SET NULL")
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DealerMetricTarget(TimestampMixin, Base):
    """AI-proposed, admin-overridable per-dealer target. Override always wins."""

    __tablename__ = "dos_metric_targets"
    __table_args__ = (UniqueConstraint("dealer_id", "metric_key", name="uq_dos_target"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    metric_key: Mapped[str] = mapped_column(String(48), nullable=False)
    ai_proposed_value: Mapped[float | None] = mapped_column(Numeric(14, 2))
    ai_rationale: Mapped[str | None] = mapped_column(Text)
    ai_proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    admin_value: Mapped[float | None] = mapped_column(Numeric(14, 2))
    admin_set_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    admin_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="ai_proposed", server_default="ai_proposed")

    @property
    def effective_value(self) -> float | None:
        return self.admin_value if self.admin_value is not None else self.ai_proposed_value


class DealerMetricSnapshot(TimestampMixin, Base):
    __tablename__ = "dos_metric_snapshots"

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # {ebitda:{...}, dscr:{...}, adb:{...}, ...}
    score: Mapped[float | None] = mapped_column(Numeric(5, 1))
    tier: Mapped[str | None] = mapped_column(String(24))


class DealerMetricLineage(Base):
    __tablename__ = "dos_metric_lineage"

    id: Mapped[uuid.UUID] = _pk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_metric_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    metric_key: Mapped[str] = mapped_column(String(48), nullable=False)
    ref_kind: Mapped[str] = mapped_column(String(24), nullable=False)  # cash_event|addback|period|target|statement
    ref_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    period: Mapped[date | None] = mapped_column(Date)


class DealerPlanAction(TimestampMixin, Base):
    __tablename__ = "dos_plan_actions"

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(24), default="liquidity", server_default="liquidity")
    owner: Mapped[str | None] = mapped_column(String(80))
    timeline: Mapped[str | None] = mapped_column(String(80))  # "45 days", "immediate · ongoing"
    due_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="todo", server_default="todo")
    expected_effect: Mapped[str | None] = mapped_column(String(120))  # "DSCR +0.09x"
    published: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class DealerAlert(TimestampMixin, Base):
    __tablename__ = "dos_alerts"

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)  # liquidity_floor|adb_target|nsf|plan_due|reconcile
    severity: Mapped[str] = mapped_column(String(12), default="warn", server_default="warn")
    message: Mapped[str] = mapped_column(String(320), nullable=False)
    ref_kind: Mapped[str | None] = mapped_column(String(24))
    ref_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DealerCreditProfile(TimestampMixin, Base):
    __tablename__ = "dos_credit_profiles"
    __table_args__ = (UniqueConstraint("dealer_id", name="uq_dos_credit_dealer"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    business_history: Mapped[dict | None] = mapped_column(JSONB)  # trade/merchant-processor monthly payment history
    personal_score: Mapped[int | None] = mapped_column(Integer)
    personal_tier: Mapped[str | None] = mapped_column(String(12))  # tier1|tier2


class DealerTaxFiling(TimestampMixin, Base):
    __tablename__ = "dos_tax_filings"
    __table_args__ = (UniqueConstraint("dealer_id", "year", name="uq_dos_tax_year"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    filed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    revenue_reported: Mapped[float | None] = mapped_column(Numeric(14, 2))
    deposits_observed: Mapped[float | None] = mapped_column(Numeric(14, 2))
    discrepancy: Mapped[str | None] = mapped_column(Text)


class DealerMessage(TimestampMixin, Base):
    """Dealer workspace message. internal=True rows are team-only notes and
    must never be surfaced to the dealer portal (Stream 6)."""

    __tablename__ = "dos_messages"

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    author_name: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    internal: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class DealerSession(TimestampMixin, Base):
    """Scheduled touchpoint with the dealer — training | call | review."""

    __tablename__ = "dos_sessions"

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="call", server_default="call")  # training|call|review
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    join_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DealerDocument(TimestampMixin, Base):
    """Uploaded financial document (bank statement, P&L, tax return, debt
    schedule). Bytes are archived to S3 best-effort (s3_key nullable — when S3
    is unconfigured we extract in-memory and keep only the summary); the
    normalized output always flows through the same classify_event /
    rebuild_periods / recompute_snapshot pipeline as every other source."""

    __tablename__ = "dos_documents"
    __table_args__ = (Index("ix_dos_documents_dealer_created", "dealer_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(260), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    s3_key: Mapped[str | None] = mapped_column(String(400))
    kind: Mapped[str] = mapped_column(String(24), default="statement", server_default="statement")  # statement|pl|tax|debt_schedule|other
    status: Mapped[str] = mapped_column(String(16), default="uploaded", server_default="uploaded")  # uploaded|extracting|extracted|failed
    error: Mapped[str | None] = mapped_column(Text)
    extracted: Mapped[dict | None] = mapped_column(JSONB)  # {months: [...], transactions_count, notes}


class DealerSourceConnection(TimestampMixin, Base):
    __tablename__ = "dos_source_connections"
    __table_args__ = (UniqueConstraint("dealer_id", "kind", name="uq_dos_source"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # uploads|plaid|quickbooks|fixtures
    status: Mapped[str] = mapped_column(String(24), default="active", server_default="active")
    encrypted_tokens: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
