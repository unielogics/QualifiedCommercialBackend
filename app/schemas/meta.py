"""Meta endpoints — expose enums + lending limits to the frontends."""

from __future__ import annotations

from pydantic import BaseModel

from app.constants import (
    DSCR_MAX_LTV_CASH_OUT,
    DSCR_MAX_LTV_PURCHASE,
    DSCR_MIN_FICO,
    DSCR_MIN_RATIO_PREFERRED,
    DSCR_MIN_RATIO_STANDARD,
    FF_MAX_ARV_LTV,
    FF_MAX_LTC,
    FF_TERM_MAX_MONTHS,
    FF_TERM_MIN_MONTHS,
    SOFT_PULL_VALIDITY_DAYS,
)


class EnumValue(BaseModel):
    value: str
    label: str


class EnumGroup(BaseModel):
    name: str
    values: list[EnumValue]


class LendingLimits(BaseModel):
    dscr_max_ltv_purchase: float = DSCR_MAX_LTV_PURCHASE
    dscr_max_ltv_cash_out: float = DSCR_MAX_LTV_CASH_OUT
    dscr_min_fico: int = DSCR_MIN_FICO
    dscr_min_ratio_standard: float = DSCR_MIN_RATIO_STANDARD
    dscr_min_ratio_preferred: float = DSCR_MIN_RATIO_PREFERRED
    ff_max_ltc: float = FF_MAX_LTC
    ff_max_arv_ltv: float = FF_MAX_ARV_LTV
    ff_term_min_months: int = FF_TERM_MIN_MONTHS
    ff_term_max_months: int = FF_TERM_MAX_MONTHS
    soft_pull_validity_days: int = SOFT_PULL_VALIDITY_DAYS


class MetaResponse(BaseModel):
    enums: list[EnumGroup]
    limits: LendingLimits
