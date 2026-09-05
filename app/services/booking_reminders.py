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
from app.services import message_render
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
                # Pre-call rows are anchored to the booking, not the call; they
                # are re-derived below rather than shifted by minutes_before.
                BookingNotificationReminder.kind == "reminder",
            )
        )
    ).scalars().all()
    for reminder in reminders:
        reminder.due_at = starts_at - timedelta(minutes=reminder.minutes_before)
    email_rows = [row for row in reminders if row.channel == "email"]
    sms_rows = [row for row in reminders if row.channel == "sms"]
    notice.email_reminder_due_at = min((row.due_at for row in email_rows), default=None)
    notice.sms_reminder_due_at = min((row.due_at for row in sms_rows), default=None)
    if notice.precall_dealer_id:
        from app.dealer_os.models import DealerBusiness
        from app.dealer_os.services import precall

        event = await db.get(CalendarEvent, notice.event_id)
        booking = (
            await db.execute(select(BookingSettings).where(BookingSettings.user_id == event.owner_user_id))
        ).scalar_one_or_none() if event is not None else None
        if booking is not None:
            dealer = await db.get(DealerBusiness, notice.precall_dealer_id)
            await precall.retime_after_reschedule(
                db, notice=notice, booking=booking, starts_at=starts_at, dealer=dealer
            )


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
        reminder.error = "cancelled" if reminder.kind == "precall" else None
    if notice.email_reminder_status == "pending":
        notice.email_reminder_status = "cancelled"
    if notice.sms_reminder_status == "pending":
        notice.sms_reminder_status = "cancelled"
    if notice.precall_dealer_id and notice.precall_stopped_at is None and notice.precall_completed_at is None:
        notice.precall_stopped_at = datetime.now(UTC)
        notice.precall_stop_reason = "cancelled"


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
    template: str | None = None,
    values: dict[str, str] | None = None,
) -> None:
    """The booking confirmation text.

    ``template``/``values`` come from the host's booking settings and the
    pre-call kit (room link and PIN). Without them the wording is the original
    confirmation, so a booking with no draft file reads exactly as before.
    """
    if row.confirmation_sms_status != "pending" or not row.invitee_phone or not row.sms_consent:
        return
    local_start = event.starts_at.astimezone(_timezone(timezone_name))
    default = (
        f"Qualified Commercial: your meeting is confirmed for "
        f"{local_start.strftime('%b %d at %I:%M %p %Z')}. "
        f"{'Join: ' + row.join_url + ' ' if row.join_url else ''}"
    )
    if values and values.get("{room_link}"):
        body = message_render.render(template, values, fallback=default)
    else:
        body = default
    body = message_render.with_stop_notice(body)
    try:
        result = await consent_delivery.send_sms_guarded(
            db, row.invitee_phone, body, context="booking_confirmation"
        )
    except Exception:  # noqa: BLE001
        log.exception("booking confirmation SMS raised notification=%s", row.id)
        row.confirmation_sms_status = "failed"
        row.record_delivery_error("sms_provider_exception")
        await db.commit()
        return
    row.confirmation_sms_status = "sent" if result.ok else "failed"
    if result.ok:
        # The stored reason outlives its cause otherwise: these rows kept
        # "SMS_PRODUCTION is disabled" for days after the provider changed.
        row.clear_delivery_error()
    else:
        row.record_delivery_error(result.detail)
    await db.commit()


#: Placeholders an operator can put in a reminder message. Kept small and
#: obvious on purpose: everything here is already known at send time, so a
#: reminder can never fail to render for want of data.
REMINDER_PLACEHOLDERS = ("{time}", "{name}", "{rep}", "{join_link}", "{room_link}", "{precall}", "{date}", "{business}", "{first}")

#: Carriers expect opt-out language on automated recurring messages, so this is
#: appended to every reminder rather than left to whoever wrote the text. A
#: custom message must not be able to drop it by accident.
STOP_NOTICE = message_render.STOP_NOTICE


def render_reminder_sms(
    template: str | None,
    *,
    event_title: str,
    when: str,
    invitee_name: str | None,
    rep_name: str | None,
    join_url: str | None,
    extra: dict[str, str] | None = None,
) -> str:
    """The text of one SMS reminder.

    An empty template falls back to the wording every reminder used before this
    was configurable, so an operator only writes the messages they care about.
    Unknown placeholders are left alone rather than raising: a typo in settings
    should send a slightly odd reminder, not silently send nothing.
    """
    values = {
        "{time}": when,
        "{name}": (invitee_name or "").strip() or "there",
        "{rep}": (rep_name or "").strip() or "Qualified Commercial",
        # Renders as nothing for an in-person meeting, and the surrounding text
        # still reads, which is why it is not "Join: <url>" here.
        "{join_link}": (join_url or "").strip(),
    }
    values.update(extra or {})
    precall_line = (values.get("{precall}") or "").strip()
    body = (template or "").strip()
    if not body:
        body = f"Qualified Commercial reminder: {event_title}, {when}."
        if join_url:
            body = f"{body} Join: {join_url}"
        # The default wording tells the client what is still open before the
        # call; a custom message places {precall} where it wants it, or omits
        # it deliberately.
        if precall_line:
            body = f"{body} {precall_line}"
    else:
        for token, value in values.items():
            body = body.replace(token, value)
        body = " ".join(body.split())
    return message_render.with_stop_notice(body)


def render_reminder_email(
    template: dict | None,
    *,
    host_name: str,
    event_title: str,
    when: str,
    duration_min: int,
    details: list[str],
    join_url: str | None,
    values: dict[str, str],
) -> tuple[str, str]:
    """(subject, body) of one email reminder; host template or the original wording."""
    custom_body = ((template or {}).get("body") or "").strip()
    custom_subject = ((template or {}).get("subject") or "").strip()
    if custom_body:
        subject = message_render.render(custom_subject, values) or f"Reminder: {event_title}"
        return subject, message_render.render_lines(custom_body, values)
    precall_line = (values.get("{precall}") or "").strip()
    body = "\n".join([
        f"Reminder: your meeting with {host_name} is coming up.",
        "",
        f"When: {when}",
        f"Duration: {duration_min} minutes",
        *(details or []),
        *([f"Join: {join_url}"] if join_url else []),
        *(["", precall_line] if precall_line else []),
        "",
        "Qualified Commercial",
    ])
    return f"Reminder: {event_title}", body


async def _precall_values(db, precall, *, notice, event, booking, host) -> dict[str, str]:
    """Placeholder values for a reminder, including the pre-call state of the
    draft file when the booking has one. Never raises: a broken file yields
    plain placeholders and the reminder still goes out."""
    tz = _timezone(booking.timezone)
    local = event.starts_at.astimezone(tz)
    name = (notice.invitee_name or "").strip() or "there"
    base = {
        "{date}": local.strftime("%A, %B %d"),
        "{business}": "",
        "{first}": name.split()[0] if name != "there" else "there",
        "{room_link}": "",
        "{precall}": "",
        "{missing}": "",
        "{done}": "",
        "{pin}": "",
    }
    if not notice.precall_dealer_id or not booking.precall_enabled:
        return base
    try:
        from app.dealer_os.models import DealerBusiness
        from app.dealer_os.services import client_room

        dealer = await db.get(DealerBusiness, notice.precall_dealer_id)
        if dealer is None or dealer.archived_at is not None:
            return base
        ready = await precall.readiness(db, dealer)
        room = await client_room.ensure_room(db, dealer, adopt_intake=False)
        values = precall.template_values(
            notice=notice, event=event, booking=booking, host=host, dealer=dealer,
            room_link=room.url, ready=ready, timezone_name=booking.timezone,
        )
        base.update({k: v for k, v in values.items() if k in base})
        base["{ready_summary}"] = (
            f"Ownership {'✓' if ready.ownership_step_complete else '—'} · "
            f"Bank {'✓' if ready.bank_complete else '—'} · "
            f"Credit {ready.credit_done}/{ready.credit_required or '?'}"
        )
        if notice.precall_stopped_at is not None or ready.complete:
            base["{precall}"] = ""
    except Exception:  # noqa: BLE001
        log.exception("reminder: pre-call values failed notification=%s", notice.id)
    return base


def _rep_readiness_line(extra: dict[str, str]) -> str:
    summary = (extra or {}).get("{ready_summary}")
    return f" Pre-call: {summary}." if summary else ""


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
                # Lock ONLY the reminder row. with_for_update locks every table
                # in the select, and Postgres refuses to lock the nullable side
                # of an outer join — "FOR UPDATE cannot be applied to the
                # nullable side of an outer join" — so this raised on every run
                # and no reminder had been dispatched since the dealer joins
                # were added. The lock exists to stop two workers claiming the
                # same reminder; the joined rows are read-only context.
                .with_for_update(skip_locked=True, of=BookingNotificationReminder)
                .limit(100)
            )
        ).all()
        from app.dealer_os.services import precall

        for reminder, notice, event, booking, host in rows:
            if reminder.kind == "precall":
                try:
                    sent += int(
                        await precall.dispatch_row(
                            db, reminder=reminder, notice=notice, event=event, booking=booking, host=host, now=now
                        )
                    )
                except Exception:  # noqa: BLE001 — one bad row must not stall the tick
                    log.exception("precall step raised reminder=%s", reminder.id)
                    reminder.status = "failed"
                    reminder.sent_at = now
                    reminder.error = "precall_exception"
                continue
            local = event.starts_at.astimezone(_timezone(booking.timezone))
            when = local.strftime("%A, %B %d at %I:%M %p %Z")
            details = _detail_lines(notice)
            # Placeholders shared with the pre-call sequence. {precall} renders
            # what is still open on the draft file, or nothing when the client
            # has finished (or there is no draft), so the reminder reads as it
            # always did for bookings without one.
            from app.services.messaging import outbox

            extra = await _precall_values(db, precall, notice=notice, event=event, booking=booking, host=host)
            if reminder.channel == "email":
                subject, body = render_reminder_email(
                    (booking.reminder_email_messages or {}).get(str(reminder.minutes_before)),
                    host_name=host.name or "Qualified Commercial",
                    event_title=event.title,
                    when=when,
                    duration_min=event.duration_min or booking.duration_min,
                    details=details,
                    join_url=notice.join_url,
                    values=extra,
                )
                result = await asyncio.to_thread(
                    ses_client.send_email,
                    to_email=notice.invitee_email or "",
                    subject=subject,
                    body_text=body,
                )
                reminder.status = "sent" if result.ok else "failed"
                reminder.sent_at = now
                reminder.provider_message_id = getattr(result, "message_id", None)
                reminder.error = None if result.ok else result.detail[:1000]
                notice.email_reminder_status = reminder.status
                notice.email_reminder_sent_at = now
                # rendered_body exists on this row and only the precall branch
                # ever filled it, so a plain reminder left no copy of itself.
                reminder.rendered_body = f"{subject}\n\n{body}"
                if result.ok:
                    notice.clear_delivery_error()
                else:
                    notice.record_delivery_error(result.detail)
                await outbox.record(
                    db, channel="email", status="sent" if result.ok else "failed",
                    draft=outbox.Draft(
                        to=notice.invitee_email or "", subject=subject, body_text=body
                    ),
                    context="booking_reminder", provider="ses",
                    provider_message_id=getattr(result, "message_id", None),
                    detail="" if result.ok else result.detail,
                    subject=outbox.Subject(owner_user_id=notice.booked_by_user_id),
                )
                sent += int(result.ok)
            elif reminder.channel == "sms":
                # Resolved at send time, not at booking time, so editing a
                # message in settings reaches meetings that are already booked.
                template = (booking.reminder_sms_messages or {}).get(str(reminder.minutes_before))
                body = render_reminder_sms(
                    template,
                    event_title=event.title,
                    when=when,
                    invitee_name=notice.invitee_name,
                    rep_name=host.name or host.email,
                    join_url=notice.join_url,
                    extra=extra,
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
                    notice.record_delivery_error("sms_provider_exception")
                    continue
                reminder.status = "sent" if result.ok else "failed"
                reminder.sent_at = now
                reminder.provider_message_id = getattr(result, "message_id", None)
                reminder.error = None if result.ok else result.detail[:1000]
                notice.sms_reminder_status = reminder.status
                notice.sms_reminder_sent_at = now
                if result.ok:
                    notice.clear_delivery_error()
                else:
                    notice.record_delivery_error(result.detail)
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
                    body=f"Your appointment starts {when}.{_rep_readiness_line(extra)}",
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
