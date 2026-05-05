"""DSCR calculation (Module 9).

DSCR = Gross Monthly Rent / PITIA

Where PITIA = Principal + Interest + Taxes + Hazard Insurance + (HOA, if any).

Convention: DSCR > 1.0 means the property covers its own debt service.
Pricing uses DSCR_MIN_RATIO_STANDARD (1.00) and DSCR_MIN_RATIO_PREFERRED (1.20).
"""

from __future__ import annotations

from app.services.math.amortization import monthly_payment


def pitia(
    principal: float,
    annual_rate: float,
    term_months: int,
    annual_taxes: float,
    annual_insurance: float,
    monthly_hoa: float = 0.0,
) -> float:
    """Monthly PITIA: P&I + monthly tax + monthly insurance + monthly HOA."""
    pi = monthly_payment(principal, annual_rate, term_months)
    return round(pi + annual_taxes / 12 + annual_insurance / 12 + monthly_hoa, 2)


def dscr(
    monthly_rent: float,
    principal: float,
    annual_rate: float,
    term_months: int,
    annual_taxes: float,
    annual_insurance: float,
    monthly_hoa: float = 0.0,
) -> float:
    """Returns the DSCR ratio. Guards against divide-by-zero."""
    p = pitia(principal, annual_rate, term_months, annual_taxes, annual_insurance, monthly_hoa)
    if p == 0:
        return 0.0
    return round(monthly_rent / p, 4)
