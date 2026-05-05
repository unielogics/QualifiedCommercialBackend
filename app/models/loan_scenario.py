from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    pass


class LoanScenario(TimestampMixin, Base):
    """A named what-if scenario on a loan — saved sliders + resulting
    RecalcResponse snapshot. Restorable as a chip in the Deal Workspace
    simulator. The most recent scenario is also injected into the AI's
    context so chat replies can reference 'the current scenario'.
    """

    __tablename__ = "loan_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    discount_points: Mapped[float] = mapped_column(Numeric(5, 3), default=0)
    loan_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    base_rate: Mapped[float | None] = mapped_column(Numeric(7, 6), nullable=True)
    annual_taxes: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    annual_insurance: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    monthly_hoa: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    ltv: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    # Cached RecalcResponse so the chip can show numbers without re-running
    # the recalc each render. Refreshed when the operator clicks "update".
    recalc_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
