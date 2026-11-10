from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field, field_validator

from app.schemas.application_profile import ApplicationRoomAccess
from app.services.business_profile import BusinessProfileFields


class ClientApplicationFields(BusinessProfileFields):
    full_name: str | None = Field(default=None, max_length=180)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    business_name: str | None = Field(default=None, max_length=180)
    loan_purpose: str | None = Field(default=None, max_length=255)
    requested_loan_amount: float | None = Field(default=None, gt=0, le=999999999999, allow_inf_nan=False)
    years_in_business: float | None = Field(default=None, ge=0, le=150, allow_inf_nan=False)
    target_property_address: str | None = Field(default=None, max_length=320)
    transaction_type: str | None = Field(default=None, max_length=80)
    estimated_value_or_purchase_price: float | None = Field(default=None, gt=0, le=999999999999, allow_inf_nan=False)
    monthly_rent: float | None = Field(default=None, ge=0, le=999999999999, allow_inf_nan=False)
    funding_need: str | None = Field(default=None, max_length=80)

    @field_validator("funding_need")
    @classmethod
    def valid_classification(cls, value: str | None, info) -> str | None:
        from app.services.main_street_programs import MAIN_STREET_INTENTS

        if value is not None and value not in MAIN_STREET_INTENTS:
            raise ValueError("Choose an available industry or funding need.")
        if info.field_name == "funding_need" and value in {"real_estate", "dealer"}:
            raise ValueError("Ask your advisor to change this file's type.")
        return value

    @field_validator("full_name", "email", "phone", "business_name", "loan_purpose", "target_property_address", "transaction_type", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str | None) -> str | None:
        if value:
            from app.dealer_os.services.consent_delivery import normalize_phone

            value = normalize_phone(value)
            if not value:
                raise ValueError("Enter a complete mobile number, including country code outside the US.")
        return value


class ClientApplicationRead(ClientApplicationFields):
    vertical: Literal["real_estate", "main_street", "dealer", "mca"]
    submitted_at: datetime | None = None
    requested_amount_editable: bool = True
    funding_need_editable: bool = True


class ClientApplicationPatch(ClientApplicationFields, ApplicationRoomAccess):
    submit: bool = False
