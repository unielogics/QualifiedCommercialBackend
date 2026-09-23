from __future__ import annotations

import uuid
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking_settings import BookingSettings, BookingSlugAlias
from app.models.user import User
from app.services import booking_metrics
from app.services.payment_authorization import primary_super_admin

INHERITABLE_BOOKING_FIELDS = frozenset({
    "duration_min",
    "buffer_before_min",
    "buffer_after_min",
    "confirmation_email_enabled",
    "confirmation_sms_enabled",
    "reminder_email_enabled",
    "reminder_email_minutes_before",
    "reminder_email_minutes",
    "reminder_sms_enabled",
    "reminder_sms_minutes_before",
    "reminder_sms_minutes",
    "reminder_sms_messages",
    "reminder_email_messages",
    "confirmation_messages",
    "precall_enabled",
    "precall_messages",
    "precall_default_variant",
    "precall_allowed_variants",
    "precall_allow_vertical_choice",
    "google_meet_enabled",
    "timezone",
    "available_days",
    "weekly_schedule",
    "advance_booking_window_enabled",
    "minimum_notice_days",
    "minimum_notice_minutes",
    "maximum_advance_days",
    "blocked_intervals",
    "booking_questions",
    "no_show_follow_up_enabled",
    "morning_digest_enabled",
    "missing_outcome_reminder_hours",
    "start_time",
    "end_time",
})


class EffectiveBookingSettings:
    """Read-only overlay of firm defaults and an individual booking page."""

    def __init__(self, personal: BookingSettings, firm: BookingSettings):
        self._personal = personal
        self._firm = firm
        self._overrides = set(personal.firm_policy_overrides or [])

    def __getattr__(self, name: str):
        if name in INHERITABLE_BOOKING_FIELDS and name not in self._overrides:
            return getattr(self._firm, name)
        return getattr(self._personal, name)


async def effective_booking_settings(
    db: AsyncSession,
    personal: BookingSettings,
) -> BookingSettings | EffectiveBookingSettings:
    if not personal.inherit_firm_policy:
        return personal
    firm_user = await primary_super_admin(db)
    if firm_user is None or firm_user.id == personal.user_id:
        return personal
    firm = (
        await db.execute(select(BookingSettings).where(BookingSettings.user_id == firm_user.id))
    ).scalar_one_or_none()
    return EffectiveBookingSettings(personal, firm) if firm is not None else personal


async def lock_calendar_owner(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialize bookings for one calendar owner inside the caller transaction."""
    started = perf_counter()
    try:
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    finally:
        booking_metrics.emit(
            "booking.calendar_owner_lock.wait",
            round((perf_counter() - started) * 1000, 2),
            unit="milliseconds",
        )


async def retain_booking_slug_alias(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    previous_slug: str | None,
    next_slug: str | None,
) -> None:
    """Keep published booking URLs alive when their canonical slug changes."""

    old = (previous_slug or "").strip() or None
    new = (next_slug or "").strip() or None
    if old == new:
        return
    if new:
        reclaimed = (
            await db.execute(
                select(BookingSlugAlias).where(BookingSlugAlias.slug == new)
            )
        ).scalar_one_or_none()
        if reclaimed is not None:
            if reclaimed.user_id != user_id:
                raise ValueError(f"booking slug {new!r} is already used")
            await db.delete(reclaimed)
    if old:
        existing = (
            await db.execute(
                select(BookingSlugAlias).where(BookingSlugAlias.slug == old)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(BookingSlugAlias(user_id=user_id, slug=old))
        elif existing.user_id != user_id:
            raise ValueError(f"booking slug {old!r} is already used")


async def team_calendar_host(db: AsyncSession) -> User:
    host = await primary_super_admin(db)
    if host is None:
        raise RuntimeError("Primary super-admin calendar owner is not configured")
    return host


async def team_booking_settings(db: AsyncSession, host: User | None = None) -> tuple[User, BookingSettings]:
    host = host or await team_calendar_host(db)
    row = (
        await db.execute(select(BookingSettings).where(BookingSettings.user_id == host.id))
    ).scalar_one_or_none()
    if row is None:
        row = BookingSettings(
            id=uuid.uuid4(),
            user_id=host.id,
            enabled=True,
            slug=None,
            title=f"Book a meeting with {host.name or 'Qualified Commercial'}",
            intro="Choose a time that works for you.",
            duration_min=20,
            buffer_before_min=5,
            buffer_after_min=5,
            timezone="America/New_York",
            available_days=[1, 2, 3, 4, 5],
            start_time="09:00",
            end_time="17:00",
            confirmation_email_enabled=True,
            confirmation_sms_enabled=True,
            reminder_email_enabled=True,
            reminder_email_minutes_before=1440,
            reminder_email_minutes=[1440],
            reminder_sms_enabled=True,
            reminder_sms_minutes_before=120,
            reminder_sms_minutes=[120],
            google_meet_enabled=True,
        )
        db.add(row)
        await db.flush()
        # The caller owns the transaction. Committing here can silently
        # release prospect/contact/calendar locks acquired earlier in a
        # booking workflow and split what must be one atomic local change.
    return host, row
