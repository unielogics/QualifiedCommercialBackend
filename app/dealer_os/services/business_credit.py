"""Business credit from observed payment behaviour.

A commercial bureau file is not required to say something true about how a
business pays. The ledger already shows every recurring obligation and whether
it was met: a tradeline is a counterparty billing this business on a repeating
basis, and "on time" is whether the expected payment actually appeared in the
month it was due.

Deterministic and explainable — every input is a row an operator can click
through to. No model call.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from .vendors import VendorRollup

# Categories that behave like a credit relationship: someone extends terms and
# expects regular payment. A one-off trade purchase is not a tradeline.
_TRADELINE_CATEGORIES = frozenset(
    {"loan", "floorplan", "credit_card", "insurance", "rent", "utilities"}
)

_MIN_TRADELINE_MONTHS = 2


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def select_tradelines(vendors: Sequence[VendorRollup]) -> list[VendorRollup]:
    """The vendors that behave like credit relationships: repeating outbound
    obligations in a credit-shaped category. Shared by summarize() and the
    per-tradeline rows endpoint so the two can never disagree on what counts."""
    return [
        v
        for v in vendors
        if v.direction < 0
        and v.category in _TRADELINE_CATEGORIES
        and v.months >= _MIN_TRADELINE_MONTHS
    ]


def on_time_pct_for(v: VendorRollup) -> float | None:
    """Share of expected monthly payments actually observed for ONE tradeline —
    the same arithmetic summarize() aggregates across the whole file."""
    span = _months_between(v.first_seen, v.last_seen) + 1
    if span <= 0:
        return None
    return round(100.0 * min(v.months, span) / span, 1)


def summarize(
    vendors: Sequence[VendorRollup],
    *,
    today: date,
    nsf_6mo: int = 0,
) -> dict:
    """Build the business-credit picture from the vendor rollup.

    on_time_pct is the share of expected monthly payments that actually
    appeared: a tradeline seen across N months but present in only M of them
    has (N - M) misses. This is intentionally strict — a lender reading the
    statements will do the same arithmetic."""
    tradelines = select_tradelines(vendors)

    expected = 0
    observed = 0
    oldest: int | None = None
    for v in tradelines:
        span = _months_between(v.first_seen, v.last_seen) + 1
        expected += span
        observed += min(v.months, span)
        age = _months_between(v.first_seen, today)
        oldest = age if oldest is None else max(oldest, age)

    on_time = round(100.0 * observed / expected, 1) if expected else None
    monthly = round(sum(abs(v.monthly_average) for v in tradelines), 2)
    months_observed = max((v.months for v in tradelines), default=0)

    factors: list[str] = []
    if not tradelines:
        factors.append("No repeating credit obligations found in the ledger yet.")
    else:
        factors.append(f"{len(tradelines)} tradeline(s) paying about ${monthly:,.0f} a month.")
        if on_time is not None:
            factors.append(f"{on_time:.0f}% of expected monthly payments were observed.")
        if oldest is not None:
            factors.append(f"Oldest relationship is {oldest} month(s) old.")
    if nsf_6mo:
        factors.append(f"{nsf_6mo} NSF/overdraft event(s) in the last 6 months — lenders weight this heavily.")

    # Grade: payment consistency first, then depth of file, then clean activity.
    grade: str | None = None
    if tradelines:
        score = 0
        if on_time is not None:
            score += 3 if on_time >= 98 else 2 if on_time >= 90 else 1 if on_time >= 75 else 0
        score += 2 if len(tradelines) >= 4 else 1 if len(tradelines) >= 2 else 0
        score += 1 if (oldest or 0) >= 12 else 0
        score -= 2 if nsf_6mo >= 3 else 1 if nsf_6mo else 0
        grade = "A" if score >= 5 else "B" if score >= 3 else "C" if score >= 1 else "D"

    return {
        "tradelines": len(tradelines),
        "on_time_pct": on_time,
        "months_observed": months_observed,
        "total_monthly_obligations": monthly,
        "nsf_6mo": nsf_6mo,
        "oldest_tradeline_months": oldest,
        "grade": grade,
        "factors": factors,
    }
