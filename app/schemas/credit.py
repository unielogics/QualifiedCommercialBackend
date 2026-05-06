from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.enums import CreditPullStatus
from app.schemas.common import ORMModel


class CreditPullRequest(BaseModel):
    """Soft-pull form payload.

    Only fields iSoftPull's API actually requires. Phone and email used to
    live here for our own records; we drop them — they're already on the
    User (Clerk) and Client records and the bureau call doesn't take them.

    SSN is the full 9 digits (no dashes). The router persists only the
    last 4 to credit_pulls.last4_ssn for the FCRA paper trail; the full
    SSN never lands in the database or in logs.
    """
    legal_first_name: str = Field(min_length=1)
    legal_last_name: str = Field(min_length=1)
    dob: date
    street: str = Field(min_length=1)
    city: str = Field(min_length=1)
    state: str = Field(min_length=2, max_length=2)
    zip: str = Field(min_length=5, max_length=10)
    ssn: str = Field(min_length=9, max_length=9, description="9 digits, no dashes")
    fcra_consent: bool

    @field_validator("ssn")
    @classmethod
    def _ssn_digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("SSN must be 9 digits, no dashes")
        return v


class CreditPullRead(ORMModel):
    id: UUID
    client_id: UUID
    status: CreditPullStatus
    fico: int | None
    pulled_at: datetime | None
    expires_at: datetime | None
    # Derived (computed in router) — shaping these here means clients can
    # render the "expires in 12 days" pill without doing date math.
    is_expired: bool = False
    days_until_expiry: int | None = None
    expiring_soon: bool = False
