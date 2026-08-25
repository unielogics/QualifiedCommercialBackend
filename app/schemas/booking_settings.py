from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    reminder_sms_enabled: bool = True
    reminder_sms_minutes_before: int = Field(default=120, ge=15, le=10080)
    google_meet_enabled: bool = True
    timezone: str = Field(default="America/New_York", min_length=3, max_length=80)
    available_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    start_time: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(default="17:00", pattern=r"^\d{2}:\d{2}$")
    logo_s3_key: str | None = None
    profile_photo_s3_key: str | None = None

    @model_validator(mode="after")
    def _validate_booking_window(self) -> "UserBookingSettingsBase":
        days = sorted(set(self.available_days))
        if any(day < 0 or day > 6 for day in days):
            raise ValueError("available_days must use 0=Sunday through 6=Saturday")
        self.available_days = days

        start_h, start_m = [int(x) for x in self.start_time.split(":")]
        end_h, end_m = [int(x) for x in self.end_time.split(":")]
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
            raise ValueError("start_time and end_time must be valid HH:MM values")
        if end - start < self.duration_min:
            raise ValueError("Booking end time must allow at least one meeting slot")
        if self.buffer_before_min + self.duration_min + self.buffer_after_min > 360:
            raise ValueError("Meeting length and buffers cannot exceed six hours")
        return self


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
