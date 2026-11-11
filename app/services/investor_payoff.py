"""Investor receipt illustrations using the canonical contractual schedule.

No principal is profit; no interest after the payoff horizon is collected.
Payoff terms are explicit staff assumptions, not a lender payoff statement.
"""
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal(0)
CENT = Decimal(".01")


def dec(value):
    return Decimal(str(value)) if value is not None else ZERO


def amount(value):
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP)) if value is not None else None


def cash_interest(row):
    """Only interest supported by cash after principal, never unpaid accrual."""
    principal = row.get("total_principal")
    if principal is None:
        principal = dec(row.get("principal")) + dec(row.get("maturity_balloon"))
    return min(dec(row.get("interest")), max(ZERO, dec(row.get("cash_payment")) - dec(principal)))


def payoff_schedule(schedule, terms, horizon):
    """Keep actual due rows, then collect remaining principal and stub interest."""
    result = deepcopy(schedule)
    ppy = dec(schedule["periods_per_year"])
    original = schedule["rows"]
    last_due = dec(original[-1]["period"]) * 12 / ppy
    horizon = min(dec(horizon), last_due)
    rows = [deepcopy(row) for row in original if dec(row["period"]) * 12 / ppy <= horizon]
    previous = dec(rows[-1]["period"]) * 12 / ppy if rows else ZERO
    remaining = dec(rows[-1].get("balance_after_cash")) if rows else dec(schedule["amount"])
    elapsed = horizon - previous
    base = dec(schedule["amount"]) if schedule.get("structure") == "simple_interest" else remaining
    accrued = (base * dec(terms.get("rate_pct")) / 100 * elapsed / 12).quantize(CENT, rounding=ROUND_HALF_UP)
    if elapsed > 0:
        rows.append({"period": float(horizon * ppy / 12), "month": amount(horizon), "payoff_horizon_month": str(horizon), "phase": "early_payoff",
            "payment": amount(remaining + accrued), "cash_payment": amount(remaining + accrued),
            "interest": amount(accrued), "principal": amount(remaining), "total_principal": amount(remaining),
            "balance": 0, "balance_after_cash": 0, "maturity_balloon": 0})
    elif rows and remaining:
        rows[-1]["cash_payment"] = amount(dec(rows[-1]["cash_payment"]) + remaining)
        rows[-1]["total_principal"] = amount(dec(rows[-1].get("total_principal")) + remaining)
        rows[-1]["balance_after_cash"] = 0
    result.update(rows=rows, balloon=0, total_interest=amount(sum((cash_interest(row) for row in rows), ZERO)),
                  total_paid=amount(sum((dec(row["cash_payment"]) for row in rows), ZERO)))
    return result, horizon


def _summary(calculation, budget, horizon, *, missing=None, minimum_charge=ZERO, payoff_income=ZERO, reserve_release=None):
    missing = list(missing or [])
    gross = calculation.get("investor_gross_interest")
    fees = calculation.get("investor_fees")
    initial = calculation.get("investor_initial_outlay")
    net = calculation.get("investor_net_income")
    receipts = calculation.get("investor_gross_receipts")
    reserves = calculation.get("investor_reserves")
    flows = deepcopy(calculation.get("cash_flows") or [])
    released = sum((dec(flow.get("reserve_release")) for flow in flows), ZERO)
    if reserve_release is False:
        released = ZERO
        for flow in flows:
            release = dec(flow.get("reserve_release"))
            flow["reserve_release"] = 0
            if flow.get("net_cash_flow") is not None:
                flow["net_cash_flow"] = amount(dec(flow["net_cash_flow"]) - release)
    if flows and payoff_income:
        flows[-1]["payoff_charge_income"] = amount(payoff_income)
        if flows[-1].get("net_cash_flow") is not None:
            flows[-1]["net_cash_flow"] = amount(dec(flows[-1]["net_cash_flow"]) + payoff_income)
    net = dec(net) + payoff_income if net is not None and not missing else None
    cash_known = bool(flows) and all(flow.get("net_cash_flow") is not None for flow in flows) and not missing
    cash_profit = sum((dec(flow.get("net_cash_flow")) for flow in flows), ZERO) if cash_known else None
    known_fees = sum((dec(row.get("total_amount")) for row in calculation.get("rows") or []
                     if row.get("status") == "calculated" and any(str(source.get("id")) == row["id"]
                         and source.get("payer") == "investor" and source.get("category") != "escrow_reserve"
                         for source in budget.get("rows") or [])), ZERO)
    return {"status": "calculated" if net is not None else "needs_review", "horizon_months": amount(dec(horizon)),
        "payoff_month": amount(dec(horizon)), "principal_returned": amount(dec(receipts) - dec(gross)) if receipts is not None and gross is not None else None,
        "gross_interest": gross, "earned_interest": amount(dec(gross) - minimum_charge) if gross is not None else None,
        "minimum_interest_charge": amount(minimum_charge), "payoff_charge_income": amount(payoff_income),
        "total_fees": fees, "known_fee_subtotal": amount(known_fees), "initial_outlay": initial,
        "scheduled_net_income": amount(dec(gross) - known_fees + payoff_income) if gross is not None else None,
        "net_income": amount(net), "net_roi_pct": amount(net / dec(initial) * 100) if net is not None and initial is not None and dec(initial) > 0 else None,
        "net_cash_profit": amount(cash_profit), "net_cash_received": amount(cash_profit + dec(initial)) if cash_profit is not None and initial is not None else None,
        "reserve_released": amount(released), "reserve_held": amount(max(ZERO, dec(reserves) - released)) if reserves is not None else None,
        "rows": deepcopy(calculation.get("rows") or []), "cash_flows": flows, "missing_assumptions": missing,
        "assumptions": ["Illustration assumes scheduled payments and full principal collection; not a payoff quote or guaranteed return.",
            "Returned principal and reserve assets are not profit. Fees are deducted once; no fee refund is assumed.",
            "Net profit retains held reserves as assets; cash profit after initial outlay is shown separately. Fees round to cents per collection interval.",
            "No future interest is collected after payoff unless an explicit minimum-interest policy requires payment.",
            "Losses, taxes, reinvestment and unrecorded charges are excluded; ROI is cumulative, not annualized."]}


def scenarios(budget, terms, schedule, full_calculation, use_of_funds=None):
    from app.services.deal_budget import calculate

    term = dec(terms.get("term_months"))
    full = _summary(full_calculation, budget, term)
    horizon = min(Decimal(6), term)
    if not schedule or not schedule.get("rows"):
        return {"six_month_payoff": _summary(full_calculation, budget, horizon, missing=["repayment_schedule"]), "full_term": full}
    last_due = dec(schedule["rows"][-1]["period"]) * 12 / dec(schedule["periods_per_year"])
    full.update(horizon_months=amount(last_due), payoff_month=amount(last_due), requested_term_months=amount(term),
                cadence_adjustment_note=schedule.get("cadence_adjustment_note"))
    if last_due <= horizon:
        early = deepcopy(full)
        early.update(horizon_months=amount(horizon), payoff_month=amount(last_due), capped_at_maturity=True)
        return {"six_month_payoff": early, "full_term": full}
    policy = budget.get("payoff_terms") or {}
    missing = []
    if policy.get("interest_policy", "unknown") == "unknown":
        missing.append("interest_policy")
    charge = policy.get("charge_amount")
    if charge is None:
        missing.append("charge_amount")
    if charge is not None and dec(charge) > 0 and policy.get("charge_recipient", "unknown") == "unknown":
        missing.append("charge_recipient")
    shortened, payoff_month = payoff_schedule(schedule, terms, horizon)
    minimum_charge = ZERO
    if policy.get("interest_policy") == "minimum_months":
        minimum = policy.get("minimum_interest_months")
        if minimum is None or dec(minimum) > term:
            missing.append("minimum_interest_months")
        else:
            minimum_schedule, _ = payoff_schedule(schedule, terms, minimum)
            minimum_charge = max(ZERO, dec(minimum_schedule["total_interest"]) - dec(shortened["total_interest"]))
            if minimum_charge:
                last = shortened["rows"][-1]
                last["cash_payment"] = amount(dec(last["cash_payment"]) + minimum_charge)
                last["interest"] = amount(dec(last["interest"]) + minimum_charge)
                shortened["total_interest"] = amount(dec(shortened["total_interest"]) + minimum_charge)
    scoped = deepcopy(budget)
    for row in scoped.get("rows") or []:
        if row.get("payer") == "investor" and row.get("category") == "escrow_reserve":
            # Determine asset release explicitly below, never infer a refund.
            row["reserve_release"] = "maturity"
    calculated = calculate(scoped, terms, use_of_funds, _schedule_override=shortened, _with_scenarios=False)
    reserves = calculated.get("investor_reserves")
    release = policy.get("reserve_release", "unknown")
    if reserves is not None and dec(reserves) > 0 and release == "unknown":
        missing.append("reserve_release")
    principal = dec(calculated.get("gross_loan_amount"))
    share = dec(calculated.get("investor_funding_amount")) / principal if principal > 0 else ZERO
    investor_charge = dec(charge) * share if charge is not None and policy.get("charge_recipient") == "investor" else ZERO
    early = _summary(calculated, scoped, horizon, missing=missing, minimum_charge=minimum_charge * share,
                     payoff_income=investor_charge, reserve_release=release == "at_payoff")
    early.update(payoff_month=amount(payoff_month), capped_at_maturity=False, interest_policy=policy.get("interest_policy", "unknown"),
                 requested_term_months=amount(term), cadence_adjustment_note=schedule.get("cadence_adjustment_note"),
                 prepayment_charge=amount(dec(charge)) if charge is not None else None,
                 charge_recipient=policy.get("charge_recipient", "unknown"), reserve_release_policy=release)
    return {"six_month_payoff": early, "full_term": full}
