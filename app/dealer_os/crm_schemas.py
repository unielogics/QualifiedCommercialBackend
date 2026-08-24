from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CompanyContactIn(BaseModel):
    company_name: str = Field(min_length=1, max_length=180)
    contact_name: str = Field(min_length=1, max_length=160)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    industry: str | None = Field(default=None, max_length=80)
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
    disclosures: dict[str, Any] | None = None
    effective_at: datetime | None = None
    active: bool | None = None


class ContactAssignmentIn(BaseModel):
    user_id: UUID


class ProductPresentationIn(BaseModel):
    contact_id: UUID
    session_id: UUID | None = None
    program_keys: list[str] = Field(min_length=1, max_length=8)
    locale: Literal["en", "es"] = "en"
    channel: Literal["in_person", "email", "sms"] = "in_person"
    subject: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=4000)

