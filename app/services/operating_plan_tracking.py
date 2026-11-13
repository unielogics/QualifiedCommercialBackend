"""Pure NOI objective progress and cash assignment, without financing addbacks."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.schemas.earnings_improvement import cost_amount, source_duplicate_missing
from app.services.financial_metric_review import number
from app.services.released_cash_flow import source

FACTORS = {"weekly": 52, "biweekly": 26, "semi_monthly": 24, "monthly": 12, "quarterly": 4, "annual": 1}
OUTSIDE = {"loan_payments", "income_taxes", "owner_draws", "transfers", "depreciation_amortization"}
CENT = Decimal(".01")


def _d(value):
    numeric = number(value)
    return Decimal(str(numeric)) if numeric is not None else None


def _money(value):
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP)) if value is not None else None


def goal_progress(goal, evaluation):
    goal, evaluation = goal or {}, evaluation or {}
    baseline = evaluation.get("baseline") or {}
    currency = goal.get("currency")
    source_currency = baseline.get("noi_currency") if "noi_currency" in baseline else (baseline.get("noi_governance") or {}).get("currency")
    target = _d(goal.get("amount")) if goal.get("status") == "set" else None
    comparable = bool(currency and source_currency and currency == source_currency)
    current = _d(baseline.get("noi")) if comparable else None
    proposed = _d(((evaluation.get("projection") or {}).get("proposed") or {}).get("noi")) if comparable else None
    reviewed = _d(((evaluation.get("projection") or {}).get("approved") or {}).get("noi")) if comparable else None
    status = "awaiting_staff" if target is None else "currency_mismatch" if source_currency and currency != source_currency else "source_unavailable" if current is None else "calculated"
    result = {"metric": "annual_noi", "status": status, "goal_revision": goal.get("revision", 0), "currency": currency,
        "current_noi": _money(current), "zero": 0, "proposed_noi": _money(proposed), "reviewed_noi": _money(reviewed),
        "target": _money(target), "current_year": baseline.get("noi_year"),
        "notes": ["Annual NOI is compared on the confirmed source currency. A source loss remains negative, not zero.",
            "Debt repayment relief funds cash uses; it does not increase operating income or close this NOI gap."]}
    for key, value in (("current", current), ("proposed", proposed), ("reviewed", reviewed)):
        result[key + "_gap"] = _money(max(Decimal(0), target - value)) if target is not None and value is not None else None
        result[key + "_met"] = value >= target if target is not None and value is not None else None
    active = [row for row in evaluation.get("actions", []) if not row.get("excluded") and row.get("review_state") != "rejected"]
    changes = evaluation.get("expense_changes") or {}
    rows = [row for row in changes.get("rows", []) if row.get("action_ids")]
    ordinary = bool(active and all(row.get("kind") in {"expense_reduction", "expense_increase"} for row in active)
        and changes.get("source_comparable") is True and (changes.get("summary") or {}).get("complete")
        and rows and all(row.get("source_group_key") and row.get("status") == "calculated" for row in rows)
        and {row["id"] for row in active} == {identifier for row in rows for identifier in row.get("action_ids", [])})
    draft = _d((changes.get("gross_projection") or {}).get("noi")) if comparable and ordinary else None
    result.update(draft_noi_before_implementation=_money(draft),
        draft_gap=_money(max(Decimal(0), target - draft)) if target is not None and draft is not None else None,
        draft_basis="before_implementation_costs_and_timing", draft_met=draft >= target if draft is not None and target is not None else None)
    return result


def _increment(row, action, result):
    """One canonical positive recurring cost increment, never an income loss."""
    if not result or not (result.get("proposed_math_valid") or result.get("included_in_approved_projection")):
        return None
    calculation = result.get("calculation") or {}
    if action.get("kind") == "expense_group_change":
        leaf = next((item for item in (calculation.get("command") or {}).get("allocations", []) if item["expense_row_id"] == row["id"]), None)
        return max(Decimal(0), -_d(leaf["gross_annual_effect"])) / 12 if leaf and _d(leaf.get("gross_annual_effect")) is not None else None
    if action.get("kind") == "unit_volume":
        leaf = next((item for item in (calculation.get("unit_economics") or {}).get("rows", []) if item["id"] == row["id"]), None)
        before, after = (_d(leaf.get("current_annual_amount")), _d(leaf.get("proposed_annual_amount"))) if leaf else (None, None)
        return max(Decimal(0), after - before) / 12 if before is not None and after is not None else None
    if action.get("expense_row_id") != row["id"]:
        return None
    if action.get("kind") == "marketing_investment":
        spend = _d(calculation.get("incremental_spend"))
        return max(Decimal(0), spend) * FACTORS[action["cadence"]] / 12 if spend is not None and action.get("cadence") in FACTORS else None
    if action.get("kind") not in {"expense_reduction", "expense_increase", "product_cost_change", "processing_cost_change"} or action.get("cadence") not in FACTORS:
        return None
    gross = _d(result.get("gross_annual_impact"))
    return max(Decimal(0), -gross) / 12 if gross is not None else None


def cash_bridge(body, cockpit, evaluation=None):
    """Saved loan + modeled operations; allocation never mutates either model."""
    body, cockpit, evaluation = body or {}, cockpit or {}, evaluation or {}
    current = source(cockpit)
    loan = ((cockpit.get("plan") or {}).get("evaluation") or {})
    before, after = _d(current["before_monthly"]), _d(current["after_monthly"])
    operating, financing = _d(loan.get("annual_new_operating_expenses")), _d(loan.get("annual_new_financing_fees"))
    reasons = list(current["reasons"])
    currency = ((cockpit.get("debt") or {}).get("service") or {}).get("currency")
    if current["saved"] and currency != "USD":
        reasons.append("Confirm USD as the saved debt-service currency before assigning recurring loan relief.")
    if current["saved"] and (operating is None or financing is None):
        reasons.append("Confirm the saved loan's recurring operating and financing charges, including explicit zero charges.")
    gross = before - after if before is not None and after is not None else None
    net = gross - operating / 12 if gross is not None and operating is not None else None
    available = bool(current["available"] and operating is not None and financing is not None and currency == "USD")
    capacity = max(Decimal(0), net).quantize(CENT, rounding=ROUND_HALF_UP) if available and net is not None else None
    allocations = (body.get("released_cash_flow") or {}).get("allocations") or []
    acknowledgement = (body.get("released_cash_flow") or {}).get("source_fingerprint")
    retained = bool(allocations or acknowledgement)
    stale = bool(retained and acknowledgement != current["fingerprint"])
    required = bool(current["positive"] or current["requires_reconciliation"] or retained)
    missing = []
    if required:
        if not available:
            missing.extend(reasons)
        if stale:
            missing.append("released_cash_flow.source_fingerprint.stale")
        if not acknowledgement:
            missing.append("released_cash_flow.source_fingerprint")
    expenses = {row["id"]: row for row in body.get("expenses", []) if not row.get("excluded")}
    duplicate_ids = {path.split(".")[1] for path in source_duplicate_missing(body)}
    actions = {row["id"]: row for row in body.get("adjustments", []) if not row.get("excluded") and row.get("status") != "rejected"}
    results = {row["id"]: row for row in evaluation.get("actions", [])}
    linked, capped = [], {}
    for allocation in allocations:
        identifier = str(allocation["id"])
        prefix = "released_cash_flow.allocations." + identifier
        amount, purpose = _d(allocation.get("amount_monthly")), allocation.get("purpose")
        expense, action = expenses.get(allocation.get("expense_row_id")), actions.get(allocation.get("action_id"))
        issues, cap = [], None
        if not allocation.get("label"):
            issues.append(prefix + ".label")
        if amount is None or amount <= 0:
            issues.append(prefix + ".amount_monthly")
        if purpose not in {"existing_cost", "planned_increment", "reserve", "other"}:
            issues.append(prefix + ".purpose")
        elif purpose in {"reserve", "other"}:
            if allocation.get("expense_row_id") or allocation.get("action_id"):
                issues.append(prefix + ".cash_assignment_only")
        elif expense is None or expense.get("id") in duplicate_ids or expense.get("category") in OUTSIDE or expense.get("cadence") not in FACTORS:
            issues.append(prefix + ".expense_row_id")
        elif purpose == "existing_cost":
            from app.services.earnings_improvement import _cost_issues

            cost = _d(cost_amount(expense))
            cap = cost * FACTORS[expense["cadence"]] / 12 if cost is not None and not _cost_issues(expense) else None
            if allocation.get("action_id"):
                issues.append(prefix + ".existing_cost_has_no_action")
        elif action is None:
            issues.append(prefix + ".action_id")
        else:
            cap = _increment(expense, action, results.get(action["id"]))
        if purpose in {"existing_cost", "planned_increment"}:
            cap = cap.quantize(CENT, rounding=ROUND_HALF_UP) if cap is not None else None
            if cap is None:
                issues.append(prefix + ".capacity_unknown")
            elif cap <= 0:
                issues.append(prefix + ".positive_cost_capacity")
            key = (purpose, allocation.get("expense_row_id"), allocation.get("action_id") if purpose == "planned_increment" else None)
            capped.setdefault(key, []).append((identifier, amount, cap))
        linked.append({"id": identifier, "purpose": purpose, "expense_row_id": allocation.get("expense_row_id"),
            "action_id": allocation.get("action_id"), "amount_monthly": _money(amount), "capacity_monthly": _money(cap),
            "status": "incomplete" if issues else "assigned", "issues": issues})
    for rows in capped.values():
        if all(amount is not None and cap is not None for _, amount, cap in rows):
            total = sum((amount for _, amount, _ in rows), Decimal(0))
            if total > rows[0][2]:
                for identifier, _, _ in rows:
                    item = next(item for item in linked if item["id"] == identifier)
                    item["issues"].append("released_cash_flow.allocations." + identifier + ".linked_capacity_exceeded")
                    item["status"] = "incomplete"
    amounts = [_d(row.get("amount_monthly")) for row in allocations]
    allocated = sum(amounts, Decimal(0)) if all(value is not None for value in amounts) else None
    remaining = capacity - allocated if capacity is not None and allocated is not None else None
    missing.extend(issue for row in linked for issue in row["issues"])
    if required and available and remaining != 0:
        missing.append("released_cash_flow.allocations.full_release")
    if required and body.get("currency") != "USD":
        missing.append("released_cash_flow.currency")
    active = [row for row in body.get("adjustments", []) if not row.get("excluded") and row.get("status") != "rejected"]
    totals = (evaluation.get("totals") or {}).get("proposed") or {}
    recurring = Decimal(0) if not active else _d(totals.get("cash_annual_impact", totals.get("annual_impact"))) if totals.get("cash_complete", totals.get("complete")) else None
    first_year = Decimal(0) if not active else _d(totals.get("cash_first_year_impact", totals.get("first_year_impact"))) if totals.get("cash_complete", totals.get("complete")) else None
    operations = recurring / 12 if recurring is not None else None
    combined = net + operations if available and net is not None and operations is not None else None
    loan_missing = list(dict.fromkeys(reasons)) if not available else []
    loan_fix_fields = []
    if not current["saved"]:
        loan_fix_fields.append("coverage_plan.saved")
    if before is None or after is None or (loan.get("ebitda") or {}).get("replacement_payment_missing"):
        loan_fix_fields.append("coverage_plan.debt_service")
    if currency != "USD":
        loan_fix_fields.append("debt_service.currency")
    if operating is None or financing is None:
        loan_fix_fields.append("coverage_plan.recurring_charges")
    principal = loan.get("principal_solution") or {}
    if principal and (principal.get("status") != "calculated" or principal.get("requires_apply")):
        loan_fix_fields.append("coverage_plan.principal")
    if not available and not loan_fix_fields:
        loan_fix_fields.append("coverage_plan.source_reconciliation")
    observed_complete = all(value is not None for value in (before, after, operating, financing)) and currency == "USD"
    loan_status = "not_saved" if not current["saved"] else "calculated" if available else "source_blocked" if observed_complete else "incomplete"
    operations_missing = [f"adjustments.{row['id']}.qualified_cash_effect" for row in active] if recurring is None else []
    explicit_window = bool(body.get("projection_start") and all(row.get("effective_date") and row.get("implementation_cost") is not None for row in active))
    modeled_first_year = first_year if explicit_window or not active else None
    complete = not missing
    return {"status": "stale" if stale else "not_saved" if not current["saved"] else "unavailable" if not available else "not_required" if not required else "complete" if complete else "incomplete",
        "available": available, "required": required, "complete": complete, "stale": stale,
        "source_fingerprint": current["fingerprint"], "currency": currency,
        "before_debt_service_monthly": _money(before), "after_debt_service_monthly": _money(after),
        "new_operating_charges_monthly": _money(operating / 12) if operating is not None else None,
        "new_financing_fees_monthly": _money(financing / 12) if financing is not None else None,
        "gross_loan_relief_monthly": _money(gross), "net_loan_cash_change_monthly": _money(net) if available else None,
        "recurring_cash_released_monthly": _money(capacity), "modeled_operations_monthly": _money(operations),
        "net_operations_first_year": _money(first_year), "combined_incremental_monthly": _money(combined),
        "allocated_monthly": _money(allocated), "unallocated_monthly": _money(remaining),
        "linked_allocations": linked, "allocations": linked, "missing": list(dict.fromkeys(missing)),
        "loan_status": loan_status, "loan_missing": loan_missing, "loan_fix_fields": loan_fix_fields,
        "before_debt_service_annual": _money(before * 12) if before is not None else None,
        "after_debt_service_annual": _money(after * 12) if after is not None else None,
        "gross_loan_relief_annual": _money(gross * 12) if gross is not None else None,
        "new_operating_charges_annual": _money(operating), "new_financing_fees_annual": _money(financing),
        "net_loan_cash_change_annual": _money(net * 12) if available and net is not None else None,
        "recurring_cash_released_annual": _money(capacity * 12) if capacity is not None else None,
        "allocated_annual": _money(allocated * 12) if allocated is not None else None,
        "unallocated_annual": _money(remaining * 12) if remaining is not None else None,
        "operations_status": "no_changes" if not active else "incomplete" if recurring is None else "calculated",
        "operations_missing": operations_missing,
        "modeled_operations_annual": _money(recurring), "modeled_operations_first_year": _money(modeled_first_year),
        "combined_incremental_annual": _money(combined * 12) if combined is not None else None,
        "combined_incremental_first_year": None,
        "run_rate_note": "Annual loan and combined cash amounts are twelve-month stabilized run-rate equivalents, not a dated first-year or year-end bank balance. Retaining cash or reserving it for an asset does not create revenue or profit.",
        "notes": ["Signed loan cash change is current regular debt service less replacement regular debt service and new operating charges. Financing fees already included in debt service are not deducted again.",
            "Operations are conditional recurring net changes; first-year timing and setup costs stay separate.",
            "Combined incremental cash is an illustration, not a bank balance or an NOI addback. Cash assignments do not create expenses or duplicate their modeled earnings effect."]}


def public_summary(evaluation):
    """Captured amounts for saved documents; no audit actors or source hashes."""
    from copy import deepcopy

    result = {}
    if "goal_progress" in evaluation:
        fields = ("metric", "status", "goal_revision", "currency", "current_noi", "zero", "proposed_noi", "reviewed_noi",
            "target", "current_year", "current_gap", "proposed_gap", "reviewed_gap", "current_met", "proposed_met",
            "reviewed_met", "draft_noi_before_implementation", "draft_gap", "draft_basis", "draft_met", "notes")
        result["goal_progress"] = {key: deepcopy(evaluation["goal_progress"].get(key)) for key in fields}
    if "cash_bridge" in evaluation:
        fields = ("status", "available", "required", "complete", "stale", "currency", "before_debt_service_monthly",
            "after_debt_service_monthly", "new_operating_charges_monthly", "new_financing_fees_monthly", "gross_loan_relief_monthly",
            "net_loan_cash_change_monthly", "recurring_cash_released_monthly", "modeled_operations_monthly", "net_operations_first_year",
            "combined_incremental_monthly", "allocated_monthly", "unallocated_monthly", "missing", "notes")
        value = evaluation["cash_bridge"]
        result["cash_bridge"] = {key: deepcopy(value.get(key)) for key in fields}
        rows = [{key: deepcopy(row.get(key)) for key in ("id", "purpose", "expense_row_id", "action_id", "amount_monthly", "capacity_monthly", "status", "issues")}
                for row in value.get("allocations", [])]
        result["cash_bridge"].update(allocations=rows, linked_allocations=deepcopy(rows))
    return result
