"""Standard amortization (Module 9).

Formula:  M = P · i(1+i)^n / ((1+i)^n - 1)
Where:
  M = monthly payment (P&I)
  P = principal (loan amount)
  i = monthly interest rate (annual / 12)
  n = total months
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AmortRow:
    month: int
    payment: float
    interest: float
    principal: float
    balance: float


def monthly_payment(principal: float, annual_rate: float, term_months: int) -> float:
    """Returns the monthly P&I payment."""
    if principal <= 0 or term_months <= 0:
        return 0.0
    if annual_rate == 0:
        return principal / term_months
    i = annual_rate / 12
    factor = (1 + i) ** term_months
    return principal * (i * factor) / (factor - 1)


def amortization_schedule(
    principal: float, annual_rate: float, term_months: int
) -> list[AmortRow]:
    """Returns the full amortization schedule, one row per month."""
    pmt = monthly_payment(principal, annual_rate, term_months)
    i = annual_rate / 12
    balance = principal
    rows: list[AmortRow] = []
    for m in range(1, term_months + 1):
        interest_due = balance * i
        principal_paid = pmt - interest_due
        balance = max(0.0, balance - principal_paid)
        rows.append(
            AmortRow(
                month=m,
                payment=round(pmt, 2),
                interest=round(interest_due, 2),
                principal=round(principal_paid, 2),
                balance=round(balance, 2),
            )
        )
    return rows


def total_interest(principal: float, annual_rate: float, term_months: int) -> float:
    """Total interest paid over the life of the loan."""
    pmt = monthly_payment(principal, annual_rate, term_months)
    return round(pmt * term_months - principal, 2)
