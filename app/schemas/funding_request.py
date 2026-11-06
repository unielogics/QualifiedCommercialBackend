"""Atomic file-scoped funding request corrections."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.use_of_funds import UseOfFundsItem, UseOfFundsPatch


class FundingRequestPatch(UseOfFundsPatch):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    requested_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    expected_use_of_funds_revision: int = Field(ge=0)
    idempotency_key: UUID
    reason: str = Field(min_length=8, max_length=2000)
    payoff_only_alternative: bool = False

    @field_validator("requested_amount", mode="before")
    @classmethod
    def _not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Requested amount must be a monetary value")
        return value


class FundingRequestRead(BaseModel):
    profile_id: UUID
    requested_amount: float | None
    requested_amount_source: str | None
    original_requested_amount: float | None
    revision: int
    use_of_funds_revision: int
    items: list[UseOfFundsItem]
    complete: bool
    can_edit: bool = False
    reason: str | None = None
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None
    payoff_only_alternative: bool = False
