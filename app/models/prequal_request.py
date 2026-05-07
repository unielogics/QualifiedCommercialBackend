"""PrequalRequest — borrower-submitted ask for a one-page pre-qualification
letter PDF. The flow is asynchronous: the borrower submits with the property
address + numbers + closing date, an underwriter reviews (can override the
approved purchase price / loan amount + leave admin notes visible to the
borrower), then either Approve (PDF rendered + uploaded to S3) or Reject
(status flips + reason saved).

The PDF is never auto-generated; human-in-the-loop sign-off is the whole
point of this table existing instead of an instant "generate" endpoint.

Submitting a request also spawns (or attaches to) a Loan record so the
operator pipeline picks the file up naturally.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan
    from app.models.user import User


class PrequalRequest(TimestampMixin, Base):
    __tablename__ = "prequal_requests"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # NULLABLE — submit no longer spawns a Loan. The Loan is created when
    # the borrower marks the seller's offer as accepted, and that's when
    # this column gets populated.
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=True
    )
    requester_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # What the borrower asked for
    target_property_address: Mapped[str] = mapped_column(Text, nullable=False)
    purchase_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    requested_loan_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    loan_type: Mapped[str] = mapped_column(String(16), nullable=False)  # "dscr" | "bridge"
    expected_closing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    borrower_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # LLC / entity name the letter is issued to. NULL = TBD (borrower
    # hasn't formed the LLC yet — falls back to the individual client's
    # legal name in the rendered PDF).
    borrower_entity: Mapped[str | None] = mapped_column(Text, nullable=True)

    # What the underwriter authorized (defaults to the borrower's request if
    # the admin doesn't override)
    approved_purchase_price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    approved_loan_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Calculator snapshot the underwriter saves on approve. Drives the
    # PDF and pre-fills the new Loan when the borrower accepts the
    # seller's offer. Shape varies by loan_type (DSCR/Bridge/F&F/GU) so
    # JSONB rather than rigid columns:
    #   { "rate": 7.625, "points": 1.0, "monthly_pi": 2150.31,
    #     "ltv": 0.75, "dscr": 1.18, "rent": 4250, ... }
    approved_scenario: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    # Quote number — generated on offer_accepted (Q-{4-digit}) and used
    # on the borrower's confirmation receipt + the converted Loan's
    # references.
    quote_number: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # State machine:
    #   pending → approved → offer_accepted (creates Loan) | offer_declined
    #              ↓
    #            rejected
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    pdf_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    loan: Mapped[Loan | None] = relationship(back_populates="prequal_requests")
    requester: Mapped[User] = relationship(foreign_keys=[requester_id])
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewed_by])

    __table_args__ = (
        # Firm-wide queue sort: pending first, then by closing date.
        Index("ix_prequal_requests_status_closing", "status", "expected_closing_date"),
        # Per-loan tab lookup
        Index("ix_prequal_requests_loan", "loan_id"),
        # Borrower's own list across all their loans
        Index("ix_prequal_requests_requester_status", "requester_id", "status"),
    )
