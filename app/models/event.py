from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (
    AITaskPriority,
    CalendarEventKind,
    CalendarEventSource,
    CalendarEventStatus,
)
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan
    from app.models.user import User


class CalendarEvent(TimestampMixin, Base):
    __tablename__ = "calendar_events"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[CalendarEventKind] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    who: Mapped[str | None] = mapped_column(String(160), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[AITaskPriority | None] = mapped_column(String(16), nullable=True)

    # Lifecycle / provenance / scoping fields (added 0013).
    status: Mapped[CalendarEventStatus] = mapped_column(
        String(16), nullable=False, default=CalendarEventStatus.PENDING
    )
    source: Mapped[CalendarEventSource] = mapped_column(
        String(16), nullable=False, default=CalendarEventSource.MANUAL
    )
    # Targeted owner — drives borrower scoping (an event with
    # owner_user_id == client.user_id surfaces in the borrower's
    # "Action needed" pill) and reminder targeting.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Idempotency keys for auto / ai entries. NULL on manual rows.
    # Partial unique index lives on (external_ref_kind, external_ref_id)
    # WHERE external_ref_kind IS NOT NULL — defined in alembic 0013.
    external_ref_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    loan: Mapped[Loan | None] = relationship(back_populates="calendar_events")
    owner: Mapped[User | None] = relationship(foreign_keys=[owner_user_id])

    __table_args__ = (
        Index("ix_calendar_events_starts_at", "starts_at"),
        Index("ix_calendar_events_loan", "loan_id"),
        Index("ix_calendar_events_owner_status", "owner_user_id", "status"),
    )
