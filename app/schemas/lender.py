from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMModel
from app.services.lender_products import normalize_products


class LenderRead(ORMModel):
    id: UUID
    name: str
    submission_email: str | None = None

    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_title: str | None = None

    # Plain strings, never validated on read: one legacy row holding a value
    # the roster no longer knows must not take the whole list down with it.
    products: list[str] = Field(default_factory=list)
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
    products: list[str] = Field(default_factory=list)
    submission_email: EmailStr | None = None

    @field_validator("products")
    @classmethod
    def _known_products(cls, value: list[str]) -> list[str]:
        return normalize_products(value)

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
    products: list[str] | None = None
    submission_email: EmailStr | None = None

    @field_validator("products")
    @classmethod
    def _known_products(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_products(value)

    contact_name: str | None = Field(default=None, max_length=160)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)
    contact_title: str | None = Field(default=None, max_length=120)

    email_domain: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None
