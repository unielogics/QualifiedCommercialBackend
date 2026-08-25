from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class BookingSettings(TimestampMixin, Base):
    __tablename__ = "booking_settings"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    slug: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    title: Mapped[str | None] = mapped_column(String(140), nullable=True)
    intro: Mapped[str | None] = mapped_column(String(600), nullable=True)
    primary_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#5eead4")
    background_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#05070d")
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    buffer_before_min: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    buffer_after_min: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    confirmation_email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    confirmation_sms_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    reminder_email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    reminder_email_minutes_before: Mapped[int] = mapped_column(Integer, nullable=False, default=1440, server_default="1440")
    reminder_email_minutes: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: [1440],
        server_default="[1440]",
    )
    reminder_sms_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    reminder_sms_minutes_before: Mapped[int] = mapped_column(Integer, nullable=False, default=120, server_default="120")
    reminder_sms_minutes: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: [120],
        server_default="[120]",
    )
    google_meet_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    timezone: Mapped[str] = mapped_column(String(80), nullable=False, default="America/New_York")
    available_days: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: [1, 2, 3, 4, 5],
        server_default="[1,2,3,4,5]",
    )
    start_time: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    end_time: Mapped[str] = mapped_column(String(5), nullable=False, default="17:00")
    logo_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    profile_photo_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped[User] = relationship(foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_booking_settings_enabled_slug", "enabled", "slug"),
    )
