"""Deal — agent-side transaction unit attached to a Client (alembic 0047).

A Client can carry multiple Deals at once (buyer search + seller
listing + investor purchase). A Deal is the *pre-funding* concept; it
represents the agent's relationship-stage work on a specific path. When
the agent fires Ready-for-Lending the Deal is promoted to a Loan via
`services/handoff.promote_deal_to_loan`, which stamps
`deal.promoted_loan_id` and `deal.status = 'promoted'` and sets the new
Loan's `source_deal_id` so the lineage is durable.

Deal is intentionally lightweight. Heavy underwriting data lives on
Loan; AI follow-up requirements live on ClientRequirementStatus
(scoped per-deal via `deal_id` once alembic 0049 lands).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class Deal(TimestampMixin, Base):
    __tablename__ = "deals"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    """DealType — buyer | seller | investor | borrower."""

    side: Mapped[str] = mapped_column(String(8), nullable=False, default="buyer")
    """LoanSide — buyer | seller. For investor/borrower deals defaults
    to 'buyer' (they purchase). Used for requirement resolution."""

    property_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("client_properties.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="open", server_default="open",
    )
    """DealStatus — open | active | paused | won | lost | promoted."""

    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    ai_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle", server_default="idle",
    )
    """DealAIStatus — idle | active | paused."""

    handoff_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="none", server_default="none",
    )
    """DealHandoffStatus — none | requested | packet_built | promoted."""

    promoted_loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Set by promote_deal_to_loan in Phase 4. Unique (partial idx) —
    a deal promotes to exactly one loan."""

    title: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    living_profile: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True,
    )

    # ── Property fields (alembic 0052) ────────────────────────────────
    # Agent edits these on the Property tab of /deals/[id]. Copied
    # onto the Loan at promote_deal_to_loan time, so funding sees the
    # snapshot. Distinct from ClientProperty (which is the broader
    # buyer-target / seller-listing browser) — these are the
    # transaction-specific overrides for THIS deal.
    address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(10), nullable=True)
    property_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    beds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baths: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year_built: Mapped[int | None] = mapped_column(Integer, nullable=True)
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    listing_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mls_number: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # ── Agent-private notes (alembic 0052) ────────────────────────────
    # Free-text notes visible ONLY to the agent on /deals/[id] Notes tab.
    # The handoff visibility filter excludes this from funding.
    notes_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships ───────────────────────────────────────────────────

    client = relationship("Client", foreign_keys=[client_id])
    assigned_agent = relationship("User", foreign_keys=[assigned_agent_id])
    property = relationship("ClientProperty", foreign_keys=[property_id])
    promoted_loan = relationship("Loan", foreign_keys=[promoted_loan_id])

    __table_args__ = (
        Index("ix_deals_client_status", "client_id", "status"),
    )
