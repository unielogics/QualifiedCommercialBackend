from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class LenderSpread(TimestampMixin, Base):
    """Spread (in basis points) added to a FRED index to produce the
    'Estimated Interest Rate' shown on the dashboard.

    One row per change. The active spread for a series is the row with the
    most recent `created_at` for that `series_id`. Older rows form the audit
    trail (who set what spread when, with notes). To deactivate a series
    entirely, insert a row with spread_bps=0 and notes explaining.
    """

    __tablename__ = "lender_spreads"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    series_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    spread_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
