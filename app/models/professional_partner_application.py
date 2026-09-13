from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ProfessionalPartnerApplication(TimestampMixin, Base):
    __tablename__ = "professional_partner_applications"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    firm_type: Mapped[str] = mapped_column(String(64), nullable=False)
    specialties: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    geographic_states: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    estimated_annual_referrals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    website: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_name: Mapped[str] = mapped_column(String(180), nullable=False)
    contact_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    contact_phone: Mapped[str] = mapped_column(String(48), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending", index=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_company_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("referral_partner_companies.id", ondelete="SET NULL"), nullable=True)
    promoted_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
