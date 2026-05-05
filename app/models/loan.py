from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import LoanPurpose, LoanStage, LoanType, PropertyType
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.activity import Activity
    from app.models.ai_task import AITask
    from app.models.client import Client
    from app.models.document import Document
    from app.models.event import CalendarEvent
    from app.models.hud import HudLineItem
    from app.models.message import Message


class Loan(TimestampMixin, Base):
    __tablename__ = "loans"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Human-friendly deal ID — used in email subject [QC-{deal_id}]
    deal_id: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)

    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE")
    )
    broker_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )

    # Property
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    property_type: Mapped[PropertyType] = mapped_column(String(32), default=PropertyType.SFR)
    sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    beds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baths: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    year_built: Mapped[int | None] = mapped_column(Integer, nullable=True)
    annual_taxes: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    annual_insurance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    monthly_hoa: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    # Loan
    type: Mapped[LoanType] = mapped_column(String(32), nullable=False)
    purpose: Mapped[LoanPurpose | None] = mapped_column(String(32), nullable=True)
    stage: Mapped[LoanStage] = mapped_column(String(32), default=LoanStage.PREQUALIFIED)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    ltv: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    ltc: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    arv: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    base_rate: Mapped[float | None] = mapped_column(Numeric(7, 6), nullable=True)
    discount_points: Mapped[float] = mapped_column(Numeric(5, 3), default=0)
    final_rate: Mapped[float | None] = mapped_column(Numeric(7, 6), nullable=True)
    origination_pct: Mapped[float] = mapped_column(Numeric(6, 4), default=0.015)
    term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Underwriting
    monthly_rent: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    dscr: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    close_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    client: Mapped[Client] = relationship(back_populates="loans")
    documents: Mapped[list[Document]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    hud_items: Mapped[list[HudLineItem]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    activities: Mapped[list[Activity]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    messages: Mapped[list[Message]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    ai_tasks: Mapped[list[AITask]] = relationship(back_populates="loan", cascade="all, delete-orphan")
    calendar_events: Mapped[list[CalendarEvent]] = relationship(back_populates="loan", cascade="all, delete-orphan")
