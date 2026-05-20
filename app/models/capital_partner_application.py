"""Capital Partner (lender) application — submitted via the public
marketing form at qualifiedcommercial.com/lenders/apply and reviewed
by super-admin in QCDashboard /admin/capital-partner-applications.

Distinct from the existing `Lender` model: this is the *intake* record
captured before we've decided whether to accept the firm onto the
platform. On approval, an operator promotes the application into a
real `Lender` row via the existing admin lender roster (or, future,
a one-click button). Until then, the application carries every field
the team needs to evaluate fit.

Status state machine: pending → approved | denied. Once decided,
review_notes, reviewed_by_id, and reviewed_at are populated. The
application row is preserved for audit even after promotion.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


# Status sentinels — kept as plain strings (no DB enum) for migration
# simplicity. Validate in the router.
APPLICATION_STATUSES: tuple[str, ...] = ("pending", "approved", "denied")


class CapitalPartnerApplication(TimestampMixin, Base):
    """One row per lender / funding-partner application."""

    __tablename__ = "capital_partner_applications"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # --- Company ---
    company_name: Mapped[str] = mapped_column(String(160), nullable=False)
    legal_entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    formation_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ein: Mapped[str | None] = mapped_column(String(20), nullable=True)
    years_in_business: Mapped[int | None] = mapped_column(Integer, nullable=True)
    website: Mapped[str | None] = mapped_column(String(240), nullable=True)

    # --- Lending appetite ---
    # List of LoanType-ish strings: ["dscr", "fix_and_flip", "bridge",
    # "sba_7a", "ground_up", "commercial", "other"]
    loan_types: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    loan_size_min: Mapped[int | None] = mapped_column(Integer, nullable=True)  # USD
    loan_size_max: Mapped[int | None] = mapped_column(Integer, nullable=True)  # USD
    # List of 2-letter state codes; ["NATIONWIDE"] is acceptable.
    geographic_states: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # ["sfr", "multifamily", "mixed_use", "office", "industrial",
    #  "retail", "land", "other"]
    asset_classes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    # --- Capital & volume ---
    # warehouse | balance_sheet | fund | table_funder | private | other
    capital_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    aum_band: Mapped[str | None] = mapped_column(String(40), nullable=True)
    monthly_origination_band: Mapped[str | None] = mapped_column(
        String(40), nullable=True
    )

    # --- Underwriting box ---
    max_ltv: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    max_ltc: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    min_dscr: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    min_fico: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_range: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # --- Primary contact + submission process ---
    contact_name: Mapped[str] = mapped_column(String(160), nullable=False)
    contact_title: Mapped[str | None] = mapped_column(String(80), nullable=True)
    contact_email: Mapped[str] = mapped_column(String(320), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    submission_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    submission_portal_url: Mapped[str | None] = mapped_column(String(320), nullable=True)
    average_response_time: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Review state ---
    # "pending" | "approved" | "denied". String (no DB enum) for
    # migration simplicity — validated at the router boundary.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Once an approved application is promoted into a real Lender row,
    # we stamp the resulting lender_id here so the admin UI can deep-link
    # to the roster entry. Stays null until that promotion happens.
    promoted_lender_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("lenders.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Consent + audit ---
    consent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
