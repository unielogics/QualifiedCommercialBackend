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


def _r1(value: float) -> float:
    return round(value + 0.0, 1)


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
    # Principal at risk, period by period: the balance still out after each
    # payment as a share of the loan, and how much of the principal is back.
    cum_principal = 0.0
    cum_interest = 0.0
    cum_paid = 0.0
    breakeven_month: float | None = None
    for r in rows:
        cum_principal = _r2(cum_principal + r["principal"])
        cum_interest = _r2(cum_interest + r["interest"])
        cum_paid = _r2(cum_paid + r["payment"])
        r["month"] = round(r["period"] * 12.0 / ppy, 2)
        r["cumulative_principal"] = cum_principal
        r["cumulative_interest"] = cum_interest
        r["cumulative_paid"] = cum_paid
        r["exposure_pct"] = _r1(r["balance"] / principal * 100.0)
        r["recovered_pct"] = _r1(cum_principal / principal * 100.0)
        # Counting every dollar received, interest included: what the
        # investor still has out until the payments have returned the
        # investment; zero from the break-even payment on.
        r["cash_at_risk_pct"] = _r1(max(0.0, principal - cum_paid) / principal * 100.0)
        if breakeven_month is None and cum_paid >= principal - 0.005:
            breakeven_month = float(math.ceil(r["month"] - 1e-9))
    term_month = rows[-1]["month"] if rows else 0.0
    if breakeven_month is None and balloon and rows and cum_paid + balloon >= principal - 0.005:
        breakeven_month = float(
            math.ceil(term_month - 1e-9)
        )  # the balloon brings it back at maturity
    weighted_life = (
        sum(r["principal"] * r["month"] for r in rows) + balloon * term_month
    ) / principal
    milestones: dict[str, float | None] = {}
    for threshold in (25, 50, 75, 100):
        hit = next((r for r in rows if r["recovered_pct"] >= threshold - 1e-9), None)
        if hit is not None:
            milestones[str(threshold)] = float(math.ceil(hit["month"] - 1e-9))
        elif balloon and rows:
            milestones[str(threshold)] = float(
                math.ceil(term_month - 1e-9)
            )  # the balloon brings it back at maturity
        else:
            milestones[str(threshold)] = None
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
        "balloon_pct": _r1(balloon / principal * 100.0) if balloon else 0.0,
        #: Months until the average dollar of principal is back (the balloon
        #: counts at maturity): the investor's measure of how long the
        #: principal is at risk.
        "weighted_average_life_months": _r1(weighted_life),
        #: The month by which a quarter, half, three quarters and all of the
        #: principal is back; None when the schedule never gets there.
        "recovery_milestones": milestones,
        #: Months the balance stays at 100%: the interest-only months, or the
        #: whole term when principal is due at maturity.
        "full_exposure_months": _half_up(io_periods * 12.0 / ppy),
        #: The month by which the payments received, interest included, add
        #: up to the investment: from here on no capital is at risk.
        "breakeven_month": breakeven_month,
        "rows": rows,
    }


def risk_profile(
    summary: dict[str, Any] | None,
    *,
    income_monthly: float | None = None,
    existing_service_monthly: float | None = None,
) -> dict[str, Any] | None:
    """Principal at risk and the coverage on every payment.

    The exposure curve is in the rows; this says what it means in words and
    numbers: the weighted average life, when a quarter, half, three quarters
    and all of the principal is back, how long the balance sits at 100%, the
    balloon. With the income available for debt service after the plan and
    the existing service beside the new loan, every payment gets its own
    coverage (income over existing service plus that payment, monthly), so
    the interest-only months and the step-up are both visible; the lowest is
    the one underwriting stands on.
    """
    if not summary or not summary.get("rows"):
        return None
    rows = summary["rows"]
    ppy = float(summary.get("periods_per_year") or 12.0)
    to_monthly = ppy / 12.0
    income = float(income_monthly) if income_monthly else None
    existing = max(0.0, float(existing_service_monthly or 0.0))
    coverage_rows: list[dict[str, Any]] = []
    if income is not None and income > 0:
        for r in rows:
            payment_monthly = _r2(r["payment"] * to_monthly)
            denominator = existing + payment_monthly
            coverage_rows.append(
                {
                    "period": r["period"],
                    "month": r["month"],
                    "phase": r["phase"],
                    "payment_monthly": payment_monthly,
                    "coverage": round(income / denominator, 2) if denominator > 0 else None,
                }
            )
    with_cov = [c for c in coverage_rows if c["coverage"] is not None]
    lowest = min(with_cov, key=lambda c: c["coverage"]) if with_cov else None
    io_cov = next((c["coverage"] for c in with_cov if c["phase"] == "interest_only"), None)
    after_cov = next((c["coverage"] for c in with_cov if c["phase"] != "interest_only"), None)
    milestones = summary.get("recovery_milestones") or {}
    return {
        "weighted_average_life_months": summary.get("weighted_average_life_months"),
        "recovery_milestones": milestones,
        "full_exposure_months": summary.get("full_exposure_months"),
        "breakeven_month": summary.get("breakeven_month"),
        "balloon": summary.get("balloon") or 0.0,
        "balloon_pct": summary.get("balloon_pct") or 0.0,
        "income_monthly": _r2(income) if income is not None else None,
        "existing_service_monthly": _r2(existing),
        "coverage_by_period": coverage_rows,
        "coverage_during_interest_only": io_cov,
        "coverage_after_interest_only": after_cov,
        "lowest_coverage": lowest["coverage"] if lowest else None,
        "lowest_coverage_month": lowest["month"] if lowest else None,
        "words": risk_words(summary, lowest=lowest, io_cov=io_cov, after_cov=after_cov),
    }


def risk_words(
    summary: dict[str, Any],
    *,
    lowest: dict[str, Any] | None = None,
    io_cov: float | None = None,
    after_cov: float | None = None,
) -> str:
    """The principal-risk reading in plain words, for the package and the offering."""
    kind = summary.get("structure")
    term = summary.get("term_months")
    io_months = int(summary.get("full_exposure_months") or 0)
    milestones = summary.get("recovery_milestones") or {}
    wal = summary.get("weighted_average_life_months")
    parts: list[str] = []

    def month(key: str) -> str:
        value = milestones.get(key)
        return f"month {int(value)}" if value else "never"

    if kind == "interest_only":
        parts.append(
            f"The whole principal stays out for the full {term} months and comes back at maturity: the exposure never falls before then."
            if term
            else "The whole principal stays out until maturity: the exposure never falls before then."
        )
    else:
        if io_months:
            parts.append(
                f"Principal is fully exposed for the first {io_months} month{'s' if io_months != 1 else ''}, then comes back "
                + (
                    "evenly"
                    if kind == "simple_interest"
                    else "with each level payment, slowly at first"
                )
                + f": a quarter by {month('25')}, half by {month('50')}, all of it by {month('100')}."
            )
        else:
            parts.append(
                "Principal comes back from the first payment"
                + (
                    " evenly"
                    if kind == "simple_interest"
                    else ", slowly at first (level payments pay mostly interest early)"
                )
                + f": a quarter by {month('25')}, half by {month('50')}, all of it by {month('100')}."
            )
        if summary.get("balloon"):
            parts.append(
                f"{summary['balloon']:,.0f} ({summary.get('balloon_pct', 0):g}% of the loan) is due at maturity."
            )
    if wal is not None:
        parts.append(f"Weighted average life {wal:g} months.")
    breakeven = summary.get("breakeven_month")
    if breakeven:
        parts.append(
            f"Counting the interest received, the capital is back by month {int(breakeven)}; nothing is at risk after that."
        )
    if lowest and lowest.get("coverage") is not None:
        if io_cov is not None and after_cov is not None and io_cov != after_cov:
            parts.append(
                f"Coverage on each payment: {io_cov:.2f}x during the interest-only months, {after_cov:.2f}x once principal starts; the lowest, {lowest['coverage']:.2f}x, is what underwriting stands on."
            )
        else:
            parts.append(f"Coverage on each payment {lowest['coverage']:.2f}x.")
    return " ".join(parts)


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
