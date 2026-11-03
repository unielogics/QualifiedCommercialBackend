"""Local time for people, not servers.

Every clock in the system runs on UTC, which is right for storage and wrong
for a sentence. An owner reading "uploaded at 04:04 PM UTC" at eleven in the
morning in Florida has to do the arithmetic the message should have done.
Outbound mail renders a moment in the firm's zone, labelled the way the
console labels it (ET, not EST/EDT).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FIRM_DEFAULT_TIMEZONE = "America/New_York"

#: The labels people use. Anything else falls back to the zone's own
#: abbreviation (GMT, CET, ...), which is at least unambiguous.
_SHORT_LABELS = {
    "America/New_York": "ET",
    "America/Detroit": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Phoenix": "MT",
    "America/Los_Angeles": "PT",
    "America/Anchorage": "AKT",
    "Pacific/Honolulu": "HT",
}


def zone(name: str | None) -> ZoneInfo:
    """The zone called ``name``; the firm's default when that is unknown or blank."""
    for candidate in (name, FIRM_DEFAULT_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(str(candidate))
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def zone_label(tz: ZoneInfo, at: datetime | None = None) -> str:
    """"ET" for New York; otherwise what the zone calls itself at ``at``."""
    label = _SHORT_LABELS.get(str(tz.key))
    if label:
        return label
    moment = (at or datetime.now(UTC)).astimezone(tz)
    return moment.strftime("%Z") or str(tz.key)


def local_stamp(value: datetime, tz: ZoneInfo | None = None, *, fmt: str = "%b %-d, %-I:%M %p", label: bool = True) -> str:
    """``value`` as a person in ``tz`` would say it: "Nov 3, 11:04 AM ET".

    A naive datetime is taken as UTC, which is what the database hands back.
    """
    tz = tz or zone(None)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(tz)
    text = local.strftime(fmt)
    return f"{text} {zone_label(tz, value)}" if label else text


async def firm_zone(db) -> ZoneInfo:
    """The zone the firm keeps its calendar in: the primary admin's booking zone."""
    from app.dealer_os.services.prospects import firm_booking_timezone

    try:
        return zone(await firm_booking_timezone(db))
    except Exception:  # noqa: BLE001 - a digest must go out even if the lookup fails
        return zone(None)
