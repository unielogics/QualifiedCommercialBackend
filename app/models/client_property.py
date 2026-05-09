"""ClientProperty — first-class property records linked to clients.

A buyer can carry many target properties (e.g. shortlist of 3-5 they're
considering); a seller usually has 1 listing but portfolio owners can
have many. Once underwriting starts, the property links to a Loan
via linked_loan_id so both surfaces agree on the same record.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ClientProperty(TimestampMixin, Base):
    __tablename__ = "client_properties"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    side: Mapped[str] = mapped_column(String(16), nullable=False)
    """buyer_target | seller_listing."""

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    """active | offered | under_contract | listed | sold | dropped | archived."""

    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(10), nullable=True)

    property_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """single_family | multifamily | mixed_use | commercial | retail | office | industrial | land."""

    target_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    sold_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    bedrooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bathrooms: Mapped[Decimal | None] = mapped_column(Numeric(3, 1), nullable=True)
    sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    units: Mapped[int | None] = mapped_column(Integer, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    linked_loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="SET NULL"),
        nullable=True,
    )
