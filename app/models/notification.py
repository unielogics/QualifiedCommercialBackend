from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class Notification(TimestampMixin, Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="system", server_default="system")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="medium", server_default="medium")
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    deep_link: Mapped[str | None] = mapped_column(String(600), nullable=True)
    channels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    batch_key: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recipient = relationship("User", foreign_keys=[recipient_user_id])

    __table_args__ = (
        Index("ix_notifications_recipient_read_created", "recipient_user_id", "read_at", "created_at"),
    )
