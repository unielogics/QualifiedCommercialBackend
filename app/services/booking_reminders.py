from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.services import consent_delivery
from app.dealer_os.services import sms_consent as sms_consent_service
from app.enums import CalendarEventStatus
from app.models.booking_notification import BookingNotification, BookingNotificationReminder
from app.models.booking_settings import BookingSettings
from app.models.event import CalendarEvent
from app.models.user import User
from app.services.email import ses_client
from app.services.notifications import notify_users

log = logging.getLogger(__name__)


def _timezone(name: str | None):
    try:
        return ZoneInfo(name or "America/New_York")
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/New_York")


async def register_booking(
    db: AsyncSession,
    *,
    event: CalendarEvent,
    booking: BookingSettings,
    invitee_name: str,
    invitee_email: str | None,
    invitee_phone: str | None,
    sms_consent: bool,
    sms_consent_method: str | None = None,
    sms_consent_ip: str | None = None,
    sms_consent_user_agent: str | None = None,
    booked_by_user_id=None,
    program_name: str | None = None,
    requested_amount: str | None = None,
    full_address: str | None = None,
) -> BookingNotification:
    email_schedule = sorted(
        set(booking.reminder_email_minutes or [booking.reminder_email_minutes_before]),
        reverse=True,
    )
    sms_schedule = sorted(
        set(booking.reminder_sms_minutes or [booking.reminder_sms_minutes_before]),
        reverse=True,
    )
    email_due = (
        event.starts_at - timedelta(minutes=email_schedule[0])
        if booking.reminder_email_enabled and invitee_email and email_schedule
        else None
    )
    sms_due = (
        event.starts_at - timedelta(minutes=sms_schedule[0])
        if booking.reminder_sms_enabled and invitee_phone and sms_consent and sms_schedule
        else None
    )
    row = BookingNotification(
        event_id=event.id,
        booked_by_user_id=booked_by_user_id,
        invitee_name=invitee_name.strip(),
        invitee_email=(invitee_email or "").strip().lower() or None,
        invitee_phone=consent_delivery.normalize_phone(invitee_phone),
        sms_consent=sms_consent,
        sms_consent_at=datetime.now(UTC) if sms_consent else None,
        sms_consent_method=sms_consent_method if sms_consent else None,
        sms_disclosure_version=(sms_consent_service.SMS_DISCLOSURE_VERSION if sms_consent else None),
        sms_disclosure_text=(sms_consent_service.text_for("transactional") if sms_consent else None),
        sms_consent_ip=(sms_consent_ip or None) if sms_consent else None,
        sms_consent_user_agent=(sms_consent_user_agent or None)[:400] if sms_consent and sms_consent_user_agent else None,
        program_name=(program_name or "").strip() or None,
        requested_amount=(requested_amount or "").strip() or None,
        full_address=(full_address or "").strip() or None,
        email_reminder_due_at=email_due,
        sms_reminder_due_at=sms_due,
        email_reminder_status="pending" if email_due else "disabled",
        sms_reminder_status="pending" if sms_due else ("blocked_no_consent" if invitee_phone and not sms_consent else "disabled"),
        confirmation_email_status="pending" if booking.confirmation_email_enabled and invitee_email else "disabled",
        confirmation_sms_status="pending" if booking.confirmation_sms_enabled and invitee_phone and sms_consent else ("blocked_no_consent" if invitee_phone and not sms_consent else "disabled"),
    )
    db.add(row)
    await db.flush()
    if booking.reminder_email_enabled and row.invitee_email:
        for minutes_before in email_schedule:
            db.add(
                BookingNotificationReminder(
                    booking_notification_id=row.id,
                    channel="email",
                    minutes_before=minutes_before,
                    due_at=event.starts_at - timedelta(minutes=minutes_before),
                )
            )
    # Staff reminders intentionally use email + in-app only. Client SMS
    # consent never authorizes messaging an employee phone number.
    if booking.reminder_email_enabled and booked_by_user_id:
        for minutes_before in email_schedule:
            db.add(
                BookingNotificationReminder(
                    booking_notification_id=row.id,
                    channel="rep",
                    minutes_before=minutes_before,
                    due_at=event.starts_at - timedelta(minutes=minutes_before),
                )
            )
    if booking.reminder_sms_enabled and row.invitee_phone and row.sms_consent:
        for minutes_before in sms_schedule:
            db.add(
                BookingNotificationReminder(
                    booking_notification_id=row.id,
                    channel="sms",
                    minutes_before=minutes_before,
                    due_at=event.starts_at - timedelta(minutes=minutes_before),
                )
            )
    await db.flush()
    return row


async def reschedule_pending(
    db: AsyncSession, notice: BookingNotification, starts_at: datetime
) -> None:
    reminders = (
        await db.execute(
            select(BookingNotificationReminder).where(
                BookingNotificationReminder.booking_notification_id == notice.id,
                BookingNotificationReminder.status == "pending",
            )
        )
    ).scalars().all()
    for reminder in reminders:
        reminder.due_at = starts_at - timedelta(minutes=reminder.minutes_before)
    email_rows = [row for row in reminders if row.channel == "email"]
    sms_rows = [row for row in reminders if row.channel == "sms"]
    notice.email_reminder_due_at = min((row.due_at for row in email_rows), default=None)
    notice.sms_reminder_due_at = min((row.due_at for row in sms_rows), default=None)


async def cancel_pending(db: AsyncSession, notice: BookingNotification) -> None:
    reminders = (
        await db.execute(
            select(BookingNotificationReminder).where(
                BookingNotificationReminder.booking_notification_id == notice.id,
                BookingNotificationReminder.status == "pending",
            )
        )
    ).scalars().all()
    for reminder in reminders:
        reminder.status = "cancelled"
        reminder.error = None
    if notice.email_reminder_status == "pending":
        notice.email_reminder_status = "cancelled"
    if notice.sms_reminder_status == "pending":
        notice.sms_reminder_status = "cancelled"


def _detail_lines(row: BookingNotification) -> list[str]:
    lines: list[str] = []
    if row.program_name:
        lines.append(f"Program: {row.program_name}")
    if row.requested_amount:
        lines.append(f"Interested amount: {row.requested_amount}")
    if row.full_address:
        lines.append(f"Address: {row.full_address}")
    return lines


async def send_confirmation_sms(
    db: AsyncSession,
    row: BookingNotification,
    event: CalendarEvent,
    *,
    timezone_name: str | None = None,
) -> None:
    if row.confirmation_sms_status != "pending" or not row.invitee_phone or not row.sms_consent:
        return
    local_start = event.starts_at.astimezone(_timezone(timezone_name))
    body = (
        f"Qualified Commercial: your meeting is confirmed for "
        f"{local_start.strftime('%b %d at %I:%M %p %Z')}. "
        f"{'Join: ' + row.join_url + ' ' if row.join_url else ''}Reply STOP to opt out."
    )
    try:
        result = await consent_delivery.send_sms_guarded(
            db, row.invitee_phone, body, context="booking_confirmation"
        )
    except Exception:  # noqa: BLE001
        log.exception("booking confirmation SMS raised notification=%s", row.id)
        row.confirmation_sms_status = "failed"
        row.last_error = "sms_provider_exception"
        await db.commit()
        return
    row.confirmation_sms_status = "sent" if result.ok else "failed"
    if not result.ok:
        row.last_error = result.detail[:1000]
    await db.commit()


async def dispatch_due_reminders() -> int:
    from app.db import SessionLocal
    from app.dealer_os.models import DealerBusiness, DealerRepAppointment

    sent = 0
    async with SessionLocal() as db:
        now = datetime.now(UTC)
        rows = (
            await db.execute(
                select(BookingNotificationReminder, BookingNotification, CalendarEvent, BookingSettings, User)
                .join(BookingNotification, BookingNotification.id == BookingNotificationReminder.booking_notification_id)
                .join(CalendarEvent, CalendarEvent.id == BookingNotification.event_id)
                .join(User, User.id == CalendarEvent.owner_user_id)
                .join(BookingSettings, BookingSettings.user_id == User.id)
                .outerjoin(
                    DealerRepAppointment,
                    DealerRepAppointment.calendar_event_id == CalendarEvent.id,
                )
                .outerjoin(
                    DealerBusiness,
                    DealerBusiness.id == DealerRepAppointment.dealer_id,
                )
                .where(
                    CalendarEvent.status != CalendarEventStatus.CANCELLED,
                    CalendarEvent.starts_at > now,
                    (
                        DealerRepAppointment.dealer_id.is_(None)
                        | DealerBusiness.is_training.is_(False)
                    ),
                    BookingNotificationReminder.status == "pending",
                    BookingNotificationReminder.due_at <= now,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        ).all()
        for reminder, notice, event, booking, host in rows:
            local = event.starts_at.astimezone(_timezone(booking.timezone))
            when = local.strftime("%A, %B %d at %I:%M %p %Z")
            details = _detail_lines(notice)
            if reminder.channel == "email":
                body = "\n".join([
                    f"Reminder: your meeting with {host.name or 'Qualified Commercial'} is coming up.",
                    "",
                    f"When: {when}",
                    f"Duration: {event.duration_min or booking.duration_min} minutes",
                    *(details or []),
                    *( [f"Join: {notice.join_url}"] if notice.join_url else [] ),
                    "",
                    "Qualified Commercial",
                ])
                result = await asyncio.to_thread(
                    ses_client.send_email,
                    to_email=notice.invitee_email or "",
                    subject=f"Reminder: {event.title}",
                    body_text=body,
                )
                reminder.status = "sent" if result.ok else "failed"
                reminder.sent_at = now
                reminder.provider_message_id = getattr(result, "message_id", None)
                reminder.error = None if result.ok else result.detail[:1000]
                notice.email_reminder_status = reminder.status
                notice.email_reminder_sent_at = now
                if not result.ok:
                    notice.last_error = result.detail[:1000]
                sent += int(result.ok)
            elif reminder.channel == "sms":
                body = (
                    f"Qualified Commercial reminder: {event.title}, {when}. "
                    f"{'Join: ' + notice.join_url + ' ' if notice.join_url else ''}Reply STOP to opt out."
                )
                try:
                    result = await consent_delivery.send_sms_guarded(
                        db, notice.invitee_phone or "", body, context="booking_reminder"
                    )
                except Exception:  # noqa: BLE001
                    log.exception("booking reminder SMS raised notification=%s", notice.id)
                    reminder.status = "failed"
                    reminder.sent_at = now
                    reminder.error = "sms_provider_exception"
                    notice.sms_reminder_status = "failed"
                    notice.sms_reminder_sent_at = now
                    notice.last_error = "sms_provider_exception"
                    continue
                reminder.status = "sent" if result.ok else "failed"
                reminder.sent_at = now
                reminder.provider_message_id = getattr(result, "message_id", None)
                reminder.error = None if result.ok else result.detail[:1000]
                notice.sms_reminder_status = reminder.status
                notice.sms_reminder_sent_at = now
                if not result.ok:
                    notice.last_error = result.detail[:1000]
                sent += int(result.ok)
            elif reminder.channel == "rep":
                rep = await db.get(User, notice.booked_by_user_id) if notice.booked_by_user_id else None
                if rep is None:
                    reminder.status = "failed"
                    reminder.sent_at = now
                    reminder.error = "booking_rep_missing"
                    continue
                await notify_users(
                    db,
                    recipient_ids={rep.id},
                    event_type="appointment_reminder",
                    category="calendar",
                    priority="high",
                    title=f"Upcoming appointment: {event.title}",
                    body=f"Your appointment starts {when}.",
                    target_type="dealer_rep_appointment",
                    target_id=str(event.external_ref_id or event.id),
                    deep_link=f"/calendar?appointment={event.external_ref_id}" if event.external_ref_id else "/calendar",
                    meta={"event_id": str(event.id), "join_url": notice.join_url},
                    email=True,
                    push=True,
                )
                reminder.status = "sent"
                reminder.sent_at = now
                reminder.error = None
                sent += 1
        await db.commit()
    return sent
