"""Versioned deterministic repayment instructions, independent of form layout."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LoanBalloon(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    month: int = Field(strict=True, ge=1, le=600)
    type: Literal["amount", "percent_balance"]
    value: float = Field(strict=True, gt=0, allow_inf_nan=False)

    model_config = {"extra": "forbid"}

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A balloon requires a stable identity")
        return value.strip()


class LoanRepaymentConfig(BaseModel):
    amortization_months: int | None = Field(default=None, strict=True, ge=1, le=1200)
    after_balloon: Literal["keep_payment", "recast"] = "keep_payment"
    balloons: list[LoanBalloon] = Field(default_factory=list, max_length=60)

    model_config = {"extra": "forbid"}


def validate_repayment_config(config, term_months, *, structure=None, interest_only_months=0, periods_per_year=12.0, amount=None, rate_pct=None):
    """Use the calculation engine's validation at every API write boundary."""
    if config is None:
        return None
    from app.services.loan_schedule import build_schedule, normalize_repayment_config

    raw = config.model_dump(exclude_none=True) if isinstance(config, LoanRepaymentConfig) else config
    normalized = normalize_repayment_config(raw, term_months, structure=structure,
        interest_only_months=interest_only_months or 0, periods_per_year=periods_per_year)
    if amount is not None:
        build_schedule(amount, rate_pct or 0, term_months, structure=structure,
            interest_only_months=interest_only_months or 0, periods_per_year=periods_per_year, repayment_config=normalized)
    return LoanRepaymentConfig.model_validate(normalized) if normalized is not None else None
