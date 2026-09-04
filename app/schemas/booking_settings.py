from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class BookingVideo(BaseModel):
    """One video in the host's library.

    The key is what a message points at. It is slugified rather than free text so
    a placeholder stays valid when the label is edited, and it is capped short
    because it appears inside a {video_key} token the host types by hand.
    """

    key: str = Field(min_length=1, max_length=24, pattern=r"^[a-z0-9](?:[a-z0-9_]*[a-z0-9])?$")
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=500)

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.lower().startswith(("http://", "https://")):
            raise ValueError("A video link must start with http:// or https://")
        return cleaned


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
    #: What each SMS reminder says, keyed by its minutes-before as a string.
    #: Blank or absent means the default wording. Placeholders: {time}, {name},
    #: {rep}, {join_link}. The opt-out notice is appended by the sender and is
    #: deliberately not authorable here.
    reminder_sms_messages: dict[str, str] = Field(default_factory=dict)
    #: Email reminder text, keyed the same way; each value {"subject", "body"}.
    reminder_email_messages: dict[str, dict[str, str]] = Field(default_factory=dict)
    #: Confirmation wording: email_subject, email_body, sms, pin_email_subject,
    #: pin_email_body. Blank means the default.
    confirmation_messages: dict[str, str] = Field(default_factory=dict)
    #: Pre-call prep: draft file + secure room on every booking, and the nudge
    #: sequence. precall_messages holds precall_block plus per-step overrides
    #: {"nudge_1": {after_hours, channel, email_subject, email_body, sms},
    #:  "nudge_2": {before_hours, channel, email_subject, email_body, sms}}.
    precall_enabled: bool = True
    precall_messages: dict[str, object] = Field(default_factory=dict)
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
    #: The short video the client watches before the call. Rendered by {video}.
    precall_video_url: str | None = Field(default=None, max_length=500)
    #: The video library. Each entry is {key, label, url}; a message references a
    #: video by its key, so re-recording one does not break the templates that
    #: point at it.
    precall_videos: list[BookingVideo] = Field(default_factory=list, max_length=12)

    @field_validator("precall_videos")
    @classmethod
    def _unique_video_keys(cls, videos: list[BookingVideo]) -> list[BookingVideo]:
        keys = [v.key for v in videos]
        duplicated = sorted({k for k in keys if keys.count(k) > 1})
        if duplicated:
            raise ValueError(f"Each video needs its own key. Repeated: {', '.join(duplicated)}")
        return videos
    logo_s3_key: str | None = None
    profile_photo_s3_key: str | None = None

    def _clean_message_templates(self, scheduled_sms: set[str]) -> None:
        """Trim, drop blanks, cap lengths and refuse {pin} outside the two
        messages that deliver it. Runs inside the main validator."""
        from app.services import message_render

        email_keys = {str(value) for value in self.reminder_email_minutes}
        emails: dict[str, dict[str, str]] = {}
        for key, item in (self.reminder_email_messages or {}).items():
            if key not in email_keys or not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            body = str(item.get("body") or "").strip()
            if not subject and not body:
                continue
            if len(subject) > 160 or len(body) > 4000:
                raise ValueError("An email reminder is limited to a 160-character subject and 4000-character body")
            for text_ in (subject, body):
                if message_render.disallowed_placeholders(text_):
                    raise ValueError("{pin} may only be used in the confirmation SMS and the PIN email")
            emails[key] = {"subject": subject, "body": body}
        self.reminder_email_messages = emails

        allowed_pin = {"sms", "pin_email_subject", "pin_email_body"}
        confirmation: dict[str, str] = {}
        for key in ("email_subject", "email_body", "sms", "pin_email_subject", "pin_email_body"):
            value = str((self.confirmation_messages or {}).get(key) or "").strip()
            if not value:
                continue
            limit = 400 if key == "sms" else 160 if key.endswith("subject") else 4000
            if len(value) > limit:
                raise ValueError(f"confirmation_messages.{key} must be {limit} characters or fewer")
            if message_render.disallowed_placeholders(value, allow_pin=key in allowed_pin):
                raise ValueError("{pin} may only be used in the confirmation SMS and the PIN email")
            confirmation[key] = value
        self.confirmation_messages = confirmation

        precall: dict[str, object] = {}
        raw = self.precall_messages or {}
        block = str(raw.get("precall_block") or "").strip()
        if block:
            if len(block) > 2000 or message_render.disallowed_placeholders(block):
                raise ValueError("precall_block must be 2000 characters or fewer and may not contain {pin}")
            precall["precall_block"] = block
        line = str(raw.get("reminder_precall_line") or "").strip()
        if line:
            if len(line) > 300 or message_render.disallowed_placeholders(line):
                raise ValueError("reminder_precall_line must be 300 characters or fewer and may not contain {pin}")
            precall["reminder_precall_line"] = line
        for step in ("nudge_1", "nudge_2"):
            item = raw.get(step)
            if not isinstance(item, dict):
                continue
            cleaned_step: dict[str, object] = {}
            for hours_key, lo, hi in (("after_hours", 1, 240), ("before_hours", 2, 240), ("fallback_before_hours", 1, 48)):
                if item.get(hours_key) not in (None, ""):
                    hours = float(item[hours_key])
                    if hours < lo or hours > hi:
                        raise ValueError(f"{step}.{hours_key} must be between {lo} and {hi} hours")
                    cleaned_step[hours_key] = hours
            channel = str(item.get("channel") or "").strip().lower()
            if channel:
                if channel not in {"email", "sms", "both"}:
                    raise ValueError(f"{step}.channel must be email, sms or both")
                cleaned_step["channel"] = channel
            for text_key, limit in (("email_subject", 160), ("email_body", 4000), ("sms", 400)):
                value = str(item.get(text_key) or "").strip()
                if not value:
                    continue
                if len(value) > limit or message_render.disallowed_placeholders(value):
                    raise ValueError(f"{step}.{text_key} must be {limit} characters or fewer and may not contain {{pin}}")
                cleaned_step[text_key] = value
            if cleaned_step:
                precall[step] = cleaned_step
        self.precall_messages = precall

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

        # Drop messages whose reminder no longer exists, so removing a reminder
        # from the middle of the schedule cannot leave text that silently
        # reappears if that timing is added back later.
        scheduled = {str(value) for value in self.reminder_sms_minutes}
        cleaned: dict[str, str] = {}
        for key, message in (self.reminder_sms_messages or {}).items():
            if key not in scheduled:
                continue
            body = (message or "").strip()
            if not body:
                continue
            # One segment of headroom for the appended opt-out notice.
            if len(body) > 400:
                raise ValueError("A reminder message must be 400 characters or fewer")
            cleaned[key] = body
        self.reminder_sms_messages = cleaned
        self._clean_message_templates(scheduled)

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
