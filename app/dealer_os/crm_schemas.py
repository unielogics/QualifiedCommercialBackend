from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class CompanyContactIn(BaseModel):
    company_name: str = Field(min_length=1, max_length=180)
    contact_name: str = Field(min_length=1, max_length=160)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    industry: str | None = Field(default=None, max_length=80)
    industry_label: str | None = Field(default=None, max_length=180)
    subindustry: str | None = Field(default=None, max_length=120)
    subindustry_label: str | None = Field(default=None, max_length=180)
    naics_code: str | None = Field(default=None, max_length=8)
    naics_label: str | None = Field(default=None, max_length=180)
    industry_entry_id: UUID | None = None
    subindustry_entry_id: UUID | None = None
    activity_entry_id: UUID | None = None
    address: str | None = Field(default=None, max_length=240)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=8)
    zip: str | None = Field(default=None, max_length=12)
    requested_amount: float = Field(gt=0, le=10_000_000)
    use_of_funds: str = Field(min_length=3, max_length=4000)
    locale: Literal["en", "es"] = "en"


class FinderAnswersIn(BaseModel):
    answers: dict[str, Any]


class FundingGoalConfirmIn(BaseModel):
    amount: float = Field(gt=0, le=10_000_000)


class ProductCatalogUpdate(BaseModel):
    pricing: dict[str, Any] | None = None
    copy: dict[str, Any] | None = None
    eligibility: dict[str, Any] | None = None
    disclosures: dict[str, Any] | None = None
    category: str | None = Field(default=None, max_length=48)
    amount_min: float | None = Field(default=None, ge=0, le=50_000_000)
    amount_max: float | None = Field(default=None, ge=0, le=50_000_000)
    term_min_months: int | None = Field(default=None, ge=1, le=480)
    term_max_months: int | None = Field(default=None, ge=1, le=480)
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    effective_at: datetime | None = None
    active: bool | None = None


class ContactAssignmentIn(BaseModel):
    user_id: UUID


class ProductPresentationIn(BaseModel):
    contact_id: UUID | None = None
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    sms_transactional_consent: bool = False
    session_id: UUID | None = None
    program_keys: list[str] = Field(min_length=1, max_length=12)
    locale: Literal["en", "es"] = "en"
    channel: Literal["in_person", "email", "sms"] = "in_person"
    subject: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=4000)


class FieldDeskProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    title: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    display_email: EmailStr | None = None
    short_bio: str | None = Field(default=None, max_length=1000)
    preferred_locale: Literal["en", "es"] | None = None
    card_visible: bool | None = None
    headshot_s3_key: str | None = Field(default=None, max_length=720)

    @field_validator("display_email", mode="before")
    @classmethod
    def blank_email_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("display_name", "title", "phone", "short_bio", "headshot_s3_key")
    @classmethod
    def trim_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class FieldDeskHeadshotUploadInit(BaseModel):
    filename: str = Field(min_length=1, max_length=180)
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
