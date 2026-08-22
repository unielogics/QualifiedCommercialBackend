"""Rep Field Desk workflow helpers.

The routes orchestrate persistence and delivery. This module owns the business
rules that should be testable without a database: the underwriting slot window
and the shared business-card copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class SlotValidationError(ValueError):
    pass


def tz(name: str | None) -> ZoneInfo:
    for candidate in (name, "America/New_York"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except Exception:  # noqa: BLE001
            continue
    return ZoneInfo("UTC")


def _aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5


def add_business_hours(start: datetime, hours: int) -> datetime:
    """Add weekday clock hours, skipping Saturday and Sunday entirely."""
    if hours <= 0:
        return start
    cursor = start
    remaining = hours
    while remaining > 0:
        cursor = cursor + timedelta(hours=1)
        if _is_weekday(cursor):
            remaining -= 1
    return cursor


def _label(local: datetime) -> str:
    minute = f"{local.minute:02d}"
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%a, %b')} {local.day} at {hour}:{minute} {suffix}"


def _date_label(local: datetime) -> str:
    return f"{local.strftime('%A, %B')} {local.day}"


def validate_underwriting_slots(
    slots: list[datetime],
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Validate exactly three weekday slots within the next 48 business hours."""
    if len(slots) != 3:
        raise SlotValidationError("Choose exactly three underwriting review times.")

    zone = tz(timezone_name)
    base = _aware_utc(now or datetime.now(timezone.utc))
    window_end = add_business_hours(base.astimezone(zone), 48).astimezone(timezone.utc)

    normalized: list[datetime] = []
    seen: set[datetime] = set()
    for raw in slots:
        slot = _aware_utc(raw).replace(second=0, microsecond=0)
        local = slot.astimezone(zone)
        if slot <= base:
            raise SlotValidationError("Every underwriting review time must be in the future.")
        if slot > window_end:
            raise SlotValidationError("Underwriting review times must be within the next 48 business hours.")
        if not _is_weekday(local):
            raise SlotValidationError("Saturday and Sunday are not available for underwriting reviews.")
        if slot in seen:
            raise SlotValidationError("Choose three different underwriting review times.")
        seen.add(slot)
        normalized.append(slot)

    normalized.sort()
    return [
        {
            "starts_at": slot.isoformat(),
            "label": _label(slot.astimezone(zone)),
            "date_label": _date_label(slot.astimezone(zone)),
        }
        for slot in normalized
    ]


def is_stop_message(body: str | None) -> bool:
    text = (body or "").strip().upper()
    return text in {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}


@dataclass(frozen=True)
class ContactShareCopy:
    subject: str
    email_body: str
    sms_body: str


def build_contact_share_copy(
    *,
    rep_name: str,
    rep_email: str | None,
    rep_phone: str | None,
    recipient_name: str,
    card_url: str,
    booking_url: str,
    application_url: str,
    notes: str | None = None,
) -> ContactShareCopy:
    subject = f"{rep_name} at Qualified Commercial"
    greeting = recipient_name.split()[0] if recipient_name.strip() else "there"
    lines = [
        f"Hi {greeting},",
        "",
        f"This is {rep_name} with Qualified Commercial. We help business owners compare funding programs for working capital, equipment, refinance, real estate, floorplan, and revenue-based needs.",
        "",
        "You can keep my contact card, book a callback, or start an application here:",
        f"Contact card: {card_url}",
        f"Book a time: {booking_url}",
        f"Open an application: {application_url}",
    ]
    if rep_email:
        lines.append(f"Email: {rep_email}")
    if rep_phone:
        lines.append(f"Phone: {rep_phone}")
    if notes:
        lines += ["", notes.strip()]
    lines += ["", "Qualified Commercial"]
    sms = (
        f"{rep_name} at Qualified Commercial: funding programs for working capital, equipment, refinance and more. "
        f"My card and booking link: {card_url} Reply STOP to opt out."
    )
    return ContactShareCopy(subject=subject, email_body="\n".join(lines), sms_body=sms[:480])
