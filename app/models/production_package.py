"""Production Package — the Production Arrangement filled inside a car-industry
AI intake file, presented to the client as a PDF, and sent for signature.

Anchored on ApplicationProfile (which may have no DealerBusiness), so the
tables live on the platform side. Breadcrumbs across the Dealer OS boundary
(intake_id, dealer_id) are plain UUIDs, the way DealerBusiness.handoff_intake_id
is, never foreign keys.

Lifecycle: draft -> out_for_signature -> executed | void. "Request signature"
freezes the arrangement into an append-only revision whose rendered PDF is the
exact document the client signs; edits after send go through reopen, which
voids the revision's signatures and returns the package to draft. Executed
packages are immutable.
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

PACKAGE_STATUSES: tuple[str, ...] = ("draft", "out_for_signature", "executed", "void")
REVISION_STATUSES: tuple[str, ...] = ("out_for_signature", "executed", "void")
SIGNATURE_PARTIES: tuple[str, ...] = ("dealer", "qc", "sponsor")
SIGNATURE_METHODS: tuple[str, ...] = ("electronic", "manual", "stored")
TERM_SHEET_STATUSES: tuple[str, ...] = ("current", "superseded", "withdrawn")
SIGNATURE_STATUSES: tuple[str, ...] = ("pending", "signed", "voided")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _user_ref() -> Mapped[uuid.UUID | None]:
    return mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))


class ProductionPackage(TimestampMixin, Base):
    __tablename__ = "production_packages"
    __table_args__ = (
        # Stage one keeps its shipped semantics (one row per profile, void is
        # terminal); only a voided FINAL may be redrafted.
        Index(
            "uq_production_packages_profile_stage_live", "profile_id", "stage", unique=True,
            postgresql_where=text("stage = 1 OR status <> 'void'"),
        ),
        # The intake room's gate query stays single-row by construction.
        Index(
            "uq_production_packages_profile_out", "profile_id", unique=True,
            postgresql_where=text("status = 'out_for_signature'"),
        ),
        CheckConstraint(
            "(stage = 1 AND parent_package_id IS NULL AND source_revision_id IS NULL AND term_sheet_id IS NULL) "
            "OR (stage = 2 AND parent_package_id IS NOT NULL AND source_revision_id IS NOT NULL AND term_sheet_id IS NOT NULL)",
            name="ck_production_packages_parent_stage",
        ),
        Index("ix_production_packages_intake", "intake_id"),
        Index("ix_production_packages_dealer", "dealer_id"),
        Index("ix_production_packages_status", "status"),
        Index("ix_production_packages_parent", "parent_package_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # RESTRICT: a profile with a sent or executed package is a retained record.
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    intake_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")

    # The editable form (see app.services.production_arrangement for the shape).
    arrangement: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # {field_key: {"source": ..., "label": ..., "confirmed": bool}}
    prefill_provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    # Last required-field snapshot: [{step, key, title, detail}]
    attention: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    computed_cache: Mapped[dict | None] = mapped_column(JSONB)

    # Sponsor = a ReferralPartnerCompany holding a signed Referral Protection Agreement.
    sponsor_company_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("referral_partner_companies.id", ondelete="SET NULL")
    )

    # Optimistic concurrency: every PATCH sends the version it read.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    presentation_s3_key: Mapped[str | None] = mapped_column(String(512))
    presentation_sha256: Mapped[str | None] = mapped_column(String(64))
    presentation_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Hash of the arrangement the PDF was built from; stale when it differs from the current one.
    presentation_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))

    frozen_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "production_package_revisions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_production_packages_frozen_revision",
        ),
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    executed_pdf_s3_key: Mapped[str | None] = mapped_column(String(512))
    executed_pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    void_reason: Mapped[str | None] = mapped_column(Text)
    # [{at, action, channel, recipient, status, detail, by}] — same shape as ContractEnvelope.delivery_history
    delivery_history: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    created_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    updated_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    updated_via: Mapped[str | None] = mapped_column(String(16))  # operator | share_link | partner
    updated_share_link_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    # ---- stage two (the final, Program Activation and Production Agreement) ----
    # The child package points at the executed stage-one package and the exact
    # revision it was drafted from; the parent is never written again.
    parent_package_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_packages.id", ondelete="RESTRICT")
    )
    source_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_package_revisions.id", ondelete="RESTRICT", use_alter=True,
                                          name="fk_production_packages_source_revision")
    )
    term_sheet_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_term_sheets.id", ondelete="RESTRICT", use_alter=True,
                                          name="fk_production_packages_term_sheet")
    )
    sent_via: Mapped[str | None] = mapped_column(String(16))  # operator | share_link | partner
    sent_share_link_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # Set when the dealer signed but the execution bundle could not be assembled; the desk retries.
    execution_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


class ProductionPackageRevision(TimestampMixin, Base):
    """Append-only: one row per send. The rendered PDF is the document signed."""

    __tablename__ = "production_package_revisions"
    __table_args__ = (
        UniqueConstraint("package_id", "revision_no", name="uq_production_package_revisions_no"),
        Index("ix_production_package_revisions_package", "package_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    package_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_packages.id", ondelete="CASCADE"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="out_for_signature", server_default="out_for_signature"
    )
    document_key: Mapped[str] = mapped_column(String(48), nullable=False)
    document_title: Mapped[str] = mapped_column(String(180), nullable=False)
    document_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # {"document_version", "arrangement", "computed", "sponsor", "parties"} — UUIDs/dates stringified
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # The agreement text as rendered, kept in the database so hash verification survives S3 outages.
    rendered_text: Mapped[str | None] = mapped_column(Text)
    rendered_pdf_s3_key: Mapped[str | None] = mapped_column(String(512))
    rendered_pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    # Progressively stamped copy (dealer signature, then manual records).
    current_pdf_s3_key: Mapped[str | None] = mapped_column(String(512))
    current_pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    # Reserved for stage two (funding activation certificate).
    funding: Mapped[dict | None] = mapped_column(JSONB)
    created_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    void_reason: Mapped[str | None] = mapped_column(Text)


class ProductionPackageSignature(TimestampMixin, Base):
    """One row per party per revision. The dealer's is electronic and starts
    pending at send (carrying sent/viewed); QC and sponsor are recorded
    manually by an operator and start signed. Rows are never edited: a wrong
    record is voided by reopen."""

    __tablename__ = "production_package_signatures"
    __table_args__ = (
        Index("ix_production_package_signatures_package", "package_id"),
        Index("ix_production_package_signatures_revision", "revision_id"),
        Index(
            "uq_production_package_signatures_live",
            "revision_id",
            "party",
            unique=True,
            postgresql_where=text("status IN ('pending', 'signed')"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    package_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_packages.id", ondelete="CASCADE"), nullable=False
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_package_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    party: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(Text)

    # Electronic (dealer)
    expected_signer_name: Mapped[str | None] = mapped_column(String(160))
    typed_name: Mapped[str | None] = mapped_column(String(160))
    signature_s3_key: Mapped[str | None] = mapped_column(String(512))
    signature_sha256: Mapped[str | None] = mapped_column(String(64))
    document_sha256: Mapped[str | None] = mapped_column(String(64))
    esign_consent_version: Mapped[str | None] = mapped_column(String(32))
    esign_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    esign_consent_ip: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signed_pdf_s3_key: Mapped[str | None] = mapped_column(String(512))
    signed_pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    certificate_sha256: Mapped[str | None] = mapped_column(String(64))

    # Manual record (qc, sponsor)
    signer_name: Mapped[str | None] = mapped_column(String(160))
    signer_title: Mapped[str | None] = mapped_column(String(120))
    signed_on: Mapped[date | None] = mapped_column(Date)
    scan_s3_key: Mapped[str | None] = mapped_column(String(512))
    scan_sha256: Mapped[str | None] = mapped_column(String(64))
    attestation_version: Mapped[str | None] = mapped_column(String(32))
    recorded_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_ip: Mapped[str | None] = mapped_column(String(64))
    recorded_user_agent: Mapped[str | None] = mapped_column(String(400))
    note: Mapped[str | None] = mapped_column(Text)

    # Typed initials (jury-trial waiver, Schedule C/3 acknowledgment) and, for
    # signatures placed from file (method="stored"), which stored signature.
    initials: Mapped[str | None] = mapped_column(String(8))
    stored_signature_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("stored_signatures.id", ondelete="RESTRICT", use_alter=True,
                                          name="fk_production_package_signatures_stored")
    )
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    placed_by_user_id: Mapped[uuid.UUID | None] = _user_ref()


class ProductionTermSheet(TimestampMixin, Base):
    """The loan terms, recorded by the desk before the final package can be
    drafted. Append-only versions; one `current` row per profile."""

    __tablename__ = "production_term_sheets"
    __table_args__ = (
        UniqueConstraint("profile_id", "version", name="uq_production_term_sheets_version"),
        Index("ix_production_term_sheets_profile", "profile_id"),
        Index("uq_production_term_sheets_current", "profile_id", unique=True, postgresql_where=text("status = 'current'")),
        CheckConstraint(
            "approved_amount > 0 AND min_activation_amount > 0 AND min_activation_amount <= approved_amount",
            name="ck_production_term_sheets_amounts",
        ),
        CheckConstraint("rate_pct >= 0 AND term_months > 0 AND monthly_debt_service > 0", name="ck_production_term_sheets_pricing"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="current", server_default="current")
    funding_party_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    lender_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="SET NULL"))
    funding_party_name: Mapped[str] = mapped_column(String(180), nullable=False)
    facility_type: Mapped[str] = mapped_column(String(48), nullable=False)
    approved_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    min_activation_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    rate_pct: Mapped[float] = mapped_column(Numeric(7, 3), nullable=False)
    term_months: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    monthly_debt_service: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    debt_service_is_level_payment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    expected_funding_date: Mapped[date | None] = mapped_column(Date)
    activation_date: Mapped[date | None] = mapped_column(Date)
    commencement_date: Mapped[date | None] = mapped_column(Date)
    maturity_date: Mapped[date | None] = mapped_column(Date)
    use_of_funds: Mapped[dict | None] = mapped_column(JSONB)
    conditions: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_term_sheets.id", ondelete="RESTRICT")
    )
    entered_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    entered_ip: Mapped[str | None] = mapped_column(String(64))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    withdraw_reason: Mapped[str | None] = mapped_column(Text)
    consumed_by_package_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_packages.id", ondelete="SET NULL", use_alter=True,
                                          name="fk_production_term_sheets_consumed_by")
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductionPackageShareLink(TimestampMixin, Base):
    """A package-scoped grant to one signed-in field rep. The raw token is
    returned once at mint and never stored; the link is useless without the
    named rep's own session."""

    __tablename__ = "production_package_share_links"
    __table_args__ = (
        Index("ix_production_package_share_links_package", "package_id"),
        Index(
            "uq_production_package_share_links_live",
            "package_id",
            "rep_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    package_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_packages.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    rep_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(120))
    # The rep was granted a file outside their own book; the operator confirmed it.
    outside_book: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
