from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import AITaskPriority, AITaskSource, AITaskStatus
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan


class AITask(TimestampMixin, Base):
    __tablename__ = "ai_tasks"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=True
    )
    source: Mapped[AITaskSource] = mapped_column(String(32), nullable=False)
    priority: Mapped[AITaskPriority] = mapped_column(String(16), default=AITaskPriority.MEDIUM)
    status: Mapped[AITaskStatus] = mapped_column(String(16), default=AITaskStatus.PENDING)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0)
    agent: Mapped[str] = mapped_column(String(64), default="general")
    draft_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    loan: Mapped[Loan | None] = relationship(back_populates="ai_tasks")
