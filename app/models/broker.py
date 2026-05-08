from __future__ import annotations

import uuid
from datetime import date
from typing import Any, TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import BrokerTier
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.user import User


class Broker(TimestampMixin, Base):
    __tablename__ = "brokers"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tier: Mapped[BrokerTier] = mapped_column(String(16), default=BrokerTier.BRONZE)
    joined: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Module 8/Rewards — schema only this pass; award/clawback rules are TODO.
    lifetime_points: Mapped[int] = mapped_column(Integer, default=0)
    redeemed_points: Mapped[int] = mapped_column(Integer, default=0)
    balance_points: Mapped[int] = mapped_column(Integer, default=0)
    funded_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    funded_count: Mapped[int] = mapped_column(Integer, default=0)

    # Per-broker overlay over the firm's settings (alembic 0023).
    # Shape: AgentSettingsData in app/schemas/broker_settings.py.
    # Includes the agent's checklist additions/disables (per loan_type
    # × side), per-broker AI cadence overrides, and personal letterhead.
    settings_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    user: Mapped[User] = relationship(back_populates="broker")
    clients: Mapped[list[Client]] = relationship(back_populates="broker")
