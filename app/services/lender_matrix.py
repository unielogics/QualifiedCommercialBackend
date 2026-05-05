"""Module 7 — Lender matrix validators.

Validates user inputs against hard-coded lending limits. Returns a list of
warnings to surface as the AI 'Warning' state in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import (
    DSCR_MAX_LTV_CASH_OUT,
    DSCR_MAX_LTV_PURCHASE,
    DSCR_MIN_FICO,
    DSCR_MIN_RATIO_STANDARD,
    FF_MAX_ARV_LTV,
    FF_MAX_LTC,
    FF_TERM_MAX_MONTHS,
    FF_TERM_MIN_MONTHS,
)
from app.enums import LoanPurpose, LoanType


@dataclass(frozen=True)
class Warning:
    code: str
    message: str
    severity: str  # "block" | "warn"


def _fico_warnings(fico: int | None) -> list[Warning]:
    if fico is None:
        return []
    out = []
    if fico < DSCR_MIN_FICO:
        out.append(
            Warning(
                code="fico_below_min",
                message=f"FICO {fico} is below DSCR minimum of {DSCR_MIN_FICO}.",
                severity="block",
            )
        )
    return out


def validate_dscr(
    *,
    ltv: float,
    purpose: LoanPurpose,
    fico: int | None,
    dscr_ratio: float | None,
) -> list[Warning]:
    out: list[Warning] = []
    out.extend(_fico_warnings(fico))

    cap = DSCR_MAX_LTV_CASH_OUT if purpose == LoanPurpose.CASH_OUT_REFI else DSCR_MAX_LTV_PURCHASE
    if ltv > cap:
        out.append(
            Warning(
                code="ltv_above_cap",
                message=f"LTV {ltv:.0%} exceeds DSCR cap of {cap:.0%} for {purpose.value}.",
                severity="block",
            )
        )
    if dscr_ratio is not None and dscr_ratio < DSCR_MIN_RATIO_STANDARD:
        out.append(
            Warning(
                code="dscr_below_min",
                message=(
                    f"DSCR {dscr_ratio:.2f} is below standard minimum of "
                    f"{DSCR_MIN_RATIO_STANDARD:.2f}."
                ),
                severity="block",
            )
        )
    return out


def validate_fix_flip(
    *,
    ltc: float,
    arv_ltv: float,
    term_months: int,
) -> list[Warning]:
    out: list[Warning] = []
    if ltc > FF_MAX_LTC:
        out.append(
            Warning(
                code="ltc_above_cap",
                message=f"LTC {ltc:.0%} exceeds Fix & Flip cap of {FF_MAX_LTC:.0%}.",
                severity="block",
            )
        )
    if arv_ltv > FF_MAX_ARV_LTV:
        out.append(
            Warning(
                code="arv_ltv_above_cap",
                message=f"ARV LTV {arv_ltv:.0%} exceeds cap of {FF_MAX_ARV_LTV:.0%}.",
                severity="block",
            )
        )
    if term_months < FF_TERM_MIN_MONTHS or term_months > FF_TERM_MAX_MONTHS:
        out.append(
            Warning(
                code="term_out_of_range",
                message=(
                    f"Term {term_months} months is outside Fix & Flip range "
                    f"{FF_TERM_MIN_MONTHS}-{FF_TERM_MAX_MONTHS}."
                ),
                severity="warn",
            )
        )
    return out


def validate_loan(
    *,
    loan_type: LoanType,
    ltv: float | None = None,
    ltc: float | None = None,
    arv_ltv: float | None = None,
    purpose: LoanPurpose | None = None,
    fico: int | None = None,
    dscr_ratio: float | None = None,
    term_months: int | None = None,
) -> list[Warning]:
    """Dispatch to the right validator based on loan type."""
    if loan_type == LoanType.DSCR:
        if ltv is None or purpose is None:
            return [Warning("missing_inputs", "DSCR requires ltv + purpose.", "warn")]
        return validate_dscr(ltv=ltv, purpose=purpose, fico=fico, dscr_ratio=dscr_ratio)

    if loan_type in {LoanType.FIX_AND_FLIP, LoanType.GROUND_UP, LoanType.BRIDGE}:
        if ltc is None or arv_ltv is None or term_months is None:
            return [Warning("missing_inputs", "Flip/Bridge needs ltc + arv_ltv + term_months.", "warn")]
        return validate_fix_flip(ltc=ltc, arv_ltv=arv_ltv, term_months=term_months)

    return []
