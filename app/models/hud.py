from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan


class HudLineItem(TimestampMixin, Base):
    __tablename__ = "hud_line_items"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)  # orig, uw, proc, appr, title, rec, insp
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    category: Mapped[str] = mapped_column(String(16), default="fixed")  # fixed | variable
    editable: Mapped[bool] = mapped_column(Boolean, default=True)

    loan: Mapped[Loan] = relationship(back_populates="hud_items")
