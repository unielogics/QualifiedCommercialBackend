"""Dealer OS domain models. Every table is prefixed dos_ (isolation contract).

Monthly-grain time series now (uploads-first, no Plaid keys yet); the same rows
gain daily grain when connected feeds land. JSONB is used for open-ended
sub-structures (liquidity buckets, flags, credit history) so the schema stays
stable while engines evolve.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
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
    address: Mapped[str | None] = mapped_column(String(240))  # street line (Geoapify-validated in UI)
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(8))  # USPS 2-letter code (dropdown-regulated in UI)
    zip: Mapped[str | None] = mapped_column(String(12))
    # How much funding the client is looking for (0119) — drives program
    # sizing and reverse-engineered metric targets.
    # One client "file" can hold an owner's multiple LLCs (0120). Metrics are
    # entirely per-dealer; the group is a console/reporting concept.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealer_groups.id", ondelete="SET NULL")
    )
    funding_goal: Mapped[float | None] = mapped_column(Numeric(14, 2))
    funding_purpose: Mapped[str | None] = mapped_column(String(48))  # working_capital|equipment|real_estate|refinance|floorplan|other
    industry: Mapped[str] = mapped_column(String(48), default="auto_dealer", server_default="auto_dealer")
    status: Mapped[str] = mapped_column(String(24), default="active", server_default="active")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    dealer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    # Linked document Bucket (Phase 2) — either adopted from the dealer's AI
    # underwriter intake (matched by email) or created lazily as an audit
    # bucket. SET NULL so deleting a bucket never cascades into the dealer.
    bucket_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="SET NULL")
    )
    # Phase 3: breadcrumb to the AI-underwriter intake this dealer was handed
    # off from. Plain UUID on purpose — NO FK across to intakes (coupling
    # avoidance; the intake table lives outside the dos_ isolation boundary).
    handoff_intake_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # 0118: the profile facts underwriting asks for first.
    started_on: Mapped[date | None] = mapped_column(Date)
    entity_type: Mapped[str | None] = mapped_column(String(32))  # llc|s_corp|c_corp|partnership|sole_prop
    naics_code: Mapped[str | None] = mapped_column(String(8))
    naics_label: Mapped[str | None] = mapped_column(String(180))


class DealerAccount(TimestampMixin, Base):
    """One bank account of the dealer (Phase 3). Roles are AI-proposed with a
    hard precedence contract: role_set_by='ai' rows may be re-proposed, but
    once an admin sets the role (role_set_by='admin') the AI never changes it
    again — newer proposals only land in ai_proposed_role/ai_rationale."""

    __tablename__ = "dos_accounts"
    __table_args__ = (
        Index("ix_dos_accounts_dealer", "dealer_id"),
        # The last-4 IS the account's identity within a dealer, and it is what
        # stops two concurrent ingest sweeps from creating the same account
        # twice (0115). Partial because a NULL mask carries no identity.
        Index(
            "uq_dos_account_mask",
            "dealer_id",
            "mask",
            unique=True,
            postgresql_where=text("mask IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    institution: Mapped[str | None] = mapped_column(String(160))
    mask: Mapped[str | None] = mapped_column(String(8))  # last-4 digits
    # primary_operating|secondary|payroll|savings|floorplan_reserve|other
    role: Mapped[str] = mapped_column(String(24), default="other", server_default="other")
    ai_proposed_role: Mapped[str | None] = mapped_column(String(24))
    ai_rationale: Mapped[str | None] = mapped_column(Text)
    role_set_by: Mapped[str] = mapped_column(String(8), default="ai", server_default="ai")  # ai|admin
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")


class DealerFinancialPeriod(TimestampMixin, Base):
    """One normalized month of financials, source-agnostic."""

    __tablename__ = "dos_financial_periods"
    # Uniqueness (Phase 3) is a FUNCTIONAL unique index created in migration
    # 0112_dos_phase3 — not expressible as a declarative UniqueConstraint:
    #   CREATE UNIQUE INDEX uq_dos_period_acct ON dos_financial_periods
    #     (dealer_id, coalesce(account_id, '00000000-...0000'::uuid), period)
    # i.e. one row per (dealer, account, month), where legacy null-account rows
    # collapse onto the zero-uuid sentinel (one legacy row per dealer-month).
    # The old uq_dos_period (dealer_id, period) constraint was dropped there.

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    # Phase 3: which bank account this month belongs to; NULL = legacy/blended.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_accounts.id", ondelete="SET NULL")
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
    __table_args__ = (
        Index("ix_dos_cash_events_dealer_document", "dealer_id", "document_id"),
        Index("ix_dos_cash_events_dealer_occurred", "dealer_id", "occurred_on"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    # Phase 3: source bank account of the line; NULL = legacy/unattributed.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_accounts.id", ondelete="SET NULL")
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
    # Which document this line was extracted from (0119) — the "reference the
    # PDF" backbone. NULL for legacy rows and CSV bulk imports.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_documents.id", ondelete="SET NULL")
    )


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
    # Phase 3: evidence document backing the add-back (SET NULL on doc delete).
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_documents.id", ondelete="SET NULL")
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
    # The tax-return document this filing row was read from (0119).
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_documents.id", ondelete="SET NULL")
    )
    # 0117: the return's own figures, kept so EBITDA can be rebuilt from the
    # filing when no P&L has been uploaded.
    entity_name: Mapped[str | None] = mapped_column(String(180))
    form_type: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict | None] = mapped_column(JSONB)


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
    kind: Mapped[str] = mapped_column(String(24), default="statement", server_default="statement")  # statement|pl|tax|debt_schedule|other|archive
    status: Mapped[str] = mapped_column(String(16), default="uploaded", server_default="uploaded")  # uploaded|extracting|extracted|failed
    error: Mapped[str | None] = mapped_column(Text)
    extracted: Mapped[dict | None] = mapped_column(JSONB)  # {months: [...], transactions_count, notes}
    # Doc hub (0114): children of an expanded ZIP archive point at their parent
    # row — CASCADE so deleting the parent removes the whole expansion.
    parent_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_documents.id", ondelete="CASCADE")
    )
    # AI-classified type (bank_statement|tax_return|profit_and_loss|
    # balance_sheet|debt_schedule|credit_report|other|archive). The uploader-
    # declared `kind` above is never touched by classification.
    detected_kind: Mapped[str | None] = mapped_column(String(24))
    doc_meta: Mapped[dict | None] = mapped_column(JSONB)  # classifier payload summary
    # Mirror row in the dealer's linked Bucket (Phase 2). Set on push (upload
    # -> bucket) and on pull (bucket file ingested -> Dealer OS). SET NULL so
    # bucket-file deletion never destroys the extraction record.
    bucket_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL")
    )
    # Phase 3: bank account the document was matched to (statement uploads).
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_accounts.id", ondelete="SET NULL")
    )


class DealerDocRequest(TimestampMixin, Base):
    """Team-requested document (Phase 3 Wave 1 schema; Wave 3 consumes it)."""

    __tablename__ = "dos_doc_requests"

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="statement", server_default="statement")
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_accounts.id", ondelete="SET NULL")
    )
    due_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open")  # open|fulfilled|cancelled
    fulfilled_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_documents.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)


class DealerAuditLog(Base):
    """Append-only action trail (Phase 3): who changed what, before/after.
    created_at only — audit rows are never updated."""

    __tablename__ = "dos_audit_log"
    __table_args__ = (Index("ix_dos_audit_dealer_created", "dealer_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)  # e.g. target.override, cash_event.recategorize
    entity_kind: Mapped[str] = mapped_column(String(24), nullable=False)  # target|cash_event|addback|account|rule|dealer
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DealerCategoryRule(TimestampMixin, Base):
    """Substring -> category rule (Phase 3). dealer_id NULL = global rule.
    At classify time rules beat heuristics, dealer-scoped rules beat global,
    and admin-corrected events (categorized_by='admin') are never retro-touched
    — human correction wins."""

    __tablename__ = "dos_category_rules"
    __table_args__ = (Index("ix_dos_category_rules_dealer", "dealer_id"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE")
    )
    pattern: Mapped[str] = mapped_column(String(160), nullable=False)  # lowercase substring
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class DealerDebt(TimestampMixin, Base):
    """One obligation on the dealer's debt schedule (0116).

    Drafted from the vendor rollup, then owned by the admin: origin='ai_draft'
    rows may be refreshed by a re-draft, but origin='admin' is never
    overwritten — the same precedence law as metric targets and account roles.
    Dismissing a drafted row keeps it from returning without deleting the
    evidence that it was proposed."""

    __tablename__ = "dos_debts"
    __table_args__ = (Index("ix_dos_debts_dealer", "dealer_id"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    lender: Mapped[str] = mapped_column(String(180), nullable=False)
    category: Mapped[str] = mapped_column(String(24), default="loan", server_default="loan")
    monthly_payment: Mapped[float | None] = mapped_column(Numeric(14, 2))
    balance: Mapped[float | None] = mapped_column(Numeric(14, 2))
    rate: Mapped[float | None] = mapped_column(Numeric(6, 3))
    term_months: Mapped[int | None] = mapped_column(Integer)
    maturity_on: Mapped[date | None] = mapped_column(Date)
    origin: Mapped[str] = mapped_column(String(16), default="ai_draft", server_default="ai_draft")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    vendor_key: Mapped[str | None] = mapped_column(String(60))
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)


class DealerOwner(TimestampMixin, Base):
    """A principal of the business (0118).

    Owners are who a personal credit pull is ABOUT — without a row per owner
    there is nobody to pull for. Only the pull's summary is echoed here;
    the FCRA-governed record stays in credit_pulls and no SSN is stored."""

    __tablename__ = "dos_owners"
    __table_args__ = (Index("ix_dos_owners_dealer", "dealer_id"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(48))
    ownership_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    is_guarantor: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    dob: Mapped[date | None] = mapped_column(Date)
    street: Mapped[str | None] = mapped_column(String(240))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(8))
    zip: Mapped[str | None] = mapped_column(String(12))
    credit_score: Mapped[int | None] = mapped_column(Integer)
    credit_tier: Mapped[str | None] = mapped_column(String(16))
    credit_pulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credit_pull_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    credit_summary: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)


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


class DealerGroup(TimestampMixin, Base):
    """A client file: one owner's set of LLCs audited together (0120)."""

    __tablename__ = "dos_dealer_groups"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(160), nullable=False)


class DealerProgramSetting(TimestampMixin, Base):
    """Desk-approved override of one program's sizing/readiness constants.

    Absent row = the PROVISIONAL code defaults in services/paths.py. The row
    carries its own change history (dos_audit_log is dealer-scoped and cannot
    record global actions)."""

    __tablename__ = "dos_program_settings"

    id: Mapped[uuid.UUID] = _pk()
    path_key: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    sizing: Mapped[dict | None] = mapped_column(JSONB)
    requirements: Mapped[list | None] = mapped_column(JSONB)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    history: Mapped[list | None] = mapped_column(JSONB)


class DealerPaymentShift(TimestampMixin, Base):
    """Team-authored payment-date shift: move a regular withdrawal later in
    the month (under real vendor terms — never statement-date window
    dressing) to raise average daily balance. status='draft' rows are
    team-only; dealers see proposed/done/dismissed."""

    __tablename__ = "dos_payment_shifts"
    __table_args__ = (Index("ix_dos_payment_shifts_dealer_status", "dealer_id", "status"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    vendor_key: Mapped[str | None] = mapped_column(String(120))
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    from_day: Mapped[int] = mapped_column(Integer, nullable=False)
    to_day: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    est_adb_impact: Mapped[float | None] = mapped_column(Numeric(14, 2))
    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")  # draft|proposed|done|dismissed
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
