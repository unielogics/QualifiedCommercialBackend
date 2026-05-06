from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.broker import Broker
    from app.models.loan import Loan
    from app.models.user import User


class Client(TimestampMixin, Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    broker_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    since: Mapped[date | None] = mapped_column(Date, nullable=True)

    tier: Mapped[str] = mapped_column(String(16), default="standard")
    fico: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avatar_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    referral_source: Mapped[str | None] = mapped_column(String(160), nullable=True)

    funded_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    funded_count: Mapped[int] = mapped_column(Integer, default=0)

    # Investor profile (Profile → Investor Profile dialog). Free-text for
    # v1 — operators see what the borrower wrote and can fold into the AI's
    # context. Structured (per-property table) lands when we wire REO.
    properties: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="client")
    broker: Mapped[Broker] = relationship(back_populates="clients")
    loans: Mapped[list[Loan]] = relationship(back_populates="client")
