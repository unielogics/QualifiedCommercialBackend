from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

DEFAULT_MINIMUM_NOTICE = timedelta(hours=2)
DEFAULT_MAXIMUM_ADVANCE_DAYS = 15


@dataclass(frozen=True)
class BookingDatePage:
    """One calendar-date page inside the configured rolling booking window.

    A page always contains whole local dates (apart from the rolling minimum
    notice on its first date). That contract prevents a global slot cap from
    silently returning half a day and makes a busy first date unable to hide
    later dates.
    """

    earliest_local: datetime
    window_end_local: datetime
    page_start_date: date
    page_end_date: date
    next_start_date: date | None
    configured_window_end_date: date


def booking_date_page(
    earliest_local: datetime,
    window_end_local: datetime,
    *,
    start_date: date | None = None,
    days: int | None = None,
) -> BookingDatePage:
    """Intersect an optional whole-date page with the rolling policy window."""

    if days is not None and not 1 <= days <= 31:
        raise ValueError("days must be between 1 and 31")
    first_date = max(start_date or earliest_local.date(), earliest_local.date())
    if first_date > window_end_local.date():
        raise ValueError("start_date is outside the configured booking window")
    last_date = (
        min(first_date + timedelta(days=days - 1), window_end_local.date())
        if days is not None
        else window_end_local.date()
    )
    midnight = datetime.combine(first_date, datetime.min.time(), tzinfo=earliest_local.tzinfo)
    page_earliest = max(earliest_local, midnight)
    if last_date == window_end_local.date():
        page_end = window_end_local
    else:
        page_end = datetime.combine(
            last_date,
            datetime.max.time(),
            tzinfo=earliest_local.tzinfo,
        )
    next_start = last_date + timedelta(days=1) if last_date < window_end_local.date() else None
    return BookingDatePage(
        earliest_local=page_earliest,
        window_end_local=page_end,
        page_start_date=first_date,
        page_end_date=last_date,
        next_start_date=next_start,
        configured_window_end_date=window_end_local.date(),
    )


def booking_window_bounds(
    booking: Any,
    now_local: datetime,
) -> tuple[datetime, datetime]:
    """Return the rolling slot-list window in the booking timezone."""

    if bool(getattr(booking, "advance_booking_window_enabled", False)):
        minimum_minutes_value = getattr(booking, "minimum_notice_minutes", None)
        if minimum_minutes_value is None:
            # Compatibility for settings objects and clients from before the
            # minute-precision migration.
            minimum_minutes = max(
                0,
                int(getattr(booking, "minimum_notice_days", 2) or 0) * 24 * 60,
            )
        else:
            minimum_minutes = max(0, int(minimum_minutes_value or 0))
        maximum_days = max(
            1,
            int(getattr(booking, "maximum_advance_days", 5) or 5),
        )
        earliest = now_local + timedelta(minutes=minimum_minutes)
        latest = now_local + timedelta(days=maximum_days)
    else:
        earliest = now_local + DEFAULT_MINIMUM_NOTICE
        latest = now_local + timedelta(days=DEFAULT_MAXIMUM_ADVANCE_DAYS)
    return earliest, latest.replace(hour=23, minute=59, second=59, microsecond=999999)


def available_slot_starts(
    booking: Any,
    *,
    earliest_local: datetime,
    window_end_local: datetime,
    duration_min: int,
    busy_intervals: Iterable[tuple[datetime, datetime]] = (),
    step_min: int = 5,
) -> list[datetime]:
    """Enumerate complete dates of eligible starts for every booking surface.

    Busy intervals must already include the configured before/after buffers.
    Keeping enumeration here prevents the authenticated and public routes from
    drifting and, importantly, avoids a global result limit cutting off a date
    halfway through or hiding later dates in the configured window.
    """

    if duration_min <= 0 or step_min <= 0 or window_end_local < earliest_local:
        return []

    duration = timedelta(minutes=duration_min)
    buffer_before = timedelta(
        minutes=max(0, int(getattr(booking, "buffer_before_min", 0) or 0))
    )
    buffer_after = timedelta(
        minutes=max(0, int(getattr(booking, "buffer_after_min", 0) or 0))
    )
    busy = list(busy_intervals)
    starts: list[datetime] = []
    day_count = (window_end_local.date() - earliest_local.date()).days + 1
    for offset in range(max(0, day_count)):
        day = earliest_local.date() + timedelta(days=offset)
        for start_minute, end_minute in daily_booking_windows(booking, day):
            midnight = datetime.combine(day, datetime.min.time(), tzinfo=earliest_local.tzinfo)
            day_start = midnight + timedelta(minutes=start_minute)
            day_end = midnight + timedelta(minutes=end_minute)
            cursor = max(
                day_start,
                earliest_local if day == earliest_local.date() else day_start,
            )
            cursor = _round_up_to_step(cursor, step_min)
            while cursor + duration <= day_end:
                slot_end = cursor + duration
                reserved_start = cursor - buffer_before
                reserved_end = slot_end + buffer_after
                if (
                    not slot_overlaps_blocked_interval(booking, cursor, slot_end)
                    and not any(
                        reserved_start < busy_end and reserved_end > busy_start
                        for busy_start, busy_end in busy
                    )
                ):
                    starts.append(cursor)
                cursor += timedelta(minutes=step_min)
    return starts


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


def _round_up_to_step(value: datetime, step_min: int) -> datetime:
    value = value.replace(second=0, microsecond=0)
    remainder = value.minute % step_min
    if remainder:
        value += timedelta(minutes=step_min - remainder)
    return value


def _value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)
