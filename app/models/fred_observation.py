from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class FredObservation(TimestampMixin, Base):
    """One time-series observation from the FRED API.

    Populated by the daily refresh job (POST /admin/fred/refresh).
    `value` is NULL when FRED returns the sentinel "." for missing data —
    we keep the row so we can distinguish "we asked, no data" from "we
    never asked", but downstream queries should COALESCE / filter as needed.
    """

    __tablename__ = "fred_observations"
    __table_args__ = (
        UniqueConstraint("series_id", "observation_date", name="uq_fred_series_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    series_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
