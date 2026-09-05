"""Immutable audit log — every router write produces an Activity row."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app import request_context
from app.db import Base

if TYPE_CHECKING:
    from app.models.loan import Loan


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=True
    )
    # alembic 0035 — client_id lets agents log calls / SMS / meetings on
    # a relationship before a loan exists. Either loan_id OR client_id
    # is set on any meaningful activity row.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 'ai', 'broker', 'client', etc.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The request or scheduler tick this happened in (alembic 0194). The
    # messages it caused carry the same id, which is what turns "which user
    # activity triggered what" into a join instead of a guess.
    #
    # Filled by a column default rather than by the writers: this table has
    # five of them and the next one has not been written yet. A default cannot
    # be forgotten. Nothing bound (a script, a test) simply leaves it NULL.
    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, default=lambda: request_context.request_id() or None
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    loan: Mapped[Loan | None] = relationship(back_populates="activities")
