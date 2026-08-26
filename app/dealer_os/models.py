"""Dealer OS domain models. Every table is prefixed dos_ (isolation contract).

Monthly-grain time series now (uploads-first, no Plaid keys yet); the same rows
gain daily grain when connected feeds land. JSONB is used for open-ended
sub-structures (liquidity buckets, flags, credit history) so the schema stays
stable while engines evolve.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
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
    # 0133: the reference a rep reads down a phone. Unique and immutable once
    # assigned, because it goes on contracts and into the client's inbox; a
    # file whose reference can change is a file two people can be looking at
    # while quoting different numbers.
    case_ref: Mapped[str | None] = mapped_column(String(24), unique=True)
    # 0136: when the desk graduated this file into a full audit client. The
    # conversion is a FLAG, never a copy: rep files and audit files are the
    # same row viewed from two apps, which is precisely why Plaid items,
    # credit pulls, documents and consent "transfer" — there is nothing to
    # move, and a conversion that minted a new row would strand all of it.
    audit_client_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0134: what the money is actually for, line by line.
    #
    # A single funding_goal answers "how much" and no lender asks only that.
    # Every credit file wants the breakdown, and the breakdown is also what
    # catches a request nobody has thought through: a rep and an owner who sit
    # down and itemise it usually discover the number was either high or low.
    #
    # JSONB rather than a child table on purpose. These rows are a statement
    # about one application at one moment, never queried across files, never
    # joined, and they should version with the case rather than drift from it.
    # A table would invite exactly the reporting that would make them look like
    # facts rather than a plan.
    use_of_proceeds: Mapped[list | None] = mapped_column(JSONB)
    use_of_proceeds_note: Mapped[str | None] = mapped_column(Text)
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
    client_requested_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    application_lifecycle: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    funding_purpose: Mapped[str | None] = mapped_column(String(48))  # working_capital|equipment|real_estate|refinance|floorplan|other
    industry: Mapped[str] = mapped_column(String(48), default="auto_dealer", server_default="auto_dealer")
    industry_label: Mapped[str | None] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(24), default="active", server_default="active")
    # Rep-facing deletion is an archive tombstone. The file and every related
    # credit, bank, document, message, and audit row remain intact.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
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
    subindustry: Mapped[str | None] = mapped_column(String(120))
    subindustry_label: Mapped[str | None] = mapped_column(String(180))
    industry_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    subindustry_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    activity_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )


class DealerApplicationProfile(TimestampMixin, Base):
    """Rep-entered fields that complete the step-4 submission package.

    DealerBusiness keeps identity and verified facts. This row keeps the fields
    a rep still has to collect after the bank and credit gate opens.
    """

    __tablename__ = "dos_application_profiles"
    __table_args__ = (
        UniqueConstraint("dealer_id", name="uq_dos_application_profiles_dealer"),
        Index("ix_dos_application_profiles_dealer", "dealer_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    dba_name: Mapped[str | None] = mapped_column(String(180))
    website: Mapped[str | None] = mapped_column(String(500))
    state_of_formation: Mapped[str | None] = mapped_column(String(2))
    location_type: Mapped[str | None] = mapped_column(String(32))
    mailing_address: Mapped[str | None] = mapped_column(String(300))
    mailing_city: Mapped[str | None] = mapped_column(String(120))
    mailing_state: Mapped[str | None] = mapped_column(String(2))
    mailing_zip: Mapped[str | None] = mapped_column(String(12))
    annual_sales: Mapped[float | None] = mapped_column(Numeric(14, 2))
    annual_cash_flow_available_for_debt: Mapped[float | None] = mapped_column(Numeric(14, 2))
    monthly_debt_payments: Mapped[float | None] = mapped_column(Numeric(14, 2))
    signer_title: Mapped[str | None] = mapped_column(String(120))
    # The engine recommends; an authorized desk reviewer releases the file.
    human_review_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    human_review_note: Mapped[str | None] = mapped_column(Text)
    human_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    human_reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    landlord_mortgagee: Mapped[str | None] = mapped_column(String(200))
    guarantor_home_address: Mapped[str | None] = mapped_column(String(300))
    guarantor_dob: Mapped[date | None] = mapped_column(Date)
    selected_program: Mapped[str | None] = mapped_column(String(80))
    term_requested_months: Mapped[int | None] = mapped_column(Integer)
    collateral_description: Mapped[str | None] = mapped_column(Text)
    use_of_proceeds_text: Mapped[str | None] = mapped_column(Text)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


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
    # Client response (0123): the dealer accepts or declines each published
    # action; declining feeds back into the simulation via the linked shift.
    client_response: Mapped[str | None] = mapped_column(String(16))  # accepted|declined
    client_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # 0132: `internal` said whether the client could see a row, which was two
    # channels wearing one boolean. `channel` says which conversation it
    # belongs to. `internal` is kept and kept in sync, because the dealer
    # portal and QCDashboard both filter on it and neither should have to
    # learn a new vocabulary to stay safe.
    channel: Mapped[str] = mapped_column(String(12), nullable=False, server_default="client")
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


REP_APPOINTMENT_KINDS: tuple[str, ...] = ("callback", "program_intro", "underwriting_review")
REP_APPOINTMENT_STATUSES: tuple[str, ...] = ("pending", "confirmed", "cancelled", "done")
REP_APPOINTMENT_OUTCOMES: tuple[str, ...] = ("not_converted", "did_not_show", "converted")
REP_APPOINTMENT_CONVERSION_TARGETS: tuple[str, ...] = ("field_desk", "ai_intake")


class DealerRepAppointment(TimestampMixin, Base):
    """A booked rep appointment, mirrored to CalendarEvent when possible."""

    __tablename__ = "dos_rep_appointments"
    __table_args__ = (
        Index("ix_dos_rep_appointments_dealer", "dealer_id", "starts_at"),
        Index("ix_dos_rep_appointments_owner", "owner_user_id", "starts_at"),
        Index("ix_dos_rep_appointments_event", "calendar_event_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    calendar_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("calendar_events.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contacts.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="callback", server_default="callback")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    timezone: Mapped[str] = mapped_column(String(80), nullable=False, default="America/New_York", server_default="America/New_York")
    invitee_name: Mapped[str] = mapped_column(String(160), nullable=False)
    invitee_email: Mapped[str | None] = mapped_column(String(320))
    invitee_phone: Mapped[str | None] = mapped_column(String(32))
    company: Mapped[str | None] = mapped_column(String(180))
    program_name: Mapped[str | None] = mapped_column(String(180))
    requested_amount: Mapped[str | None] = mapped_column(String(40))
    full_address: Mapped[str | None] = mapped_column(String(500))
    join_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    booked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    outcome: Mapped[str | None] = mapped_column(String(24))
    outcome_note: Mapped[str | None] = mapped_column(Text)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    conversion_target: Mapped[str | None] = mapped_column(String(24))
    converted_dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    converted_intake_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL")
    )


UNDERWRITING_PREFERENCE_STATUSES: tuple[str, ...] = ("pending", "selected", "booked", "expired")


class DealerUnderwritingReviewPreference(TimestampMixin, Base):
    """Three client-friendly review windows required between steps 3 and 4."""

    __tablename__ = "dos_underwriting_review_preferences"
    __table_args__ = (
        Index("ix_dos_uw_review_prefs_dealer", "dealer_id", "submitted_at"),
        Index("ix_dos_uw_review_prefs_status", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    rep_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    timezone: Mapped[str] = mapped_column(String(80), nullable=False, default="America/New_York", server_default="America/New_York")
    slots: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    selected_slot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_appointments.id", ondelete="SET NULL")
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
    # 0127: Plaid statement identity — partial-unique per dealer, so a
    # refresh can never ingest the same statement twice.
    plaid_statement_id: Mapped[str | None] = mapped_column(String(64))
    # The Plaid institution that supplied this statement. Older rows remain
    # null because statement ids alone cannot be safely reverse-mapped after
    # ingestion; new rows keep exact institution lineage.
    plaid_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_plaid_items.id", ondelete="SET NULL")
    )
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
    # 0129: THE per-row DSCR law — this row's monthly figure counts toward
    # the debt-service denominator iff true. Toggled from the DSCR composer,
    # audited; drafted credit cards default false (operating spend routed
    # through a card), everything else true.
    count_in_dscr: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Refinance workbench (0126): the contract's native cadence. monthly_payment
    # stays the monthly EQUIVALENT the engines read; payment_amount/frequency
    # preserve what the agreement actually says ($420/day daily MCA).
    payment_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    payment_frequency: Mapped[str | None] = mapped_column(String(12))  # daily|weekly|biweekly|monthly
    factor_rate: Mapped[float | None] = mapped_column(Numeric(6, 3))
    payoff_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_documents.id", ondelete="SET NULL")
    )


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
    # is_primary marks the login's own person (0125): the client may self-pull
    # this row exactly once; every other owner consents via a one-time link.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    invite_token_hash: Mapped[str | None] = mapped_column(String(64))
    invite_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invite_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credit_workflow_status: Mapped[str | None] = mapped_column(String(32))
    credit_delivery_detail: Mapped[str | None] = mapped_column(String(240))
    credit_provider_request_id: Mapped[str | None] = mapped_column(String(120))
    credit_provider_error_category: Mapped[str | None] = mapped_column(String(48))

    @property
    def has_invite(self) -> bool:
        """Outstanding consent link? Read by OwnerRead via ORM mode so the
        API can say "a link exists" without ever exposing the hash."""
        return self.invite_token_hash is not None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def credit_required(self) -> bool:
        return float(self.ownership_pct or 0) >= 20.0

    @property
    def credit_complete(self) -> bool:
        return self.credit_pulled_at is not None

    @property
    def credit_contact_complete(self) -> bool:
        return bool((self.email or "").strip() and (self.phone or "").strip())

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


class DealerPlaidItem(TimestampMixin, Base):
    """One connected bank via Plaid.

    Statements are imported into the document pipeline and the same Item may
    be used for an explicitly requested Asset Report. Access tokens are
    Fernet-encrypted at rest and isolated by Plaid environment.
    """

    __tablename__ = "dos_plaid_items"
    __table_args__ = (Index("ix_dos_plaid_items_dealer", "dealer_id"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    environment: Mapped[str] = mapped_column(
        String(16), nullable=False, default="sandbox", server_default="sandbox"
    )
    institution_name: Mapped[str | None] = mapped_column(String(160))
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encryption_provider: Mapped[str] = mapped_column(String(16), default="fernet", server_default="fernet")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")  # active|error|removed
    error: Mapped[str | None] = mapped_column(Text)
    last_pulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0128: super-admin controls — pause/resume the 30-day cycle; the
    # connected accounts' names + last-4 for the row label.
    auto_refresh: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    accounts_label: Mapped[str | None] = mapped_column(String(200))
    is_primary_operating: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    update_mode_reason: Mapped[str | None] = mapped_column(String(32))
    update_mode_account_selection: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # 'out' = pay later under vendor terms; 'in' = collect earlier (real
    # receivables change — deposits only ever move EARLIER, 0121).
    direction: Mapped[str] = mapped_column(String(8), default="out", server_default="out")
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    from_day: Mapped[int] = mapped_column(Integer, nullable=False)
    to_day: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    est_adb_impact: Mapped[float | None] = mapped_column(Numeric(14, 2))
    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")  # draft|proposed|done|dismissed
    # The Plan action this proposal materialized as (0122): proposing a shift
    # IS telling the client to call the vendor — it lands on the Plan page.
    plan_action_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_plan_actions.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DealerPlanComment(TimestampMixin, Base):
    """Per-action discussion between the team and the client (0123)."""

    __tablename__ = "dos_plan_comments"
    __table_args__ = (Index("ix_dos_plan_comments_action", "action_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    action_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_plan_actions.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    author_role: Mapped[str] = mapped_column(String(16), nullable=False)  # team|dealer
    author_name: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text, nullable=False)


class DealerMessageSeen(TimestampMixin, Base):
    """When a viewer last opened the dealer's message thread (0124)."""

    __tablename__ = "dos_message_seen"
    __table_args__ = (UniqueConstraint("dealer_id", "user_id", name="uq_dos_message_seen"),)

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- Field-rep pipeline (0130) ------------------------------------------------

# The lifecycle of a rep-collected file, in order. Enumerated here and enforced
# by the API rather than left as free text: the AI-underwriter side writes only
# two of the three statuses its own admin UI filters on, which makes a filter
# that silently returns nothing. One list, one source of truth.
REP_LEAD_STATUSES: tuple[str, ...] = (
    "draft",           # created on site, still being filled in
    "info_collected",  # the rep's form is complete
    "awaiting_docs",   # link sent, waiting on the client's bank or uploads
    "analyzing",       # documents landed, metrics computing
    "decision_ready",  # a verdict exists for the desk to act on
    "forms_out",       # application PDFs sent for signature
    "signed",          # signed, awaiting final checks
    "complete",        # done
    "declined",        # not proceeding
    "stalled",         # no client response; distinct from declined, and the
                       # one a performance report should surface loudest
)

# Statuses that end the file. Used by the performance rollup so a stalled file
# is never counted as still-working.
REP_LEAD_TERMINAL: frozenset[str] = frozenset({"complete", "declined", "stalled"})


class DealerRepLead(TimestampMixin, Base):
    """A field rep's file: the pipeline wrapper around a DealerBusiness.

    The business carries the financials and gets the whole Capital OS engine.
    This row carries who owns it and where it is in the process, which is
    everything the engine deliberately has no opinion about.

    Kept separate from DealerBusiness rather than adding status columns to it
    because a business is durable and a lead is a moment: the same business can
    be worked again next year, and its metrics history should not be entangled
    with the pipeline state of a file that closed.
    """

    __tablename__ = "dos_rep_leads"
    __table_args__ = (
        # One open lead per business. A second attempt is a new row only once
        # the previous one is terminal, which the API enforces.
        Index("ix_dos_rep_leads_dealer", "dealer_id"),
        Index("ix_dos_rep_leads_rep_status", "rep_user_id", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    # The owning rep. SET NULL rather than CASCADE: a rep leaving must never
    # delete the files they collected.
    rep_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    # The desk's answer, once there is one: fundable | conditional | not_yet.
    # Mirrors fundability_verdict rather than inventing a second vocabulary.
    decision: Mapped[str | None] = mapped_column(String(16))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Where the rep met them. Free text on purpose; this is a note, not a key.
    source_note: Mapped[str | None] = mapped_column(String(240))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Append-only [{at, from, to, by, by_name}] so time-in-status is derivable
    # without a second table. Capped by the API.
    status_history: Mapped[list | None] = mapped_column(JSONB)


# --- SMS consent (0131) -------------------------------------------------------

# The exact disclosure a person agreed to, versioned. Carriers audit the
# WORDING, not just the fact of a checkbox, so the text lives here beside the
# record and every consent stores which version it accepted. Changing the
# wording means a new version, never an edit in place.
SMS_DISCLOSURE_VERSION = "2026-08-25-1"

SMS_CONSENT_KINDS: tuple[str, ...] = ("transactional", "marketing")

# How the consent was obtained. Carriers weigh these differently: a person
# ticking the box themselves is the strongest, a rep recording what they were
# told in person is acceptable when attested and logged, and anything else is
# not consent at all.
SMS_CONSENT_METHODS: tuple[str, ...] = (
    "self_web",        # the person checked the box on their own device
    "in_person_device",# the rep handed their device over and the person checked it
    "rep_attested",    # the rep recorded verbal consent given in front of them
)


class DealerSmsConsent(TimestampMixin, Base):
    """Proof that a specific phone number agreed to receive a specific kind of
    message, under a specific disclosure.

    Kept as its own row rather than a flag on the business because consent is
    evidence: it has to survive the business record being edited, it has to be
    revocable without losing the history, and a regulator or carrier asking
    "show me" needs a timestamp, an IP and the exact words shown.

    Modelled on the e-signature record, which solved the same problem for
    documents.
    """

    __tablename__ = "dos_sms_consent"
    __table_args__ = (
        Index("ix_dos_sms_consent_dealer", "dealer_id"),
        # Lookups on send are always "is this number cleared for this kind".
        Index("ix_dos_sms_consent_phone_kind", "phone_e164", "consent_kind"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    # E.164. Consent belongs to a NUMBER, not a person or a business: if the
    # number changes, the old consent does not carry over to the new one.
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    consent_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    method: Mapped[str] = mapped_column(String(24), nullable=False)

    # What they actually saw, and proof of it.
    disclosure_version: Mapped[str] = mapped_column(String(24), nullable=False)
    disclosure_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    disclosure_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Who was in the room, and from where.
    captured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    captured_by_name: Mapped[str | None] = mapped_column(String(120))
    consenter_name: Mapped[str | None] = mapped_column(String(160))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))

    # Revocation is a new state on the same row, so the grant is never erased.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(120))


REP_INBOX_CHANNELS: tuple[str, ...] = ("email", "sms")
REP_INBOX_DIRECTIONS: tuple[str, ...] = ("inbound", "outbound")


class DealerRepCompany(TimestampMixin, Base):
    """A CRM company that may have many people and funding files."""

    __tablename__ = "dos_rep_companies"
    __table_args__ = (
        Index("ix_dos_rep_companies_owner", "owner_user_id", "updated_at"),
        Index("ix_dos_rep_companies_name", "owner_user_id", "name"),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(80))
    industry_label: Mapped[str | None] = mapped_column(String(180))
    subindustry: Mapped[str | None] = mapped_column(String(120))
    subindustry_label: Mapped[str | None] = mapped_column(String(180))
    naics_code: Mapped[str | None] = mapped_column(String(8))
    naics_label: Mapped[str | None] = mapped_column(String(180))
    industry_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    subindustry_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    activity_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    address: Mapped[str | None] = mapped_column(String(240))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(8))
    zip: Mapped[str | None] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )


class DealerRepContact(TimestampMixin, Base):
    """A person a rep is working, with or without a dealer file yet."""

    __tablename__ = "dos_rep_contacts"
    __table_args__ = (
        Index("ix_dos_rep_contacts_owner", "owner_user_id", "last_activity_at"),
        Index("ix_dos_rep_contacts_email", "owner_user_id", "email"),
        Index("ix_dos_rep_contacts_phone", "owner_user_id", "phone_e164"),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_companies.id", ondelete="SET NULL")
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    company: Mapped[str | None] = mapped_column(String(180))
    email: Mapped[str | None] = mapped_column(String(320))
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", server_default="manual")
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_transactional_consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_marketing_consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_consent_meta: Mapped[dict | None] = mapped_column(JSONB)
    sms_opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DealerRepContactAssignment(TimestampMixin, Base):
    __tablename__ = "dos_rep_contact_assignments"
    __table_args__ = (
        UniqueConstraint("contact_id", "user_id", name="uq_dos_rep_contact_assignment"),
        Index("ix_dos_rep_contact_assignments_user", "user_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contacts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DealerApplicationContact(TimestampMixin, Base):
    __tablename__ = "dos_application_contacts"
    __table_args__ = (
        UniqueConstraint("dealer_id", "contact_id", name="uq_dos_application_contact"),
        Index("ix_dos_application_contacts_contact", "contact_id", "dealer_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contacts.id", ondelete="CASCADE"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(
        String(24), nullable=False, default="owner", server_default="owner"
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class DealerProductCatalog(TimestampMixin, Base):
    __tablename__ = "dos_product_catalog"
    __table_args__ = (
        UniqueConstraint("program_key", "version", name="uq_dos_product_catalog_version"),
        Index("ix_dos_product_catalog_active", "active", "category", "sort_order"),
    )

    id: Mapped[uuid.UUID] = _pk()
    program_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    copy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    pricing: Mapped[dict | None] = mapped_column(JSONB)
    eligibility: Mapped[dict | None] = mapped_column(JSONB)
    disclosures: Mapped[dict | None] = mapped_column(JSONB)
    amount_min: Mapped[float | None] = mapped_column(Numeric(14, 2))
    amount_max: Mapped[float | None] = mapped_column(Numeric(14, 2))
    term_min_months: Mapped[int | None] = mapped_column(Integer)
    term_max_months: Mapped[int | None] = mapped_column(Integer)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DealerProductFinderSession(TimestampMixin, Base):
    __tablename__ = "dos_product_finder_sessions"
    __table_args__ = (
        Index("ix_dos_product_finder_owner", "owner_user_id", "updated_at"),
        Index("ix_dos_product_finder_contact", "contact_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_companies.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contacts.id", ondelete="CASCADE"), nullable=False
    )
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    locale: Mapped[str] = mapped_column(String(2), nullable=False, default="en", server_default="en")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="screening", server_default="screening"
    )
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    current_result: Mapped[dict | None] = mapped_column(JSONB)
    client_requested_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    recommended_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    funding_goal_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DealerProductScreeningSnapshot(TimestampMixin, Base):
    __tablename__ = "dos_product_screening_snapshots"
    __table_args__ = (Index("ix_dos_product_screening_session", "session_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_product_finder_sessions.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="self_reported", server_default="self_reported"
    )
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DealerFieldDeskProfile(TimestampMixin, Base):
    """One public-facing Field Desk identity per internal staff user."""

    __tablename__ = "dos_field_desk_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_dos_field_desk_profile_user"),
        Index("ix_dos_field_desk_profiles_visible", "card_visible", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(String(160))
    title: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32))
    display_email: Mapped[str | None] = mapped_column(String(320))
    short_bio: Mapped[str | None] = mapped_column(Text)
    preferred_locale: Mapped[str] = mapped_column(
        String(2), nullable=False, default="en", server_default="en"
    )
    card_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    headshot_s3_key: Mapped[str | None] = mapped_column(String(720))


class DealerRepContactShare(TimestampMixin, Base):
    """A business-card/program intro sent by a rep."""

    __tablename__ = "dos_rep_contact_shares"
    __table_args__ = (
        Index("ix_dos_rep_contact_shares_owner", "owner_user_id", "created_at"),
        Index("ix_dos_rep_contact_shares_contact", "contact_id"),
        Index("ix_dos_rep_contact_shares_token", "card_token", unique=True),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contacts.id", ondelete="SET NULL")
    )
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    recipient_name: Mapped[str] = mapped_column(String(160), nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    recipient_phone_e164: Mapped[str | None] = mapped_column(String(20))
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="email", server_default="email")
    card_token: Mapped[str] = mapped_column(String(48), nullable=False)
    subject: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    email_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_requested", server_default="not_requested")
    sms_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_requested", server_default="not_requested")
    provider_refs: Mapped[dict | None] = mapped_column(JSONB)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DealerRepInboxThread(TimestampMixin, Base):
    """Owner-scoped email/SMS conversation for reps."""

    __tablename__ = "dos_rep_inbox_threads"
    __table_args__ = (
        Index("ix_dos_rep_inbox_threads_owner", "owner_user_id", "last_message_at"),
        Index("ix_dos_rep_inbox_threads_contact", "contact_id"),
        Index("ix_dos_rep_inbox_threads_dealer", "dealer_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contacts.id", ondelete="SET NULL")
    )
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    subject_key: Mapped[str | None] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="email", server_default="email")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", server_default="manual")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")


class DealerRepInboxMessage(TimestampMixin, Base):
    """One inbound or outbound message in a rep inbox thread."""

    __tablename__ = "dos_rep_inbox_messages"
    __table_args__ = (
        Index("ix_dos_rep_inbox_messages_thread", "thread_id", "created_at"),
        Index("ix_dos_rep_inbox_messages_owner", "owner_user_id", "created_at"),
        Index(
            "uq_dos_rep_inbox_provider_message",
            "provider",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    thread_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_inbox_threads.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contacts.id", ondelete="SET NULL")
    )
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    direction: Mapped[str] = mapped_column(String(12), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(160))
    provider_error: Mapped[str | None] = mapped_column(String(500))
    delivery_status: Mapped[str] = mapped_column(String(24), nullable=False, default="stored", server_default="stored")
    sender: Mapped[str | None] = mapped_column(String(320))
    recipient: Mapped[str | None] = mapped_column(String(320))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DealerProductPresentation(TimestampMixin, Base):
    """A product, comparison, or catalog shown or delivered to a contact."""

    __tablename__ = "dos_product_presentations"
    __table_args__ = (
        Index("ix_dos_product_presentations_contact", "contact_id", "created_at"),
        Index("ix_dos_product_presentations_owner", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_companies.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contacts.id", ondelete="CASCADE"), nullable=False
    )
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_product_finder_sessions.id", ondelete="SET NULL")
    )
    program_keys: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    locale: Mapped[str] = mapped_column(String(2), nullable=False, default="en", server_default="en")
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_person", server_default="in_person"
    )
    delivery_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="presented", server_default="presented"
    )
    catalog_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    contact_share_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_contact_shares.id", ondelete="SET NULL")
    )
    inbox_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_rep_inbox_threads.id", ondelete="SET NULL")
    )


class DealerApplicationPreScreen(TimestampMixin, Base):
    """Auditable Step 1.5 eligibility answers and deterministic result.

    Owner answers stay keyed by owner id so changing an ownership percentage
    can change who is currently required without deleting what was previously
    disclosed. The result is a snapshot of the rule version named here; later
    verified evidence may create a recalculation while this self-report stays
    available to the desk.
    """

    __tablename__ = "dos_application_pre_screens"
    __table_args__ = (
        UniqueConstraint("dealer_id", name="uq_dos_application_pre_screen_dealer"),
        Index("ix_dos_application_pre_screen_updated", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    rules_version: Mapped[str] = mapped_column(
        String(48), nullable=False, default="qc_direct_programs_v2", server_default="qc_direct_programs_v2"
    )
    file_answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    owner_answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    routing_result: Mapped[dict | None] = mapped_column(JSONB)
    self_report_routing_result: Mapped[dict | None] = mapped_column(JSONB)
    verified_routing_result: Mapped[dict | None] = mapped_column(JSONB)
    routing_history: Mapped[list | None] = mapped_column(JSONB)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DealerProductPresentationArtifact(TimestampMixin, Base):
    """Immutable presentation PDF plus a revocable public download token."""

    __tablename__ = "dos_product_presentation_artifacts"
    __table_args__ = (
        UniqueConstraint("presentation_id", name="uq_dos_product_presentation_artifact"),
        Index("ix_dos_product_presentation_token", "token_hash", unique=True),
    )

    id: Mapped[uuid.UUID] = _pk()
    presentation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dos_product_presentations.id", ondelete="CASCADE"),
        nullable=False,
    )
    s3_key: Mapped[str] = mapped_column(String(720), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DealerBankConsent(TimestampMixin, Base):
    """Proof that a person authorised connecting a bank account (0137).

    Sibling of DealerSmsConsent, not a reuse of it: SMS consent belongs to a
    NUMBER, a bank authorisation belongs to the business whose account is being
    connected. Same proof columns, different subject.

    The row is never mutated into nothing — a withdrawal sets revoked_at so the
    history reads "they consented, then withdrew", which is what an audit needs.
    """

    __tablename__ = "dos_bank_consent"
    __table_args__ = (
        # The gate asks one question per link-token request: is there a live
        # consent for this dealer? Newest first.
        Index("ix_dos_bank_consent_dealer", "dealer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    method: Mapped[str] = mapped_column(String(24), nullable=False)

    # What they actually saw, and proof of it.
    disclosure_version: Mapped[str] = mapped_column(String(24), nullable=False)
    disclosure_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    disclosure_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Who was in the room, and from where.
    captured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    captured_by_name: Mapped[str | None] = mapped_column(String(120))
    consenter_name: Mapped[str | None] = mapped_column(String(160))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(120))


# --- file conversation channels (0132) ---------------------------------------

# desk   the working conversation between rep, underwriter and super admin
# client the thread the business owner sees and can reply to
# note   short annotations pinned to the file, never a conversation
#
# The AI thread is deliberately NOT here. It is per-user and private, so it
# cannot share a table whose rows are visible to everyone on the desk.
MESSAGE_CHANNELS: tuple[str, ...] = ("desk", "client", "note")

# Which channels a client login may ever read. One place, so a future channel
# is invisible to the client until someone deliberately adds it here.
CLIENT_VISIBLE_CHANNELS: frozenset[str] = frozenset({"client"})


class DealerAIMessage(TimestampMixin, Base):
    """One turn in a private, per-user AI thread about one file.

    Private is the whole point. A rep asking "why did coverage come out at
    1.02" should not surface in the underwriter's view, and an underwriter
    stress-testing a file should not surface in the rep's. So every read and
    write is filtered by user_id as well as dealer_id, and there is no route
    that returns another user's turns.
    """

    __tablename__ = "dos_ai_messages"
    __table_args__ = (
        Index("ix_dos_ai_messages_thread", "dealer_id", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(12), nullable=False)  # user | assistant
    body: Mapped[str] = mapped_column(Text, nullable=False)


# --- contract package (0135) --------------------------------------------------

# The lender-facing documents a case executes at step 5. Registry-driven: a
# template is uploaded once by the desk, its fillable fields are discovered
# from the PDF itself, and each case then instantiates documents from it. The
# contracts are DATA, not code — next template needs an upload, not a deploy.
CONTRACT_DOC_STATUSES: tuple[str, ...] = (
    "draft",        # instantiated, fields still being filled
    "ready",        # every required field filled, not yet sent
    "out_for_signature",
    "executed",     # signed, certificate attached
    "void",
)


class ContractTemplate(TimestampMixin, Base):
    """One blank lender document plus what we know about its fields.

    `field_names` is what pypdf discovered in the PDF's AcroForm; `field_map`
    is the desk's mapping of those names to case-record sources
    ("business_name" -> "dealer.name"). Discovery is automatic, mapping is
    judgement: every lender names fields differently, and guessing a mapping
    writes the wrong value into a legal document.
    """

    __tablename__ = "dos_contract_templates"

    id: Mapped[uuid.UUID] = _pk()
    # Stable slug the UI and documents reference: "loan_app",
    # "consulting_agreement". Immutable once documents exist against it.
    key: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    s3_key: Mapped[str | None] = mapped_column(String(512))
    render_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="uploaded_pdf", server_default="uploaded_pdf"
    )
    page_count: Mapped[int | None] = mapped_column(Integer)
    # True when the PDF carries a real AcroForm. False means a flat scan:
    # filling needs the PyMuPDF coordinate overlay instead.
    has_acroform: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    field_names: Mapped[list | None] = mapped_column(JSONB)
    field_map: Mapped[dict | None] = mapped_column(JSONB)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Bumped when the underlying PDF is replaced, so an executed document can
    # always say which revision of the paper it was signed on.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ContractDocument(TimestampMixin, Base):
    """One case's instance of a template: its values, state and executed copy.

    Signing is evidence, so the executed record mirrors document_signature.py:
    the hash of the exact filled document the signer saw, the signature, and
    where the certificate lives. ESIGN/UETA compliance facts are stored per
    signing, not assumed: the consent-to-electronic-records acknowledgement is
    its own recorded act with its own timestamp, because "the platform's terms
    say so" is not the same as this signer agreeing on this document.
    """

    __tablename__ = "dos_contract_documents"
    __table_args__ = (
        Index("ix_dos_contract_docs_dealer", "dealer_id"),
        UniqueConstraint("dealer_id", "template_key", name="uq_dos_contract_doc"),
    )

    id: Mapped[uuid.UUID] = _pk()
    dealer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False
    )
    template_key: Mapped[str] = mapped_column(String(48), nullable=False)
    template_revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    field_values: Mapped[dict | None] = mapped_column(JSONB)
    filled_s3_key: Mapped[str | None] = mapped_column(String(512))
    filled_sha256: Mapped[str | None] = mapped_column(String(64))
    executed_s3_key: Mapped[str | None] = mapped_column(String(512))
    executed_sha256: Mapped[str | None] = mapped_column(String(64))
    # ESIGN §101(c): the signer's affirmative consent to do business
    # electronically, recorded as its own act before signing.
    esign_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    esign_consent_ip: Mapped[str | None] = mapped_column(String(64))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signer_name: Mapped[str | None] = mapped_column(String(160))
    signer_title: Mapped[str | None] = mapped_column(String(120))
    signature_sha256: Mapped[str | None] = mapped_column(String(64))
    signer_ip: Mapped[str | None] = mapped_column(String(64))
    signer_user_agent: Mapped[str | None] = mapped_column(String(400))
