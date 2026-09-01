from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def slot_overlaps_blocked_interval(
    booking: Any,
    slot_start: datetime,
    slot_end: datetime,
) -> bool:
    """Return true when a reservation intersects a recurring or dated break."""

    weekday = (slot_start.weekday() + 1) % 7
    reserved_start = slot_start
    reserved_end = slot_end
    before = max(0, int(getattr(booking, "buffer_before_min", 0) or 0))
    after = max(0, int(getattr(booking, "buffer_after_min", 0) or 0))
    if before:
        reserved_start = reserved_start.replace(microsecond=0) - timedelta(minutes=before)
    if after:
        reserved_end = reserved_end.replace(microsecond=0) + timedelta(minutes=after)

    for interval in getattr(booking, "blocked_intervals", None) or []:
        if not isinstance(interval, dict):
            continue
        interval_date = interval.get("on_date")
        applies_on_date = interval_date == slot_start.date().isoformat()
        applies_on_weekday = interval_date is None and interval.get("weekday") == weekday
        if not applies_on_date and not applies_on_weekday:
            continue
        try:
            start_hour, start_minute = _parse_time(str(interval["start_time"]))
            end_hour, end_minute = _parse_time(str(interval["end_time"]))
        except (KeyError, TypeError, ValueError):
            continue
        blocked_start = slot_start.replace(
            hour=start_hour,
            minute=start_minute,
            second=0,
            microsecond=0,
        )
        blocked_end = slot_start.replace(
            hour=end_hour,
            minute=end_minute,
            second=0,
            microsecond=0,
        )
        if reserved_start < blocked_end and reserved_end > blocked_start:
            return True
    return False


def _parse_time(value: str) -> tuple[int, int]:
    hour, minute = [int(part) for part in value.split(":")]
    if hour > 23 or minute > 59:
        raise ValueError("Invalid time")
    return hour, minute
