from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class DealRegistration(TimestampMixin, Base):
    """One Deal Registration issued by Qualified Commercial under Article 4
    of a signed Referral Protection Agreement -- Exhibit 1's "Deal
    Registration and Introduction Confirmation" form, filled in by an admin
    each time QC actually introduces a specific financing opportunity to the
    referral partner. Distinct from, and issued well after, the master
    agreement itself: signing the Referral Protection Agreement creates no
    Deal Registration on its own.

    `registration_number` is generated from deal_registration_number_seq
    (mirrors contract_number_seq's pattern) and rendered into Exhibit 1 as
    two template placeholders (deal_registration_number_prefix = year,
    _suffix = zero-padded sequence) to match that exhibit's existing
    two-blank convention."""

    __tablename__ = "deal_registrations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    referral_partner_company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("referral_partner_companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    registration_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    introduced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_borrower: Mapped[str] = mapped_column(String(255), nullable=False)
    financing_opportunity: Mapped[str] = mapped_column(Text, nullable=False)
    introduced_capital_source: Mapped[str] = mapped_column(String(255), nullable=False)
    introduced_program: Mapped[str | None] = mapped_column(String(255), nullable=True)
    introduced_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    method_of_introduction: Mapped[str] = mapped_column(String(32), nullable=False)
    method_other_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    documents_transmitted: Mapped[str | None] = mapped_column(Text, nullable=True)
    coded_designation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capital_source_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    date_identity_disclosed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    certificate_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
