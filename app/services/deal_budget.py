"""One audited cost schedule; funding math never changes the debt contract."""
import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.schemas.deal_budget import DealBudgetApply, DealBudgetPatch, DealBudgetRead, DealCostRow

EDIT_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
ZERO = Decimal(0)
CENT = Decimal(".01")


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def money(value):
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP)) if value is not None else None


def origination_points(budget, loan_amount):
    """Comparable platform origination charge, including fixed dollar fees."""
    loan = number(loan_amount)
    points, unresolved = ZERO, False
    for row in budget.get("rows") or []:
        if not row.get("enabled", True) or row.get("payer") != "borrower" or row.get("category") != "origination" or (row.get("recipient_role") or "platform") != "platform":
            continue
        value = number(row.get("value"))
        if value is None:
            unresolved = True
        elif row.get("method") == "percent":
            points += value
        elif loan is not None and loan > 0 and (budget.get("currency") or "USD") == "USD":
            points += value / loan * 100
        else:
            unresolved = True
    return points, unresolved


async def enforce_origination_cap(db, profile, budget, loan_amount):
    from app.services.funding_programs import enforce_qc_fee_cap_for_profile

    points, unresolved = origination_points(budget, loan_amount)
    if not any(row.get("enabled", True) and row.get("payer") == "borrower" and row.get("category") == "origination" and (row.get("recipient_role") or "platform") == "platform" for row in budget.get("rows") or []):
        return {"equivalent_points": 0.0, "requires_loan_or_pricing": False}
    await enforce_qc_fee_cap_for_profile(db, profile, points)
    return {"equivalent_points": float(points) if not unresolved else None, "requires_loan_or_pricing": unresolved}


def require_staff(user):
    if user.role not in EDIT_ROLES:
        raise HTTPException(403, "Only authorized underwriting staff can manage deal costs")


def legacy_row(profile, field, value):
    points = field == "forecast_fee_points"
    return DealCostRow(id=uuid5(NAMESPACE_URL, f"deal-budget:{profile.id}:{field}"),
        label="QC origination points" if points else "Consulting fee", category="origination" if points else "consulting",
        method="percent" if points else "fixed", value=value, payer="borrower", basis="loan_amount", timing="closing",
        settlement="withheld", origin="legacy_setting", source_ref=f"profile.{field}", legacy_field=field).model_dump(mode="json")


def snapshot(profile):
    raw = getattr(profile, "deal_budget", None)
    if isinstance(raw, dict) and raw:
        return deepcopy(raw)
    # Existing staff pricing is adopted once, never added again beside itself.
    rows = [legacy_row(profile, field, number(getattr(profile, field, None)))
        for field in ("forecast_fee_points", "forecast_consulting_fee") if number(getattr(profile, field, None)) is not None]
    return {"schema_version": 1, "revision": 0, "funding_mode": "single_investor", "investor_funding_amount": None,
        "minimum_investor_contribution": None, "costs_confirmed": False, "currency": "USD", "rows": rows,
        "note": None, "updated_at": None, "updated_by_user_id": None}


def _schedule(terms):
    from app.services.loan_schedule import build_schedule

    if terms.get("active_proposal") is False or terms.get("schedule_unavailable") or terms.get("payment_note") or terms.get("amount") is None or terms.get("rate_pct") is None or not terms.get("term_months") or "payments_per_year" in terms and terms["payments_per_year"] is None:
        return None
    try:
        return build_schedule(float(terms["amount"]), float(terms["rate_pct"]), int(terms["term_months"]),
            structure=terms.get("structure"), interest_only_months=terms.get("interest_only_months") or 0,
            periods_per_year=terms.get("payments_per_year") or 12, repayment_config=terms.get("repayment_config"))
    except (ValueError, TypeError, OverflowError):
        return None


def _borrower_closing_rows(budget, settlements):
    return [row for row in budget.get("rows") or [] if row.get("enabled", True)
        and row.get("payer", "borrower") == "borrower" and row.get("timing", "closing") == "closing"
        and row.get("settlement", "separate") in settlements]


def _closing_amount(row, gross, budget, terms):
    value = number(row.get("value"))
    if value is None:
        return None
    if row.get("method", "fixed") == "fixed":
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    basis = row.get("basis", "loan_amount")
    if basis in {"loan_amount", "outstanding_principal"}:
        base = gross
    elif basis == "investor_funding":
        base = number(budget.get("investor_funding_amount"))
        base = gross if base is None else base
    elif basis == "interest":
        schedule = _schedule({**terms, "amount": money(gross)})
        base = number(schedule.get("total_interest")) if schedule else None
    else:
        base = None
    return (value * base / 100).quantize(CENT, rounding=ROUND_HALF_UP) if base is not None else None


def solve_principal(budget, terms_dict):
    """Solve the smallest feasible cent principal without feeding gross back as base.

    Each fee is rounded separately. Its affine rounding error bounds a finite
    candidate window; scanning that window handles one-cent net discontinuities
    which ordinary binary search incorrectly assumes away. Exotic interest-based
    gross-ups and excessive near-100% windows remain explicitly unresolved.
    """
    budget, terms = budget or {}, terms_dict or {}
    mode = budget.get("loan_amount_mode") or "gross_loan"
    current = number(terms.get("amount")) if terms.get("active_proposal") is not False else None
    base, target = number(budget.get("base_loan_amount")), number(budget.get("target_net_amount"))
    anchor = current if mode == "gross_loan" else base if mode == "base_loan" else target
    result = {"mode": mode, "status": "unknown", "input_amount": money(anchor), "base_loan_amount": money(base),
        "target_net_amount": money(target), "current_loan_amount": money(current), "gross_loan_amount": None,
        "financed_closing_costs": None, "withheld_closing_costs": None, "net_client_cash": None,
        "funding_variance": None, "requires_apply": False, "issues": []}
    if anchor is None or anchor <= 0:
        result["issues"].append("Enter a positive gross loan, explicit base loan, or target net amount for the selected mode.")
        return result
    if (budget.get("currency") or "USD") != (terms.get("currency") or "USD"):
        result["issues"].append("Budget and loan currencies differ; no gross-up or currency conversion is assumed.")
        return result
    financed = _borrower_closing_rows(budget, {"financed"})
    withheld = _borrower_closing_rows(budget, {"withheld"})
    solved_rows = financed + (withheld if mode == "target_net" else [])
    gross = anchor
    if mode != "gross_loan":
        constant, slope, dynamic_count = ZERO, ZERO, 0
        for row in solved_rows:
            value = number(row.get("value"))
            if value is None:
                result["issues"].append(f"{row.get('label') or 'Closing cost'}: amount or rate is unknown.")
                return result
            basis = row.get("basis", "loan_amount")
            if row.get("method", "fixed") == "fixed":
                constant += value.quantize(CENT, rounding=ROUND_HALF_UP)
            elif basis in {"loan_amount", "outstanding_principal"} or basis == "investor_funding" and number(budget.get("investor_funding_amount")) is None:
                slope += value / 100
                dynamic_count += 1
            elif basis == "investor_funding":
                constant += (value * number(budget["investor_funding_amount"]) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
            else:
                result["issues"].append("Automatic gross-up supports fixed closing costs and percentages of principal or known investor funding. Review interest-based closing costs using a fixed gross loan.")
                return result
        if slope >= 1:
            result["status"] = "impossible"
            result["issues"].append("Closing costs consume 100% or more of each additional loan dollar; no positive gross-up can deliver this amount.")
            return result
        estimate = (anchor + constant) / (1 - slope)
        error = Decimal(dynamic_count) * Decimal(".005") / (1 - slope)
        first = max(anchor, (estimate - error).quantize(CENT, rounding=ROUND_CEILING))
        last = (estimate + error).quantize(CENT, rounding=ROUND_CEILING)
        if last > Decimal("999999999999"):
            result["status"] = "impossible"
            result["issues"].append("The required principal exceeds the supported loan amount.")
            return result
        if (last - first) / CENT > 100_000:
            result["issues"].append("The aggregate percentage is too close to 100% for a bounded cent-exact gross-up. Reduce the rate or enter and review a fixed gross principal.")
            return result
        gross = first
        while gross <= last:
            total = sum((_closing_amount(row, gross, budget, terms) for row in solved_rows), ZERO)
            if gross - total >= anchor:
                break
            gross += CENT
        if gross > last:
            result["issues"].append("No cent-rounded principal could be verified; enter a fixed gross loan for review.")
            return result
    def total(rows):
        amounts = [_closing_amount(row, gross, budget, terms) for row in rows]
        return sum(amounts, ZERO) if all(value is not None for value in amounts) else None
    financed_total, withheld_total = total(financed), total(withheld)
    net = gross - financed_total - withheld_total if financed_total is not None and withheld_total is not None else None
    delivered = gross - financed_total if mode == "base_loan" and financed_total is not None else net
    result.update(status="calculated", gross_loan_amount=money(gross), financed_closing_costs=money(financed_total),
        withheld_closing_costs=money(withheld_total), net_client_cash=money(net),
        funding_variance=money(delivered - anchor) if mode != "gross_loan" and delivered is not None else None,
        requires_apply=current is None or gross != current)
    return result


def assert_principal_matches(budget, terms):
    solution = solve_principal(budget, terms)
    if solution["status"] != "calculated":
        raise HTTPException(422, " ".join(solution["issues"]) or "The loan principal cannot be calculated from these costs.")
    if solution["requires_apply"]:
        raise HTTPException(409, "Apply the calculated gross principal to the shared loan before saving its terms or coverage plan.")
    return solution


def allocation_reconciliation(net_client_cash, use_of_funds):
    """Current use allocations against a supplied immutable proceeds figure."""
    net = number(net_client_cash)
    uses = list(use_of_funds) if isinstance(use_of_funds, list) else []
    amounts = [number(item.get("amount")) for item in uses if item.get("category") != "closing_fees"]
    allocated = sum(amounts, ZERO) if uses and all(item is not None for item in amounts) else None
    variance = net - allocated if net is not None and allocated is not None else None
    return {"allocated_uses": money(allocated), "available_for_uses": money(net), "variance": money(variance),
        "status": "unknown" if variance is None else "overallocated" if variance < 0 else "balanced" if variance == 0 else "unallocated"}


def scoped_allocation_reconciliation(net_client_cash, terms, requested_uses):
    """A client request is not a smaller or different financing proposal."""
    proposal = terms.get("proposal_use_of_funds")
    loan, requested = number(terms.get("amount")), number(terms.get("requested_amount"))
    proposal_basis = number(terms.get("proposal_use_of_funds_amount"))
    requested_reference = allocation_reconciliation(None, requested_uses)
    metadata = {"requested_amount": money(requested), "proposed_loan_amount": money(loan),
        "requested_uses_total": requested_reference["allocated_uses"]}
    if isinstance(proposal, dict) and proposal:
        proposal = [{"category": "debt_refinance" if key == "debt_payoff" else key, "amount": value}
            for key, value in proposal.items() if key != "other_label" and value not in (None, "")]
    if isinstance(proposal, list) and proposal:
        result = allocation_reconciliation(net_client_cash, proposal)
        if proposal_basis is not None and loan is not None and proposal_basis != loan:
            return {**metadata, "scope": "proposed_reference", "status": "different_scope", "allocated_uses": None,
                "available_for_uses": money(number(net_client_cash)), "variance": None,
                "condition": "Recorded proposal uses belong to a different facility amount. Confirm closing allocations for the current proposal."}
        # The existing dealer schedule is a gross-facility budget. Without an
        # explicit fee category, its 'other' purpose cannot be relabeled as a
        # closing cost merely because a separate fee schedule was supplied.
        total = sum((number(row.get("amount")) or ZERO for row in proposal), ZERO)
        net = number(net_client_cash)
        if loan is not None and net is not None and net < loan and total == loan and not any(row.get("category") == "closing_fees" for row in proposal):
            return {**result, **metadata, "scope": "proposed_gross_budget", "status": "needs_closing_reconciliation",
                "variance": None, "condition": "Approved gross-facility uses are recorded. Confirm which uses include closing deductions and the final net disbursement allocation."}
        return {**result, **metadata, "scope": "proposed_loan"}
    if "requested_amount" in terms and (requested is None or loan is None or requested != loan):
        return {**metadata, "scope": "requested_reference", "status": "different_scope" if requested is not None and loan is not None else "unknown",
            "allocated_uses": None, "available_for_uses": money(number(net_client_cash)), "variance": None,
            "condition": "Requested uses are a reference, not closing allocations for this proposal. Confirm proposed-loan uses before closing; the client request is unchanged."}
    return {**allocation_reconciliation(net_client_cash, requested_uses), **metadata, "scope": "requested_budget"}


def calculate(budget, terms_dict, use_of_funds=None):
    """Contractual receipt illustration, with actual declining principal bases.

    Monthly/annual rates accrue pro rata over repayment intervals. A rate on
    interest is applied to interval interest once, without annualizing twice.
    Reserves remain held assets and never become fees or investor income.
    """
    budget, terms = budget or {}, dict(terms_dict or {})
    principal = solve_principal(budget, terms)
    if principal["status"] == "calculated":
        terms["amount"] = principal["gross_loan_amount"]
    elif principal["mode"] != "gross_loan":
        terms["amount"] = None
    loan = number(terms.get("amount")) if terms.get("active_proposal") is not False else None
    funding = number(budget.get("investor_funding_amount"))
    funding = loan if funding is None else funding
    mode = budget.get("funding_mode") or "single_investor"
    minimum = funding if mode == "single_investor" else number(budget.get("minimum_investor_contribution"))
    issues = list(principal["issues"])
    if principal["requires_apply"]:
        issues.append("The solved gross principal is a working preview; save the budget and shared loan together before issuing or publishing terms.")
    if loan is None or loan <= 0:
        issues.append("Enter an active gross loan amount.")
    if funding is not None and loan is not None and funding > loan:
        issues.append("Investor funding exceeds the gross loan amount.")
    if minimum is None or funding is None or minimum <= 0 or minimum > funding:
        issues.append("Set a positive minimum contribution no greater than investor funding.")
    confirmed = bool(budget.get("costs_confirmed"))
    if not confirmed:
        issues.append("Confirm the cost schedule, including any decision that no additional charges apply.")
    currency_matches = (budget.get("currency") or "USD") == (terms.get("currency") or "USD")
    if not currency_matches:
        confirmed = False
        funding, minimum = None, None
        issues.append("Budget and loan currencies differ; no currency conversion is assumed.")
    schedule = _schedule(terms)
    if schedule is None:
        issues.append("A complete repayment schedule is required for recurring costs and investor returns.")
    cap_points, cap_unresolved = origination_points(budget, loan)
    cap_unresolved = cap_unresolved or not currency_matches
    if cap_unresolved:
        issues.append("Platform origination pricing or its loan basis is unknown; the published fee cap cannot be confirmed.")
    share = funding / loan if funding is not None and loan is not None and 0 < funding <= loan else None
    schedule_rows = schedule["rows"] if schedule else []
    ppy = number(schedule.get("periods_per_year")) if schedule else None
    gross_interest = number(schedule.get("total_interest")) * share if schedule and share is not None else None
    gross_receipts = sum((number(row["cash_payment"]) for row in schedule_rows), ZERO) * share if schedule and share is not None else None
    buckets = {key: ZERO for key in ("borrower_withheld_fees", "borrower_financed_fees", "borrower_paid_separately", "borrower_recurring_fees",
        "borrower_withheld_reserves", "borrower_financed_reserves", "borrower_separate_reserves", "escrow_reserves", "investor_fees", "investor_closing_fees", "investor_reserves", "sponsor_costs",
        "platform_fee_revenue", "management_revenue", "borrower_closing_costs", "investor_closing_costs", "sponsor_closing_costs")}
    unknown = set()
    monthly = {}
    for row in schedule_rows:
        month = max(1, int((number(row["period"]) * 12 / ppy).to_integral_value(rounding="ROUND_CEILING")))
        flow = monthly.setdefault(month, {"month": month, "gross_receipts": ZERO, "interest": ZERO, "fees": ZERO, "reserve_release": ZERO})
        if share is not None:
            flow["gross_receipts"] += number(row["cash_payment"]) * share
            flow["interest"] += number(row["interest"]) * share
    results = []
    borrower_monthly_fees = {}
    borrower_monthly_operating = {}
    unknown_new_financing, unknown_new_operating = False, False
    unknown_investor_reserve = False
    for row in budget.get("rows") or []:
        row_issues = []
        enabled = row.get("enabled", True)
        if enabled and not currency_matches:
            row_issues.append("The cost and loan currencies differ; convert reviewed amounts before calculating.")
        value = number(row.get("value"))
        timing, payer = row.get("timing", "closing"), row.get("payer", "borrower")
        reserve = row.get("category") == "escrow_reserve"
        total, allocations = ZERO, []
        if enabled and value is None:
            row_issues.append("Amount or rate is unknown.")
        if enabled and value is not None:
            events = [(None, ZERO)] if timing == "closing" else [(item, number(item["period"]) * 12 / ppy) for item in schedule_rows]
            if timing != "closing" and not schedule_rows:
                row_issues.append("Repayment timing is unknown.")
            opening, previous_month = loan, ZERO
            for event, due_month in events:
                factor = Decimal(1) if timing == "closing" else (due_month - previous_month) / (12 if timing == "annual" else 1)
                basis = row.get("basis", "loan_amount")
                base = loan if basis == "loan_amount" else funding if basis == "investor_funding" else opening if basis == "outstanding_principal" else number(event["interest"]) if event else number(schedule.get("total_interest")) if schedule else None
                if basis in {"outstanding_principal", "interest"} and payer == "investor":
                    base = base * share if base is not None and share is not None else None
                if row.get("method") == "percent":
                    amount = value * base / 100 if base is not None else None
                    if basis == "interest":
                        factor = Decimal(1)
                else:
                    amount = value
                if amount is None:
                    row_issues.append("The selected calculation base is unknown.")
                    break
                amount = (amount * factor).quantize(CENT, rounding=ROUND_HALF_UP)
                total += amount
                if event:
                    month = max(1, int(due_month.to_integral_value(rounding="ROUND_CEILING")))
                    allocations.append((month, amount))
                    opening = number(event["balance_after_cash"])
                previous_month = due_month
        settlement = row.get("settlement", "separate")
        key = ("borrower_withheld_reserves" if settlement == "withheld" else "borrower_financed_reserves" if settlement == "financed" else "borrower_separate_reserves") if reserve and payer == "borrower" else "escrow_reserves" if reserve else (
            "borrower_withheld_fees" if timing == "closing" and settlement == "withheld" else "borrower_financed_fees" if timing == "closing" and settlement == "financed" else "borrower_paid_separately" if timing == "closing" else "borrower_recurring_fees") if payer == "borrower" else "investor_fees" if payer == "investor" else "sponsor_costs"
        new_borrower = enabled and payer == "borrower" and timing != "closing" and not reserve and row.get("coverage_treatment", "new_cost") != "already_in_baseline"
        operating = row.get("earnings_treatment", "financing_cost") == "operating_expense"
        keys = {key}
        if reserve:
            keys.add("escrow_reserves")
            if payer == "investor":
                keys.add("investor_reserves")
                if enabled and row.get("reserve_release", "unknown") != "maturity" and (row_issues or total > 0):
                    unknown_investor_reserve = True
                    issues.append(f"{row.get('label') or 'Reserve'}: Investor reserve release timing is unknown.")
        elif payer == "investor" and timing == "closing":
            keys.add("investor_closing_fees")
        if not reserve:
            if timing == "closing":
                keys.add(f"{payer}_closing_costs")
            recipient = row.get("recipient_role") or ("platform" if row.get("category") in {"origination", "consulting", "servicing", "management"} else "third_party")
            if recipient == "platform":
                keys.add("platform_fee_revenue")
                if row.get("category") in {"servicing", "management"}:
                    keys.add("management_revenue")
        if row_issues and enabled:
            if new_borrower:
                if operating:
                    unknown_new_operating = True
                else:
                    unknown_new_financing = True
            unknown.update(keys)
            issues.extend(f"{row.get('label') or 'Cost row'}: {issue}" for issue in dict.fromkeys(row_issues))
        else:
            for key in keys:
                buckets[key] += total
            if payer == "investor" and not reserve:
                for month, amount in allocations:
                    monthly[month]["fees"] += amount
            if new_borrower:
                for month, amount in allocations:
                    flow = borrower_monthly_operating if operating else borrower_monthly_fees
                    flow[month] = flow.get(month, ZERO) + amount
            if payer == "investor" and reserve and enabled and row.get("reserve_release") == "maturity" and monthly:
                monthly[max(monthly)]["reserve_release"] += total
        results.append({"id": str(row.get("id")), "total_amount": None if row_issues else money(total),
            "closing_amount": None if row_issues else money(total if timing == "closing" else ZERO),
            "recurring_total": None if row_issues else money(total if timing != "closing" else ZERO),
            "status": "excluded" if not enabled else "unknown" if row_issues else "calculated", "issues": list(dict.fromkeys(row_issues))})
    def known(key):
        return buckets[key] if confirmed and key not in unknown else None
    withheld, held = known("borrower_withheld_fees"), known("borrower_withheld_reserves")
    financed, financed_held = known("borrower_financed_fees"), known("borrower_financed_reserves")
    net_client = loan - withheld - held - financed - financed_held if all(value is not None for value in (loan, withheld, held, financed, financed_held)) else None
    fees, upfront, reserves = known("investor_fees"), known("investor_closing_fees"), known("investor_reserves")
    initial = funding + upfront + reserves if funding is not None and upfront is not None and reserves is not None else None
    net_income = gross_interest - fees if gross_interest is not None and fees is not None and not unknown_investor_reserve else None
    annual_borrower_fees = (max((sum((value for due, value in borrower_monthly_fees.items() if end - 12 < due <= end), ZERO)
        for end in borrower_monthly_fees), default=ZERO) if confirmed and not unknown_new_financing else None)
    annual_operating = (max((sum((value for due, value in borrower_monthly_operating.items() if end - 12 < due <= end), ZERO)
        for end in borrower_monthly_operating), default=ZERO) if confirmed and not unknown_new_operating else None)
    coverage = terms.get("coverage_context") or {}
    numerator, denominator = number(coverage.get("income_annual")), number(coverage.get("debt_service_annual"))
    fee_denominator, coverage_method = None, "unavailable"
    if denominator is not None and annual_borrower_fees is not None:
        new_service = number(coverage.get("new_loan_annual_debt_service"))
        if schedule and new_service is not None and denominator >= new_service:
            loan_monthly = {}
            for item in schedule_rows:
                due = max(1, int((number(item["period"]) * 12 / ppy).to_integral_value(rounding="ROUND_CEILING")))
                cash = item["cash_payment"] if schedule.get("repayment_config") is not None else item["payment"]
                loan_monthly[due] = loan_monthly.get(due, ZERO) + number(cash)
            combined = {month: loan_monthly.get(month, ZERO) + borrower_monthly_fees.get(month, ZERO)
                for month in loan_monthly.keys() | borrower_monthly_fees.keys()}
            peak = max((sum((value for due, value in combined.items() if end - 12 < due <= end), ZERO) for end in combined), default=ZERO)
            fee_denominator = denominator - new_service + peak
            coverage_method = "aligned_rolling_12_month_cash_due"
        else:
            fee_denominator = denominator + annual_borrower_fees
            coverage_method = "conservative_sum_of_separate_peaks"
    adjusted_numerator = numerator - annual_operating if numerator is not None and annual_operating is not None else None
    fee_dscr = adjusted_numerator / fee_denominator if adjusted_numerator is not None and fee_denominator is not None and fee_denominator > 0 else None
    if net_client is not None and net_client < 0:
        issues.append("Closing deductions exceed the gross loan; reconcile borrower funding before proceeding.")
    allocation = scoped_allocation_reconciliation(net_client, terms, use_of_funds)
    if allocation["status"] == "overallocated":
        issues.append("Borrower uses exceed cash available after closing deductions; reconcile use of funds.")
    elif allocation["status"] == "unallocated":
        issues.append("Some cash available after closing deductions is unallocated; reconcile use of funds.")
    cash_flows = [{"month": 0, "gross_receipts": 0, "interest": 0, "fees": money(upfront), "reserve_release": 0, "net_cash_flow": money(-initial) if initial is not None else None}]
    cash_flows.extend({**{key: money(value) if key != "month" else value for key, value in flow.items()},
        "net_cash_flow": money(flow["gross_receipts"] - flow["fees"] + flow["reserve_release"]) if fees is not None and not unknown_investor_reserve else None} for flow in monthly.values())
    payment = schedule.get("payment_after") if schedule and schedule.get("payment_after") is not None else schedule.get("payment_io") if schedule else None
    return {"loan_currency": terms.get("currency") or "USD", "gross_loan_amount": money(loan), "investor_funding_amount": money(funding), "funding_mode": mode,
        "principal_solution": principal,
        "scheduled_payment": {"amount": payment, "frequency": terms.get("repayment_frequency"),
            "monthly_equivalent": schedule.get("ordinary_payment_monthly", schedule.get("payment_after_monthly") or schedule.get("payment_io_monthly")) if schedule else None,
            "payments_per_year": schedule.get("periods_per_year") if schedule else None,
            "annual_debt_service": schedule.get("coverage_annual_debt_service") if schedule else None,
            "total_interest": schedule.get("total_interest") if schedule else None,
            "total_repayment": schedule.get("total_paid") if schedule else None,
            "residual_balloon": schedule.get("balloon") if schedule else None},
        "borrower_fee_cash_flows": [{"month": month, "amount": money(value)} for month, value in sorted(borrower_monthly_fees.items())] if annual_borrower_fees is not None else None,
        "borrower_operating_expense_cash_flows": [{"month": month, "amount": money(value)} for month, value in sorted(borrower_monthly_operating.items())] if annual_operating is not None else None,
        "annual_new_operating_expenses": money(annual_operating),
        "other_funding_required": money(max(ZERO, loan - funding)) if loan is not None and funding is not None else None,
        "other_funding_status": "unknown" if loan is None or funding is None else "not_confirmed" if loan > funding else "not_required",
        "platform_origination_equivalent_points": float(cap_points) if not cap_unresolved else None,
        "minimum_investor_contribution": money(minimum), "participation_pct": money(share * 100) if share is not None else None,
        **{key: money(known(key)) for key in buckets}, "net_client_cash": money(net_client),
        "borrower_cash_after_separate_costs": money(net_client - known("borrower_paid_separately") - known("borrower_separate_reserves")) if net_client is not None and known("borrower_paid_separately") is not None and known("borrower_separate_reserves") is not None else None,
        "investor_gross_interest": money(gross_interest), "investor_gross_receipts": money(gross_receipts),
        "investor_initial_outlay": money(initial), "investor_net_income": money(net_income),
        "investor_net_roi_pct": money(net_income / initial * 100) if net_income is not None and initial is not None and initial > 0 else None,
        "investor_gross_roi_pct": money(gross_interest / funding * 100) if gross_interest is not None and funding is not None and funding > 0 else None,
        "allocation_reconciliation": allocation,
        "rows": results, "cash_flows": cash_flows, "status": "needs_review" if issues else "ready", "issues": list(dict.fromkeys(issues)),
        "closing_costs_by_payer": {payer: money(known(f"{payer}_closing_costs")) for payer in ("borrower", "investor", "sponsor")},
        "financing_fee_coverage": {"base_dscr": coverage.get("base_dscr"), "with_financing_fees_dscr": float(round(fee_dscr, 4)) if fee_dscr is not None else None,
            "annual_borrower_financing_fees": money(annual_borrower_fees), "basis": coverage.get("basis"),
            "annual_new_operating_expenses": money(annual_operating), "income_annual_after_new_operating_expenses": money(adjusted_numerator),
            "annual_debt_service_including_fees": money(fee_denominator), "method": coverage_method,
            "assumption": "Future proposal financing fees only; verify these charges are not already in retained debt or the operating earnings baseline. Closing costs and investor-paid fees do not change this denominator."},
        "assumptions": ["Contractual full collection; no guarantee, defaults, taxes or unrecorded fees are assumed.",
            "Investor receives a pro rata share of principal and interest; borrower fees are not assumed to be investor income.",
            "Recurring monthly or annual charges accrue pro rata over actual repayment intervals using opening principal. Rates on interest apply once to scheduled interval interest.",
            "Reserves are held assets, not fees or income. Only explicitly recorded maturity releases are illustrated.",
            "Recorded APR and DSCR/EBITDA/NOI baselines stay unchanged. Explicitly financed closing costs change the proposed principal when applied. New recurring financing costs affect projected coverage; explicit operating expenses affect projected earnings once."]}


def reporting(profile, terms_dict):
    current = snapshot(profile)
    terms = terms_dict or {}
    recorded = terms.get("deal_budget_snapshot") or {}
    has_terms = bool(terms.get("recorded_terms_ref")) or terms.get("source") == "term_sheet"
    revision = terms.get("deal_budget_revision")
    working = terms.get("source") == "plan"
    basis = "staff_proposal" if working else "recorded_terms" if has_terms and revision is not None else "unrecorded_terms" if has_terms else "staff_proposal"
    selected = recorded if has_terms and not working else current
    calculation = deepcopy(terms["deal_budget_calculation"]) if has_terms and terms.get("source") != "plan" and terms.get("deal_budget_calculation") else calculate(selected, terms, getattr(profile, "use_of_funds", None))
    allocation = scoped_allocation_reconciliation(calculation.get("net_client_cash"), terms, getattr(profile, "use_of_funds", None))
    current_coverage = None
    if terms.get("coverage_context"):
        current_coverage = calculate(selected, terms).get("financing_fee_coverage")
        current_coverage["earnings_basis"] = "current_file_pro_forma"
        current_coverage["fee_rules_basis"] = basis
    allocation_issues = []
    if allocation["status"] == "overallocated":
        allocation_issues.append("Current use-of-funds allocations exceed the loan proceeds available after recorded closing deductions.")
    elif allocation["status"] == "unallocated":
        allocation_issues.append("Current use-of-funds allocations leave some loan proceeds unallocated.")
    allocation_conditions = [allocation["condition"]] if allocation.get("condition") else []
    return {"current": current, "recorded": deepcopy(recorded), "basis": basis,
        "stale": bool(has_terms and revision != current.get("revision", 0)),
        "calculation": calculation, "current_financing_fee_coverage": current_coverage,
        "current_allocation_reconciliation": allocation, "current_allocation_issues": allocation_issues,
        "current_allocation_conditions": allocation_conditions}


def _terms_ref(cockpit):
    ref = (cockpit.get("proposed_terms") or {}).get("recorded_terms_ref")
    return {"kind": "application_term_sheet" if ref.get("kind") == "client_terms" else ref.get("kind"),
        "id": str(ref["id"]), "version": ref.get("version")} if isinstance(ref, dict) and ref.get("id") else None


def _loan_plan(record, loan):
    value = deepcopy(record or {})
    if loan is not None:
        for source, target in {"amount": "new_loan_amount", "rate_pct": "new_loan_rate_pct", "term_months": "new_loan_term_months",
            "structure": "new_loan_structure", "interest_only_months": "new_loan_interest_only_months",
            "payments_per_year": "new_loan_payments_per_year", "repayment_config": "new_loan_repayment_config"}.items():
            if source in loan:
                value[target] = loan[source]
        value["new_loan_interest_only"] = loan.get("structure") == "interest_only"
        value["new_payment_monthly"] = None
    return value


async def read(db, profile, user=None, *, budget=None, loan=None, financial_plan=None):
    from app.services import financial_cockpit
    cockpit = await financial_cockpit.load_cockpit(db, profile)
    current_loan = cockpit.get("proposed_terms") or {}
    value = budget if budget is not None else snapshot(profile)
    evaluation = (cockpit.get("plan") or {}).get("evaluation")
    if budget is not None or financial_plan is not None or loan is not None:
        plan = _loan_plan(financial_plan if financial_plan is not None else (cockpit.get("plan") or {}).get("record"), loan)
        preview = financial_cockpit.financing_preview(cockpit, plan, value)
        preview_loan, evaluation = preview["loan"], preview["evaluation"]
        if loan is not None and "payments_per_year" in loan and loan["payments_per_year"] is None:
            # FinancialPlan's ordinary missing cadence defaults to monthly.
            # A partial custom-cadence preview explicitly says unknown instead.
            preview_loan = {**preview_loan, "payments_per_year": None, "schedule_unavailable": True}
            evaluation = None
        loan = preview_loan
    else:
        loan = current_loan
    loan = dict(loan or {})
    if "requested_amount" not in loan:
        from app.services.use_of_funds import source_funding_data
        requested, _source, _purpose, _dealer = await source_funding_data(db, profile)
        loan["requested_amount"] = money(requested)
    calculation = calculate(value, loan, getattr(profile, "use_of_funds", None))
    if isinstance(evaluation, dict) and isinstance(evaluation.get("principal_solution"), dict):
        # The evaluator retains the input principal before solving. Its output
        # loan already contains G, so recalculation alone would falsely say the
        # original working loan needed no application.
        calculation["principal_solution"] = deepcopy(evaluation["principal_solution"])
    return DealBudgetRead(profile_id=profile.id, **{key: value.get(key) for key in DealBudgetRead.model_fields if key in value},
        can_edit=bool(user and user.role in EDIT_ROLES), suggested_rows=await suggestions(db, profile, value),
        financing_context={"plan_updated_at": (getattr(profile, "financial_plan", None) or {}).get("updated_at"),
            "terms_ref": _terms_ref(cockpit), "current_loan_amount": current_loan.get("amount")},
        financial_plan={"profile_id": str(profile.id), "obligations": (cockpit.get("debt") or {}).get("obligations", []), **(cockpit.get("plan") or {})},
        financial_plan_evaluation=evaluation,
        calculation=calculation)


async def suggestions(db, profile, budget):
    """Published settings are suggestions, never additional automatic charges."""
    if not isinstance(db, AsyncSession):
        return []
    from app.models.application_profile import ApplicationProgramSelection
    from app.models.funding_program import FundingProgramCatalog, FundingProgramCommercialTerms
    from app.models.production_package import ProductionPackage
    rows = budget.get("rows") or []
    result = []
    if not any(row.get("category") == "origination" and row.get("payer") == "borrower" for row in rows):
        selected = select(ApplicationProgramSelection.program_key).where(ApplicationProgramSelection.profile_id == profile.id,
            ApplicationProgramSelection.removed_at.is_(None))
        defaults = (await db.execute(select(FundingProgramCatalog.program_key, FundingProgramCommercialTerms.qc_fee_default_percent)
            .join(FundingProgramCommercialTerms, FundingProgramCommercialTerms.program_id == FundingProgramCatalog.id)
            .where(FundingProgramCatalog.program_key.in_(selected), FundingProgramCommercialTerms.status == "published",
                FundingProgramCommercialTerms.qc_fee_default_percent.is_not(None)))).all()
        # Multiple conflicting defaults require choosing a program first.
        if defaults and len({number(value) for _, value in defaults}) == 1:
            key, value = defaults[0]
            suggestion = legacy_row(profile, "forecast_fee_points", value)
            suggestion.update(origin="program_default", source_ref=f"funding_program:{key}")
            result.append(suggestion)
    if not any(row.get("category") in {"management", "servicing"} for row in rows):
        package = (await db.execute(select(ProductionPackage).where(ProductionPackage.profile_id == profile.id,
            ProductionPackage.stage == 1, ProductionPackage.status != "void"))).scalar_one_or_none()
        amount = number((package.arrangement or {}).get("mgmt_fee")) if package else None
        if amount is not None:
            result.append(DealCostRow(id=uuid5(NAMESPACE_URL, f"deal-budget:{profile.id}:programme-management"),
                label="Programme management", category="management", payer="sponsor", value=amount, timing="monthly",
                recipient_role="platform", origin="production_arrangement", source_ref=f"production_package:{package.id}:mgmt_fee").model_dump(mode="json"))
    return result


def project_legacy(profile, budget):
    for field in ("forecast_fee_points", "forecast_consulting_fee"):
        category, method = ("origination", "percent") if field == "forecast_fee_points" else ("consulting", "fixed")
        rows = [row for row in budget.get("rows") or [] if row.get("enabled", True) and row.get("payer") == "borrower"
            and row.get("timing") == "closing" and row.get("basis") == "loan_amount" and row.get("category") == category
            and row.get("method") == method and (row.get("recipient_role") or "platform") == "platform"]
        values = [number(row.get("value")) for row in rows]
        setattr(profile, field, sum(values, ZERO) if values and all(value is not None for value in values) else None)


def _timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) and value.utcoffset() is not None else value


def _budget_payload(payload):
    return {key: value for key, value in payload.model_dump(mode="json").items()
        if key in DealBudgetPatch.model_fields and key != "expected_revision"}


async def apply_principal(db, profile, payload: DealBudgetApply, user):
    """Save reviewed costs and their shared working principal in one transaction."""
    require_staff(user)
    if payload.currency != "USD":
        raise HTTPException(422, "Loan cost budgets use USD; no currency conversion is performed.")
    with getattr(db, "no_autoflush", nullcontext()):
        locked = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one()
    before = snapshot(locked)
    saved_plan = deepcopy(getattr(locked, "financial_plan", None) or {})
    fingerprint = hashlib.sha256(json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipts = list(before.get("apply_receipts") or [])
    receipt = next((item for item in receipts if item.get("key") == payload.idempotency_key), None)
    if receipt:
        if receipt.get("fingerprint") != fingerprint:
            raise HTTPException(409, "This apply key was already used with different costs or loan inputs.")
        if receipt.get("revision") != before.get("revision") or _timestamp(receipt.get("plan_updated_at")) != _timestamp(saved_plan.get("updated_at")):
            raise HTTPException(409, "This change was already applied and the file has since changed. Reload the current loan.")
        return await read(db, locked, user)
    if before.get("revision", 0) != payload.expected_revision:
        raise HTTPException(409, "Deal costs changed; reload before saving the budget and loan.")
    if _timestamp(saved_plan.get("updated_at")) != _timestamp(payload.expected_plan_updated_at):
        raise HTTPException(409, "The coverage plan changed; reload before applying a new principal.")
    from app.schemas.financial_cockpit import FinancialPlanWrite
    from app.services import financial_cockpit
    from app.services.financial_plan import validate_plan_write
    from app.services.financial_plan_budget import loan_from_plan

    cockpit = await financial_cockpit.load_cockpit(db, locked)
    expected_ref = payload.expected_terms_ref.model_dump(mode="json") if payload.expected_terms_ref else None
    if _terms_ref(cockpit) != expected_ref:
        raise HTTPException(409, "The saved terms version changed; reload before applying the budget and loan.")
    after = _budget_payload(payload)
    record = {**saved_plan, **(payload.financial_plan or {})}
    if payload.financial_plan is None and record.get("new_loan_amount") is None:
        proposed = cockpit.get("proposed_terms") or {}
        recorded = ((cockpit.get("plan") or {}).get("terms_link") or {}).get("recorded")
        if expected_ref:
            from app.services.plan_terms_link import adopt
            # The full recorded form carries refinance treatment and retained
            # service. Older cockpit references expose those beside a bare ref.
            if not isinstance(recorded, dict) or not recorded.get("id"):
                recorded = {**proposed, **(proposed.get("recorded_terms_ref") or {}),
                    "payment_monthly": proposed.get("monthly_payment")}
            record = adopt(record, recorded, by=user.id)
        else:
            record = _loan_plan(record, proposed)
    solution = solve_principal(after, loan_from_plan(record))
    if solution["status"] != "calculated":
        raise HTTPException(422, " ".join(solution["issues"]) or "The principal could not be solved.")
    record.update(new_loan_amount=solution["gross_loan_amount"], new_payment_monthly=None,
        new_payment_source="recorded_terms" if record.get("new_payment_source") == "recorded_terms" else "manual_terms",
        expected_updated_at=payload.expected_plan_updated_at)
    plan_payload = FinancialPlanWrite.model_validate(record)
    validate_plan_write(saved_plan, plan_payload)
    await enforce_origination_cap(db, locked, after, solution["gross_loan_amount"])
    record = plan_payload.model_dump(mode="json", exclude={"expected_updated_at", "expected_deal_budget_revision"})
    stamp = datetime.now(UTC).isoformat()
    record.update(updated_at=stamp, updated_by=str(user.id))
    revision = before.get("revision", 0) + 1
    receipts.append({"key": payload.idempotency_key, "fingerprint": fingerprint, "revision": revision, "plan_updated_at": stamp})
    after.update(schema_version=1, revision=revision, updated_at=stamp, updated_by_user_id=str(user.id), apply_receipts=receipts[-100:])
    locked.deal_budget, locked.financial_plan = after, record
    project_legacy(locked, after)
    from app.services.application_profiles import log_profile_action
    await log_profile_action(db, locked, user, "deal_budget.principal_applied", "Saved deal costs and the calculated shared loan principal together",
        metadata={"before_budget": before, "after_budget": after, "before_plan": saved_plan, "after_plan": record, "principal_solution": solution})
    await db.flush()
    return await read(db, locked, user)


async def update(db, profile, payload: DealBudgetPatch, user):
    require_staff(user)
    if payload.currency != "USD":
        raise HTTPException(422, "Loan cost budgets use USD. Enter reviewed USD amounts before saving; no currency conversion is performed.")
    locked = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one()
    before = snapshot(locked)
    if before["revision"] != payload.expected_revision:
        raise HTTPException(409, "Deal costs changed; reload before saving")
    after = payload.model_dump(mode="json", exclude={"expected_revision"})
    if before.get("apply_receipts"):
        after["apply_receipts"] = deepcopy(before["apply_receipts"])
    # Fixed-dollar platform origination charges share the same published cap.
    loan_amount = (getattr(locked, "financial_plan", None) or {}).get("new_loan_amount")
    if loan_amount is None and any(row["enabled"] and row["category"] == "origination" and row["method"] == "fixed" and row["recipient_role"] == "platform" for row in after["rows"]):
        from app.services.financial_cockpit import load_cockpit
        loan_amount = ((await load_cockpit(db, locked)).get("proposed_terms") or {}).get("amount")
    solution = solve_principal(after, {"amount": loan_amount})
    if after.get("loan_amount_mode") != "gross_loan" and solution["status"] == "calculated" and solution["requires_apply"]:
        raise HTTPException(409, "These costs change the loan principal. Use Save budget and loan to apply both atomically.")
    await enforce_origination_cap(db, locked, after, loan_amount)
    after.update(schema_version=1, revision=payload.expected_revision + 1, updated_at=datetime.now(UTC).isoformat(), updated_by_user_id=str(user.id))
    locked.deal_budget = after
    project_legacy(locked, after)
    from app.services.application_profiles import log_profile_action
    await log_profile_action(db, locked, user, "deal_budget.update", "Updated deal costs and investor funding", metadata={"before": before, "after": after})
    await db.flush()
    return await read(db, locked, user)


async def sync_legacy_write(db, profile, changes, user):
    fields = {key for key in ("forecast_fee_points", "forecast_consulting_fee") if key in changes}
    if not fields:
        return
    from app.services.application_terms import lock_profile_terms
    with getattr(db, "no_autoflush", nullcontext()):
        await lock_profile_terms(db, profile.id)
    if isinstance(db, AsyncSession):
        await db.refresh(profile, attribute_names=["deal_budget"])
    before = snapshot(profile)
    current_projection = SimpleNamespace()
    project_legacy(current_projection, before)
    fields = {field for field in fields if number(getattr(current_projection, field, None)) != number(changes[field])}
    if not fields:
        return
    after = deepcopy(before)
    for field in fields:
        replacement = legacy_row(profile, field, changes[field])
        category, method = ("origination", "percent") if field == "forecast_fee_points" else ("consulting", "fixed")
        qualifying = [row for row in after["rows"] if row.get("enabled", True) and row.get("payer") == "borrower"
            and row.get("category") == category and row.get("method") == method and row.get("basis") == "loan_amount"
            and row.get("timing") == "closing" and (row.get("recipient_role") or "platform") == "platform"]
        if len(qualifying) > 1:
            raise HTTPException(409, "These fees have an itemized budget. Edit its rows rather than replacing the aggregate fee setting.")
        if qualifying:
            chosen = qualifying[0]
            after["rows"] = [{**row, "value": replacement["value"], "legacy_field": field} if row["id"] == chosen["id"] else row for row in after["rows"]]
        else:
            after["rows"].append(replacement)
    after.update(revision=before["revision"] + 1, costs_confirmed=False, updated_at=datetime.now(UTC).isoformat(), updated_by_user_id=str(user.id))
    loan_amount = (getattr(profile, "financial_plan", None) or {}).get("new_loan_amount") or getattr(profile, "underwriting_term_sheet_amount", None)
    solution = solve_principal(after, {"amount": loan_amount})
    if after.get("loan_amount_mode", "gross_loan") != "gross_loan" and solution["status"] == "calculated" and solution["requires_apply"]:
        raise HTTPException(409, "These fee settings change the target-net loan principal. Edit and save the budget and shared loan together.")
    await enforce_origination_cap(db, profile, after, loan_amount)
    profile.deal_budget = after
    from app.services.application_profiles import log_profile_action
    await log_profile_action(db, profile, user, "deal_budget.legacy_fee_sync", "Updated deal costs from recorded fee settings", metadata={"before": before, "after": after})
