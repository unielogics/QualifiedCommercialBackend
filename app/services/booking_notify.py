"""What happens after someone books a time.

Three things have to land, and they are ranked by how much the business cares:

  1. The host learns about it. Previously this went out through the Gmail
     domain-delegation relay only, which is unconfigured in production, so
     `gmail_config()` returned None and the function returned without sending.
     Every booking since the feature shipped notified nobody. SES is verified
     for this domain with production access, so that is the transport now.
  2. The person who booked gets a real calendar invitation. They received
     nothing at all before. An .ics with METHOD:REQUEST gives them Accept and
     Decline in their own mail client, whatever they use, with no dependency on
     the host having connected Google.
  3. The event mirrors to the host's Google Calendar, carrying the invitee as an
     attendee so Google issues its own invite and a Meet link. This one is
     conditional: it needs a connected Google account, and it degrades to
     nothing at all when there isn't one.

Everything here is best-effort by contract. A booking that is committed to our
database is a real booking, and a mail or Google outage must never turn it into
a 500 for the person who just filled the form in.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking_settings import BookingSettings
from app.models.event import CalendarEvent
from app.models.user import User
from app.services.email import ses_client
from app.services.email.ics import ICS_CONTENT_TYPE, build_invite
from app.services.email.ses_client import SesSendResult

log = logging.getLogger(__name__)

# The address the SES identity policy pins sends to. Anything else is denied by
# the instance role, so this is not a preference.
_FALLBACK_FROM = "no-reply@qualifiedcommercial.com"


def _tz(name: str | None) -> ZoneInfo:
    for candidate in (name, "America/New_York"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except Exception:  # noqa: BLE001
            continue
    return ZoneInfo("UTC")


def _when(starts_at: datetime, tz: ZoneInfo, duration_min: int) -> str:
    local = starts_at.astimezone(tz)
    end = (local + timedelta(minutes=duration_min or 30)).strftime("%I:%M %p")
    return f"{local.strftime('%A, %B %-d')} at {local.strftime('%-I:%M %p')} to {end.lstrip('0')} {local.strftime('%Z')}"


def _host_email(user: User) -> str:
    return (user.email or "").strip()


def _from_address() -> str:
    from app.config import get_settings

    return (get_settings().ses_from_address or "").strip() or _FALLBACK_FROM


def notify_host(
    user: User,
    booking: BookingSettings,
    starts_at: datetime,
    *,
    invitee_name: str,
    invitee_email: str,
    invitee_phone: str | None,
    notes: str | None,
    join_url: str | None = None,
) -> None:
    """Tell the host a call was booked. Never raises."""
    to = _host_email(user)
    if not to:
        return
    try:
        tz = _tz(booking.timezone)
        when = _when(starts_at, tz, booking.duration_min)
        lines = [
            f"{invitee_name} booked time with you.",
            "",
            f"When:     {when}",
            f"Duration: {booking.duration_min} minutes",
            f"Name:     {invitee_name}",
            f"Email:    {invitee_email}",
            f"Phone:    {invitee_phone or 'not provided'}",
        ]
        if join_url:
            lines.append(f"Join:     {join_url}")
        lines += ["", "Notes:", (notes or "none").strip(), ""]
        body = "\n".join(lines)
        result = ses_client.send_email(
            to_email=to,
            subject=f"New booking: {invitee_name} on {starts_at.astimezone(tz).strftime('%b %-d')}",
            body_text=body,
        )
        if not result.ok:
            log.warning("booking: host notify failed to=%s detail=%s", to, result.detail)
    except Exception:  # noqa: BLE001
        log.exception("booking: host notification raised")


def send_invitee_invite(
    user: User,
    booking: BookingSettings,
    event: CalendarEvent,
    starts_at: datetime,
    *,
    invitee_name: str,
    invitee_email: str,
    join_url: str | None = None,
) -> SesSendResult | None:
    """Send the person who booked a genuine calendar invitation. Never raises."""
    to = (invitee_email or "").strip()
    if not to or "@" not in to:
        return None
    try:
        tz = _tz(booking.timezone)
        when = _when(starts_at, tz, booking.duration_min)
        host_name = user.name or "Qualified Commercial"
        title = booking.title or f"Call with {host_name}"

        detail = [
            f"You are booked with {host_name}.",
            "",
            f"When: {when}",
            f"Duration: {booking.duration_min} minutes",
        ]
        if join_url:
            detail += ["", f"Join here: {join_url}"]
        detail += [
            "",
            "Accept the invitation to add this to your calendar.",
            "Need to change it? Reply to this email and we will sort it out.",
            "",
            "Qualified Commercial",
        ]
        body = "\n".join(detail)

        ics = build_invite(
            # The event UUID is the calendar identity. Reusing it on any future
            # update is what lets a client replace the entry instead of adding
            # a second one.
            uid=f"{event.id}@qualifiedcommercial.com",
            summary=title,
            starts_at=starts_at,
            duration_min=booking.duration_min or 30,
            organizer_email=_from_address(),
            organizer_name=host_name,
            attendee_email=to,
            attendee_name=invitee_name,
            description=body,
            location=join_url or "",
        )

        result = ses_client.send_raw_email(
            to_emails=[to],
            subject=f"Confirmed: {title}, {starts_at.astimezone(tz).strftime('%b %-d')}",
            body_text=body,
            attachments=[("invite.ics", ics, ICS_CONTENT_TYPE)],
        )
        if not result.ok:
            log.warning("booking: invitee invite failed to=%s detail=%s", to, result.detail)
        return result
    except Exception:  # noqa: BLE001
        log.exception("booking: invitee invite raised")
        return SesSendResult(False, None, "invite_exception")


async def push_to_google(
    db: AsyncSession,
    event: CalendarEvent,
    *,
    invitee_email: str | None,
    invitee_name: str,
    want_meet: bool = True,
) -> str | None:
    """Mirror the booking to the host's Google Calendar with the invitee on it.

    Returns the Meet link when Google issues one. Does nothing and returns None
    when the host has no connected calendar, which is the common case until the
    OAuth client credentials are provisioned.
    """
    try:
        from app.services.google.calendar_sync import push_event

        attendees = (
            [{"email": invitee_email, "displayName": invitee_name}]
            if invitee_email
            else None
        )
        return await push_event(
            db,
            event,
            attendees=attendees,
            want_conference=want_meet,
            send_updates="all",
        )
    except Exception:  # noqa: BLE001
        log.exception("booking: google push failed event=%s", getattr(event, "id", None))
        return None
