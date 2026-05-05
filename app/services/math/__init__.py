"""Module 9 — Internal Amortization & DSCR Math Engine.

All functions are pure: deterministic, no I/O, side-effect-free.
The simulator slider (mobile + DCR HUD sim) calls these via /loans/{id}/recalc.
"""

from app.services.math.amortization import (
    amortization_schedule,
    monthly_payment,
    total_interest,
)
from app.services.math.dscr import dscr, pitia
from app.services.math.interest_only import interest_only_payment, interest_only_total
from app.services.math.pricing import (
    broker_compensation,
    final_rate_after_buydown,
    pricing_quote,
)

__all__ = [
    "amortization_schedule",
    "broker_compensation",
    "dscr",
    "final_rate_after_buydown",
    "interest_only_payment",
    "interest_only_total",
    "monthly_payment",
    "pitia",
    "pricing_quote",
    "total_interest",
]
