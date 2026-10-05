"""Canonical internal deal-economics calculations.

Approved amount, accepted amount, and funded amount are different lifecycle
facts.  QC earnings are forecast only from the amount the client accepted,
plus any fixed consulting fee.  Keeping this calculation here prevents the
operator table, underwriting workspace, and calendar projection from drifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

MONEY_QUANTUM = Decimal("0.01")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _money(value: Decimal | None) -> Decimal | None:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP) if value is not None else None


@dataclass(frozen=True)
class DealEarnings:
    """An explicit breakdown of the two allowed earnings components.

    ``total`` is ``None`` when neither component can be calculated.  A supplied
    zero percent or zero consulting fee is a real forecast and therefore
    produces ``0.00``.  A fixed consulting fee remains calculable even when the
    accepted amount has not been recorded yet.
    """

    accepted_amount: Decimal | None
    origination_points: Decimal | None
    origination_earnings: Decimal | None
    consulting_fee: Decimal | None
    total: Decimal | None


def calculate_deal_earnings(
    *,
    accepted_amount: Any,
    origination_points: Any,
    consulting_fee: Any,
) -> DealEarnings:
    accepted = _decimal(accepted_amount)
    points = _decimal(origination_points)
    consulting = _money(_decimal(consulting_fee))
    origination = (
        _money(accepted * points / Decimal("100"))
        if accepted is not None and points is not None
        else None
    )
    components = [value for value in (origination, consulting) if value is not None]
    total = _money(sum(components, Decimal("0"))) if components else None
    return DealEarnings(
        accepted_amount=_money(accepted),
        origination_points=points,
        origination_earnings=origination,
        consulting_fee=consulting,
        total=total,
    )


def optional_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None
