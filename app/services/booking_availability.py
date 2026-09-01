from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

DEFAULT_MINIMUM_NOTICE = timedelta(hours=2)
DEFAULT_MAXIMUM_ADVANCE_DAYS = 15


def booking_window_bounds(
    booking: Any,
    now_local: datetime,
) -> tuple[datetime, datetime]:
    """Return the rolling slot-list window in the booking timezone."""

    if bool(getattr(booking, "advance_booking_window_enabled", False)):
        minimum_days = max(0, int(getattr(booking, "minimum_notice_days", 2) or 0))
        maximum_days = max(
            minimum_days,
            int(getattr(booking, "maximum_advance_days", 5) or 5),
        )
        earliest = now_local + timedelta(days=minimum_days)
        latest = now_local + timedelta(days=maximum_days)
    else:
        earliest = now_local + DEFAULT_MINIMUM_NOTICE
        latest = now_local + timedelta(days=DEFAULT_MAXIMUM_ADVANCE_DAYS)
    return earliest, latest.replace(hour=23, minute=59, second=59, microsecond=999999)


def slot_within_custom_booking_window(
    booking: Any,
    slot_start: datetime,
    *,
    now_local: datetime,
) -> bool:
    """Apply the administrator's rolling window to manual calendar actions."""

    if not bool(getattr(booking, "advance_booking_window_enabled", False)):
        return True
    earliest, latest = booking_window_bounds(booking, now_local)
    return earliest <= slot_start <= latest


def daily_booking_windows(
    booking: Any,
    day: date,
) -> list[tuple[int, int]]:
    """Return available minute ranges for one date, using legacy hours as fallback."""

    weekday = (day.weekday() + 1) % 7
    weekly_schedule = getattr(booking, "weekly_schedule", None) or []
    if weekly_schedule:
        for schedule in weekly_schedule:
            schedule_weekday = _value(schedule, "weekday")
            if schedule_weekday != weekday:
                continue
            windows: list[tuple[int, int]] = []
            for interval in _value(schedule, "intervals") or []:
                try:
                    start = _parse_time_minutes(str(_value(interval, "start_time")))
                    end = _parse_time_minutes(str(_value(interval, "end_time")))
                except (TypeError, ValueError):
                    continue
                if end > start:
                    windows.append((start, end))
            return sorted(windows)
        return []

    available_days = getattr(booking, "available_days", None)
    if available_days is None:
        available_days = [1, 2, 3, 4, 5]
    if weekday not in available_days:
        return []
    try:
        start = _parse_time_minutes(str(getattr(booking, "start_time", "09:00") or "09:00"))
        end = _parse_time_minutes(str(getattr(booking, "end_time", "17:00") or "17:00"))
    except ValueError:
        return []
    return [(start, end)] if end > start else []


def slot_fits_daily_schedule(
    booking: Any,
    slot_start: datetime,
    slot_end: datetime,
) -> bool:
    if slot_start.date() != slot_end.date():
        return False
    start_minute = slot_start.hour * 60 + slot_start.minute
    end_minute = slot_end.hour * 60 + slot_end.minute
    return any(
        start_minute >= window_start and end_minute <= window_end
        for window_start, window_end in daily_booking_windows(booking, slot_start.date())
    )


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


def _parse_time_minutes(value: str) -> int:
    hour, minute = _parse_time(value)
    return hour * 60 + minute


def _value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)
