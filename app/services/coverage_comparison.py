"""Compare funding needs without interpreting a signed loss ratio as cash relief."""

from __future__ import annotations

import math
from typing import Any


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def coverage_comparison(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    before_comparable: bool,
    after_comparable: bool,
) -> dict[str, Any]:
    """The signed DSCR stays factual; a smaller funding gap means relief.

    Debt-service amounts can be compared independently. Subtracting earnings
    from debt service requires a confirmed common currency and reporting basis.
    """
    def snapshot(inputs: dict[str, Any], comparable: bool) -> dict[str, Any]:
        income = _finite(inputs.get("income_annual"))
        debt = _finite(inputs.get("debt_service_annual"))
        service = debt if debt is not None and debt >= 0 else None
        surplus = round(income - service, 2) if comparable and income is not None and service is not None else None
        return {
            "income_annual": income,
            "debt_service_annual": service,
            "dscr": round(income / service, 2) if income is not None and service is not None and service > 0 else None,
            "cash_surplus_annual": surplus,
            "cash_shortfall_annual": max(0.0, -surplus) if surplus is not None else None,
        }

    current = snapshot(before, before_comparable)
    planned = snapshot(after, after_comparable)

    def change(left: float | None, right: float | None) -> float | None:
        return round(right - left, 2) if left is not None and right is not None else None

    surplus_change = change(current["cash_surplus_annual"], planned["cash_surplus_annual"])
    nonpositive = any(row["income_annual"] is not None and row["income_annual"] <= 0 for row in (current, planned))
    negative = any(row["income_annual"] is not None and row["income_annual"] < 0 for row in (current, planned))
    note = None
    if not before_comparable or not after_comparable:
        note = "Confirm a common currency and reporting basis before comparing earnings with debt service. No currency conversion is assumed."
    elif negative:
        note = "DSCR is annual earnings divided by annual debt service. With negative earnings, lower payments can make the signed ratio more negative. Compare the coverage shortfall and payment savings; refinancing alone does not change EBITDA."
    elif nonpositive:
        note = "DSCR is annual earnings divided by annual debt service. With zero earnings, reducing payments leaves the ratio at zero while debt service remains. Compare the coverage shortfall and payment savings; refinancing alone does not change EBITDA."
    return {
        "before": current,
        "after": planned,
        "has_nonpositive_earnings": nonpositive,
        "cash_surplus_change_annual": surplus_change,
        "debt_service_savings_annual": change(planned["debt_service_annual"], current["debt_service_annual"]),
        "shortfall_reduction_annual": change(planned["cash_shortfall_annual"], current["cash_shortfall_annual"]),
        "effect": "unavailable" if surplus_change is None else "cash_relief" if surplus_change > 0 else "cash_worsens" if surplus_change < 0 else "unchanged",
        "ratio_delta_direction_meaningful": before_comparable and after_comparable and not nonpositive and current["dscr"] is not None and planned["dscr"] is not None,
        "note": note,
    }
