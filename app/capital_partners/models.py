"""Capital Partners domain models. Every table is prefixed cp_ (isolation contract).

Status vocabularies live in ``enums.py``; the columns are plain strings with
check constraints so a bad value fails at the database, not at the next read.
Money is Numeric(14, 2), rates and percentages Numeric(7, 3).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

from . import enums as E


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _user_fk(nullable: bool = True) -> Mapped[uuid.UUID | None]:
    return mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=nullable)


def _in(column: str, values: type[E.StrEnum] | frozenset[str] | set[str], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in sorted(str(x) for x in values))
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class CpIssuerSettings(TimestampMixin, Base):
    """One row: who the issuer is, how long the waiting period runs, who the
    desk notifies. ``key`` is always ``default`` so the row is addressable."""

    __tablename__ = "cp_issuer_settings"

    id: Mapped[uuid.UUID] = _pk()
    key: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, default="default")
    issuer_name: Mapped[str | None] = mapped_column(String(200))
    cik: Mapped[str | None] = mapped_column(String(20))
    edgar_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_set")
    edgar_access_granted_at: Mapped[date | None] = mapped_column(Date)
    admin_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    default_waiting_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    servicer_label: Mapped[str] = mapped_column(String(120), nullable=False, default="Independent third-party servicer")
    #: Who hears about access requests and subscriptions (list of emails).
    notify_emails: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: Sent with the subscription documents; never shown on a public page.
    wiring_instructions: Mapped[str | None] = mapped_column(Text)


class CpAccessRequest(TimestampMixin, Base):
    """The public "Request an introduction" form on qualifiedcommercial.com."""

    __tablename__ = "cp_access_requests"
    __table_args__ = (_in("investing_as", E.InvestingAs, "ck_cp_access_requests_investing_as"),
                      _in("status", E.AccessRequestStatus, "ck_cp_access_requests_status"))

    id: Mapped[uuid.UUID] = _pk()
    investing_as: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_name: Mapped[str | None] = mapped_column(String(200))
    capital_band: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(40), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(String(5), nullable=False, default="en")
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    emailed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=E.AccessRequestStatus.NEW, index=True)
    invited_user_id: Mapped[uuid.UUID | None] = _user_fk()
    handled_by_id: Mapped[uuid.UUID | None] = _user_fk()
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_note: Mapped[str | None] = mapped_column(Text)


class CpMember(TimestampMixin, Base):
    """One per capital-partner login. The status machine is the 506(b) gate."""

    __tablename__ = "cp_members"
    __table_args__ = (_in("status", E.MemberStatus, "ck_cp_members_status"),)

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=E.MemberStatus.REQUESTED, index=True)
    investing_as: Mapped[str | None] = mapped_column(String(24))
    display_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    state_of_residence: Mapped[str | None] = mapped_column(String(2))
    capital_band: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str | None] = mapped_column(String(120))
    access_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_access_requests.id", ondelete="SET NULL")
    )
    invited_by_id: Mapped[uuid.UUID | None] = _user_fk()
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the desk decided the relationship existed: set on approval, and
    #: the waiting period counts from it.
    relationship_established_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waiting_period_days: Mapped[int | None] = mapped_column(Integer)
    waiting_period_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[uuid.UUID | None] = _user_fk()
    accreditation_basis: Mapped[str | None] = mapped_column(String(64))
    review_notes: Mapped[str | None] = mapped_column(Text)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    last_questionnaire_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    entities: Mapped[list[CpMemberEntity]] = relationship(
        "CpMemberEntity", back_populates="member", cascade="all, delete-orphan", order_by="CpMemberEntity.created_at"
    )


class CpMemberEntity(TimestampMixin, Base):
    """An investing vehicle: the person, a trust, an LLC, a family office entity."""

    __tablename__ = "cp_member_entities"
    __table_args__ = (_in("kind", E.EntityKind, "ck_cp_member_entities_kind"),)

    id: Mapped[uuid.UUID] = _pk()
    member_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str | None] = mapped_column(String(2))
    accreditation_basis: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    member: Mapped[CpMember] = relationship("CpMember", back_populates="entities")


class CpQuestionnaire(TimestampMixin, Base):
    """Evidence of accreditation: what was asked, what was answered, by whom,
    from where. Never edited in place; a resubmission is a new version."""

    __tablename__ = "cp_questionnaires"
    __table_args__ = (_in("outcome", E.QuestionnaireOutcome, "ck_cp_questionnaires_outcome"),)

    id: Mapped[uuid.UUID] = _pk()
    member_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_member_entities.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    basis: Mapped[str | None] = mapped_column(String(64))
    investing_as: Mapped[str | None] = mapped_column(String(24))
    state_of_residence: Mapped[str | None] = mapped_column(String(2))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_id: Mapped[uuid.UUID | None] = _user_fk()
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default=E.QuestionnaireOutcome.PENDING)
    review_notes: Mapped[str | None] = mapped_column(Text)


class CpListing(TimestampMixin, Base):
    """A loan participation offered to members. ``committed_amount`` is never
    stored: it is the sum of committed subscriptions (services/listings.py)."""

    __tablename__ = "cp_listings"
    __table_args__ = (
        _in("status", E.ListingStatus, "ck_cp_listings_status"),
        _in("source_kind", E.ListingSourceKind, "ck_cp_listings_source_kind"),
        _in("vertical", E.Vertical, "ck_cp_listings_vertical"),
        _in("lien_position", E.LienPosition, "ck_cp_listings_lien_position"),
        _in("ltv_kind", E.LtvKind, "ck_cp_listings_ltv_kind"),
        _in("payment_frequency", E.PaymentFrequency, "ck_cp_listings_payment_frequency"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('performing', 'repaid_in_full', 'repaid_after_extension', 'in_workout')",
            name="ck_cp_listings_outcome",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    ref: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default=E.ListingSourceKind.MANUAL)
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL"), index=True
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="SET NULL"), index=True
    )
    vertical: Mapped[str] = mapped_column(String(16), nullable=False)
    program_label: Mapped[str] = mapped_column(String(80), nullable=False, default="Bridge")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(2))
    collateral_type: Mapped[str | None] = mapped_column(String(80))
    lien_position: Mapped[str] = mapped_column(String(8), nullable=False, default=E.LienPosition.FIRST)
    note_rate_pct: Mapped[float | None] = mapped_column(Numeric(7, 3))
    term_months: Mapped[int | None] = mapped_column(Integer)
    ltv_pct: Mapped[float | None] = mapped_column(Numeric(7, 3))
    ltv_kind: Mapped[str] = mapped_column(String(8), nullable=False, default=E.LtvKind.LTV)
    loan_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    offered_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    minimum_investment: Mapped[float | None] = mapped_column(Numeric(14, 2))
    payment_frequency: Mapped[str] = mapped_column(String(16), nullable=False, default=E.PaymentFrequency.MONTHLY)
    personal_guarantee: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    guarantee_text: Mapped[str | None] = mapped_column(String(200))
    borrower_track_record: Mapped[str | None] = mapped_column(String(200))
    exit_plan: Mapped[str | None] = mapped_column(Text)
    valuation_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    valuation_source: Mapped[str | None] = mapped_column(String(120))
    title_status: Mapped[str | None] = mapped_column(String(120))
    servicer_label: Mapped[str | None] = mapped_column(String(120))
    maturity_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=E.ListingStatus.DRAFT, index=True)
    outcome: Mapped[str | None] = mapped_column(String(32))
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The first binding subscription. Starts the Form D clock.
    first_sale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    repaid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by_id: Mapped[uuid.UUID | None] = _user_fk()
    publish_reason: Mapped[str | None] = mapped_column(Text)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)
    #: What the loan, profile and term sheet said when the listing was cut.
    source_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_id: Mapped[uuid.UUID | None] = _user_fk()

    documents: Mapped[list[CpListingDocument]] = relationship(
        "CpListingDocument", back_populates="listing", cascade="all, delete-orphan", order_by="CpListingDocument.created_at"
    )


class CpListingDocument(TimestampMixin, Base):
    """An offering document (PPM, subscription agreement, valuation...). Served
    to approved members only, by short-lived presigned link, every access logged."""

    __tablename__ = "cp_listing_documents"
    __table_args__ = (
        _in("kind", E.DocumentKind, "ck_cp_listing_documents_kind"),
        _in("status", E.DocumentStatus, "ck_cp_listing_documents_status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    listing_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(600), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=E.DocumentStatus.PENDING)
    uploaded_by_id: Mapped[uuid.UUID | None] = _user_fk()
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    listing: Mapped[CpListing] = relationship("CpListing", back_populates="documents")


class CpListingEvent(Base):
    """Audit trail per listing: published, status changed, document added,
    subscription acted on."""

    __tablename__ = "cp_listing_events"

    id: Mapped[uuid.UUID] = _pk()
    listing_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = _user_fk()
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CpSubscription(TimestampMixin, Base):
    """A member's allocation in a listing. ``signed`` is the binding moment."""

    __tablename__ = "cp_subscriptions"
    __table_args__ = (
        _in("status", E.SubscriptionStatus, "ck_cp_subscriptions_status"),
        CheckConstraint("amount > 0", name="ck_cp_subscriptions_amount_positive"),
        Index("ix_cp_subscriptions_listing_member", "listing_id", "member_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    listing_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_member_entities.id", ondelete="SET NULL")
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=E.SubscriptionStatus.REQUESTED, index=True)
    ack_ppm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ack_accredited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    documents_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    funded_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    admin_notes: Mapped[str | None] = mapped_column(Text)
    acted_by_id: Mapped[uuid.UUID | None] = _user_fk()


class CpParticipation(TimestampMixin, Base):
    """A funded subscription as the member holds it, with the servicer's
    reporting entered by the desk."""

    __tablename__ = "cp_participations"
    __table_args__ = (_in("servicer_status", E.ServicerStatus, "ck_cp_participations_servicer_status"),)

    id: Mapped[uuid.UUID] = _pk()
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_subscriptions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_member_entities.id", ondelete="SET NULL")
    )
    invested_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    principal_outstanding: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    interest_received_ytd: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    interest_received_total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    next_payment_on: Mapped[date | None] = mapped_column(Date)
    servicer_status: Mapped[str] = mapped_column(String(24), nullable=False, default=E.ServicerStatus.CURRENT)
    status_as_of: Mapped[date | None] = mapped_column(Date)
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by_id: Mapped[uuid.UUID | None] = _user_fk()


class CpTransfer(TimestampMixin, Base):
    """Secondary: a participation listed for transfer to another approved
    member. Completes only after the issuer's consent."""

    __tablename__ = "cp_transfers"
    __table_args__ = (
        _in("status", E.TransferStatus, "ck_cp_transfers_status"),
        CheckConstraint("principal_amount > 0", name="ck_cp_transfers_principal_positive"),
    )

    id: Mapped[uuid.UUID] = _pk()
    ref: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    participation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_participations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seller_member_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    principal_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    ask_pct: Mapped[float] = mapped_column(Numeric(7, 3), nullable=False, default=100)
    listed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=E.TransferStatus.LISTED, index=True)
    buyer_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_members.id", ondelete="SET NULL")
    )
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consented_by_id: Mapped[uuid.UUID | None] = _user_fk()
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class CpFiling(TimestampMixin, Base):
    """The compliance tracker: Form D, annual amendments, state notices,
    bad-actor checks. Rows are created by the first binding sale and edited
    by the desk; counsel confirms deadlines before filing."""

    __tablename__ = "cp_filings"
    __table_args__ = (
        _in("kind", E.FilingKind, "ck_cp_filings_kind"),
        _in("status", E.FilingStatus, "ck_cp_filings_status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_listings.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[str | None] = mapped_column(String(2))
    subject: Mapped[str | None] = mapped_column(String(200))
    subject_role: Mapped[str | None] = mapped_column(String(120))
    first_sale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[date | None] = mapped_column(Date)
    filed_at: Mapped[date | None] = mapped_column(Date)
    checked_at: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=E.FilingStatus.DUE, index=True)
    fee_amount: Mapped[float | None] = mapped_column(Numeric(10, 2))
    reference: Mapped[str | None] = mapped_column(String(120))
    sources: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = _user_fk()


class CpDocumentAccess(Base):
    """Every presigned link handed to a member, with who, what, when, from where."""

    __tablename__ = "cp_document_access"

    id: Mapped[uuid.UUID] = _pk()
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_members.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = _user_fk()
    listing_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cp_listing_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
