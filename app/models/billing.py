from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ClientPaymentMethod(TimestampMixin, Base):
    __tablename__ = "client_payment_methods"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stripe_customer_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stripe_payment_method_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    setup_intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    brand: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    exp_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exp_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billing_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    billing_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    billing_phone: Mapped[str | None] = mapped_column(String(48), nullable=True)
    billing_line1: Mapped[str | None] = mapped_column(String(240), nullable=True)
    billing_line2: Mapped[str | None] = mapped_column(String(240), nullable=True)
    billing_city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    billing_state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    billing_postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    billing_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    verification_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class PaymentAuthorization(TimestampMixin, Base):
    __tablename__ = "payment_authorizations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payment_method_row_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("client_payment_methods.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started", server_default="started", index=True)
    document_version: Mapped[str] = mapped_column(String(32), nullable=False)
    document_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    typed_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    esign_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    payment_terms_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    signature_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signature_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    certificate_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    certificate_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    stripe_payment_method_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    setup_intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    setup_intent_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billing_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    billing_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    billing_phone: Mapped[str | None] = mapped_column(String(48), nullable=True)
    billing_line1: Mapped[str | None] = mapped_column(String(240), nullable=True)
    billing_line2: Mapped[str | None] = mapped_column(String(240), nullable=True)
    billing_city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    billing_state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    billing_postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    billing_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    device_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_payment_authorizations_client_status", "client_id", "status"),
        Index("ix_payment_authorizations_user_status", "user_id", "status"),
    )


class ESignEvent(Base):
    __tablename__ = "esign_events"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("payment_authorizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BillableExpense(TimestampMixin, Base):
    __tablename__ = "billable_expenses"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bucket_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_approval", server_default="pending_approval", index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="other", server_default="other", index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd", server_default="usd")
    vendor_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_billable_expenses_client_status", "client_id", "status"),
        Index("ix_billable_expenses_client_created", "client_id", "created_at"),
    )


class ChargeAttempt(TimestampMixin, Base):
    __tablename__ = "charge_attempts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    expense_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("billable_expenses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payment_method_row_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("client_payment_methods.id", ondelete="SET NULL"), nullable=True
    )
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd", server_default="usd")
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_action_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
