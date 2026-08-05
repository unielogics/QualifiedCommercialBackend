from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import Role
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.broker import Broker
    from app.models.client import Client
    from app.models.referral_partner_company import ReferralPartnerCompany


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: an invited team member exists in our DB before they complete
    # Clerk sign-up. The row gets bound to a real clerk_id on first JIT
    # auto-provision (deps.get_current_user matches by email).
    clerk_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    role: Mapped[Role] = mapped_column(String(32), nullable=False, default=Role.CLIENT)
    # Soft-delete: super-admin revoke sets this rather than physically deleting,
    # so historical FK references (loans.broker_id → brokers.user_id, etc.) survive.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Presence (alembic 0046). Bumped automatically by deps.get_current_user
    # on every authed request. Drives the "online" dot in the loan header.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set for Role.DEALER_PARTNER users at invite time (typed company name,
    # find-or-create against ReferralPartnerCompany). Whether that COMPANY
    # has a signed Referral Protection Agreement on file is a separate,
    # company-scoped ContractAgreement query — this FK only records which
    # company a given individual belongs to. NULL for every other role.
    referral_partner_company_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("referral_partner_companies.id", ondelete="SET NULL"), nullable=True
    )

    # Optional links to broker/client profiles (one-to-one).
    # foreign_keys disambiguates Client.user_id from the post-0029
    # routing FKs (originating_agent_id / current_agent_id).
    broker: Mapped[Broker | None] = relationship(back_populates="user", uselist=False)
    client: Mapped[Client | None] = relationship(
        back_populates="user", uselist=False, foreign_keys="Client.user_id",
    )
    referral_partner_company: Mapped[ReferralPartnerCompany | None] = relationship(
        foreign_keys=[referral_partner_company_id]
    )
