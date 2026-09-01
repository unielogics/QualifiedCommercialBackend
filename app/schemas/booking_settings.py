from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BookingBlockedInterval(BaseModel):
    weekday: int | None = Field(default=None, ge=0, le=6)
    on_date: date | None = None
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    label: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _validate_interval(self) -> BookingBlockedInterval:
        if (self.weekday is None) == (self.on_date is None):
            raise ValueError("Choose either a recurring weekday or one calendar date")
        start_h, start_m = [int(value) for value in self.start_time.split(":")]
        end_h, end_m = [int(value) for value in self.end_time.split(":")]
        if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
            raise ValueError("Blocked times must be valid HH:MM values")
        if (end_h * 60 + end_m) <= (start_h * 60 + start_m):
            raise ValueError("A blocked interval must end after it starts")
        self.label = self.label.strip() if self.label else None
        return self


class BookingTimeRange(BaseModel):
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")

    @model_validator(mode="after")
    def _validate_range(self) -> BookingTimeRange:
        start = _time_minutes(self.start_time, "Schedule")
        end = _time_minutes(self.end_time, "Schedule")
        if end <= start:
            raise ValueError("A schedule range must end after it starts")
        return self


class BookingDaySchedule(BaseModel):
    weekday: int = Field(ge=0, le=6)
    intervals: list[BookingTimeRange] = Field(default_factory=list, max_length=6)


class UserBookingSettingsBase(BaseModel):
    enabled: bool = False
    slug: str | None = Field(default=None, min_length=3, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str | None = Field(default=None, max_length=140)
    intro: str | None = Field(default=None, max_length=600)
    primary_color: str = Field(default="#5eead4", pattern=r"^#[0-9A-Fa-f]{6}$")
    background_color: str = Field(default="#05070d", pattern=r"^#[0-9A-Fa-f]{6}$")
    duration_min: int = Field(default=20, ge=15, le=180)
    buffer_before_min: int = Field(default=5, ge=0, le=120)
    buffer_after_min: int = Field(default=5, ge=0, le=120)
    confirmation_email_enabled: bool = True
    confirmation_sms_enabled: bool = True
    reminder_email_enabled: bool = True
    reminder_email_minutes_before: int = Field(default=1440, ge=15, le=10080)
    reminder_email_minutes: list[int] = Field(default_factory=lambda: [1440], max_length=5)
    reminder_sms_enabled: bool = True
    reminder_sms_minutes_before: int = Field(default=120, ge=15, le=10080)
    reminder_sms_minutes: list[int] = Field(default_factory=lambda: [120], max_length=5)
    google_meet_enabled: bool = True
    timezone: str = Field(default="America/New_York", min_length=3, max_length=80)
    available_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    weekly_schedule: list[BookingDaySchedule] = Field(default_factory=list, max_length=7)
    advance_booking_window_enabled: bool = False
    minimum_notice_days: int = Field(default=2, ge=0, le=365)
    maximum_advance_days: int = Field(default=5, ge=1, le=365)
    blocked_intervals: list[BookingBlockedInterval] = Field(default_factory=list, max_length=56)
    booking_questions: dict[Literal["business_name", "phone", "requested_amount", "bank_statement"], bool] = Field(
        default_factory=lambda: {
            "business_name": True,
            "phone": True,
            "requested_amount": True,
            "bank_statement": False,
        }
    )
    no_show_follow_up_enabled: bool = True
    morning_digest_enabled: bool = True
    missing_outcome_reminder_hours: int = Field(default=48, ge=1, le=720)
    start_time: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(default="17:00", pattern=r"^\d{2}:\d{2}$")
    logo_s3_key: str | None = None
    profile_photo_s3_key: str | None = None

    @model_validator(mode="after")
    def _validate_booking_window(self) -> UserBookingSettingsBase:
        if self.maximum_advance_days < self.minimum_notice_days:
            raise ValueError("Latest booking day must be on or after the earliest booking day")

        for field_name in ("reminder_email_minutes", "reminder_sms_minutes"):
            values = getattr(self, field_name)
            if any(value < 15 or value > 10080 for value in values):
                raise ValueError(f"{field_name} values must be between 15 minutes and 7 days")
            normalized = sorted(set(values), reverse=True)
            if len(normalized) != len(values):
                raise ValueError(f"{field_name} cannot contain duplicate reminder times")
            setattr(self, field_name, normalized)

        if self.reminder_email_enabled and not self.reminder_email_minutes:
            raise ValueError("At least one email reminder time is required when email reminders are enabled")
        if self.reminder_sms_enabled and not self.reminder_sms_minutes:
            raise ValueError("At least one SMS reminder time is required when SMS reminders are enabled")

        days = sorted(set(self.available_days))
        if any(day < 0 or day > 6 for day in days):
            raise ValueError("available_days must use 0=Sunday through 6=Saturday")
        self.available_days = days

        start = _time_minutes(self.start_time, "Booking")
        end = _time_minutes(self.end_time, "Booking")
        if end - start < self.duration_min:
            raise ValueError("Booking end time must allow at least one meeting slot")
        if self.buffer_before_min + self.duration_min + self.buffer_after_min > 360:
            raise ValueError("Meeting length and buffers cannot exceed six hours")

        schedules_by_day: dict[int, BookingDaySchedule] = {}
        for schedule in self.weekly_schedule:
            if schedule.weekday in schedules_by_day:
                raise ValueError(f"Weekly schedule contains weekday {schedule.weekday} more than once")
            intervals = sorted(schedule.intervals, key=lambda item: item.start_time)
            for interval in intervals:
                interval_start = _time_minutes(interval.start_time, "Schedule")
                interval_end = _time_minutes(interval.end_time, "Schedule")
                if interval_end - interval_start < self.duration_min:
                    raise ValueError("Every schedule range must fit the default meeting duration")
            for previous, current in zip(intervals, intervals[1:], strict=False):
                if current.start_time < previous.end_time:
                    raise ValueError(f"Schedule ranges cannot overlap for weekday {schedule.weekday}")
            schedule.intervals = intervals
            schedules_by_day[schedule.weekday] = schedule

        if self.weekly_schedule:
            self.weekly_schedule = [
                schedules_by_day[weekday]
                for weekday in sorted(schedules_by_day)
            ]
            active_ranges = [
                interval
                for schedule in self.weekly_schedule
                for interval in schedule.intervals
            ]
            self.available_days = [
                schedule.weekday
                for schedule in self.weekly_schedule
                if schedule.intervals
            ]
            if active_ranges:
                self.start_time = min(item.start_time for item in active_ranges)
                self.end_time = max(item.end_time for item in active_ranges)
                start = _time_minutes(self.start_time, "Booking")
                end = _time_minutes(self.end_time, "Booking")

        grouped: dict[str, list[BookingBlockedInterval]] = {}
        for interval in self.blocked_intervals:
            interval_start = _time_minutes(interval.start_time, "Blocked")
            interval_end = _time_minutes(interval.end_time, "Blocked")
            weekday = (
                interval.weekday
                if interval.weekday is not None
                else (interval.on_date.weekday() + 1) % 7
            )
            if self.weekly_schedule:
                schedule = schedules_by_day.get(weekday)
                schedule_intervals = schedule.intervals if schedule else []
                if schedule_intervals:
                    inside_schedule = any(
                        interval_start >= _time_minutes(item.start_time, "Schedule")
                        and interval_end <= _time_minutes(item.end_time, "Schedule")
                        for item in schedule_intervals
                    )
                    if not inside_schedule:
                        raise ValueError("Blocked intervals must remain inside one configured range for that day")
            else:
                schedule_start, schedule_end = start, end
                if interval_start < schedule_start or interval_end > schedule_end:
                    raise ValueError("Blocked intervals must remain inside the daily booking start and end times")
            group_key = (
                f"weekday:{interval.weekday}"
                if interval.weekday is not None
                else f"date:{interval.on_date.isoformat()}"
            )
            grouped.setdefault(group_key, []).append(interval)

        for group_key, intervals in grouped.items():
            intervals.sort(key=lambda item: item.start_time)
            for previous, current in zip(intervals, intervals[1:], strict=False):
                if current.start_time < previous.end_time:
                    raise ValueError(f"Blocked intervals cannot overlap for {group_key}")
        self.blocked_intervals = sorted(
            self.blocked_intervals,
            key=lambda item: (
                item.on_date.isoformat() if item.on_date else "",
                item.weekday if item.weekday is not None else 8,
                item.start_time,
                item.end_time,
            ),
        )
        return self


def _time_minutes(value: str, label: str) -> int:
    hour, minute = [int(item) for item in value.split(":")]
    if hour > 23 or minute > 59:
        raise ValueError(f"{label} times must be valid HH:MM values")
    return hour * 60 + minute


class UserBookingSettingsUpdate(UserBookingSettingsBase):
    pass


class UserBookingSettingsRead(UserBookingSettingsBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    logo_url: str | None = None
    profile_photo_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BookingAssetUploadInitRequest(BaseModel):
    content_type: Literal["image/png", "image/jpeg"] = "image/png"


class BookingAssetUploadInitResponse(BaseModel):
    s3_key: str
    upload_url: str | None
