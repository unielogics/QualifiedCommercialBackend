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
MAX_PERIODS = 20_000
MAX_TERM_MONTHS = 600
MAX_AMORTIZATION_MONTHS = 1200


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


def _build_legacy_schedule(
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
    if periods > MAX_PERIODS:
        raise ValueError(f"The payment cadence exceeds the {MAX_PERIODS} payment-period limit")
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


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or not low <= value <= high:
        raise ValueError(f"{label} must be a whole number from {low} to {high}")
    return int(value)


def normalize_repayment_config(config: Any, term_months: int | None, *, structure: str | None = None,
                               interest_only_months: int | None = 0, periods_per_year: float = 12.0) -> dict[str, Any] | None:
    """Validate the persisted contract without guessing missing financial terms."""
    if config is None:
        return None
    if hasattr(config, "model_dump"):
        config = config.model_dump(exclude_none=True)
    if not isinstance(config, dict) or set(config) - {"amortization_months", "after_balloon", "balloons"}:
        raise ValueError("Repayment configuration contains unsupported fields")
    months = _integer(term_months, "Maturity months", 1, MAX_TERM_MONTHS)
    io = _integer(0 if interest_only_months is None else interest_only_months, "Interest-only months", 0, months)
    ppy = float(periods_per_year)
    if not math.isfinite(ppy) or not 1 <= ppy <= 366:
        raise ValueError("Payment periods per year must be finite and between 1 and 366")
    if months * ppy / 12 < 1 - 1e-9:
        raise ValueError("Maturity must include at least one payment period at the selected cadence")
    kind = normalize_structure(structure, months)
    amort = config.get("amortization_months")
    if amort is not None:
        amort = _integer(amort, "Amortization months after interest-only", 1, MAX_AMORTIZATION_MONTHS)
        if kind == "interest_only" or io == months:
            raise ValueError("An all-interest-only loan cannot specify an amortization period")
    after = config.get("after_balloon", "keep_payment")
    if after not in {"keep_payment", "recast"}:
        raise ValueError("After balloon must be keep_payment or recast")
    raw = config.get("balloons", [])
    if not isinstance(raw, list) or len(raw) > 60:
        raise ValueError("A repayment configuration supports at most 60 scheduled balloons")
    balloons, ids = [], set()
    for event in raw:
        if not isinstance(event, dict) or set(event) != {"id", "month", "type", "value"}:
            raise ValueError("Each balloon requires only id, month, type and value")
        identifier = event["id"]
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 80 or identifier in ids:
            raise ValueError("Scheduled balloon IDs must be nonblank, unique and at most 80 characters")
        month = _integer(event["month"], "Balloon month", 1, months)
        value = event["value"]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 1e15:
            raise ValueError(f"Balloon {identifier} requires a positive finite value")
        if event["type"] not in {"amount", "percent_balance"}:
            raise ValueError("Balloon type must be amount or percent_balance")
        if event["type"] == "percent_balance" and value > 100:
            raise ValueError("A remaining-balance balloon percentage cannot exceed 100")
        if event["type"] == "amount" and _r2(float(value)) <= 0:
            raise ValueError("A fixed balloon amount must be at least one cent")
        ids.add(identifier)
        balloons.append({"id": identifier, "month": month, "type": event["type"], "value": float(value)})
    return {**({"amortization_months": amort} if amort is not None else {}), "after_balloon": after, "balloons": balloons}


def schedule_cash_flows(summary: dict[str, Any] | None) -> list[float]:
    """Actual receipts: scheduled balloons in payment, maturity residual once."""
    if not summary:
        return []
    if isinstance(summary.get("cash_flows"), list) and all(isinstance(value, (int, float)) and math.isfinite(value) for value in summary["cash_flows"]):
        return list(summary["cash_flows"])
    rows = summary.get("rows") or []
    return [_r2(float(row["cash_payment"]) if row.get("cash_payment") is not None else float(row.get("payment") or 0) + (float(summary.get("balloon") or 0) if index == len(rows) - 1 else 0)) for index, row in enumerate(rows)]


def effective_rate_pct(proceeds: float, payments: list[float], balloon: float = 0, periods_per_year: float = 12) -> float | None:
    """Nominal annual IRR for actual period receipts; no balloon duplication."""
    if not math.isfinite(proceeds) or proceeds <= 0 or not payments:
        return None
    flows = list(payments)
    flows[-1] += balloon
    if any(not math.isfinite(flow) or flow < 0 for flow in flows):
        return None
    if sum(flows) <= proceeds:
        return 0.0
    def present(rate):
        return sum(payment * math.exp(-(index + 1) * math.log1p(rate)) for index, payment in enumerate(flows)) - proceeds
    low, high = 0.0, 1.0
    if present(high) > 0:
        return None
    for _ in range(200):
        if high - low <= 1e-10:
            break
        middle = (low + high) / 2
        if present(middle) > 0:
            low = middle
        else:
            high = middle
    return _r2((low + high) / 2 * periods_per_year * 100)


def _cash_metadata(summary: dict[str, Any], *, advanced: bool) -> dict[str, Any]:
    rows, principal = summary["rows"], summary["amount"]
    ppy = summary["periods_per_year"]
    for index, row in enumerate(rows):
        residual = summary["balloon"] if index == len(rows) - 1 else 0.0
        row.setdefault("regular_payment", row["payment"])
        row.setdefault("regular_principal", row["principal"])
        row.setdefault("scheduled_balloon", 0.0)
        row["maturity_balloon"] = residual
        row["cash_payment"] = _r2(row["payment"] + residual)
        row["total_principal"] = _r2(row["principal"] + residual)
        row["balance_after_cash"] = _r2(max(0, row["balance"] - residual))
    flows = [row["cash_payment"] for row in rows]
    first_year = _r2(sum(row["cash_payment"] for row in rows if row["period"] <= ppy + 1e-9))
    peak, running, left, peak_end = 0.0, 0.0, 0, None
    for row in rows:
        running += row["cash_payment"]
        while row["period"] - rows[left]["period"] >= ppy - 1e-9:
            running -= rows[left]["cash_payment"]
            left += 1
        if running > peak:
            peak, peak_end = running, row["month"]
    peak = _r2(peak)
    regular_peak = max((row["regular_payment"] for row in rows), default=0)
    summary.update(cash_flows=flows, maturity_balloon=summary["balloon"],
                   scheduled_balloons_total=_r2(sum(row["scheduled_balloon"] for row in rows)),
                   first_year_actual_debt_service=first_year, peak_rolling_12_month_debt_service=peak,
                   peak_rolling_12_month_end=peak_end, peak_cash_payment=max(flows, default=0),
                   actual_payoff_month=rows[-1]["month"] if rows else None,
                   principal_repaid=_r2(sum(row["total_principal"] for row in rows)),
                   maturity_exposure_pct=_r1(summary["balloon"] / principal * 100),
                   regular_coverage_payment_monthly=_r2(regular_peak * ppy / 12),
                   coverage_basis="peak_rolling_12_month_actual_cash_due" if advanced else "legacy_regular_payment",
                   cash_flow_convention="payment includes scheduled balloons; maturity residual is separate; cash_payment includes both",
                   balloon_period_mapping="nearest scheduled cadence period, half up; list order within a period")
    if advanced:
        summary["annual_debt_service"] = first_year
        summary["coverage_annual_debt_service"] = peak
        summary["coverage_payment_monthly"] = _r2(peak / 12)
        cum_principal = cum_paid = cum_interest = 0.0
        breakeven = None
        for row in rows:
            cum_principal = _r2(cum_principal + row["total_principal"])
            cum_paid = _r2(cum_paid + row["cash_payment"])
            cum_interest = _r2(cum_interest + row["interest"])
            row.update(cumulative_principal=cum_principal, cumulative_paid=cum_paid, cumulative_interest=cum_interest,
                       recovered_pct=_r1(cum_principal / principal * 100), exposure_pct=_r1(row["balance_after_cash"] / principal * 100),
                       cash_at_risk_pct=_r1(max(0, principal - cum_paid) / principal * 100))
            if breakeven is None and cum_paid >= principal - .005:
                breakeven = float(math.ceil(row["month"] - 1e-9))
        summary["breakeven_month"] = breakeven
        summary["weighted_average_life_months"] = _r1(sum(row["total_principal"] * row["month"] for row in rows) / principal)
        summary["recovery_milestones"] = {str(threshold): next((float(math.ceil(row["month"] - 1e-9)) for row in rows if row["cumulative_principal"] >= principal * threshold / 100 - .005), None) for threshold in (25, 50, 75, 100)}
        first_principal = next((row for row in rows if row["total_principal"] > 0), None)
        summary["full_exposure_months"] = max(0, math.ceil(first_principal["month"] - 1e-9) - 1) if first_principal else summary.get("term_months")
    return summary


def build_schedule(amount: float | None, rate_pct: float | None, term_months: int | None, *,
                   structure: str | None = None, interest_only_months: int | None = 0,
                   periods_per_year: float = 12.0, repayment_config: dict | None = None) -> dict[str, Any] | None:
    principal = float(amount or 0)
    if not math.isfinite(principal) or principal <= 0:
        return None
    ppy = float(periods_per_year)
    if not math.isfinite(ppy) or not 1 <= ppy <= 366:
        raise ValueError("Payment periods per year must be finite and between 1 and 366")
    rate = float(rate_pct or 0)
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("The annual rate must be finite and nonnegative")
    if term_months is not None:
        _integer(term_months, "Maturity months", 0, MAX_TERM_MONTHS)
    config = normalize_repayment_config(repayment_config, term_months, structure=structure,
                                        interest_only_months=interest_only_months, periods_per_year=ppy)
    if config is None:
        legacy = _build_legacy_schedule(amount, rate_pct, term_months, structure=structure,
                                        interest_only_months=interest_only_months, periods_per_year=ppy)
        if legacy is None:
            return None
        legacy.update(repayment_config=None, scheduled_balloons=[], after_balloon=None, amortization_months=None,
                      contractual_periods=legacy["periods"])
        return _cash_metadata(legacy, advanced=False)
    principal = _r2(principal)
    if principal <= 0:
        raise ValueError("The loan amount must be at least one cent")
    months = int(term_months)
    periods = max(1, _half_up(months * ppy / 12))
    if periods > MAX_PERIODS:
        raise ValueError(f"The payment cadence exceeds the {MAX_PERIODS} payment-period limit")
    kind = normalize_structure(structure, months)
    io_periods = periods if kind == "interest_only" else min(periods, _half_up(int(interest_only_months or 0) * ppy / 12))
    if io_periods == periods:
        kind = "interest_only"
    amort_periods = max(1, _half_up(config.get("amortization_months", (periods - io_periods) * 12 / ppy) * ppy / 12))
    periodic_rate = rate / 100 / ppy
    flat_interest = _r2(principal * periodic_rate)
    payment_after = level_payment(principal, periodic_rate, amort_periods) if kind == "amortizing" else principal / amort_periods + flat_interest if kind == "simple_interest" else 0.0
    principal_installment = principal / amort_periods
    event_periods = {}
    for event in config["balloons"]:
        due = min(periods, max(1, _half_up(event["month"] * ppy / 12)))
        event_periods.setdefault(due, []).append(event)
    rows, applied, balance = [], [], principal
    for index in range(1, periods + 1):
        if balance <= 0:
            if any(due >= index for due in event_periods):
                raise ValueError("A scheduled balloon occurs after the loan has already been repaid")
            break
        io = index <= io_periods
        interest = _r2(flat_interest if kind == "simple_interest" else balance * periodic_rate)
        if io:
            regular_principal = 0.0
        elif kind == "amortizing":
            regular_principal = min(balance, max(0, _r2(payment_after - interest)))
        else:
            regular_principal = min(balance, _r2(principal_installment))
        if not io and index - io_periods >= amort_periods:
            regular_principal = balance
        regular_payment = _r2(interest + regular_principal)
        balance = _r2(balance - regular_principal)
        scheduled = 0.0
        for event in event_periods.get(index, []):
            if balance <= 0:
                raise ValueError(f"Balloon {event['id']} has no remaining principal after the regular payment or an earlier balloon")
            before = balance
            paid = _r2(balance * event["value"] / 100) if event["type"] == "percent_balance" else _r2(event["value"])
            if paid > balance + .005:
                raise ValueError(f"Balloon {event['id']} exceeds remaining principal after the regular payment ({balance:.2f})")
            paid = min(paid, balance)
            balance, scheduled = _r2(balance - paid), _r2(scheduled + paid)
            recast = None
            if config["after_balloon"] == "recast":
                remaining = max(1, amort_periods - max(0, index - io_periods))
                principal_installment = balance / remaining
                payment_after = level_payment(balance, periodic_rate, remaining) if kind == "amortizing" else principal_installment + flat_interest if kind == "simple_interest" else balance * periodic_rate
                recast = _r2(payment_after)
            applied.append({**event, "period": index, "due_month": _r2(index * 12 / ppy),
                            "balance_before": before, "amount": paid, "balance_after": balance,
                            "basis": "remaining_principal_after_regular_payment_and_prior_events",
                            "recast_payment": recast, "recast_payment_monthly": _r2(recast * ppy / 12) if recast is not None else None,
                            "recast_effective_period": (max(index + 1, io_periods + 1) if kind != "interest_only" else index + 1) if recast is not None and balance > 0 and (max(index + 1, io_periods + 1) if kind != "interest_only" else index + 1) <= periods else None})
        rows.append({"period": index, "month": _r2(index * 12 / ppy), "phase": "interest_only" if io else kind,
                     "regular_payment": regular_payment, "regular_principal": _r2(regular_principal),
                     "scheduled_balloon": scheduled, "payment": _r2(regular_payment + scheduled),
                     "interest": interest, "principal": _r2(regular_principal + scheduled), "balance": balance})
    balloon = _r2(balance)
    if balloon and rows:
        rows[-1]["balloon"] = balloon
    payment_io = rows[0]["regular_payment"] if io_periods else None
    after_row = next((row for row in rows if row["phase"] != "interest_only"), None)
    after = after_row["regular_payment"] if after_row else None
    summary = {"structure": kind, "structure_label": STRUCTURE_LABELS[kind], "amount": principal, "rate_pct": rate,
               "term_months": months, "interest_only_months": _half_up(io_periods * 12 / ppy),
               "periods": len(rows), "contractual_periods": periods, "periods_per_year": ppy,
               "payment_io": payment_io, "payment_after": after,
               "payment_io_monthly": _r2(payment_io * ppy / 12) if payment_io is not None else None,
               "payment_after_monthly": _r2(after * ppy / 12) if after is not None else None,
               "total_interest": _r2(sum(row["interest"] for row in rows)),
               "total_paid": _r2(sum(row["payment"] for row in rows) + balloon), "balloon": balloon,
               "balloon_pct": _r1(balloon / principal * 100), "rows": rows,
               "repayment_config": config, "scheduled_balloons": applied, "after_balloon": config["after_balloon"],
               "amortization_months": None if kind == "interest_only" else config.get("amortization_months", _half_up(amort_periods * 12 / ppy))}
    return _cash_metadata(summary, advanced=True)


def risk_profile(
    summary: dict[str, Any] | None,
    *,
    income_monthly: float | None = None,
    existing_service_monthly: float | None = None,
    income_currency_comparable: bool = True,
) -> dict[str, Any] | None:
    """Principal at risk and the coverage on every payment.

    The exposure curve is in the rows; this says what it means in words and
    numbers: the weighted average life, when a quarter, half, three quarters
    and all of the principal is back, how long the balance sits at 100%, the
    balloon. With the income available for debt service after the plan and
    the existing service beside the new loan, every payment gets its own
    coverage (income over existing service plus that payment, monthly), so
    the interest-only months and the step-up are both visible. Signed ratios
    remain visible for losses; earnings-based coverage shortfalls show their burden.
    """
    if not summary or not summary.get("rows"):
        return None
    rows = summary["rows"]
    cash_flows = schedule_cash_flows(summary)
    ppy = float(summary.get("periods_per_year") or 12.0)
    to_monthly = ppy / 12.0
    income = (float(income_monthly) if income_monthly is not None
              and not isinstance(income_monthly, bool) and math.isfinite(float(income_monthly)) else None)
    existing = (float(existing_service_monthly) if existing_service_monthly is not None
                and not isinstance(existing_service_monthly, bool) and math.isfinite(float(existing_service_monthly))
                and float(existing_service_monthly) >= 0 else None)
    coverage_available = income is not None and existing is not None

    def payment_position(payment_monthly):
        total = _r2(existing + payment_monthly) if existing is not None else None
        surplus = _r2(income - total) if coverage_available and income_currency_comparable else None
        return {"debt_service_monthly": total, "cash_surplus_monthly": surplus,
                "cash_shortfall_monthly": _r2(max(0.0, -surplus)) if surplus is not None else None}

    coverage_rows: list[dict[str, Any]] = []
    if coverage_available:
        for index, r in enumerate(rows):
            payment_monthly = _r2((cash_flows[index] if summary.get("repayment_config") is not None else r["payment"]) * to_monthly)
            denominator = existing + payment_monthly
            coverage_rows.append(
                {
                    "period": r["period"],
                    "month": r["month"],
                    "phase": r["phase"],
                    "payment_monthly": payment_monthly,
                    "coverage": round(income / denominator, 2) if denominator > 0 else None,
                    **payment_position(payment_monthly),
                }
            )
    with_cov = [c for c in coverage_rows if c["coverage"] is not None]
    cash_coverage = [{"period": row["period"], "month": row["month"],
                      "payment_monthly": _r2(cash_flows[index] * to_monthly),
                      "coverage": _r2(income / (existing + cash_flows[index] * to_monthly)) if existing + cash_flows[index] * to_monthly > 0 else None,
                      **payment_position(cash_flows[index] * to_monthly)}
                     for index, row in enumerate(rows)] if coverage_available else []
    cash_values = [row["coverage"] for row in cash_coverage if row["coverage"] is not None]
    lowest = min(with_cov, key=lambda c: c["coverage"]) if with_cov else None
    io_cov = next((c["coverage"] for c in with_cov if c["phase"] == "interest_only"), None)
    after_cov = next((c["coverage"] for c in with_cov if c["phase"] != "interest_only"), None)
    shortfalls = [row for row in cash_coverage if row["cash_shortfall_monthly"] is not None]
    peak_shortfall = max(shortfalls, key=lambda row: row["cash_shortfall_monthly"]) if shortfalls else None
    coverage_note = ("Selected earnings are zero or negative. A smaller debt denominator can make a negative DSCR more negative; compare payment amounts and earnings-based coverage shortfalls rather than ranking signed ratios."
                     if income is not None and income <= 0 else None)
    if not income_currency_comparable:
        coverage_note = " ".join(part for part in (coverage_note, "Source earnings currency is not confirmed against USD debt service. Earnings-based coverage shortfalls are not calculated without comparable currencies; no currency conversion is assumed.") if part)
    milestones = summary.get("recovery_milestones") or {}
    return {
        "weighted_average_life_months": summary.get("weighted_average_life_months"),
        "recovery_milestones": milestones,
        "full_exposure_months": summary.get("full_exposure_months"),
        "breakeven_month": summary.get("breakeven_month"),
        "balloon": summary.get("balloon") or 0.0,
        "balloon_pct": summary.get("balloon_pct") or 0.0,
        "income_monthly": _r2(income) if income is not None else None,
        "existing_service_monthly": _r2(existing) if existing is not None else None,
        "coverage_by_period": coverage_rows,
        "cash_coverage_by_period": cash_coverage,
        "lowest_cash_coverage": min(cash_values) if cash_values else None,
        "peak_cash_shortfall_monthly": peak_shortfall["cash_shortfall_monthly"] if peak_shortfall else None,
        "peak_cash_shortfall_period": peak_shortfall["period"] if peak_shortfall else None,
        "peak_cash_shortfall_month": peak_shortfall["month"] if peak_shortfall else None,
        "coverage_note": coverage_note,
        "coverage_unavailable_reason": ("Income available for debt service has not been established." if income is None
                                        else "Existing debt service has not been established." if existing is None
                                        else "Earnings-based coverage shortfalls require earnings comparable with USD debt service." if not income_currency_comparable else None),
        "coverage_basis": "actual_cash_due_per_period" if summary.get("repayment_config") is not None else "legacy_regular_payment",
        "peak_rolling_12_month_debt_service": summary.get("peak_rolling_12_month_debt_service"),
        "maturity_balloon": summary.get("maturity_balloon", summary.get("balloon")),
        "scheduled_balloons_total": summary.get("scheduled_balloons_total", 0),
        "coverage_during_interest_only": io_cov,
        "coverage_after_interest_only": after_cov,
        "lowest_coverage": lowest["coverage"] if lowest else None,
        "lowest_coverage_month": lowest["month"] if lowest else None,
        "words": risk_words(summary, lowest=lowest, io_cov=io_cov, after_cov=after_cov,
                            income_nonpositive=income is not None and income <= 0)
        + (" " + coverage_note if coverage_note else ""),
    }


def risk_words(
    summary: dict[str, Any],
    *,
    lowest: dict[str, Any] | None = None,
    io_cov: float | None = None,
    after_cov: float | None = None,
    income_nonpositive: bool = False,
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

    if summary.get("repayment_config") is not None:
        parts.append(f"Actual principal cash flows include {len(summary.get('scheduled_balloons') or [])} scheduled balloon(s), totaling {summary.get('scheduled_balloons_total', 0):,.2f}.")
        if summary.get("balloon"):
            parts.append(f"A separate residual principal payment of {summary['balloon']:,.2f} is due at maturity; it is counted once.")
        parts.append("Coverage uses the peak rolling twelve months of actual cash due; payment-by-payment coverage also shows balloon stress.")
    elif kind == "interest_only":
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
            f"Cumulative principal and interest receipts equal the original advance by month {int(breakeven)}. This cash-recovery point does not eliminate credit exposure on unpaid principal."
        )
    if lowest and lowest.get("coverage") is not None:
        if income_nonpositive:
            parts.append(f"The mathematical minimum signed payment coverage is {lowest['coverage']:.2f}x; this does not identify the largest payment or coverage shortfall when earnings are nonpositive.")
        elif summary.get("repayment_config") is not None:
            parts.append(f"Lowest payment-period liquidity coverage {lowest['coverage']:.2f}x; annual coverage uses peak rolling twelve-month cash due.")
        elif io_cov is not None and after_cov is not None and io_cov != after_cov:
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
    if summary.get("repayment_config") is not None:
        return (f"{summary.get('structure_label', '')} over {summary.get('term_months')} months; "
                + (f"amortization {summary.get('amortization_months')} months after interest-only; " if summary.get("amortization_months") is not None else "no principal amortization; ")
                +
                f"{len(summary.get('scheduled_balloons') or [])} scheduled balloons; "
                f"{summary.get('balloon', 0):,.2f} residual at maturity; {summary.get('after_balloon')} after balloons")
    io = int(summary.get("interest_only_months") or 0)
    kind = summary.get("structure")
    term = summary.get("term_months")
    if kind == "interest_only":
        return f"interest-only{f' over {term} months' if term else ''}, principal due at maturity"
    label = STRUCTURE_LABELS.get(str(kind), str(kind)).lower()
    if io:
        return f"over {term} months, {io} interest-only then {label}"
    return f"over {term} months, {label}"
