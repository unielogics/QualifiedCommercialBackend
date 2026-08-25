from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking_settings import BookingSettings
from app.models.user import User
from app.services.payment_authorization import primary_super_admin


async def lock_calendar_owner(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialize bookings for one calendar owner inside the caller transaction."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


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
            reminder_sms_enabled=True,
            reminder_sms_minutes_before=120,
            google_meet_enabled=True,
        )
        db.add(row)
        await db.flush()
        await db.commit()
        await db.refresh(row)
    return host, row
