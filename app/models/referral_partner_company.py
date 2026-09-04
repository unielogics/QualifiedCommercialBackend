from __future__ import annotations

import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ReferralPartnerCompany(TimestampMixin, Base):
    """A dealer-partner referral company (e.g. a car dealership group) whose
    individual owners/officers/employees sign the Platform Access Agreement
    and get Role.DEALER_PARTNER accounts. The COMPANY itself, separately,
    must always have a signed Strategic Referral, Capital Advisory and
    Business Relationship Protection Agreement on file — checked via a
    ContractAgreement row with subject_type=COMPANY, subject_id=this row's
    id, contract_type=REFERRAL_PROTECTION (no separate status column here;
    signed-or-not is a query against that table, same as everywhere else in
    this codebase status is derived rather than duplicated).

    Created either when an admin invites a dealer-partner user and types a
    new company name (find-or-create), or when the company itself signs the
    Referral Protection Agreement directly via the public agreement portal
    (agreement.qualifiedcommercial.com)."""

    __tablename__ = "referral_partner_companies"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_of_formation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    principal_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Notice and signatory details. Backfilled in 0189 from the company's own
    # signed Strategic Referral agreement, which records all but the phone;
    # editable from the desk thereafter, because a company created blank by the
    # invite path could not otherwise ever be corrected.
    notice_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    notice_attention: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notice_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    platform_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signatory_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signatory_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
