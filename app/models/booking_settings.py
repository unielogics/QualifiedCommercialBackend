from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, text
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
    #: What each SMS reminder says, keyed by its minutes-before as a string —
    #: {"1440": "See you tomorrow...", "60": "Starting in an hour..."}. Missing
    #: or blank falls back to the default wording, so an operator writes only
    #: the ones they care about. Keyed rather than index-aligned with
    #: reminder_sms_minutes: parallel lists drift when a reminder is removed.
    reminder_sms_messages: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    reminder_sms_minutes: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: [120],
        server_default="[120]",
    )
    #: Email reminder text, keyed like reminder_sms_messages by minutes-before,
    #: each value {"subject": ..., "body": ...}. Missing means the default.
    reminder_email_messages: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Confirmation templates: {"email_subject", "email_body", "sms", "pin_email_subject", "pin_email_body"}.
    confirmation_messages: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Pre-call prep: whether bookings open a draft file + room and run the
    #: nudge sequence, and the host's overrides for each step's text/timing.
    precall_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    precall_messages: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    google_meet_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    timezone: Mapped[str] = mapped_column(String(80), nullable=False, default="America/New_York")
    available_days: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: [1, 2, 3, 4, 5],
        server_default="[1,2,3,4,5]",
    )
    weekly_schedule: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    advance_booking_window_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    minimum_notice_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2,
        server_default="2",
    )
    maximum_advance_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default="5",
    )
    blocked_intervals: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    booking_questions: Mapped[dict[str, bool]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {
            "business_name": True,
            "phone": True,
            "requested_amount": True,
            "bank_statement": False,
        },
        server_default='{"business_name": true, "phone": true, "requested_amount": true, "bank_statement": false}',
    )
    no_show_follow_up_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    morning_digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    missing_outcome_reminder_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=48, server_default="48"
    )
    start_time: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    end_time: Mapped[str] = mapped_column(String(5), nullable=False, default="17:00")
    #: Watched before the call, carried into the pre-call messages by {video}.
    precall_video_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: The video library: [{key, label, url}]. {video} renders the first one and
    #: {video_<key>} renders any of them, so a message references a video by a
    #: stable key rather than by a URL that changes when it is re-recorded.
    precall_videos: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    logo_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    profile_photo_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped[User] = relationship(foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_booking_settings_enabled_slug", "enabled", "slug"),
    )
