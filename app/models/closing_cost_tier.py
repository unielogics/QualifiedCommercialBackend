from __future__ import annotations

import uuid

from sqlalchemy import Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ClosingCostTier(TimestampMixin, Base):
    """A tier in the global closing-cost table.

    SUPER_ADMIN-configured. The Deal Analyzer finds the tier whose
    `[from_amount, to_amount]` range contains the *base* being charged
    (BRV + construction when the construction is financed; BRV alone
    when the borrower self-funds construction) and applies the matching
    percentage: `percentage` for the with-construction (financed) case,
    `percentage_no_construction` for the without-construction
    (self-funded) case. `from_amount` null = open-ended bottom,
    `to_amount` null = open-ended top. Global — keyed by amount range
    only (no per-program / per-state split).
    """

    __tablename__ = "closing_cost_tiers"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # null bottom = "from zero"; null top = "and up".
    from_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    to_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Fractional rate WITH construction financed, e.g. 0.02 == 2%.
    percentage: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=0)
    # Fractional rate WITHOUT construction (borrower self-funds it).
    percentage_no_construction: Mapped[float] = mapped_column(
        Numeric(6, 4), nullable=False, default=0, server_default="0"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
