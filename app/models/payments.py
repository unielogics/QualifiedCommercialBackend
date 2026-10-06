"""Audited ACH fee collection and private-funding payment ledger.

These tables are deliberately separate from ``billing.py`` (Stripe-backed file
expenses) and from evidence-only Plaid Items.  Money movement is always tied to
an application profile and every mutable workflow carries a record version.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _user_ref(*, nullable: bool = True) -> Mapped[uuid.UUID | None]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=nullable,
    )


class FeeObligation(TimestampMixin, Base):
    """Immutable economic snapshot plus a mutable collection lifecycle."""

    __tablename__ = "fee_obligations"
    __table_args__ = (
        UniqueConstraint("application_profile_id", "version", name="uq_fee_obligations_profile_version"),
        Index(
            "uq_fee_obligations_current",
            "application_profile_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL AND status != 'cancelled'"),
        ),
        Index("ix_fee_obligations_status", "status"),
        CheckConstraint("version > 0 AND record_version > 0", name="ck_fee_obligations_versions"),
        CheckConstraint("gross_fee_cents >= 0", name="ck_fee_obligations_gross_nonnegative"),
        CheckConstraint(
            "client_ach_cents >= 0 AND bank_direct_cents >= 0 AND external_cents >= 0 "
            "AND deferred_cents >= 0 AND waived_cents >= 0",
            name="ck_fee_obligations_allocations_nonnegative",
        ),
        CheckConstraint(
            "origination_client_ach_cents >= 0 AND consulting_client_ach_cents >= 0",
            name="ck_fee_obligations_component_ach_nonnegative",
        ),
        CheckConstraint(
            "origination_client_ach_cents + consulting_client_ach_cents = client_ach_cents",
            name="ck_fee_obligations_component_ach_balanced",
        ),
        CheckConstraint(
            "origination_client_ach_cents <= origination_fee_cents "
            "AND consulting_client_ach_cents <= consulting_fee_cents",
            name="ck_fee_obligations_component_ach_within_fee",
        ),
        CheckConstraint(
            "client_ach_cents + bank_direct_cents + external_cents + deferred_cents + waived_cents = gross_fee_cents",
            name="ck_fee_obligations_allocation_balanced",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), index=True
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL"), index=True
    )
    intake_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL"), index=True
    )
    production_package_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_packages.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    record_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared", server_default="prepared")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd", server_default="usd")
    accepted_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    funded_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    origination_points: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    origination_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    consulting_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    gross_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_ach_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    origination_client_ach_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    consulting_client_ach_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    bank_direct_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    external_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    deferred_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    waived_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    business_name_snapshot: Mapped[str | None] = mapped_column(String(240))
    client_name_snapshot: Mapped[str | None] = mapped_column(String(180))
    client_email_snapshot: Mapped[str | None] = mapped_column(String(320))
    economics_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    agreement_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="RESTRICT")
    )
    agreement_reference: Mapped[str | None] = mapped_column(String(240))
    agreement_sha256: Mapped[str | None] = mapped_column(String(64))
    agreement_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    consulting_milestone_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consulting_milestone_confirmed_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    created_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    authorization_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_user_id: Mapped[uuid.UUID | None] = _user_ref()


class FeeObligationLine(TimestampMixin, Base):
    __tablename__ = "fee_obligation_lines"
    __table_args__ = (
        UniqueConstraint("obligation_id", "line_type", name="uq_fee_obligation_lines_type"),
        CheckConstraint("amount_cents >= 0", name="ck_fee_obligation_lines_amount"),
        CheckConstraint(
            "client_ach_cents >= 0 AND client_ach_cents <= amount_cents",
            name="ck_fee_obligation_lines_client_ach",
        ),
        CheckConstraint(
            "(governing_agreement_document_id IS NULL "
            "AND governing_agreement_sha256 IS NULL "
            "AND agreement_component_scope IS NULL) OR "
            "(governing_agreement_document_id IS NOT NULL "
            "AND governing_agreement_sha256 IS NOT NULL "
            "AND agreement_component_scope IS NOT NULL)",
            name="ck_fee_obligation_lines_governing_agreement_complete",
        ),
        CheckConstraint(
            "agreement_component_scope IS NULL OR "
            "(agreement_component_scope IN ('origination', 'consulting') "
            "AND agreement_component_scope = line_type)",
            name="ck_fee_obligation_lines_agreement_scope",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("fee_obligations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_ach_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    calculation_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    agreement_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    governing_agreement_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bucket_files.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    governing_agreement_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agreement_component_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    earning_milestone: Mapped[str | None] = mapped_column(Text, nullable=True)
    earned_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    earned_confirmed_by_user_id: Mapped[uuid.UUID | None] = _user_ref()


class FeeAllocationVersion(TimestampMixin, Base):
    __tablename__ = "fee_allocation_versions"
    __table_args__ = (
        UniqueConstraint("application_profile_id", "version", name="uq_fee_allocation_versions_profile_version"),
        CheckConstraint("version > 0", name="ck_fee_allocation_versions_version"),
        CheckConstraint(
            "origination_client_ach_cents >= 0 AND consulting_client_ach_cents >= 0 "
            "AND origination_client_ach_cents + consulting_client_ach_cents <= gross_fee_cents",
            name="ck_fee_allocation_versions_component_ach",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    obligation_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("fee_obligations.id", ondelete="SET NULL"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    collection_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    gross_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origination_client_ach_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    consulting_client_ach_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    allocation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    allocation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = _user_ref()


class ActualFundingConfirmation(TimestampMixin, Base):
    __tablename__ = "actual_funding_confirmations"
    __table_args__ = (
        UniqueConstraint("application_profile_id", "version", name="uq_actual_funding_profile_version"),
        Index(
            "uq_actual_funding_current",
            "application_profile_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        CheckConstraint("actual_funded_amount > 0", name="ck_actual_funding_amount"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    production_package_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_packages.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_funding_date: Mapped[date] = mapped_column(Date, nullable=False)
    actual_funded_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    funding_party_name: Mapped[str] = mapped_column(String(180), nullable=False)
    funding_reference: Mapped[str | None] = mapped_column(String(160))
    note: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", server_default="manual")
    confirmed_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_user_id: Mapped[uuid.UUID | None] = _user_ref()


class PaymentFundingSource(TimestampMixin, Base):
    """Plaid Transfer funding source; never an evidence Plaid Item."""

    __tablename__ = "payment_funding_sources"
    __table_args__ = (
        Index(
            "uq_payment_funding_sources_current_account",
            "application_profile_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND status = 'verified'"),
        ),
        Index("ix_payment_funding_sources_profile_status", "application_profile_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ach_class: Mapped[str] = mapped_column(String(8), nullable=False)
    plaid_item_id: Mapped[str | None] = mapped_column(String(128), index=True)
    plaid_account_id: Mapped[str | None] = mapped_column(String(128))
    access_token_ciphertext: Mapped[str | None] = mapped_column(Text)
    account_name: Mapped[str | None] = mapped_column(String(180))
    account_mask: Mapped[str | None] = mapped_column(String(8))
    account_subtype: Mapped[str | None] = mapped_column(String(48))
    institution_name: Mapped[str | None] = mapped_column(String(180))
    holder_name: Mapped[str | None] = mapped_column(String(180))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class AchMandate(TimestampMixin, Base):
    __tablename__ = "ach_mandates"
    __table_args__ = (
        CheckConstraint(
            "(fee_obligation_id IS NOT NULL)::int + (private_plan_id IS NOT NULL)::int = 1",
            name="ck_ach_mandates_one_target",
        ),
        CheckConstraint("authorized_amount_cents > 0", name="ck_ach_mandates_amount"),
        CheckConstraint(
            "notice_business_days IS NULL OR notice_business_days >= 0",
            name="ck_ach_mandates_notice_days",
        ),
        CheckConstraint(
            "debit_window_start_at IS NULL OR debit_window_end_at IS NULL "
            "OR debit_window_end_at >= debit_window_start_at",
            name="ck_ach_mandates_debit_window",
        ),
        CheckConstraint(
            "(agreement_document_id IS NULL AND agreement_sha256 IS NULL) OR "
            "(agreement_document_id IS NOT NULL AND agreement_sha256 IS NOT NULL)",
            name="ck_ach_mandates_agreement_complete",
        ),
        Index("ix_ach_mandates_status", "status"),
        Index("ix_ach_mandates_retention_until", "retention_until"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    funding_source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_funding_sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    fee_obligation_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("fee_obligations.id", ondelete="RESTRICT"), index=True
    )
    private_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "private_funding_payment_plans.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_ach_mandates_private_plan",
        ),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    ach_class: Mapped[str] = mapped_column(String(8), nullable=False)
    authorized_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authorization_text_version: Mapped[str] = mapped_column(String(32), nullable=False)
    authorization_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    authorization_text_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    obligation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    agreement_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bucket_files.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    agreement_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    funding_source_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    funding_source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    typed_name: Mapped[str] = mapped_column(String(180), nullable=False)
    payer_name: Mapped[str] = mapped_column(String(180), nullable=False)
    payer_email: Mapped[str | None] = mapped_column(String(320))
    signature_sha256: Mapped[str | None] = mapped_column(String(64))
    certificate_s3_key: Mapped[str | None] = mapped_column(String(512))
    certificate_sha256: Mapped[str | None] = mapped_column(String(64))
    certificate_bucket_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bucket_files.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    scheduled_debit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    debit_window_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    debit_window_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notice_business_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revocation_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    revocation_cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signer_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    proof_copy_delivery_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proof_copy_message_send_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("message_sends.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    proof_copy_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proof_copy_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proof_copy_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentDebitNotice(TimestampMixin, Base):
    """Exact, durable advance notice for one ACH debit.

    A notice is prepared before the mandate is signed, so ``mandate_id`` is
    deliberately nullable.  The immutable snapshot and digest are the record
    of what the payer was told; delivery lifecycle is tracked separately from
    the business lifecycle in ``status``.
    """

    __tablename__ = "payment_debit_notices"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payment_debit_notices_idempotency"),
        Index("ix_payment_debit_notices_digest", "notice_sha256"),
        Index("ix_payment_debit_notices_due", "status", "scheduled_debit_at"),
        Index("ix_payment_debit_notices_delivery", "delivery_status", "created_at"),
        Index("ix_payment_debit_notices_created_by_user_id", "created_by_user_id"),
        Index("ix_payment_debit_notices_superseded_by_user_id", "superseded_by_user_id"),
        Index("ix_payment_debit_notices_revoked_by_user_id", "revoked_by_user_id"),
        Index(
            "uq_payment_debit_notices_current_obligation",
            "fee_obligation_id",
            unique=True,
            postgresql_where=text(
                "superseded_at IS NULL AND revoked_at IS NULL "
                "AND status NOT IN ('cancelled', 'consumed')"
            ),
        ),
        CheckConstraint("amount_cents > 0", name="ck_payment_debit_notices_amount"),
        CheckConstraint(
            "notice_business_days IS NULL OR notice_business_days >= 0",
            name="ck_payment_debit_notices_notice_days",
        ),
        CheckConstraint(
            "debit_window_start_at IS NULL OR debit_window_end_at IS NULL "
            "OR debit_window_end_at >= debit_window_start_at",
            name="ck_payment_debit_notices_window",
        ),
        CheckConstraint(
            "notice_type != 'one_time_fee' OR "
            "(notice_business_days IS NOT NULL "
            "AND debit_window_start_at IS NOT NULL "
            "AND debit_window_end_at IS NOT NULL)",
            name="ck_payment_debit_notices_one_time_fee_window_required",
        ),
        CheckConstraint(
            "notice_type != 'one_time_fee' OR "
            "(revocation_cutoff_at <= scheduled_debit_at "
            "AND debit_window_start_at <= scheduled_debit_at "
            "AND scheduled_debit_at <= debit_window_end_at)",
            name="ck_payment_debit_notices_one_time_fee_timing",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    fee_obligation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fee_obligations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ach_mandates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_transfers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    installment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_installments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_send_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("message_sends.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    notice_bucket_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("bucket_files.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    delivery_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notice_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="one_time_fee", server_default="one_time_fee"
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="usd", server_default="usd"
    )
    recipient_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    account_mask: Mapped[str | None] = mapped_column(String(8), nullable=True)
    scheduled_debit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    debit_window_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    debit_window_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notice_business_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revocation_cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorization_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notice_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    notice_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(
        String(320), nullable=True, index=True
    )
    rfc_message_id: Mapped[str | None] = mapped_column(
        String(320), nullable=True, index=True
    )
    delivery_evidence_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[uuid.UUID | None] = _user_ref()


class PaymentServicingAuthority(TimestampMixin, Base):
    __tablename__ = "payment_servicing_authorities"
    __table_args__ = (Index("ix_payment_servicing_authorities_profile_active", "application_profile_id", "status"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    agreement_reference: Mapped[str] = mapped_column(String(240), nullable=False)
    agreement_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    creditor_name: Mapped[str] = mapped_column(String(180), nullable=False)
    payee_name: Mapped[str] = mapped_column(String(180), nullable=False)
    settlement_destination_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by_user_id: Mapped[uuid.UUID | None] = _user_ref()


class PrivateFundingPaymentPlan(TimestampMixin, Base):
    __tablename__ = "private_funding_payment_plans"
    __table_args__ = (
        UniqueConstraint("application_profile_id", "version", name="uq_private_payment_plans_profile_version"),
        Index(
            "uq_private_payment_plans_active_profile",
            "application_profile_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_private_payment_plans_status_due", "status", "next_due_date"),
        CheckConstraint("version > 0 AND record_version > 0", name="ck_private_payment_plans_versions"),
        CheckConstraint("total_amount_cents > 0 AND installment_count > 0", name="ck_private_payment_plans_amount_count"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), index=True
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL"), index=True
    )
    production_term_sheet_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_term_sheets.id", ondelete="RESTRICT"), nullable=False
    )
    production_term_sheet_version: Mapped[int] = mapped_column(Integer, nullable=False)
    production_package_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_packages.id", ondelete="RESTRICT"), nullable=False
    )
    production_package_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("production_package_revisions.id", ondelete="RESTRICT")
    )
    agreement_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    agreement_executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    funding_party_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    creditor_name: Mapped[str] = mapped_column(String(180), nullable=False)
    payee_name: Mapped[str | None] = mapped_column(String(180))
    settlement_destination_ref: Mapped[str | None] = mapped_column(String(240))
    agreement_reference: Mapped[str] = mapped_column(String(240), nullable=False)
    funding_confirmation_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("actual_funding_confirmations.id", ondelete="RESTRICT")
    )
    servicing_authority_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("payment_servicing_authorities.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    record_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    cadence: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York", server_default="America/New_York")
    total_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    installment_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    next_due_date: Mapped[date | None] = mapped_column(Date, index=True)
    schedule_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    schedule_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("private_funding_payment_plans.id", ondelete="RESTRICT")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentInstallment(TimestampMixin, Base):
    __tablename__ = "payment_installments"
    __table_args__ = (
        UniqueConstraint("plan_id", "sequence", name="uq_payment_installments_plan_sequence"),
        CheckConstraint("sequence > 0 AND amount_cents > 0", name="ck_payment_installments_sequence_amount"),
        Index("ix_payment_installments_status_due", "status", "due_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("private_funding_payment_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="scheduled", server_default="scheduled")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentTransfer(TimestampMixin, Base):
    __tablename__ = "payment_transfers"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payment_transfers_idempotency"),
        UniqueConstraint("plaid_transfer_id", name="uq_payment_transfers_plaid_id"),
        UniqueConstraint(
            "attempt_group_id",
            "attempt_no",
            name="uq_payment_transfers_attempt_group_no",
        ),
        CheckConstraint(
            "(fee_obligation_id IS NOT NULL)::int + (installment_id IS NOT NULL)::int = 1",
            name="ck_payment_transfers_one_target",
        ),
        CheckConstraint("amount_cents > 0", name="ck_payment_transfers_amount"),
        Index("ix_payment_transfers_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    fee_obligation_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("fee_obligations.id", ondelete="RESTRICT"), index=True
    )
    installment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("payment_installments.id", ondelete="RESTRICT"), index=True
    )
    funding_source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("payment_funding_sources.id", ondelete="RESTRICT"), nullable=False
    )
    mandate_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("ach_mandates.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_group_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4, index=True
    )
    retry_of_transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_transfers.id", ondelete="RESTRICT"),
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="authorizing", server_default="authorizing")
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ach_class: Mapped[str] = mapped_column(String(8), nullable=False)
    plaid_authorization_id: Mapped[str | None] = mapped_column(String(128), index=True)
    plaid_transfer_id: Mapped[str | None] = mapped_column(String(128))
    provider_status: Mapped[str | None] = mapped_column(String(48))
    provider_failure_code: Mapped[str | None] = mapped_column(String(64))
    provider_failure_message: Mapped[str | None] = mapped_column(Text)
    provider_failure_retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    funds_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class PaymentTransferEvent(Base):
    __tablename__ = "payment_transfer_events"
    __table_args__ = (
        UniqueConstraint("plaid_event_id", name="uq_payment_transfer_events_plaid_event"),
        Index("ix_payment_transfer_events_transfer_created", "transfer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("payment_transfers.id", ondelete="SET NULL"), index=True
    )
    plaid_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plaid_transfer_id: Mapped[str | None] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_event: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaymentRefund(TimestampMixin, Base):
    __tablename__ = "payment_refunds"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payment_refunds_idempotency"),
        UniqueConstraint(
            "transfer_id",
            "operation_fingerprint",
            name="uq_payment_refunds_transfer_operation",
        ),
        UniqueConstraint("plaid_refund_id", name="uq_payment_refunds_plaid_id"),
        CheckConstraint("amount_cents > 0", name="ck_payment_refunds_amount"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    transfer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("payment_transfers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_idempotency_key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    plaid_refund_id: Mapped[str | None] = mapped_column(String(128))
    requested_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BankDirectFeeReceipt(TimestampMixin, Base):
    __tablename__ = "bank_direct_fee_receipts"
    __table_args__ = (
        UniqueConstraint("obligation_id", "reference", name="uq_bank_direct_receipts_reference"),
        CheckConstraint("amount_cents > 0", name="ck_bank_direct_receipts_amount"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("fee_obligations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    receipt_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default="bank_direct", server_default="bank_direct"
    )
    received_on: Mapped[date] = mapped_column(Date, nullable=False)
    reference: Mapped[str] = mapped_column(String(180), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    recorded_by_user_id: Mapped[uuid.UUID | None] = _user_ref()


class PlaidTransferCursor(TimestampMixin, Base):
    __tablename__ = "plaid_transfer_cursors"

    environment: Mapped[str] = mapped_column(String(24), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class PaymentAuditEvent(Base):
    __tablename__ = "payment_audit_events"
    __table_args__ = (Index("ix_payment_audit_profile_created", "application_profile_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = _user_ref()
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    summary: Mapped[str] = mapped_column(String(320), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
