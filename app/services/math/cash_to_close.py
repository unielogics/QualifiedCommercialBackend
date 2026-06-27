"""Cash-to-close math helpers.

`cash_to_close_pricing` is intentionally narrow: broker origination plus
discount points. Full borrower cash to close also includes equity/down
payment and settlement costs.
"""

from __future__ import annotations

from app.enums import LoanPurpose
from app.services.math.sizing import SizingResult


def borrower_equity_required(
    *,
    sizing: SizingResult | None,
    purpose: LoanPurpose | None,
    amount: float,
    arv: float | None,
) -> float:
    """Borrower principal/equity gap before settlement costs."""
    is_refi = purpose in {LoanPurpose.RATE_TERM_REFI, LoanPurpose.CASH_OUT_REFI}
    if sizing is not None:
        if sizing.cash_to_close is not None:
            return max(0.0, float(sizing.cash_to_close))
        if sizing.cash_to_borrower is not None:
            return max(0.0, -float(sizing.cash_to_borrower))
    if is_refi:
        return 0.0
    if arv is not None:
        return max(0.0, float(arv) - float(amount))
    return 0.0


def total_cash_to_close(
    *,
    borrower_equity: float,
    hud_total: float,
    discount_dollars: float,
    lender_fees: float = 0.0,
    reserves_required: float = 0.0,
    construction_holdback: float = 0.0,
) -> float:
    """Full borrower cash requirement at closing.

    HUD already includes broker origination. Discount points are tracked
    separately by the pricing engine, so add only discount_dollars here to
    avoid double-counting origination.
    """
    return round(
        borrower_equity
        + hud_total
        + discount_dollars
        + lender_fees
        + reserves_required
        - construction_holdback,
        2,
    )
