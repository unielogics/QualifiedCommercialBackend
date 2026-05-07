from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.enums import LoanType
from app.schemas.common import ORMModel


class LenderRead(ORMModel):
    id: UUID
    name: str
    submission_email: str | None = None

    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_title: str | None = None

    products: list[LoanType] = Field(default_factory=list)
    email_domain: str | None = None
    notes: str | None = None
    is_active: bool = True

    created_at: datetime
    updated_at: datetime


class LenderCreate(BaseModel):
    """Required: name + at least one product. Everything else is
    optional — operators can stub a lender now and fill in
    contact / submission details later."""

    name: str = Field(min_length=1, max_length=160)
    products: list[LoanType] = Field(default_factory=list)
    submission_email: EmailStr | None = None

    contact_name: str | None = Field(default=None, max_length=160)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)
    contact_title: str | None = Field(default=None, max_length=120)

    email_domain: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True


class LenderUpdate(BaseModel):
    """Partial update — every field optional. None means 'don't
    touch'; explicit empty string ('' / [] for products) means
    'clear'."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    products: list[LoanType] | None = None
    submission_email: EmailStr | None = None

    contact_name: str | None = Field(default=None, max_length=160)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)
    contact_title: str | None = Field(default=None, max_length=120)

    email_domain: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None
