from __future__ import annotations

import uuid

from sqlalchemy import Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ClosingCostTier(TimestampMixin, Base):
    """A loan-amount tier in the global closing-cost table.

    SUPER_ADMIN-configured. The Deal Analyzer resolves the closing
    percentage by finding the tier whose `[from_amount, to_amount]`
    range contains the loan amount, then applies
    `max(percentage, minimum_dollar / loan_amount)` so a small loan
    floors at `minimum_dollar`. `from_amount` null = open-ended bottom,
    `to_amount` null = open-ended top. Global — keyed by loan-amount
    range only (no per-program / per-state split).
    """

    __tablename__ = "closing_cost_tiers"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # null bottom = "from zero"; null top = "and up".
    from_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    to_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Fractional rate, e.g. 0.02 == 2%.
    percentage: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=0)
    # Dollar floor applied to the loan amount.
    minimum_dollar: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
