from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import LoanType
from app.models._mixins import TimestampMixin


class RateSheetEntry(TimestampMixin, Base):
    __tablename__ = "rate_sheet"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    loan_type: Mapped[LoanType] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    base_rate: Mapped[float] = mapped_column(Numeric(7, 6), nullable=False)
    points: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0, server_default="0")
    min_fico: Mapped[int] = mapped_column(Integer, nullable=False, default=680, server_default="680")
    max_fico: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credit_tier: Mapped[str] = mapped_column(String(80), nullable=False, default="Base", server_default="Base")
    min_loan_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0, server_default="0")
    max_loan_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    delta_bps: Mapped[int] = mapped_column(Integer, default=0)
    max_ltv: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
