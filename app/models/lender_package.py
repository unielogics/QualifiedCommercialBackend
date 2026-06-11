from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class LenderUser(TimestampMixin, Base):
    """Authenticated portal user access for a lender roster row."""

    __tablename__ = "lender_users"
    __table_args__ = (
        UniqueConstraint("user_id", "lender_id", name="uq_lender_users_user_lender"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lender_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class LenderPackage(TimestampMixin, Base):
    __tablename__ = "lender_packages"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", server_default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class LenderPackageDocument(TimestampMixin, Base):
    __tablename__ = "lender_package_documents"
    __table_args__ = (
        UniqueConstraint("package_id", "document_id", name="uq_lender_package_document"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    package_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lender_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class LenderPackageRecipient(TimestampMixin, Base):
    __tablename__ = "lender_package_recipients"
    __table_args__ = (
        UniqueConstraint("package_id", "lender_id", "email", name="uq_lender_package_recipient"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    package_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lender_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lender_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="sent", server_default="sent")
    invited_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    email_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("email_drafts.id", ondelete="SET NULL"), nullable=True
    )
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terms_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    no_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LenderPackageEvent(Base):
    __tablename__ = "lender_package_events"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    package_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lender_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lender_package_recipients.id", ondelete="CASCADE"), nullable=True, index=True
    )
    lender_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LenderTerm(TimestampMixin, Base):
    __tablename__ = "lender_terms"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lender_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lender_packages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    package_recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lender_package_recipients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    submitted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="manual", server_default="manual")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="received", server_default="received")

    requested_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    approved_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    base_rate: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    final_rate: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    discount_points: Mapped[float | None] = mapped_column(Numeric(5, 3), nullable=True)
    origination_pct: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    lender_fees: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amortization_style: Mapped[str | None] = mapped_column(String(24), nullable=True)
    interest_only: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    prepay_penalty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ltv: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    ltc: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    dscr: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reserves_required: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    estimated_close_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    conditions: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    missing_items: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    construction_holdback_pct: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    draw_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_strategy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    selected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
