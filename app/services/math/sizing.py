"""Loan sizing — single source of truth for the cap rules behind the
calculator. Mirrored on the frontend by qcdesktop/src/lib/eligibility.ts
and qcmobile/src/lib/eligibility.ts; if the math here changes, those two
files must change to match.

Three product surfaces:

- DSCR purchase   loan = min(requested, ltv_cap * value)              ltv_cap = DSCR_MAX_LTV_PURCHASE
- DSCR refi       loan = min(requested, refi_cap * value)             refi_cap = DSCR_MAX_LTV_CASH_OUT
- F&F / Ground Up loan = min(requested, LTC * total_cost, ARV * cap)  total_cost = brv + rehab

Every path returns a SizingResult that names which cap actually bound, so
the UI can render the right "capped at $X — ARV cap binds" hint and the
RangeGauge can highlight the binding tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.constants import (
    DSCR_MAX_LTV_CASH_OUT,
    DSCR_MAX_LTV_PURCHASE,
    FF_MAX_ARV_LTV,
    FF_MAX_LTC,
)
from app.enums import LoanPurpose, LoanType

BindingConstraint = Literal["ltv", "ltc", "arv", "refi-cap", "requested"]


@dataclass(frozen=True)
class SizingResult:
    loan_amount: float
    max_allowed: float
    binding_constraint: BindingConstraint
    clamped: bool
    # Diagnostic ratios — None when the ratio doesn't apply for this product.
    ltv: float | None = None
    ltc: float | None = None
    arv_ltv: float | None = None
    effective_ltv_cap: float | None = None
    total_cost: float | None = None
    cash_to_borrower: float | None = None  # DSCR refi only
    cash_to_close: float | None = None     # F&F / Ground Up only


def _is_reno(loan_type: LoanType) -> bool:
    return loan_type in {LoanType.FIX_AND_FLIP, LoanType.GROUND_UP}


def compute_loan_amount(
    *,
    loan_type: LoanType,
    purpose: LoanPurpose | None = None,
    arv: float | None = None,
    brv: float | None = None,
    rehab_budget: float | None = None,
    payoff: float | None = None,
    requested_amount: float | None = None,
    ltv_tier_cap: float | None = None,
) -> SizingResult:
    """Compute the loan amount and identify which cap binds.

    `ltv_tier_cap` is the credit-tier cap (e.g. 0.65 for the warn tier);
    `None` means "no tier cap, use product default".
    """
    is_refi = purpose in {LoanPurpose.RATE_TERM_REFI, LoanPurpose.CASH_OUT_REFI}

    if _is_reno(loan_type):
        if arv is None or brv is None:
            raise ValueError("Fix & Flip / Ground Up requires arv and brv")
        rehab = float(rehab_budget or 0.0)
        total_cost = float(brv) + rehab
        ltc_cap = float(brv) * 0  # placeholder removed below
        ltc_max = FF_MAX_LTC * total_cost
        arv_max = FF_MAX_ARV_LTV * float(arv)
        max_allowed = min(ltc_max, arv_max)
        requested = float(requested_amount) if requested_amount else max_allowed
        clamped = requested > max_allowed + 0.005
        loan = min(requested, max_allowed)
        # Whichever raw cap equals the binding max is the constraint that
        # bound. If they're within $1 of each other, prefer ARV (the harder
        # collateral cap).
        if abs(arv_max - max_allowed) < 1.0:
            binding: BindingConstraint = "arv"
        else:
            binding = "ltc"
        if not clamped and requested < max_allowed - 0.005:
            binding = "requested"
        return SizingResult(
            loan_amount=round(loan, 2),
            max_allowed=round(max_allowed, 2),
            binding_constraint=binding,
            clamped=clamped,
            ltc=round(loan / total_cost, 4) if total_cost > 0 else None,
            arv_ltv=round(loan / float(arv), 4) if float(arv) > 0 else None,
            total_cost=round(total_cost, 2),
            cash_to_close=round(total_cost - loan, 2),
        )

    # DSCR (and other amortized products) ─ value-based caps.
    if arv is None:
        raise ValueError("DSCR requires property value (arv)")
    product_cap = DSCR_MAX_LTV_CASH_OUT if is_refi else DSCR_MAX_LTV_PURCHASE
    if ltv_tier_cap is not None:
        effective_cap = min(product_cap, float(ltv_tier_cap))
    else:
        effective_cap = product_cap
    max_allowed = effective_cap * float(arv)
    requested = float(requested_amount) if requested_amount else max_allowed
    clamped = requested > max_allowed + 0.005
    loan = min(requested, max_allowed)

    if clamped:
        binding = "refi-cap" if is_refi and effective_cap == DSCR_MAX_LTV_CASH_OUT else "ltv"
    elif requested < max_allowed - 0.005:
        binding = "requested"
    else:
        binding = "ltv"

    cash_to_borrower: float | None = None
    if is_refi and payoff is not None:
        cash_to_borrower = round(loan - float(payoff), 2)

    return SizingResult(
        loan_amount=round(loan, 2),
        max_allowed=round(max_allowed, 2),
        binding_constraint=binding,
        clamped=clamped,
        ltv=round(loan / float(arv), 4) if float(arv) > 0 else None,
        effective_ltv_cap=round(effective_cap, 4),
        cash_to_borrower=cash_to_borrower,
    )
