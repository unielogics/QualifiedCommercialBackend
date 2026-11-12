"""Staff-configured positive annual NOI goal; never a customer default."""
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GoalWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    amount: Decimal = Field(gt=0, le=Decimal("1000000000000000"), max_digits=18, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("amount", "expected_revision", mode="before")
    @classmethod
    def no_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("Use an explicit number")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value):
        return str(value or "").strip().upper()
