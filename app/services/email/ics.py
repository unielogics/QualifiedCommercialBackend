"""iCalendar (RFC 5545) generation for meeting invitations.

Why hand-rolled rather than a library: we emit exactly one shape, a single
non-recurring VEVENT, and the `icalendar` package is not in the image. The
folding and escaping rules for that shape fit in fifty lines and are easier to
audit than a dependency.

The important part is METHOD:REQUEST plus an ATTENDEE carrying RSVP=TRUE.
That combination is what makes Gmail, Outlook and Apple Mail render the mail as
an invitation with Accept / Decline buttons rather than as a file attachment.
Sent as a text/calendar part, it is the transport-independent way to invite
someone: it works whether or not the host has connected Google, and it is what
keeps the booking flow honest when the OAuth path is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

__all__ = ["build_invite", "ICS_CONTENT_TYPE"]

# SES needs the method on the part's content type, or clients treat the body as
# a plain attachment and the RSVP buttons never appear.
ICS_CONTENT_TYPE = "text/calendar; method=REQUEST; charset=UTF-8"

_PRODID = "-//Qualified Commercial//Booking//EN"


def _stamp(dt: datetime) -> str:
    """UTC in iCalendar basic format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _escape(value: str) -> str:
    """RFC 5545 §3.3.11 text escaping. Backslash first, or we double-escape."""
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Content lines are capped at 75 octets, continued with a leading space.

    Folding is measured in OCTETS, not characters, so a multi-byte character
    must not be split across the boundary or the continuation is invalid.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out: list[str] = []
    chunk = bytearray()
    for ch in line:
        enc = ch.encode("utf-8")
        limit = 75 if not out else 74  # continuations lose one octet to the space
        if len(chunk) + len(enc) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = bytearray()
        chunk += enc
    if chunk:
        out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def build_invite(
    *,
    uid: str,
    summary: str,
    starts_at: datetime,
    duration_min: int,
    organizer_email: str,
    organizer_name: str,
    attendee_email: str,
    attendee_name: str = "",
    description: str = "",
    location: str = "",
    sequence: int = 0,
    cancel: bool = False,
) -> bytes:
    """One VEVENT, ready to attach.

    `sequence` must increase on every update to the same `uid`, or clients
    ignore the change. `cancel=True` emits METHOD:CANCEL, which removes the
    event from the attendee's calendar.
    """
    ends_at = starts_at + timedelta(minutes=duration_min or 30)
    method = "CANCEL" if cancel else "REQUEST"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_stamp(datetime.now(UTC))}",
        f"DTSTART:{_stamp(starts_at)}",
        f"DTEND:{_stamp(ends_at)}",
        f"SUMMARY:{_escape(summary)}",
        f"SEQUENCE:{sequence}",
        f"STATUS:{'CANCELLED' if cancel else 'CONFIRMED'}",
        "TRANSP:OPAQUE",
        (
            f"ORGANIZER;CN={_escape(organizer_name)}:mailto:{organizer_email}"
            if organizer_name
            else f"ORGANIZER:mailto:{organizer_email}"
        ),
        # RSVP=TRUE is what surfaces Accept / Decline in the mail client.
        "ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE"
        + (f";CN={_escape(attendee_name)}" if attendee_name else "")
        + f":mailto:{attendee_email}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]

    return "\r\n".join(_fold(ln) for ln in lines).encode("utf-8") + b"\r\n"
