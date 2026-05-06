from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.enums import CreditPullStatus
from app.schemas.common import ORMModel


class CreditPullRequest(BaseModel):
    legal_first_name: str
    legal_last_name: str
    dob: date
    street: str
    city: str
    state: str = Field(min_length=2, max_length=2)
    zip: str
    phone: str
    email: EmailStr
    last4_ssn: str = Field(min_length=4, max_length=4)
    fcra_consent: bool


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
