from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class BookingNotification(TimestampMixin, Base):
    """Persistent confirmation and reminder delivery state for a booking."""

    __tablename__ = "booking_notifications"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("calendar_events.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    booked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    invitee_name: Mapped[str] = mapped_column(String(160), nullable=False)
    invitee_email: Mapped[str | None] = mapped_column(String(320))
    invitee_phone: Mapped[str | None] = mapped_column(String(40))
    sms_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    sms_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_consent_method: Mapped[str | None] = mapped_column(String(32))
    sms_disclosure_version: Mapped[str | None] = mapped_column(String(40))
    sms_disclosure_text: Mapped[str | None] = mapped_column(Text)
    sms_consent_ip: Mapped[str | None] = mapped_column(String(64))
    sms_consent_user_agent: Mapped[str | None] = mapped_column(String(400))
    program_name: Mapped[str | None] = mapped_column(String(180))
    requested_amount: Mapped[str | None] = mapped_column(String(40))
    full_address: Mapped[str | None] = mapped_column(String(500))
    join_url: Mapped[str | None] = mapped_column(String(500))
    confirmation_email_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    confirmation_sms_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    email_reminder_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_reminder_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_reminder_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    sms_reminder_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    #: When `last_error` was recorded. Kept as its own column because the
    #: alternative — deriving it from `updated_at` — is not the same fact:
    #: any later write to the booking moves `updated_at`, so a resolved
    #: failure redated itself every time a reminder went out.
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Pre-call prep. Every booking opens a draft dealer file; the sequence that
    # nudges the client to finish ownership / bank / credit hangs off it here,
    # on the booking, because a public booking has no appointment row.
    precall_dealer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_dealers.id", ondelete="SET NULL")
    )
    precall_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    precall_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # completed|call_started|cancelled|sms_stop|email_stop|superseded|host_disabled
    precall_stop_reason: Mapped[str | None] = mapped_column(String(32))
    # sms|email|rep — how the first PIN reached the client.
    precall_pin_delivered_via: Mapped[str | None] = mapped_column(String(12))

    #: The four channel statuses `last_error` can be describing. One text field
    #: is shared by all of them, so "is anything still broken" has to be asked
    #: of the statuses rather than of the message.
    DELIVERY_STATUS_FIELDS = (
        "confirmation_email_status",
        "confirmation_sms_status",
        "email_reminder_status",
        "sms_reminder_status",
    )

    def record_delivery_error(self, detail: str | None) -> None:
        """Store why a delivery failed, and when it actually failed."""
        self.last_error = (detail or "").strip()[:1000] or None
        self.last_error_at = datetime.now(UTC) if self.last_error else None

    def clear_delivery_error(self) -> None:
        """Drop the stored reason once no channel is failing any more.

        Called after every success. The guard matters because `last_error` is
        one field for four channels: a text going out does not mean the
        confirmation email is fine, so the reason survives until the statuses
        agree there is nothing left to explain. Set the channel's own status
        before calling this.
        """
        if any(getattr(self, field, None) == "failed" for field in self.DELIVERY_STATUS_FIELDS):
            return
        self.last_error = None
        self.last_error_at = None

    __table_args__ = (
        Index("ix_booking_notifications_email_due", "email_reminder_due_at", "email_reminder_sent_at"),
        Index("ix_booking_notifications_sms_due", "sms_reminder_due_at", "sms_reminder_sent_at"),
    )


class BookingNotificationReminder(TimestampMixin, Base):
    """One independently delivered reminder in a booking's channel schedule."""

    __tablename__ = "booking_notification_reminders"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_notification_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("booking_notifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(12), nullable=False)
    # reminder = minutes before the call, re-timed on reschedule.
    # precall  = a pre-call prep step anchored to the booking, absolute due_at.
    kind: Mapped[str] = mapped_column(String(12), nullable=False, default="reminder", server_default="reminder")
    step_key: Mapped[str | None] = mapped_column(String(24))
    minutes_before: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(Text)
    # What was actually sent, so the rep's timeline shows the real text.
    rendered_body: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "booking_notification_id",
            "channel",
            "minutes_before",
            name="uq_booking_notification_reminder_schedule",
        ),
        Index("ix_booking_notification_reminders_due", "status", "due_at"),
    )
