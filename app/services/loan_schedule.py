"""One loan schedule for every screen that prices a loan.

A loan is an amount, a rate, a term, a structure and an interest-only
period. Under three years the house default is simple interest (interest on
the original principal every period, principal spread evenly after any
interest-only months); at three years and over it is amortizing (a level
payment on the declining balance after the interest-only months); a loan
can also be interest-only for its whole term, principal due at maturity.
The schedule lists every period's payment, interest, principal and balance,
and the summary says what the plan, the terms and the package print: the
payment during the interest-only months, the payment after, the first-year
debt service and the payment coverage stands on (the higher of the two).
"""

from __future__ import annotations

import math
from typing import Any, Literal

Structure = Literal["amortizing", "simple_interest", "interest_only"]
STRUCTURES: tuple[str, ...] = ("amortizing", "simple_interest", "interest_only")
STRUCTURE_LABELS: dict[str, str] = {
    "amortizing": "Amortizing",
    "simple_interest": "Simple interest",
    "interest_only": "Interest-only",
}
#: Under this many months the default is simple interest; at or over, amortizing.
AMORTIZING_FROM_MONTHS = 36
MAX_PERIODS = 600


def default_structure(term_months: int | None) -> str:
    return (
        "amortizing"
        if term_months and int(term_months) >= AMORTIZING_FROM_MONTHS
        else "simple_interest"
    )


def normalize_structure(value: Any, term_months: int | None) -> str:
    text = str(value or "").strip().lower()
    if text in STRUCTURES:
        return text
    return default_structure(term_months)


def _r2(value: float) -> float:
    return round(value + 0.0, 2)


def _half_up(value: float) -> int:
    """Period counts round half up (floor(x + .5)): identical in Python and in
    the browser preview, where Python's ties-to-even round() would not be."""
    return int(math.floor(value + 0.5))


def level_payment(principal: float, periodic_rate: float, periods: int) -> float:
    if periods <= 0:
        return 0.0
    if periodic_rate <= 0:
        return principal / periods
    return principal * periodic_rate / (1 - (1 + periodic_rate) ** (-periods))


def build_schedule(
    amount: float | None,
    rate_pct: float | None,
    term_months: int | None,
    *,
    structure: str | None = None,
    interest_only_months: int | None = 0,
    periods_per_year: float = 12.0,
) -> dict[str, Any] | None:
    """The schedule and its summary, or None without an amount and a term.

    Periods follow `periods_per_year` (12 for monthly; the client terms also
    price weekly and daily); the interest-only months convert to periods.
    """
    principal = float(amount or 0)
    if principal <= 0:
        return None
    months = int(term_months or 0)
    kind = normalize_structure(structure, months or None)
    if kind != "interest_only" and months <= 0:
        return None
    ppy = float(periods_per_year or 12.0)
    rate = float(rate_pct or 0.0) / 100.0
    periodic_rate = rate / ppy
    if kind == "interest_only":
        periods = max(1, _half_up(months * ppy / 12.0)) if months > 0 else _half_up(ppy)
        io_periods = periods
    else:
        periods = max(1, _half_up(months * ppy / 12.0))
        io_months = min(max(0, int(interest_only_months or 0)), months)
        io_periods = min(periods, _half_up(io_months * ppy / 12.0))
        if io_periods >= periods:
            # Interest-only for the whole term is the interest-only structure.
            kind, io_periods = "interest_only", periods
    periods = min(periods, MAX_PERIODS)
    rows: list[dict[str, Any]] = []
    balance = principal
    remaining = periods - io_periods
    interest_flat = principal * periodic_rate
    if kind == "amortizing":
        after_payment = level_payment(principal, periodic_rate, remaining) if remaining else 0.0
    elif kind == "simple_interest":
        after_payment = (principal / remaining + interest_flat) if remaining else 0.0
    else:
        after_payment = 0.0
    # Every row is in cents and the balance is the running difference, so the
    # schedule adds up on paper; the last principal row takes the rounding
    # residue and the balance ends at exactly zero (or the balloon).
    for index in range(1, periods + 1):
        last = index == periods
        if index <= io_periods:
            interest = _r2(balance * periodic_rate if kind == "amortizing" else interest_flat)
            payment, principal_paid, phase = interest, 0.0, "interest_only"
        elif kind == "amortizing":
            interest = _r2(balance * periodic_rate)
            principal_paid = balance if last else min(balance, _r2(after_payment - interest))
            payment = _r2(principal_paid + interest)
            phase = "amortizing"
        else:  # simple interest: flat interest, even principal
            interest = _r2(interest_flat)
            principal_paid = balance if last else min(balance, _r2(principal / remaining))
            payment = _r2(principal_paid + interest)
            phase = "simple_interest"
        balance = _r2(max(0.0, balance - principal_paid))
        rows.append(
            {
                "period": index,
                "phase": phase,
                "payment": payment,
                "interest": interest,
                "principal": _r2(principal_paid),
                "balance": balance,
            }
        )
    balloon = _r2(balance) if balance > 0.005 else 0.0
    if balloon and rows:
        rows[-1]["balloon"] = balloon
    first_year = rows[: _half_up(ppy)] if ppy >= 1 else rows
    annual_debt_service = _r2(sum(r["payment"] for r in first_year))
    payment_io = _r2(rows[0]["payment"]) if io_periods else None
    payment_after = _r2(rows[io_periods]["payment"]) if io_periods < periods else None
    to_monthly = ppy / 12.0
    coverage_payment = _r2(max(payment_after or 0.0, payment_io or 0.0) * to_monthly)
    # The annual service coverage stands on: the post-interest-only payment over
    # the periods actually due in a year (a six-month loan is six payments).
    coverage_annual = _r2(
        max(payment_after or 0.0, payment_io or 0.0) * min(periods, _half_up(ppy))
    )
    return {
        "structure": kind,
        "structure_label": STRUCTURE_LABELS[kind],
        "amount": _r2(principal),
        "rate_pct": float(rate_pct or 0.0),
        "term_months": months if months > 0 else None,
        "interest_only_months": _half_up(io_periods * 12.0 / ppy),
        "periods": periods,
        "periods_per_year": ppy,
        "payment_io": payment_io,
        "payment_after": payment_after,
        "payment_io_monthly": _r2(payment_io * to_monthly) if payment_io is not None else None,
        "payment_after_monthly": _r2(payment_after * to_monthly)
        if payment_after is not None
        else None,
        "coverage_payment_monthly": coverage_payment,
        "coverage_annual_debt_service": coverage_annual,
        "annual_debt_service": annual_debt_service,
        "total_interest": _r2(sum(r["interest"] for r in rows)),
        "total_paid": _r2(sum(r["payment"] for r in rows) + balloon),
        "balloon": balloon,
        "rows": rows,
    }


def describe(summary: dict[str, Any] | None) -> str:
    """The structure in a phrase: "24 months, 6 interest-only then simple interest"."""
    if not summary:
        return ""
    io = int(summary.get("interest_only_months") or 0)
    kind = summary.get("structure")
    term = summary.get("term_months")
    if kind == "interest_only":
        return f"interest-only{f' over {term} months' if term else ''}, principal due at maturity"
    label = STRUCTURE_LABELS.get(str(kind), str(kind)).lower()
    if io:
        return f"over {term} months, {io} interest-only then {label}"
    return f"over {term} months, {label}"
