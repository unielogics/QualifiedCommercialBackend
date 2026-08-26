"""Rep Field Desk workflow helpers.

The routes orchestrate persistence and delivery. This module owns the business
rules that should be testable without a database: the underwriting slot window
and the shared business-card copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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


def underwriting_window_end(
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> datetime:
    """Return the UTC end of the rolling 48-weekday-hour review window."""
    zone = tz(timezone_name)
    base = _aware_utc(now or datetime.now(UTC))
    return add_business_hours(base.astimezone(zone), 48).astimezone(UTC)


def _label(local: datetime) -> str:
    minute = f"{local.minute:02d}"
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{minute} {suffix}"


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
    base = _aware_utc(now or datetime.now(UTC))
    window_end = underwriting_window_end(timezone_name=timezone_name, now=base)

    normalized: list[datetime] = []
    seen: set[datetime] = set()
    for raw in slots:
        slot = _aware_utc(raw).replace(second=0, microsecond=0)
        local = slot.astimezone(zone)
        if slot <= base:
            raise SlotValidationError("Every underwriting review time must be in the future.")
        if slot > window_end:
            raise SlotValidationError("Review windows must be within the next 48 hours, excluding weekends.")
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


@dataclass(frozen=True)
class ProgramPdf:
    key: str
    title: str
    description: str
    filename: str
    lines: tuple[str, ...]


PROGRAM_PDFS: tuple[ProgramPdf, ...] = (
    ProgramPdf(
        key="program-overview",
        title="Qualified Commercial Program Overview",
        description="A high-level overview of the funding programs and intake path.",
        filename="qualified-commercial-program-overview.pdf",
        lines=(
            "Qualified Commercial helps business owners compare funding paths for working capital, equipment, refinance, real estate, floorplan, and revenue-based needs.",
            "A field rep can open the application, collect permissions, request bank and credit authorizations, and help the client book a review with underwriting.",
            "The goal is to route each file into the right program without forcing a client to guess which product fits before the file is reviewed.",
        ),
    ),
    ProgramPdf(
        key="working-capital",
        title="Working Capital Programs",
        description="Program notes for operating capital, bridge capital, and revenue-based options.",
        filename="qualified-commercial-working-capital.pdf",
        lines=(
            "Working capital programs can support payroll, inventory, vendor balances, marketing, short-term bridge needs, and operating liquidity.",
            "The file review focuses on bank activity, revenue trend, current obligations, use of funds, and whether the request can be structured without creating payment stress.",
            "Qualified Commercial compares available options after documents and authorizations are returned.",
        ),
    ),
    ProgramPdf(
        key="equipment-real-estate",
        title="Equipment and Real Estate Programs",
        description="Program notes for equipment, owner-occupied real estate, refinance, and collateral-backed requests.",
        filename="qualified-commercial-equipment-real-estate.pdf",
        lines=(
            "Equipment and real estate requests are reviewed around collateral, use of proceeds, repayment ability, owner history, and the supporting documents available for the asset.",
            "Qualified Commercial can help organize the file for equipment finance, refinance, real estate acquisition, and owner-occupied commercial property scenarios.",
            "Final options depend on verified income, credit, collateral details, existing debt, and lender appetite.",
        ),
    ),
)


def program_pdf_options() -> list[dict[str, str]]:
    return [
        {
            "key": pdf.key,
            "title": pdf.title,
            "description": pdf.description,
            "filename": pdf.filename,
        }
        for pdf in PROGRAM_PDFS
    ]


def selected_program_pdfs(keys: list[str] | None) -> list[ProgramPdf]:
    if not keys:
        return []
    by_key = {pdf.key: pdf for pdf in PROGRAM_PDFS}
    selected: list[ProgramPdf] = []
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            continue
        pdf = by_key.get(key)
        if pdf is None:
            raise ValueError(f"Unknown program PDF: {key}")
        selected.append(pdf)
        seen.add(key)
    return selected


def program_pdf(key: str) -> ProgramPdf | None:
    return next((pdf for pdf in PROGRAM_PDFS if pdf.key == key), None)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_program_pdf(pdf: ProgramPdf) -> bytes:
    """Create a compact, valid PDF without adding another PDF dependency."""
    text_lines = [
        "Qualified Commercial",
        pdf.title,
        "",
        *pdf.lines,
        "",
        "This material is informational only and is not a commitment to lend.",
    ]
    commands = ["BT", "/F1 18 Tf", "72 740 Td", f"({_pdf_escape(text_lines[0])}) Tj"]
    commands += ["0 -28 Td", "/F1 14 Tf", f"({_pdf_escape(text_lines[1])}) Tj"]
    commands += ["0 -28 Td", "/F1 10 Tf"]
    for line in text_lines[2:]:
        if not line:
            commands.append("0 -16 Td")
            continue
        words = line.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) > 92:
                commands.append(f"({_pdf_escape(current)}) Tj")
                commands.append("0 -14 Td")
                current = word
            else:
                current = candidate
        if current:
            commands.append(f"({_pdf_escape(current)}) Tj")
            commands.append("0 -14 Td")
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_start}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


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
