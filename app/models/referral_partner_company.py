from __future__ import annotations

import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

KIND_REFERRAL_PARTNER = "referral_partner"
KIND_HOUSE = "house"
# The one row of kind "house". It is what internal staff are linked to, and it
# is the same string the arrangement prints as the manager's employer
# (production_arrangement.DEFAULTS["rm_employer"]); a test pins the two together.
HOUSE_COMPANY_NAME = "Qualified Commercial LLC"


class ReferralPartnerCompany(TimestampMixin, Base):
    """A business relationship profile.

    Every agent and every employee is linked to one (`users.referral_partner_company_id`).
    Two kinds:

    * `referral_partner` — a dealer-partner referral company (e.g. a car
      dealership group) whose owners/officers/employees sign the Platform Access
      Agreement and get Role.DEALER_PARTNER accounts. Before it can be a sponsor
      on a Production Package, or its people have standing on the platform, the
      COMPANY itself must hold a signed Strategic Referral, Capital Advisory and
      Business Relationship Protection Agreement — checked via a ContractAgreement
      row with subject_type=COMPANY, subject_id=this row's id,
      contract_type=REFERRAL_PROTECTION (no status column here; signed-or-not is
      a query against that table, same as everywhere else status is derived
      rather than duplicated). Linking a person to it may precede the signature.
    * `house` — Qualified Commercial itself, exactly one row (0198). Internal
      staff are linked to it. It never signs an agreement and is never a sponsor;
      when the sponsor defaults from the agent on a file, the house is transparent.

    Created when an admin invites a user and types a new company name
    (find-or-create), or when the company itself signs the Referral Protection
    Agreement directly via the public agreement portal
    (agreement.qualifiedcommercial.com)."""

    __tablename__ = "referral_partner_companies"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_REFERRAL_PARTNER, server_default=KIND_REFERRAL_PARTNER)
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
