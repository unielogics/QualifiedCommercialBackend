"""Lazy investor cash illustrations. Never added to saved financial snapshots."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal
from functools import lru_cache
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.services.deal_budget import _schedule, calculate, money, number
from app.services.investor_payoff import cash_interest, scenarios

POLICY_VERSION = "monthly-investor-returns-v1"
CURVE_WORK_LIMIT = 200_000
ZERO = Decimal(0)
ASSUMPTIONS = [
    "Scheduled collections are illustrations, not guaranteed payments. Months are measured from funding, not promised calendar dates.",
    "Returned principal and reserve assets are not profit. Cumulative ROI is not annualized; unrecovered capital is not an investment loss estimate.",
    "Early exit means a hypothetical full borrower payoff under the recorded assumptions, not a note resale, market valuation or guaranteed liquidity.",
    "A specific amount is a pro-rata illustration of offering-level charges, not a negotiated personal quote. Taxes, losses, fee refunds and unrecorded charges are excluded.",
    "Cent rounding is allocated through cumulative receipts so the final period reconciles to the illustrated capital and income.",
]
BUDGET_KEYS = ("revision", "currency", "funding_mode", "investor_funding_amount", "minimum_investor_contribution", "costs_confirmed",
               "loan_amount_mode", "base_loan_amount", "target_net_amount", "servicing_mode", "payoff_terms")
ROW_KEYS = ("id", "enabled", "category", "payer", "method", "value", "basis", "timing", "settlement", "recipient_role",
            "reserve_release", "applicability", "coverage_treatment", "earnings_treatment")
TERM_KEYS = ("amount", "rate_pct", "term_months", "structure", "interest_only_months", "payments_per_year", "repayment_config",
             "currency", "active_proposal", "schedule_unavailable")
EXIT_MONEY = ("principal_returned", "gross_interest", "earned_interest", "minimum_interest_charge", "payoff_charge_income",
              "total_fees", "known_fee_subtotal", "initial_outlay", "net_income", "net_cash_profit", "net_cash_received", "reserve_released", "reserve_held")


def _invalid(message):
    return HTTPException(422, {"code": "invalid_investor_illustration", "message": message})


def pricing_inputs(budget, terms):
    """Keep pricing rules, never borrower names, provider identities or notes."""
    budget, terms = budget or {}, terms or {}
    selected = {key: deepcopy(budget[key]) for key in BUDGET_KEYS if key in budget}
    rows = budget.get("rows") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        selected["costs_confirmed"] = False
        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    selected["rows"] = [{key: deepcopy(row[key]) for key in ROW_KEYS if key in row} for row in rows]
    loan = {key: deepcopy(terms[key]) for key in TERM_KEYS if key in terms}
    if terms.get("payment_note"):
        loan["payment_note"] = "schedule unavailable"
    return selected, loan


def project(budget, terms, *, exit_month=6, investment_amount=None, curve_work_limit=CURVE_WORK_LIMIT):
    horizon = number(exit_month)
    if horizon is None or not 0 < horizon <= 601:
        raise _invalid("Choose a positive payoff month no greater than 601.")
    stake = number(investment_amount)
    if investment_amount is not None and (stake is None or stake <= 0):
        raise _invalid("The illustrated investment must be a positive finite amount.")
    if stake is not None and (stake > Decimal("9999999999999999.99") or stake != stake.quantize(Decimal(".01"))):
        raise _invalid("Use no more than two decimal places for the illustrated investment.")
    budget, terms = pricing_inputs(budget, terms)
    # Immutable, bounded process cache. Routes check ACL before entering here;
    # every pricing-input change yields a different key, and callers get copies.
    payload = json.dumps({"budget": budget, "terms": terms, "exit_month": str(horizon),
        "investment_amount": str(stake) if stake is not None else None, "curve_work_limit": max(0, int(curve_work_limit))},
        sort_keys=True, separators=(",", ":"), default=str)
    return deepcopy(_cached(payload))


def _scaled(value, ratio):
    value = number(value)
    return money(value * ratio) if value is not None else None


def _exit(budget, terms, schedule, calculation, horizon, ratio, stake):
    scenario = scenarios(budget, terms, schedule, calculation, _horizon_months=horizon)["six_month_payoff"]
    row = {key: _scaled(scenario.get(key), ratio) for key in EXIT_MONEY}
    maturity = number(schedule["rows"][-1]["period"]) * 12 / number(schedule["periods_per_year"])
    row.update(month=float(horizon), payoff_month=float(min(horizon, maturity)),
        capped_at_maturity=horizon > maturity,
        status=scenario["status"], missing_assumptions=list(scenario.get("missing_assumptions") or []), assumptions=ASSUMPTIONS[:])
    # Payoff scenarios explicitly assume full remaining principal collection.
    row["principal_returned"] = money(stake)
    if row["net_income"] is not None:
        row["net_income"] = money(number(row["gross_interest"]) - number(row["total_fees"]) + number(row["payoff_charge_income"]))
        row["net_cash_profit"] = money(number(row["net_income"]) - number(row["reserve_held"]))
        row["net_cash_received"] = money(number(row["initial_outlay"]) + number(row["net_cash_profit"]))
    initial = number(row["initial_outlay"])
    row["net_roi_pct"] = money(number(row["net_income"]) / initial * 100) if row["net_income"] is not None and initial and initial > 0 else None
    return row


def _holding(schedule, calculation, ratio, stake, maximum):
    funding, loan = number(calculation["investor_funding_amount"]), number(schedule["amount"])
    share = funding / loan * ratio
    grouped = {}
    ppy = number(schedule["periods_per_year"])
    for event in schedule["rows"]:
        month = max(1, int((number(event["period"]) * 12 / ppy).to_integral_value(rounding=ROUND_CEILING)))
        row = grouped.setdefault(month, {"interest": ZERO, "principal": ZERO})
        row["interest"] += cash_interest(event) * share
        principal = number(event.get("total_principal"))
        if principal is None:
            principal = (number(event.get("principal")) or ZERO) + (number(event.get("maturity_balloon")) or ZERO)
        row["principal"] += min(principal, number(event["cash_payment"])) * share
    flows = {int(row["month"]): row for row in calculation.get("cash_flows") or []}
    net_known = calculation.get("investor_net_income") is not None
    fees_known = calculation.get("investor_fees") is not None
    initial = number(_scaled(calculation.get("investor_initial_outlay"), ratio))
    interest = principal = fees = released = ZERO
    previous_interest = previous_principal = previous_fees = ZERO
    previous_release = ZERO
    rows = []
    for month in range(maximum + 1):
        due, flow = grouped.get(month, {}), flows.get(month, {})
        interest += due.get("interest", ZERO)
        principal += due.get("principal", ZERO)
        fees += (number(flow.get("fees")) or ZERO) * ratio
        released += (number(flow.get("reserve_release")) or ZERO) * ratio
        cumulative_interest, cumulative_principal = number(money(interest)), number(money(principal))
        cumulative_fees, cumulative_release = number(money(fees)), number(money(released))
        if month == maximum:
            cumulative_interest = number(_scaled(calculation.get("investor_gross_interest"), ratio))
            cumulative_principal = min(stake, cumulative_principal)
            if fees_known:
                cumulative_fees = number(_scaled(calculation.get("investor_fees"), ratio))
        paid_interest, paid_principal = cumulative_interest - previous_interest, cumulative_principal - previous_principal
        paid_fees, paid_release = cumulative_fees - previous_fees, cumulative_release - previous_release
        profit = cumulative_interest - cumulative_fees if net_known else None
        distribution = paid_interest + paid_principal - paid_fees + paid_release if net_known else None
        if month == 0:
            distribution = ZERO if net_known else None
        cash = cumulative_interest + cumulative_principal - cumulative_fees + cumulative_release - initial if net_known and initial is not None else None
        # Upfront fees/reserves are already included in the initial outlay.
        cash = cash + number(_scaled((flows.get(0) or {}).get("fees"), ratio)) if cash is not None else None
        rows.append({"month": month, "gross_receipts": money(paid_interest + paid_principal), "interest": money(paid_interest),
            "principal_returned": money(paid_principal), "fees": money(paid_fees) if fees_known else None,
            "reserve_release": money(paid_release) if net_known or not calculation.get("investor_reserves") else None,
            "net_distribution": money(distribution), "net_cash_flow": money(-initial) if month == 0 and initial is not None else money(distribution),
            "cumulative_interest": money(cumulative_interest), "cumulative_principal_returned": money(cumulative_principal),
            "cumulative_fees": money(cumulative_fees) if fees_known else None, "cumulative_net_profit": money(profit),
            "cumulative_net_roi_pct": money(profit / initial * 100) if profit is not None and initial and initial > 0 else None,
            "cumulative_net_cash_flow": money(cash), "outstanding_principal": money(max(ZERO, stake - cumulative_principal))})
        previous_interest, previous_principal, previous_fees, previous_release = cumulative_interest, cumulative_principal, cumulative_fees, cumulative_release
    return rows


@lru_cache(maxsize=32)
def _cached(payload):
    args = json.loads(payload)
    budget, terms = args["budget"], args["terms"]
    horizon = Decimal(args["exit_month"])
    calculation = calculate(budget, terms, _with_scenarios=False)
    funding = number(calculation.get("investor_funding_amount"))
    stake = Decimal(args["investment_amount"]) if args["investment_amount"] is not None else funding
    if funding is not None and stake is not None and stake > funding:
        raise _invalid("The illustrated investment cannot exceed the offering's investor capital.")
    schedule = _schedule({**terms, "amount": calculation.get("gross_loan_amount")})
    source = hashlib.sha256(json.dumps({"policy": POLICY_VERSION, "budget": budget, "terms": terms}, sort_keys=True, default=str).encode()).hexdigest()
    result = {"policy_version": POLICY_VERSION, "status": "unavailable", "currency": budget.get("currency") or terms.get("currency"),
        "pricing_fingerprint": source, "request": {"exit_month": float(horizon), "investment_amount": float(Decimal(args["investment_amount"])) if args["investment_amount"] is not None else None},
        "basis": "pro_rata_illustration" if args["investment_amount"] is not None else "offering_investor_capital",
        "offering_investor_capital": money(funding), "investment_amount": money(stake), "initial_outlay": None,
        "maturity_month": None, "maximum_month": None, "holding": [], "selected_exit": None, "full_term": None,
        "payoff_curve_status": "unavailable", "payoff_curve": [], "missing_inputs": [], "assumptions": ASSUMPTIONS[:],
        "performance": {"curve_work_estimate": 0, "curve_work_limit": args["curve_work_limit"], "curve_points": 0}}
    if not result["currency"]:
        # The legacy calculator permits two absent currencies for compatibility.
        # A new monetary illustration must not turn that into a confirmed quote.
        result.update(currency=None, offering_investor_capital=None, investment_amount=None, missing_inputs=["currency"])
        return result
    if not schedule or not schedule.get("rows") or not funding or funding <= 0 or stake is None:
        result["missing_inputs"] = ["repayment_schedule" if not schedule or not schedule.get("rows") else "investor_capital"]
        return result
    ratio = stake / funding
    maturity = number(schedule["rows"][-1]["period"]) * 12 / number(schedule["periods_per_year"])
    maximum = int(maturity.to_integral_value(rounding=ROUND_CEILING))
    result.update(initial_outlay=_scaled(calculation.get("investor_initial_outlay"), ratio), maturity_month=float(maturity), maximum_month=maximum,
        holding=_holding(schedule, calculation, ratio, stake, maximum),
        selected_exit=_exit(budget, terms, schedule, calculation, horizon, ratio, stake),
        full_term=_exit(budget, terms, schedule, calculation, maturity, ratio, stake))
    work = maximum * len(schedule["rows"]) * max(1, len(budget.get("rows") or []))
    complete = work <= args["curve_work_limit"]
    result["payoff_curve_status"] = "complete" if complete else "on_demand"
    if complete:
        result["payoff_curve"] = [_exit(budget, terms, schedule, calculation, Decimal(month), ratio, stake) for month in range(1, maximum + 1)]
    result["performance"].update(curve_work_estimate=work, curve_points=len(result["payoff_curve"]))
    if calculation.get("investor_net_income") is None:
        result["missing_inputs"].append("investor_costs_or_reserve_policy")
    if result["selected_exit"]["missing_assumptions"]:
        result["missing_inputs"].append("payoff_assumptions")
    if (calculation.get("principal_solution") or {}).get("requires_apply"):
        result["missing_inputs"].append("principal_not_applied")
    result["status"] = "needs_review" if result["missing_inputs"] else "calculated"
    return result


def sourced_projection(budget, terms, *, kind, identifier, updated_at=None, query=None):
    request = query.model_dump() if query is not None else {}
    result = project(budget, terms, **request)
    version = (terms.get("recorded_terms_ref") or {}).get("version") if terms.get("source") != "plan" else None
    source = {"kind": kind, "id": str(identifier), "budget_revision": budget.get("revision"),
              "terms_version": version, "updated_at": updated_at}
    source["fingerprint"] = hashlib.sha256(json.dumps({**source, "pricing": result.pop("pricing_fingerprint")},
        sort_keys=True, default=str).encode()).hexdigest()
    return {**result, "source": source}


async def staff_projection(db, profile, *, query, preview=None):
    """Use the same canonical context/preview as the existing budget editor."""
    from app.services import deal_budget, financial_cockpit

    cockpit = await financial_cockpit.load_cockpit(db, profile)
    loan = dict(cockpit.get("proposed_terms") or {})
    budget = deal_budget.snapshot(profile)
    if preview is not None:
        budget = preview.model_dump(mode="json", exclude={"expected_revision", "loan", "financial_plan"})
        budget["revision"] = preview.expected_revision
        requested_loan = preview.loan.model_dump(mode="json") if preview.loan is not None else None
        plan = deal_budget._loan_plan(preview.financial_plan if preview.financial_plan is not None else (cockpit.get("plan") or {}).get("record"), requested_loan)
        loan = financial_cockpit.financing_preview(cockpit, plan, budget)["loan"]
        if requested_loan is not None and "payments_per_year" in requested_loan and requested_loan["payments_per_year"] is None:
            loan = {**loan, "payments_per_year": None, "schedule_unavailable": True}
    return await run_in_threadpool(sourced_projection, budget, loan, kind="staff_preview" if preview is not None else "staff_saved",
        identifier=profile.id, updated_at=deal_budget.snapshot(profile).get("updated_at"), query=query)


def listing_projection(listing, *, query, member=False):
    """Never reprice a member offer from today's live file or draft budget."""
    snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
    def record(value):
        return value if isinstance(value, dict) else {}

    financials = record(snapshot.get("financials"))
    terms = record(financials.get("proposed_terms"))
    report = record(financials.get("deal_budget"))
    basis = report.get("basis")
    unknown_basis = False
    if basis == "recorded_terms":
        budget = record(report.get("recorded"))
    elif basis == "staff_proposal":
        budget = record(report.get("current"))
    elif basis == "unrecorded_terms":
        budget = {}
    else:
        budget = record(terms.get("deal_budget_snapshot")) if basis is None else {}
        unknown_basis = not budget
    if unknown_basis:
        terms = {}
    result = sourced_projection(budget, terms, kind="published_listing" if member else "listing_saved",
        identifier=listing.ref if member else listing.id, updated_at=snapshot.get("refreshed_at"), query=query)
    if not financials:
        result["missing_inputs"] = ["saved_financial_snapshot"]
    elif unknown_basis:
        result["missing_inputs"] = ["saved_cost_basis"]
    return result


async def read_listing_projection(listing, *, query, member=False):
    # No ORM access from the pricing worker and no event-loop pause for a curve.
    saved = SimpleNamespace(id=listing.id, ref=listing.ref, source_snapshot=deepcopy(listing.source_snapshot))
    return await run_in_threadpool(listing_projection, saved, query=query, member=member)
