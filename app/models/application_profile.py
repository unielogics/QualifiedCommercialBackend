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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ApplicationProfile(TimestampMixin, Base):
    """One explicit business-file lineage across intake and funding sources."""

    __tablename__ = "application_profiles"
    __table_args__ = (
        UniqueConstraint("deal_id", name="uq_application_profiles_deal"),
        UniqueConstraint("loan_id", name="uq_application_profiles_loan"),
        UniqueConstraint("intake_id", name="uq_application_profiles_intake"),
        UniqueConstraint("dealer_id", name="uq_application_profiles_dealer"),
        Index("ix_application_profiles_client", "client_id"),
        Index("ix_application_profiles_bucket", "primary_bucket_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL")
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL")
    )
    intake_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL"),
    )
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    primary_bucket_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="SET NULL")
    )

    vertical: Mapped[str] = mapped_column(
        String(32), nullable=False, default="main_street", server_default="main_street"
    )
    funding_category: Mapped[str | None] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(32))
    industry: Mapped[str | None] = mapped_column(String(80))
    subindustry: Mapped[str | None] = mapped_column(String(120))
    naics_code: Mapped[str | None] = mapped_column(String(8))
    naics_label: Mapped[str | None] = mapped_column(String(180))
    custom_industry: Mapped[str | None] = mapped_column(String(180))
    industry_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    subindustry_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    activity_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    taxonomy_version: Mapped[str] = mapped_column(
        String(24), nullable=False, default="2022", server_default="2022"
    )
    classification_provenance: Mapped[dict | None] = mapped_column(JSONB)
    classification_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    classification_state: Mapped[dict | None] = mapped_column(JSONB)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    backfill_needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_draft: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    draft_finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extraction_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bank_verification_override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bank_verification_override_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    bank_verification_override_reason: Mapped[str | None] = mapped_column(Text)


class FundingCategory(TimestampMixin, Base):
    __tablename__ = "application_funding_categories"
    __table_args__ = (
        UniqueConstraint("vertical", "slug", name="uq_application_funding_category_slug"),
        Index("ix_application_funding_categories_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vertical: Mapped[str] = mapped_column(String(32), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    requirements: Mapped[dict | None] = mapped_column(JSONB)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class ApplicationTaxonomyEntry(TimestampMixin, Base):
    __tablename__ = "application_taxonomy_entries"
    __table_args__ = (
        Index("ix_application_taxonomy_level_status", "level", "status"),
        Index("ix_application_taxonomy_parent", "parent_id"),
        Index("ix_application_taxonomy_normalized", "normalized_label"),
        Index(
            "uq_application_taxonomy_active_code",
            "taxonomy_version", "level", "code",
            unique=True,
            postgresql_where=text("code IS NOT NULL AND status IN ('official', 'approved')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str | None] = mapped_column(String(8))
    label: Mapped[str] = mapped_column(String(180), nullable=False)
    normalized_label: Mapped[str] = mapped_column(String(180), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="custom", server_default="custom")
    taxonomy_version: Mapped[str] = mapped_column(String(24), nullable=False, default="2022", server_default="2022")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    aliases: Mapped[list | None] = mapped_column(JSONB)
    originating_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canonical_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_taxonomy_entries.id", ondelete="SET NULL")
    )
    review_note: Mapped[str | None] = mapped_column(Text)


class ApplicationExtractedFact(TimestampMixin, Base):
    __tablename__ = "application_extracted_facts"
    __table_args__ = (
        Index("ix_application_extracted_facts_profile_status", "profile_id", "status"),
        Index("ix_application_extracted_facts_source", "source_file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL")
    )
    source_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_file_analyses.id", ondelete="SET NULL")
    )
    extraction_method: Mapped[str] = mapped_column(String(32), nullable=False, default="document_ai", server_default="document_ai")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="suggested", server_default="suggested")
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationVerificationInvitation(TimestampMixin, Base):
    __tablename__ = "application_verification_invitations"
    __table_args__ = (
        Index("ix_application_verification_invitations_profile", "profile_id"),
        Index("ix_application_verification_invitations_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    recipient_phone: Mapped[str | None] = mapped_column(String(48))
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="business_banking", server_default="business_banking")
    delivery_status: Mapped[str] = mapped_column(String(24), nullable=False, default="created", server_default="created")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationOwner(TimestampMixin, Base):
    """An owner on a non-Dealer application profile.

    Dealer-backed profiles continue to use dos_owners through the profile
    adapter so rep and Audit records remain canonical and unchanged.
    """

    __tablename__ = "application_owners"
    __table_args__ = (
        Index("ix_application_owners_profile", "profile_id"),
        Index("ix_application_owners_credit_pull", "credit_pull_id"),
        Index(
            "uq_application_owners_primary",
            "profile_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index(
            "uq_application_owners_email",
            "profile_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        CheckConstraint(
            "ownership_pct IS NULL OR (ownership_pct >= 0 AND ownership_pct <= 100)",
            name="ck_application_owners_percentage",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(48))
    ownership_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_guarantor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    dob: Mapped[date | None] = mapped_column(Date)
    street: Mapped[str | None] = mapped_column(String(240))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(8))
    zip: Mapped[str | None] = mapped_column(String(12))
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    invite_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invite_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credit_pull_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("credit_pulls.id", ondelete="SET NULL")
    )
    credit_score: Mapped[int | None] = mapped_column(Integer)
    credit_tier: Mapped[str | None] = mapped_column(String(16))
    credit_pulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credit_summary: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)
    backfill_needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

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

    @property
    def has_invite(self) -> bool:
        return self.invite_token_hash is not None


class ApplicationPlaidItem(TimestampMixin, Base):
    __tablename__ = "application_plaid_items"
    __table_args__ = (
        Index("ix_application_plaid_items_profile", "profile_id"),
        Index(
            "uq_application_plaid_items_primary",
            "profile_id",
            unique=True,
            postgresql_where=text("is_primary_operating AND status <> 'removed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    institution_name: Mapped[str | None] = mapped_column(String(160))
    accounts_label: Mapped[str | None] = mapped_column(String(200))
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encryption_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fernet", server_default="fernet"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    error: Mapped[str | None] = mapped_column(Text)
    last_pulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_refresh: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_primary_operating: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class ApplicationBankConsent(TimestampMixin, Base):
    __tablename__ = "application_bank_consents"
    __table_args__ = (Index("ix_application_bank_consents_profile", "profile_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    method: Mapped[str] = mapped_column(String(24), nullable=False)
    disclosure_version: Mapped[str] = mapped_column(String(24), nullable=False)
    disclosure_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    disclosure_text: Mapped[str] = mapped_column(Text, nullable=False)
    consenter_name: Mapped[str | None] = mapped_column(String(160))
    captured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
