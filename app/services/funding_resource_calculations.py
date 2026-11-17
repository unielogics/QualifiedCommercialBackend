"""Deterministic, self-reported illustrations; never a credit decision.

The public funnel may model cash-flow capacity, but it cannot manufacture a
lender limit from deposits or treat a factor as an interest rate.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

GOALS = {"mca_refinance", "merchant_cash_advance", "term_loan"}
APPLICATION_PATHS = {
    "mca_refinance": "/mca-refinance-intake",
    "merchant_cash_advance": "/mca-funding-intake",
    "term_loan": "/business-funding-intake",
}
DEFAULTS = {"annual_rates_pct": [15, 20, 25], "term_months": 60,
            "coverage_buffer": 1.25, "fee_pct": 0, "mca_duration_months": None}


def number(value: Any, *, negative: bool = False) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Financial inputs must be finite numbers or unknown.")
    value = float(value)
    if not math.isfinite(value) or abs(value) > 1e12 or (value < 0 and not negative):
        raise ValueError("Financial input is outside the supported range.")
    return value


def money(value: float | None) -> float | None:
    return round(value, 2) if value is not None and math.isfinite(value) else None


def payment(principal: float, rate_pct: float, months: int) -> float:
    rate = rate_pct / 1200
    return principal / months if rate == 0 else principal * rate / (-math.expm1(-months * math.log1p(rate)))


def present_value(monthly: float, rate_pct: float, months: int) -> float:
    rate = rate_pct / 1200
    return monthly * months if rate == 0 else monthly * (-math.expm1(-months * math.log1p(rate))) / rate


def normalized_assumptions(values: dict | None) -> dict:
    if values is not None and not isinstance(values, dict):
        raise ValueError("Assumptions must be an object.")
    result = {**DEFAULTS, **(values or {})}
    rates = result["annual_rates_pct"]
    if not isinstance(rates, list) or len(rates) != 3:
        raise ValueError("Provide three illustrative annual rates.")
    rates = [number(value) for value in rates]
    if any(value is None or value > 100 for value in rates):
        raise ValueError("Illustrative annual rates must be between 0 and 100 percent.")
    months = number(result["term_months"])
    coverage = number(result["coverage_buffer"])
    fee = number(result["fee_pct"])
    duration = number(result["mca_duration_months"])
    if months is None or not months.is_integer() or not 1 <= months <= 120:
        raise ValueError("Illustrative amortization must be 1 to 120 whole months.")
    if coverage is None or not 1 <= coverage <= 3:
        raise ValueError("The illustrative coverage buffer must be between 1 and 3.")
    if fee is None or fee >= 100:
        raise ValueError("Illustrative fees must be between 0 and less than 100 percent.")
    if duration is not None and (not duration.is_integer() or not 1 <= duration <= 24):
        raise ValueError("The optional hypothetical MCA duration must be 1 to 24 whole months.")
    return {"annual_rates_pct": rates, "term_months": int(months), "coverage_buffer": coverage,
            "fee_pct": fee, "mca_duration_months": int(duration) if duration else None}


def calculate_funding_report(goal: str, answers: dict, assumptions: dict | None = None) -> dict:
    if goal not in GOALS or not isinstance(answers, dict):
        raise ValueError("Choose a supported business funding goal.")
    opts = normalized_assumptions(assumptions)
    from app.services import mca_funding_policy, mca_refinance_policy

    low_factor, high_factor = float(mca_funding_policy.MIN_FACTOR), float(mca_funding_policy.MAX_FACTOR)
    factor_examples = [low_factor, (low_factor + high_factor) / 2, high_factor]
    position_limit = mca_funding_policy.MAX_EXISTING_MCAS if goal == "merchant_cash_advance" else mca_refinance_policy.MAX_ACTIVE_MCAS
    missing: list[str] = []
    readiness: list[dict] = []
    manual = False

    def check(key: str, status: str, message: str) -> None:
        readiness.append({"key": key, "status": status, "message": message})
        if status == "missing":
            missing.append(message)

    requested = number(answers.get("requested_amount"))
    extra = number(answers.get("additional_cash"))
    tax_income = number(answers.get("tax_income"), negative=True)
    revenue = number(answers.get("annual_revenue"))
    # Revenue is context only. It is never substituted for earnings.
    _ = revenue
    period = number(answers.get("period_months"))
    addbacks = [number(answers.get(key)) for key in ("interest", "depreciation", "amortization")]
    annual = tax_income + sum(value for value in addbacks if value is not None) if tax_income is not None and period == 12 else None
    basis = "reported_profit_only" if all(value is None for value in addbacks) else "reported_profit_plus_known_addbacks"
    if annual is None:
        check("earnings", "missing", "Enter reported business income and confirm a complete 12-month tax period to model capacity.")
    else:
        check("earnings", "review" if annual <= 0 else "ready", "Reported earnings need document verification; known addbacks are illustrative, not verified EBITDA.")

    raw_debts = answers.get("debts")
    if raw_debts is not None and (not isinstance(raw_debts, list) or len(raw_debts) > 50):
        raise ValueError("Provide at most 50 debt entries.")
    debts = raw_debts if isinstance(raw_debts, list) else []
    current, retained, payoff = 0.0, 0.0, 0.0
    payments_known = raw_debts is not None
    payoffs_known = raw_debts is not None
    factors = {"daily": 21, "weekly": 52 / 12, "monthly": 1}
    for debt in debts:
        if not isinstance(debt, dict) or debt.get("type") not in {"mca", "other"}:
            raise ValueError("Each obligation must identify its debt type.")
        amount = number(debt.get("payment"))
        balance = number(debt.get("balance"))
        frequency = debt.get("frequency")
        if frequency not in factors:
            raise ValueError("Debt payment frequency must be daily, weekly or monthly.")
        retire = debt.get("refinance") is True and goal != "merchant_cash_advance"
        if amount is None:
            payments_known = False
        else:
            monthly = amount * factors[frequency]
            current += monthly
            if not retire:
                retained += monthly
        if retire:
            if balance is None:
                payoffs_known = False
            else:
                payoff += balance
    current_value = current if payments_known else None
    retained_value = retained if payments_known else None
    payoff_value = payoff if payoffs_known else None
    if not payments_known or not payoffs_known:
        check("debt", "missing", "Complete the current debt payments and balances being refinanced, or explicitly confirm there is no debt.")
    else:
        check("debt", "ready", "Every entered payment remains self-reported until the debt schedule and payoffs are verified.")
    if requested is None and extra is not None and payoff_value is not None:
        requested = (payoff_value + extra) / (1 - opts["fee_pct"] / 100)
    if requested is None or requested <= 0:
        check("requested_amount", "missing", "Enter the gross financing amount or the additional cash needed above known payoffs.")
        requested = None

    raw_banks = answers.get("bank_months") or []
    if not isinstance(raw_banks, list) or len(raw_banks) > 6:
        raise ValueError("Provide the recent monthly statement rows.")
    rows: list[dict] = []
    months_seen: set[str] = set()
    now = datetime.now(ZoneInfo("America/New_York"))
    current_month = now.year * 12 + now.month - 1
    for row in raw_banks:
        if not isinstance(row, dict):
            raise ValueError("Each bank month must be an object.")
        month = row.get("month")
        if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month) or month in months_seen:
            raise ValueError("Bank statements must identify distinct YYYY-MM periods.")
        months_seen.add(month)
        parsed = {key: number(row.get(key), negative=key in {"beginning_balance", "ending_balance"}) for key in ("deposits", "beginning_balance", "ending_balance", "nsf_count")}
        if parsed["nsf_count"] is not None and not parsed["nsf_count"].is_integer():
            raise ValueError("NSF counts must be whole numbers.")
        rows.append(parsed)
    expected_months = set(range(current_month - 3, current_month))
    supplied_months = {int(month[:4]) * 12 + int(month[5:]) - 1 for month in months_seen}
    recent_periods = supplied_months == expected_months
    if len(rows) == 3 and not recent_periods:
        check("bank_periods", "missing", "Use the three most recent completed statement months. Nonconsecutive, stale or future months cannot establish current banking readiness.")

    def average(key: str) -> float | None:
        values = [row[key] for row in rows]
        return sum(values) / 3 if len(values) == 3 and all(value is not None for value in values) else None

    avg_begin, avg_end, avg_deposits = (average(key) for key in ("beginning_balance", "ending_balance", "deposits"))
    nsfs = sum(row["nsf_count"] for row in rows) if len(rows) == 3 and all(row["nsf_count"] is not None for row in rows) else None
    if len(rows) != 3 or any(value is None for value in (avg_begin, avg_end, avg_deposits)):
        check("banking", "missing", "Complete three recent statement months to compare beginning balances, ending balances and deposits.")
    else:
        check("banking", "review" if not recent_periods or avg_end < 0 or (nsfs is not None and nsfs > 0) else "ready", "Statement snapshots describe liquidity, not average daily balance, profit or lender approval.")
    count = number(answers.get("active_mca_count"))
    if count is not None and not count.is_integer():
        raise ValueError("Active MCA count must be a whole number.")
    if count is not None and raw_debts is not None and count != sum(debt["type"] == "mca" for debt in debts):
        check("position_debt_count", "missing", "The active MCA count does not match the listed MCA obligations. Complete the debt schedule before relying on capacity or payoff estimates.")
        current_value = retained_value = payoff_value = None
    if goal == "mca_refinance" and count == 0:
        check("refinance_goal", "missing", "MCA refinancing requires an existing active MCA. Confirm the count or choose a new-funding or term-loan goal.")
    modified = answers.get("modified_mca")
    if modified is not None and not isinstance(modified, bool):
        raise ValueError("Modified MCA must be yes, no or unknown.")
    if goal in {"mca_refinance", "merchant_cash_advance"}:
        if count is None:
            check("positions", "missing", "Confirm the number of current active MCAs.")
        elif count > position_limit:
            manual = True
            check("positions", "review", "More than two existing MCAs needs manual review; a modeled amount is not a standard-program approval.")
    if goal == "mca_refinance" and ((requested is not None and requested > mca_refinance_policy.MAX_AMOUNT) or (payoff_value is not None and payoff_value > mca_refinance_policy.MAX_AMOUNT) or opts["term_months"] > mca_refinance_policy.MAX_TERM_MONTHS):
        manual = True
        check("refinance_amount", "review", "The standard MCA refinance route is up to $350,000; this request needs manual review.")
    if goal == "merchant_cash_advance":
        prior_year = mca_funding_policy.required_tax_year()
        if answers.get("tax_year") != prior_year or period != 12:
            check("funding_tax_period", "missing", "Confirm reported income from the prior calendar-year business return for the current new-MCA funding profile.")
        if modified is True or (tax_income is not None and tax_income < 0):
            manual = True
            check("funding_review", "review", "Modified active advances or negative reported tax income fall outside the current new-funding profile; discuss alternatives with the desk.")
        elif (count != 0 and modified is None) or tax_income is None:
            check("funding_review", "missing", "Confirm modified-advance status and prior-calendar-year reported business income before addbacks.")

    capacity_payment = max(0, annual / (12 * opts["coverage_buffer"]) - retained_value) if annual is not None and retained_value is not None else None
    if goal != "merchant_cash_advance" and annual is not None and capacity_payment == 0:
        manual = True
        check("payment_capacity", "review", "The entered earnings and retained debt leave no new monthly payment capacity under these assumptions.")
    check("verification", "review", "Credit, ownership, industry, legal standing, bank conduct and lender terms remain subject to application review.")
    scenarios = []
    for index, rate in enumerate(opts["annual_rates_pct"] if goal != "merchant_cash_advance" else [None] * 3):
        factor = factor_examples[index] if goal == "merchant_cash_advance" else None
        capacity = present_value(capacity_payment, rate, opts["term_months"]) if rate is not None and capacity_payment is not None else None
        amount = min(requested, capacity) if requested is not None and capacity is not None else requested
        duration = opts["mca_duration_months"] if factor is not None else opts["term_months"]
        monthly = None
        total = None
        if amount is not None:
            if factor is not None:
                total = amount * factor
                monthly = total / duration if duration else None
            else:
                monthly = payment(amount, rate, duration)
                total = monthly * duration
        proceeds = amount * (1 - opts["fee_pct"] / 100) if amount is not None else None
        net = proceeds - payoff_value if proceeds is not None and payoff_value is not None else None
        gap = max(0, (requested - amount) if requested is not None and amount is not None else 0, -net if net is not None else 0) if amount is not None else None
        difference = current_value - retained_value - monthly if goal != "merchant_cash_advance" and payoff_value and net is not None and net >= 0 and monthly is not None and current_value is not None and retained_value is not None else None
        scenarios.append({"key": f"scenario_{index + 1}", "label": f"{factor:.2f}× factor" if factor else f"{rate:g}% annual-rate scenario",
                          "annual_rate_pct": rate, "factor": factor, "term_months": duration,
                          "modeled_capacity": money(capacity), "financing_amount": money(amount),
                          "monthly_payment": money(monthly), "total_repayment": money(total),
                          "net_cash": money(max(0, net)) if net is not None else None,
                          "funding_gap": money(gap), "monthly_difference": money(difference)})
    shortfalls = [item["funding_gap"] for item in scenarios if item["funding_gap"] is not None]
    if goal != "merchant_cash_advance" and any(value > 0 for value in shortfalls):
        check("payoff_funding", "review", "Some scenarios do not cover the requested financing or planned payoffs. Payment relief is suppressed where the entered obligations cannot be fully retired.")
        if len(shortfalls) == 3 and all(value > 0 for value in shortfalls):
            manual = True
    status = "manual_review" if manual else "needs_information" if missing else "preliminary"
    headline = {"manual_review": "A specialist should review your numbers", "needs_information": "Your preliminary picture needs a few more details", "preliminary": "Your preliminary funding scenarios are ready"}[status]
    return {"version": 1, "goal": goal, "status": status, "headline": headline,
            "summary": "Explore the numbers, then continue to the matching AI application. These self-reported scenarios are not offers or approvals.",
            "earnings_basis": basis, "metrics": {"annual_earnings": money(annual), "monthly_payment_capacity": money(capacity_payment),
            "current_monthly_payments": money(current_value), "retained_monthly_payments": money(retained_value),
            "payoff_total": money(payoff_value), "average_beginning_balance": money(avg_begin), "average_ending_balance": money(avg_end),
            "average_monthly_deposits": money(avg_deposits), "nsf_count": nsfs}, "scenarios": scenarios, "readiness": readiness,
            "missing_information": missing, "assumptions": opts, "application_path": APPLICATION_PATHS[goal],
            "disclosures": ["Illustration only. No credit decision, approval probability, offer, or commitment is made.",
            "The coverage buffer and rate/term examples are editable modeling assumptions, not universal lender requirements.",
            "Reported profit and known addbacks are not verified EBITDA; deposits are not business earnings.",
            "Beginning and ending statement averages are not average daily balance. Daily debt equivalents assume 21 business days and weekly equivalents 52/12 weeks per month.",
            "Factor multipliers are not APR. Optional MCA duration shows a hypothetical monthly equivalent, not a promised term or debit schedule.",
            "Fees are excluded unless entered. Lower monthly payments can increase total cost; payoffs and final lender terms require verification."]}
