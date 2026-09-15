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
    LargeBinary,
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


class ApplicationTermSheet(TimestampMixin, Base):
    """An immutable, versioned client-facing offer for an application file."""

    __tablename__ = "application_term_sheets"
    __table_args__ = (
        UniqueConstraint("profile_id", "version", name="uq_application_term_sheet_version"),
        Index(
            "uq_application_term_sheet_current",
            "profile_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        CheckConstraint("amount > 0", name="ck_application_term_sheet_amount"),
        CheckConstraint("apr_pct BETWEEN 0 AND 100", name="ck_application_term_sheet_apr"),
        CheckConstraint("term_months BETWEEN 1 AND 480", name="ck_application_term_sheet_term"),
        CheckConstraint("expiration_days BETWEEN 1 AND 180", name="ck_application_term_sheet_expiration"),
        CheckConstraint("closing_estimate_days BETWEEN 0 AND 180", name="ck_application_term_sheet_close_estimate"),
        CheckConstraint("status IN ('draft', 'issued', 'superseded')", name="ck_application_term_sheet_status"),
        CheckConstraint("repayment_frequency IN ('daily', 'weekly', 'biweekly', 'monthly', 'custom')", name="ck_application_term_sheet_frequency"),
        CheckConstraint("debt_service_treatment IN ('additive', 'refinance')", name="ck_application_term_sheet_debt_treatment"),
        CheckConstraint("payments_per_year > 0", name="ck_application_term_sheet_payments_year"),
        CheckConstraint("periodic_payment > 0", name="ck_application_term_sheet_payment"),
        CheckConstraint("payment_count > 0", name="ck_application_term_sheet_payment_count"),
        CheckConstraint("annual_new_debt_service > 0", name="ck_application_term_sheet_annual_debt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    program_key: Mapped[str] = mapped_column(String(64), nullable=False)
    program_name: Mapped[str] = mapped_column(String(160), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    apr_pct: Mapped[float] = mapped_column(Numeric(7, 3), nullable=False)
    term_months: Mapped[int] = mapped_column(Integer, nullable=False)
    funder_type: Mapped[str] = mapped_column(String(32), nullable=False)
    funder_name: Mapped[str | None] = mapped_column(String(160))
    repayment_frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    payments_per_year: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    custom_repayment_label: Mapped[str | None] = mapped_column(String(80))
    debt_service_treatment: Mapped[str] = mapped_column(String(20), nullable=False)
    retained_annual_debt_service: Mapped[float | None] = mapped_column(Numeric(14, 2))
    periodic_payment: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    payment_count: Mapped[int] = mapped_column(Integer, nullable=False)
    annual_new_debt_service: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    projected_annual_debt_service: Mapped[float | None] = mapped_column(Numeric(14, 2))
    cash_flow_value: Mapped[float | None] = mapped_column(Numeric(14, 2))
    cash_flow_label: Mapped[str] = mapped_column(String(80), nullable=False)
    current_annual_debt_service: Mapped[float | None] = mapped_column(Numeric(14, 2))
    annual_property_carrying_costs: Mapped[float | None] = mapped_column(Numeric(14, 2))
    dscr_before: Mapped[float | None] = mapped_column(Numeric(8, 4))
    dscr_after: Mapped[float | None] = mapped_column(Numeric(8, 4))
    dscr_method: Mapped[str] = mapped_column(String(24), nullable=False)
    dscr_status: Mapped[str] = mapped_column(String(24), nullable=False)
    dscr_explanation: Mapped[str] = mapped_column(Text, nullable=False)
    dscr_source: Mapped[str] = mapped_column(String(200), nullable=False)
    expiration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    closing_estimate_days: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_on: Mapped[date | None] = mapped_column(Date)
    issued_pdf_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    issued_pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    issued_filename: Mapped[str | None] = mapped_column(String(240))
    co_brand_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sponsor_name: Mapped[str | None] = mapped_column(String(160))
    client_note: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class ApplicationTermSheetDelivery(TimestampMixin, Base):
    __tablename__ = "application_term_sheet_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('sending', 'sent', 'failed')", name="ck_application_term_sheet_delivery_status"),
        CheckConstraint("char_length(pdf_sha256) = 64", name="ck_application_term_sheet_delivery_sha"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    term_sheet_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_term_sheets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    to_emails: Mapped[list] = mapped_column(JSONB, nullable=False)
    cc_emails: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_detail: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(320))
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
