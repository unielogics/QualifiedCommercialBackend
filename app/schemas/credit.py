from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.enums import CreditPullStatus
from app.schemas.common import ORMModel


class CreditPullRequest(BaseModel):
    """Soft-pull form payload.

    Only fields iSoftPull's API actually requires. Phone and email live
    on the User / Client rows already and the bureau doesn't take them.

    SSN is **optional** by design — the borrower-facing form attempts the
    pull on name + address + DOB first, since most consumers can be
    matched on those alone. Only when the bureau returns no-hit does the
    UI ask for SSN and retry. The full SSN, when provided, is forwarded
    to iSoftPull and only the last 4 are persisted.
    """
    legal_first_name: str = Field(min_length=1)
    legal_last_name: str = Field(min_length=1)
    dob: date
    street: str = Field(min_length=1)
    city: str = Field(min_length=1)
    state: str = Field(min_length=2, max_length=2)
    zip: str = Field(min_length=5, max_length=10)
    ssn: str | None = Field(default=None, description="9 digits, no dashes; optional")
    fcra_consent: bool

    @field_validator("ssn")
    @classmethod
    def _ssn_digits_only(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not v.isdigit() or len(v) != 9:
            raise ValueError("SSN must be exactly 9 digits, no dashes")
        return v


class CreditPullRead(ORMModel):
    id: UUID
    client_id: UUID
    status: CreditPullStatus
    fico: int | None
    pulled_at: datetime | None
    expires_at: datetime | None
    # Operator-typed notes from the credit pull — what iSoftpull / the
    # human reviewer captured about THIS report (e.g. "FICO bumped 18
    # points after Capital One $0 charge-off cleared"). Operator-only
    # surface; the frontend gates rendering by role.
    notes: str | None = None
    # Derived (computed in router) — shaping these here means clients can
    # render the "expires in 12 days" pill without doing date math.
    is_expired: bool = False
    days_until_expiry: int | None = None
    expiring_soon: bool = False
