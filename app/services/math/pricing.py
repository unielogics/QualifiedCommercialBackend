"""Module 8 — Broker Pricing & Slider Engine.

Separates lender wholesale rate from broker compensation.

  Final_Rate         = Base_Rate − (Discount_Points × 0.25%)
  Total_Broker_Points = Origination_Fee + Discount_Points

The slider in the Broker Dashboard and Client App both call into this.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import (
    DEFAULT_BROKER_ORIGINATION_PCT,
    DEFAULT_MAX_BUY_DOWN_POINTS,
    RATE_DROP_PER_DISCOUNT_POINT,
)


@dataclass(frozen=True)
class PricingQuote:
    base_rate: float            # wholesale, set by Super Admin
    discount_points: float      # paid by client to buy down rate
    final_rate: float           # what the client actually gets
    origination_pct: float      # broker's origination % of loan amount
    total_broker_points: float  # origination_pct (in points) + discount_points
    broker_origination_dollars: float  # origination_pct × loan_amount
    discount_dollars: float     # discount_points × loan_amount
    cash_to_close_pricing: float  # broker_origination + discount


def final_rate_after_buydown(
    base_rate: float,
    discount_points: float,
    rate_drop_per_point: float = RATE_DROP_PER_DISCOUNT_POINT,
) -> float:
    """Apply the buydown to the base rate. Floors at 0."""
    return max(0.0, round(base_rate - discount_points * rate_drop_per_point, 6))


def broker_compensation(
    loan_amount: float,
    discount_points: float,
    origination_pct: float = DEFAULT_BROKER_ORIGINATION_PCT,
) -> tuple[float, float, float]:
    """Returns (origination_dollars, discount_dollars, total_broker_points_pct).

    total_broker_points_pct expresses points as a percent of loan amount
    (origination_pct already in pct form, plus discount points / 100).
    """
    orig_dollars = round(loan_amount * origination_pct, 2)
    disc_dollars = round(loan_amount * (discount_points / 100), 2)
    total_pct = round(origination_pct * 100 + discount_points, 4)
    return orig_dollars, disc_dollars, total_pct


def pricing_quote(
    base_rate: float,
    loan_amount: float,
    discount_points: float = 0.0,
    origination_pct: float = DEFAULT_BROKER_ORIGINATION_PCT,
    max_buy_down: float = DEFAULT_MAX_BUY_DOWN_POINTS,
) -> PricingQuote:
    """Build a complete pricing quote for the slider UI.

    Raises ValueError if discount_points > max_buy_down.
    """
    if discount_points < 0:
        raise ValueError("discount_points cannot be negative")
    if discount_points > max_buy_down:
        raise ValueError(
            f"discount_points ({discount_points}) exceeds max buy down ({max_buy_down})"
        )

    final_rate = final_rate_after_buydown(base_rate, discount_points)
    orig_d, disc_d, total_pct = broker_compensation(loan_amount, discount_points, origination_pct)

    return PricingQuote(
        base_rate=base_rate,
        discount_points=discount_points,
        final_rate=final_rate,
        origination_pct=origination_pct,
        total_broker_points=total_pct,
        broker_origination_dollars=orig_d,
        discount_dollars=disc_d,
        cash_to_close_pricing=round(orig_d + disc_d, 2),
    )
