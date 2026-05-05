"""Interest-only payments (Module 9).

Used by Fix & Flip and Bridge (always IO).
Some 40-year DSCR products are 10-year IO + 30-year amortizing.

Formula:  Monthly_IO_Payment = Loan_Amount × Annual_Rate / 12
"""

from __future__ import annotations


def interest_only_payment(principal: float, annual_rate: float) -> float:
    """Monthly interest-only payment."""
    if principal <= 0 or annual_rate <= 0:
        return 0.0
    return round(principal * annual_rate / 12, 2)


def interest_only_total(principal: float, annual_rate: float, term_months: int) -> float:
    """Total interest paid over an IO term (no principal reduction)."""
    return round(interest_only_payment(principal, annual_rate) * term_months, 2)
