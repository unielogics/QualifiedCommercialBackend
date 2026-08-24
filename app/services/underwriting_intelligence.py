"""Pure underwriting calculations shared by Audit and operator read models."""

from __future__ import annotations


def calculate_dscr(
    bankable_ebitda: float | None,
    annual_debt_service: float | None,
) -> float | None:
    """Return lender DSCR only when both deterministic inputs are usable."""
    if bankable_ebitda is None or annual_debt_service is None:
        return None
    if annual_debt_service <= 0:
        return None
    return round(float(bankable_ebitda) / float(annual_debt_service), 3)
