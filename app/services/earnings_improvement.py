"""Conditional operating changes beside unchanged source earnings.

This module has no database writer. Version publication and a staff-only
review route supply trusted review fields. Client disclosures and proposals
never become historical P&L facts or program qualification inputs here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import HTTPException

from app.enums import Role
from app.schemas.earnings_improvement import (
    REVIEW_FIELDS,
    action_missing,
    cost_amount,
    expense_missing,
    validate_body,
)
from app.services.financial_metric_review import number

FACTORS = {"weekly": 52, "biweekly": 26, "semi_monthly": 24, "monthly": 12, "quarterly": 4, "annual": 1, "one_time": 0}
NON_OPERATING = {"loan_payments", "income_taxes", "owner_draws", "transfers"}
NON_CASH = {"depreciation_amortization"}
PRODUCT_KINDS = {"price_change", "product_cost_change"}


def _stamp(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _money(value):
    return round(value, 2) if value is not None else None


def _context_values(cockpit):
    from app.services.financial_metric_reporting import noi_governance_brief

    earnings = cockpit.get("ebitda") or {}
    source = earnings.get("headline") or {}
    noi = (cockpit.get("noi") or {}).get("headline") or {}
    service = (cockpit.get("debt") or {}).get("service") or {}
    plan = cockpit.get("plan") or {}
    evaluation = plan.get("evaluation") or {}
    evidence = cockpit.get("operating_evidence") or {}
    return {
        "earnings": {key: source.get(key) for key in ("key", "value", "annualized_value", "period", "currency", "basis", "source_refs")},
        "noi": {key: noi.get(key) for key in ("key", "value", "annualized_value", "period", "currency", "basis", "source_refs")},
        "noi_governance": noi_governance_brief(cockpit.get("noi")),
        "selection": {key: (earnings.get("source_selection") or {}).get(key) for key in ("selected_file_id", "source_stamp", "stale")},
        "debt": {key: service.get(key) for key in ("source", "version_id", "annual", "currency", "identity_association_conflict")},
        "financing": {key: evaluation.get(key) for key in ("active", "debt_service_after_annual", "coverage_unavailable_reason", "loan_terms", "schedule")},
        "legacy_adjustments": (plan.get("record") or {}).get("add_backs"),
        "legacy_overrides": (plan.get("record") or {}).get("overrides"),
        # No generated timestamp: reloading an unchanged file is not a source change.
        "evidence": evidence,
    }


def context_stamp(cockpit):
    return _stamp(_context_values(cockpit))


def _before_guidance(context):
    """Reconstruct only the previous evidence shape for existing receipts.

    New reviews bind the full guidance context. Original source identities,
    analyses, hashes, associations and prior bank/merchant suggestions remain
    dependencies even when the display adds derived expense suggestions.
    """
    context = deepcopy(context)
    evidence = context["evidence"]
    evidence.pop("guidance", None)
    for row in evidence.get("files", []):
        row.pop("eligible_for_guidance", None)
    candidates = []
    for row in evidence.get("expense_candidates", []):
        if row.get("source_group_key") and str(row.get("key") or "").startswith("source:"):
            continue
        row.pop("source_group_key", None)
        row.pop("source_candidate_key", None)
        candidates.append(row)
    if "expense_candidates" in evidence:
        evidence["expense_candidates"] = candidates
    return context


def proposal_stamp(action, linked_expense=None, body=None):
    proposal = {key: value for key, value in action.items() if key not in REVIEW_FIELDS}
    expense = deepcopy(linked_expense)
    # Adding optional unit controls must not stale a previously reviewed total
    # solely because schema validation materialized their empty defaults.
    action_defaults = {"cost_basis": "total", "unit_label": "", "current_units_per_period": None,
        "proposed_units_per_period": None, "current_fixed_amount": None, "proposed_fixed_amount": None,
        "product_name": "", "revenue_return_multiple": None, "unit_change_mode": "absolute", "unit_change_value": None,
        "expense_category": None, "source_group_key": "", "command": None, "price_basis": "per_unit"}
    expense_defaults = {"cost_basis": "total", "unit_label": "", "current_units_per_period": None,
        "current_unit_cost": None, "current_fixed_amount": None, "source_group_key": "", "source_candidate_key": "", "volume_behavior": "unknown", "itemization_parent_id": None}
    if action.get("command"):
        if action.get("kind") in {"expense_reduction", "expense_increase"}:
            proposal["kind"] = "expense_command"
        derived = {"current_amount", "proposed_amount", "proposed_unit_price", "proposed_unit_cost", "proposed_units_per_period", "proposed_fixed_amount"}
        if action["command"]["target"] != "unit_price":
            derived.update({"cadence", "cost_basis", "unit_label", "current_unit_cost", "current_units_per_period", "current_fixed_amount"})
        for key in derived:
            proposal.pop(key, None)
    for row, defaults in ((proposal, action_defaults), (expense, expense_defaults)):
        if row is not None:
            for key, default in defaults.items():
                if row.get(key) == default:
                    row.pop(key, None)
    extra = {}
    if action.get("command"):
        from app.services.operating_plan_commands import participants

        rows = participants(action, body or {})
        parent_ids = {row.get("itemization_parent_id") for row in rows if row.get("itemization_parent_id")}
        extra["command_expenses"] = sorted([deepcopy(row) for row in rows], key=lambda row: str(row["id"]))
        extra["itemization_references"] = sorted([deepcopy(row) for row in (body or {}).get("expenses", []) if row["id"] in parent_ids], key=lambda row: str(row["id"]))
        keys = {row.get("source_group_key") for row in rows}
        extra["historical_corrections"] = [row for row in (body or {}).get("historical_corrections", []) if row["source_group_key"] in keys]
        if action["command"]["target"] == "annual_price_revenue":
            extra["historical_revenue_annual"] = (body or {}).get("historical_revenue_annual")
    if action.get("kind") == "unit_volume":
        extra["unit_inputs"] = {key: (body or {}).get(key) for key in ("unit_label", "historical_units_annual",
            "historical_revenue_annual", "historical_units_period_start", "historical_units_period_end")}
        extra["volume_expenses"] = sorted([
            {key: value for key, value in row.items() if expense_defaults.get(key, object()) != value}
            for row in (body or {}).get("expenses", []) if not row.get("excluded")], key=lambda row: str(row["id"]))
        extra["no_operating_expenses_confirmed"] = (body or {}).get("no_operating_expenses_confirmed", False)
    return _stamp({"proposal": proposal, **extra,
        "expense": expense,
        "scope": {key: (body or {}).get(key) for key in ("business_name", "period_start", "period_end", "projection_start", "currency", "basis")}})


def review_stamp(action, cockpit, linked_expense=None, body=None):
    # A rent/processor/price review binds operating evidence and its timing,
    # not the debt-service denominator. Repricing a loan does not invalidate
    # an otherwise unchanged supported operating improvement.
    context = _context_values(cockpit)
    for key in ("financing", "legacy_adjustments", "legacy_overrides"):
        context.pop(key, None)
    return _stamp({"review_schema": "operating-evidence-v3", "proposal": proposal_stamp(action, linked_expense, body), "context": _stamp(context),
                   "treatment": action.get("baseline_treatment"), "reconciliation": action.get("baseline_reconciliation_note")})


def _prior_v2_review_stamp(action, cockpit, linked_expense=None, body=None):
    context = _before_guidance(_context_values(cockpit))
    for key in ("financing", "legacy_adjustments", "legacy_overrides"):
        context.pop(key, None)
    return _stamp({"review_schema": "operating-evidence-v2", "proposal": proposal_stamp(action, linked_expense, body), "context": _stamp(context),
                   "treatment": action.get("baseline_treatment"), "reconciliation": action.get("baseline_reconciliation_note")})


def _legacy_review_stamp(action, cockpit, linked_expense=None, body=None, *, before_guidance=False):
    """Retain an existing review only while its original context still matches."""
    context = _context_values(cockpit)
    if before_guidance:
        context = _before_guidance(context)
    applied = (((cockpit.get("plan") or {}).get("evaluation") or {}).get("operating_plan") or {})
    if applied.get("applied") and "financing_active_before_operating_plan" in applied:
        # This feature may activate a projection without adding a loan. Ignore
        # only that derived activation when checking an old review receipt;
        # actual loan terms, schedule and debt service still bind the legacy hash.
        context["financing"]["active"] = applied["financing_active_before_operating_plan"]
    return _stamp({"proposal": proposal_stamp(action, linked_expense, body), "context": _stamp(context),
                   "treatment": action.get("baseline_treatment"), "reconciliation": action.get("baseline_reconciliation_note")})


def _annual(amount, cadence):
    value = number(amount)
    return value * FACTORS[cadence] if value is not None and cadence != "one_time" else None


def _period_matches(body, earnings):
    start, end = earnings.get("period_start"), earnings.get("period_end")
    if start and end:
        return body.get("period_start") == str(start)[:10] and body.get("period_end") == str(end)[:10]
    match = re.search(r"\b(?:19|20)\d{2}\b", str(earnings.get("year") or earnings.get("period") or ""))
    return bool(match and body.get("period_start") == f"{match.group()}-01-01" and body.get("period_end") == f"{match.group()}-12-31")


def _active_days(start, horizon):
    end = date(horizon.year + 1, horizon.month, min(horizon.day, 28)) if horizon.month == 2 and horizon.day == 29 else horizon.replace(year=horizon.year + 1)
    return max(0, (end - max(start, horizon)).days) / (end - horizon).days


def _scope_files(cockpit, allowed):
    evidence = cockpit.get("operating_evidence") or {}
    files = {str(row.get("file_id") or row.get("id")): row for row in evidence.get("files", [])}
    permitted = {str(value) for value in allowed} if allowed is not None else set(files)
    return files, permitted


def _near(first, second, tolerance=0.01):
    first, second = number(first), number(second)
    return first is not None and second is not None and abs(first - second) <= tolerance


def _cost_issues(row, side="current"):
    if row.get("cost_basis") != "per_unit":
        return []
    total, reference = cost_amount(row, side), row.get(f"{side}_amount")
    if total is None:
        return [f"Complete {side} quantity, unit cost and fixed amount; unknown fixed charges are not zero."]
    if reference is not None and not _near(total, reference):
        return [f"The {side} unit calculation does not reconcile to its recorded total. Keep one reconciled cost basis before review."]
    return []


def _merchant_approval_issues(action, cockpit, files):
    evidence = cockpit.get("operating_evidence") or {}
    sources = [row for row in evidence.get("merchant_sources", [])
               if str(row.get("file_id")) in action["source_file_ids"]
               and (not action.get("merchant_source_key") or row.get("source_key") == action["merchant_source_key"])]
    issues = []
    if len(sources) != 1 or not sources[0].get("eligible_for_review"):
        return ["Choose one current, business-confirmed merchant statement and processor/account scope for staff review."]
    source = sources[0]
    if not action.get("merchant_source_key") or action["merchant_source_key"] != source.get("source_key"):
        issues.append("Bind the adjustment to the current merchant statement's processor/account source key.")
    try:
        first, last = date.fromisoformat(source["period_start"]), date.fromisoformat(source["period_end"])
        import calendar

        expected_months = 1 if action["cadence"] == "monthly" else 12
        whole = first.day == 1 and last.day == calendar.monthrange(last.year, last.month)[1]
        months = (last.year - first.year) * 12 + last.month - first.month + 1
        if not whole or months != expected_months:
            issues.append("Processing volume must use the same complete monthly or annual period as its source statement.")
    except (KeyError, TypeError, ValueError):
        issues.append("The merchant statement period must be established before staff can approve its fee comparison.")
    if not _near(action["processing_volume"], source.get("volume")):
        issues.append("The processing volume differs from the linked statement's documented gross card volume.")
    if action["current_processing_rate_basis"] != source.get("rate_basis") or not _near(action["current_processing_rate_pct"], source.get("effective_rate_pct"), 0.0001):
        issues.append("The current processing-rate basis and rate must match the linked statement.")
    if action["current_processing_rate_basis"] == "all_in" and source.get("total_fees") is not None:
        fees = action["processing_volume"] * action["current_processing_rate_pct"] / 100
        if not _near(fees, source["total_fees"], max(.05, fees * 0.0001)):
            issues.append("The current all-in rate and volume do not reconcile to the statement's processing fees.")
    if action["current_processing_rate_basis"] == "processor_markup":
        for field, source_key in (("current_processing_fixed_fee", "fixed_fee"), ("current_processing_per_transaction_fee", "per_transaction_fee"), ("processing_transaction_count", "transactions")):
            if not _near(action.get(field), source.get(source_key)):
                issues.append("Current processor markup, fixed fees, item fees and transaction count must each be documented; unknown fees are not zero.")
                break
    offer = next((row for row in evidence.get("merchant_offers", []) if str(row.get("id")) == action.get("merchant_offer_id")), None)
    if offer is None or offer.get("status") in {"withdrawn", "superseded", "declined", "unreadable"}:
        issues.append("Link a current readable merchant pricing offer before staff approves proposed processor savings.")
        return issues
    quote_file = files.get(str(offer.get("source_file_id")))
    if quote_file is None or not quote_file.get("eligible_for_review"):
        issues.append("The linked pricing offer's business association and current source evidence must be accepted.")
    monthly_volume = action["processing_volume"] / (12 if action["cadence"] == "annual" else 1)
    if not _near(monthly_volume, offer.get("current_monthly_volume")):
        issues.append("The quote uses a different or unknown processing volume; reconcile the same volume before applying its fees.")
    if action["proposed_processing_rate_basis"] == "all_in":
        if not _near(action["proposed_processing_rate_pct"], offer.get("proposed_effective_rate_pct"), 0.0001):
            issues.append("The proposed all-in rate must match the linked offer; processor markup is not an all-in rate.")
        if offer.get("proposed_monthly_fees") is not None and not _near(monthly_volume * action["proposed_processing_rate_pct"] / 100, offer["proposed_monthly_fees"], .05):
            issues.append("The quoted proposed monthly fees do not reconcile to its effective rate and processing volume.")
    else:
        markup = number(offer.get("proposed_markup_bps"))
        if markup is None or not _near(action["proposed_processing_rate_pct"], markup / 100, 0.0001):
            issues.append("The proposed processor markup must match the quoted basis points.")
        month_factor = 12 if action["cadence"] == "annual" else 1
        fixed_fee = action["proposed_processing_fixed_fee"]
        if not _near(fixed_fee / month_factor if fixed_fee is not None else None, offer.get("proposed_fixed_monthly_fees")) or not _near(action["proposed_processing_per_transaction_fee"], offer.get("proposed_per_item_fee")):
            issues.append("Proposed fixed and per-item fees must match the current offer, including explicitly stated zero fees.")
    termination = number(offer.get("early_termination_fee"))
    if termination and (action["implementation_cost_cadence"] != "one_time" or action["implementation_cost"] < termination):
        issues.append("Include the quoted termination/switching cost as a separate one-time implementation cost.")
    return list(dict.fromkeys(issues))


def _processing(action, cockpit, issues, risks):
    current_basis, proposed_basis = action["current_processing_rate_basis"], action["proposed_processing_rate_basis"]
    if current_basis == "unknown" or current_basis != proposed_basis:
        issues.append("Compare the same documented all-in or processor-markup basis for current and proposed fees.")
        return None
    volume = action["processing_volume"]
    if volume is None or volume <= 0:
        issues.append("A positive documented gross card-processing volume is required; net settlement deposits are not gross card volume.")
        return None
    sources = (cockpit.get("operating_evidence") or {}).get("merchant_sources") or []
    matched = [row for row in sources if str(row.get("file_id")) in action["source_file_ids"]
               and (not action.get("merchant_source_key") or row.get("source_key") == action["merchant_source_key"])]
    if not matched:
        issues.append("Link the business's current merchant-processing statement before projecting processing savings.")
    if any(row.get("currency") and row.get("currency") != (cockpit.get("_operating_body_currency") or "USD") for row in matched):
        issues.append("Merchant statement and operating-plan currencies differ.")
    if action["cadence"] not in {"monthly", "annual"}:
        issues.append("Use a monthly or annual processing-volume period, matching the statement evidence.")
    if current_basis == "all_in":
        if any(action.get(key) not in {None, 0} for key in (
            "current_processing_fixed_fee", "proposed_processing_fixed_fee", "current_processing_per_transaction_fee", "proposed_processing_per_transaction_fee")):
            issues.append("An all-in effective rate already includes fixed and transaction fees; do not add them a second time.")
        current = volume * action["current_processing_rate_pct"] / 100
        proposed = volume * action["proposed_processing_rate_pct"] / 100
    else:
        fields = ("current_processing_fixed_fee", "proposed_processing_fixed_fee", "current_processing_per_transaction_fee", "proposed_processing_per_transaction_fee")
        if any(action.get(key) is None for key in fields):
            issues.append("Itemize both current and proposed fixed and per-transaction processor fees, including explicit zeros.")
            return None
        count = action["processing_transaction_count"]
        if count is None:
            issues.append("Document the transaction count when comparing per-transaction processor fees.")
            return None
        current = volume * action["current_processing_rate_pct"] / 100 + action["current_processing_fixed_fee"] + count * action["current_processing_per_transaction_fee"]
        proposed = volume * action["proposed_processing_rate_pct"] / 100 + action["proposed_processing_fixed_fee"] + count * action["proposed_processing_per_transaction_fee"]
        risks.append("Only comparable processor markup and fees are modeled. Unchanged pass-through interchange is excluded from both sides.")
    risks.extend(["Card volume and mix are held constant; a quote is not a client-accepted processing offer.",
                  "Confirm terminal leases, termination charges and switching costs in implementation costs."])
    return current - proposed


def _impact(action, body, expenses, cockpit, allowed_files, now, command_result=None):
    issues = [f"Missing {key.replace('_', ' ')}" for key in action_missing(action)]
    if command_result:
        issues.extend(command_result["issues"])
    risks = []
    marketing_calculation = {}
    unit_calculation = {}
    linked = expenses.get(action.get("expense_row_id"))
    if linked and linked["excluded"]:
        issues.append("The linked expense is excluded or marked duplicate; restore or relink it before using this action.")
    if linked and linked["category"] in NON_OPERATING | NON_CASH:
        issues.append("Debt principal, financing interest, income tax, distributions, transfers and noncash D&A are not operating earnings improvements.")
    if any(str(file_id) not in allowed_files for file_id in action["source_file_ids"]):
        issues.append("A linked source is unavailable to this business file or no longer current.")
    if not action["source_file_ids"]:
        risks.append("No supporting file is linked; this remains a disclosed operating assumption.")
    kind = action["kind"]
    if action["cost_basis"] == "per_unit" and kind not in {"expense_reduction", "expense_increase"}:
        issues.append("Use the per-unit cost basis with an expense reduction or increase; price, product and processor actions have their own drivers.")
    gross = None
    if not action_missing(action):
        if kind == "unit_volume":
            from app.services.operating_unit_economics import evaluate as unit_economics

            unit_calculation = unit_economics(body, action=action)
            gross = unit_calculation["net_annual_impact"]
            issues.extend("Complete unit-volume input: " + path for path in unit_calculation["missing"])
            reference = ((cockpit.get("ebitda") or {}).get("potential") or {}).get("baseline") or {}
            headline = (cockpit.get("ebitda") or {}).get("headline") or {}
            source_revenue = number(reference.get("revenue")) if _period_matches(body, headline) and headline.get("currency") == body["currency"] and headline.get("basis") == body["basis"] else None
            unit_calculation["source_revenue_annual"] = source_revenue
            declared = body.get("historical_revenue_annual")
            unit_calculation["revenue_reconciled"] = _near(declared, source_revenue) if source_revenue is not None and declared is not None else None
            if unit_calculation["revenue_reconciled"] is False:
                issues.append("Reconcile historical_revenue_annual to the selected same-period revenue before modeling additional business units; the source baseline stays unchanged.")
                unit_calculation["missing"].append("historical_revenue_annual.source_reconciliation")
            groups = {row["key"]: row for row in ((cockpit.get("operating_evidence") or {}).get("guidance") or {}).get("expense_groups", [])}
            for expense in expenses.values():
                key = expense.get("source_group_key") or ""
                if expense["excluded"] or not key.startswith("source:"):
                    continue
                source = groups.get(key)
                if source is None or any(source.get(field) != body.get(field) for field in ("period_start", "period_end", "currency", "basis")):
                    issues.append(f"Expense row {expense['id']} has a changed or different-period source group. Reconcile or relink that row before using unit-volume action {action['id']}.")
                    unit_calculation["missing"].append(f"expenses.{expense['id']}.source_group_key")
            if unit_calculation["missing"]:
                unit_calculation["status"] = "incomplete"
                for key in ("proposed_revenue_annual", "revenue_change_annual", "variable_cost_change_annual", "net_annual_impact"):
                    unit_calculation[key] = None
                gross = None
            risks.append("The volume illustration holds selling price and variable cost per business unit constant. Confirm capacity, demand and collection assumptions; fixed costs do not scale automatically.")
        elif kind == "price_change":
            if action.get("price_basis") == "annual_revenue":
                gross = action["proposed_amount"] - action["current_amount"] if action["proposed_amount"] is not None and action["current_amount"] is not None else None
                risks.append("Annual pricing holds sales volume, product mix and current variable-cost dollars constant; review demand, capacity and any revenue-linked fees separately.")
            else:
                gross = (action["proposed_unit_price"] - action["current_unit_price"]) * action["units_per_period"] if all(action.get(key) is not None for key in ("proposed_unit_price", "current_unit_price", "units_per_period")) else None
            risks.append("Unit volume is held constant. Validate demand, customer retention and any price-sensitive volume loss.")
        elif kind == "expense_group_change":
            gross = command_result["gross_annual_effect"] if command_result and not command_result["issues"] else None
            risks.append("Proportional annual budget targets do not establish new quantities, unit rates or supplier agreements.")
        elif kind == "product_cost_change":
            gross = (action["current_unit_cost"] - action["proposed_unit_cost"]) * action["units_per_period"]
            risks.append("Unit volume is held constant. Confirm supplier pricing, quality, capacity and contract availability.")
        elif kind == "revenue_change":
            gross = (action["proposed_amount"] - action["current_amount"]) * action["contribution_margin_pct"] / 100
            risks.append("Incremental revenue uses the stated contribution margin after variable costs; it is not dollar-for-dollar earnings.")
        elif kind == "marketing_investment":
            incremental_spend = action["proposed_amount"] - action["current_amount"]
            incremental_revenue = incremental_spend * action["revenue_return_multiple"]
            contribution = incremental_revenue * action["contribution_margin_pct"] / 100
            gross = contribution - incremental_spend
            marketing_calculation = {"incremental_spend": _money(incremental_spend),
                "incremental_revenue": _money(incremental_revenue), "incremental_contribution": _money(contribution),
                "formula": "(proposed spend - current spend) * (revenue return multiple * contribution margin / 100 - 1)"}
            if incremental_spend < 0:
                issues.append("A marketing investment must keep or increase current spend; use an expense reduction for a lower marketing budget.")
            if linked is None or linked["category"] != "marketing":
                issues.append("Link the investment to a disclosed marketing expense row.")
            else:
                issues.extend(_cost_issues(linked))
                declared = _annual(cost_amount(linked), linked["cadence"])
                current = _annual(action["current_amount"], action["cadence"])
                if declared is None or current is None or not _near(declared, current):
                    issues.append("Current marketing spend and cadence must reconcile to the linked marketing expense disclosure.")
            risks.append("Marketing revenue return and contribution margin are explicit assumptions, not guaranteed sales. Incremental spend is deducted once from its modeled contribution.")
        elif kind == "processing_cost_change":
            enriched = {**cockpit, "_operating_body_currency": body["currency"]}
            gross = _processing(action, enriched, issues, risks)
            if linked and linked["category"] != "merchant_processing":
                issues.append("Link processor savings to a merchant-processing expense rather than another operating category.")
            if linked and gross is not None and action["current_processing_rate_basis"] == "all_in":
                declared = _annual(cost_amount(linked), linked["cadence"])
                current = _annual(action["processing_volume"] * action["current_processing_rate_pct"] / 100, action["cadence"])
                if declared is None or current is None or not _near(declared, current, .05):
                    issues.append("Current processing fees must reconcile to the linked merchant-processing expense disclosure.")
        else:
            current, proposed = cost_amount(action), cost_amount(action, "proposed")
            issues.extend(_cost_issues(action))
            issues.extend(_cost_issues(action, "proposed"))
            gross = current - proposed if current is not None and proposed is not None else None
            if kind in {"expense_reduction", "expense_increase"}:
                if linked is None:
                    issues.append("Link this expense adjustment to a disclosed expense row.")
                else:
                    issues.extend(_cost_issues(linked))
                    declared, reference = _annual(cost_amount(linked), linked["cadence"]), _annual(current, action["cadence"])
                    if declared is None or reference is None or abs(declared - reference) > 0.01:
                        issues.append("The adjustment's current amount and cadence must reconcile to its linked expense disclosure.")
                    if action["cost_basis"] == linked["cost_basis"] == "per_unit":
                        same_unit = action["unit_label"].strip().casefold() == linked["unit_label"].strip().casefold()
                        same_rate = _near(action["current_unit_cost"], linked["current_unit_cost"])
                        same_quantity = _near(_annual(action["current_units_per_period"], action["cadence"]), _annual(linked["current_units_per_period"], linked["cadence"]))
                        same_fixed = _near(_annual(action["current_fixed_amount"], action["cadence"]), _annual(linked["current_fixed_amount"], linked["cadence"]))
                        if not all((same_unit, same_rate, same_quantity, same_fixed)):
                            issues.append("The current unit label, quantity, rate and fixed charges must reconcile to the linked unit-cost disclosure, not just to the same total.")
                if gross is not None and (kind == "expense_reduction" and gross < 0 or kind == "expense_increase" and gross > 0):
                    issues.append("Choose the expense action type matching the direction of the proposed change.")
                if action["cost_basis"] == "per_unit":
                    risks.append("Quantity, unit rates and fixed charges are modeled separately. Validate any effect of volume or usage changes on revenue, capacity and service quality.")
    if body["currency"] != "USD":
        issues.append("Reconcile plan currency with USD debt-service and implementation amounts before projecting coverage.")
    recurring = None if gross is None else 0 if action["cadence"] == "one_time" or kind == "one_time_adjustment" else gross * FACTORS[action["cadence"]]
    one_time_benefit = gross if gross is not None and (action["cadence"] == "one_time" or kind == "one_time_adjustment") else 0
    implementation = action["implementation_cost"]
    recurring_cost = implementation * (12 if action["implementation_cost_cadence"] == "monthly" else 1) if implementation is not None and action["implementation_cost_cadence"] != "one_time" else 0
    one_time_cost = implementation if implementation is not None and action["implementation_cost_cadence"] == "one_time" else 0
    horizon = date.fromisoformat(body["projection_start"]) if body.get("projection_start") else now.date()
    effective = date.fromisoformat(action["effective_date"]) if action.get("effective_date") else None
    fraction = _active_days(effective, horizon) if effective else None
    net_annual = recurring - recurring_cost if recurring is not None and implementation is not None else None
    first_year = (net_annual * fraction + one_time_benefit - one_time_cost if fraction and net_annual is not None else 0 if fraction == 0 and net_annual is not None else None)
    if effective and effective < horizon:
        risks.append("The effective date precedes the projection horizon; verify realized savings are not already included in the current source baseline.")
    if one_time_benefit:
        risks.append("One-time effects are shown in the first-year illustration and are not multiplied into recurring annual coverage.")
    return {"id": action["id"], "label": action["label"], "kind": kind, "status": action["status"],
        "review_state": action["status"], "gross_annual_impact": _money(recurring),
        "implementation_recurring_annual": _money(recurring_cost), "implementation_one_time": _money(one_time_cost),
        "net_annual_impact": _money(net_annual), "first_year_impact": _money(first_year),
        "monthly_impact": _money(net_annual / 12) if net_annual is not None else None,
        "included_in_approved_projection": False, "issues": issues, "risk_flags": risks,
        "source_file_ids": list(action["source_file_ids"]), "effective_date": action["effective_date"],
        "explanation": action["explanation"], "source_note": action["source_note"], "owner_name": action["owner_name"],
        "driver_key": action["driver_key"], "product_name": action["product_name"],
        "expense_row_id": action["expense_row_id"], "excluded": action["excluded"],
        "merchant_source_key": action.get("merchant_source_key"), "merchant_offer_id": action.get("merchant_offer_id"),
        **({"expense_category": linked["category"] if linked else action["expense_category"],
            "source_group_key": linked["source_group_key"] if linked else action["source_group_key"]}
            if action["expense_category"] is not None or action["source_group_key"] else {}),
        "calculation": {**{key: action.get(key) for key in ("cadence", "cost_basis", "unit_label", "current_units_per_period", "proposed_units_per_period",
            "current_fixed_amount", "proposed_fixed_amount", "units_per_period", "current_amount", "proposed_amount",
            "current_unit_price", "proposed_unit_price", "current_unit_cost", "proposed_unit_cost", "contribution_margin_pct",
            "product_name", "revenue_return_multiple",
            "processing_volume", "processing_transaction_count", "current_processing_rate_pct", "proposed_processing_rate_pct",
            "current_processing_rate_basis", "proposed_processing_rate_basis", "current_processing_fixed_fee", "proposed_processing_fixed_fee",
            "current_processing_per_transaction_fee", "proposed_processing_per_transaction_fee")}, **marketing_calculation,
            **({"command": deepcopy(command_result)} if command_result else {}),
            **({"price_basis": "annual_revenue"} if action.get("price_basis") == "annual_revenue" else {}),
            **({"unit_change_mode": action["unit_change_mode"], "unit_change_value": action["unit_change_value"],
                "unit_economics": unit_calculation} if kind == "unit_volume" else {})}}


def _conflicts(actions, expenses, body=None):
    conflicts = defaultdict(list)
    by_driver, by_expense, merchant = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in actions:
        by_driver[row["driver_key"].strip().casefold()].append(row)
        if row.get("expense_row_id"):
            by_expense[row["expense_row_id"]].append(row)
        if row["kind"] == "processing_cost_change":
            key = row.get("merchant_source_key") or tuple(sorted(row["source_file_ids"]))
            merchant[str(key)].append(row)
    for group in by_driver.values():
        if len(group) < 2:
            continue
        unique = {row["kind"] for row in group}
        compatible_pair = len(group) == 2 and unique == PRODUCT_KINDS and len({(row["cadence"], row["units_per_period"]) for row in group}) == 1
        if not compatible_pair:
            for row in group:
                conflicts[row["id"]].append("Overlapping actions use the same driver. Keep one action, or pair price and unit-cost changes using identical volume and cadence.")
    for group in by_expense.values():
        # Distinct products may share a COGS disclosure, but a whole-expense
        # reduction cannot also count the same processor/product savings.
        if len(group) > 1 and any(row["kind"] in {"expense_reduction", "expense_increase", "processing_cost_change", "marketing_investment"} for row in group):
            for row in group:
                conflicts[row["id"]].append("This expense is adjusted more than once. Select one treatment to avoid counting the same savings twice.")
    for group in merchant.values():
        if len(group) > 1:
            for row in group:
                conflicts[row["id"]].append("The same merchant statement/processor scope appears in multiple processing adjustments.")
    processing = [row for row in actions if row["kind"] == "processing_cost_change"]
    for row in actions:
        expense = expenses.get(row.get("expense_row_id")) or {}
        if row["kind"] not in {"expense_reduction", "expense_increase"} or expense.get("category") != "merchant_processing":
            continue
        support = set(row["source_file_ids"]) | set(expense.get("source_file_ids") or [])
        for processor in processing:
            if support & set(processor["source_file_ids"]):
                issue = "The same processing evidence supports both a whole-expense adjustment and a processor-rate comparison. Select one treatment."
                conflicts[row["id"]].append(issue)
                conflicts[processor["id"]].append(issue)
    aggregate = [row for row in actions if row["kind"] == "revenue_change"]
    unit_or_cogs = [row for row in actions if row["kind"] in PRODUCT_KINDS or (expenses.get(row.get("expense_row_id")) or {}).get("category") == "cogs"]
    if aggregate and unit_or_cogs:
        for row in [*aggregate, *unit_or_cogs]:
            conflicts[row["id"]].append("Choose aggregate revenue/contribution-margin modeling or itemized price/product-cost modeling; combining them can duplicate the same margin change.")
    marketing = [row for row in actions if row["kind"] == "marketing_investment"]
    if aggregate and marketing:
        for row in [*aggregate, *marketing]:
            conflicts[row["id"]].append("Choose aggregate revenue/contribution-margin modeling or the marketing-return model; the marketing model already includes its incremental revenue and spend.")
    annual_prices = [row for row in actions if row["kind"] == "price_change" and row.get("price_basis") == "annual_revenue"]
    for price in annual_prices:
        for row in actions:
            if row["id"] != price["id"] and row["kind"] in {"price_change", "revenue_change", "unit_volume", "marketing_investment"}:
                issue = f"Annual pricing action {price['id']} overlaps revenue or price action {row['id']}; use one price/volume/revenue scope. Ordinary cost changes may be modeled separately."
                conflicts[price["id"]].append(issue)
                conflicts[row["id"]].append(issue)
    volumes = [row for row in actions if row["kind"] == "unit_volume"]
    from app.services.operating_plan_commands import group_rows

    groups = [row for row in actions if row["kind"] == "expense_group_change"]
    for group in groups:
        ids = {row["id"] for row in group_rows(group, body or {"expenses": list(expenses.values())})}
        for row in actions:
            if row["id"] == group["id"]:
                continue
            other_ids = {item["id"] for item in group_rows(row, body or {"expenses": list(expenses.values())})} if row["kind"] == "expense_group_change" else {row.get("expense_row_id")}
            if ids & other_ids or row["kind"] == "unit_volume" and any(expenses[key].get("volume_behavior") in {"variable", "mixed"} for key in ids):
                issue = f"Group command {group['id']} overlaps action {row['id']}; keep one group or item treatment before modeling the same costs."
                conflicts[group["id"]].append(issue)
                conflicts[row["id"]].append(issue)
    for volume in volumes:
        for row in actions:
            if row["id"] == volume["id"]:
                continue
            linked = expenses.get(row.get("expense_row_id")) or {}
            affected = linked.get("volume_behavior") in {"variable", "mixed"}
            if row["kind"] in PRODUCT_KINDS | {"unit_volume", "revenue_change", "marketing_investment"} or affected:
                detail = f"expense row {linked['id']}" if affected else "the same sales volume/revenue"
                conflicts[volume["id"]].append(f"Action {row['id']} also models {detail}. Keep the unit-volume model or that action, or link a separate fixed expense.")
                conflicts[row["id"]].append(f"Unit-volume action {volume['id']} already models {detail}. Exclude one treatment before review to avoid counting it twice.")
    return conflicts


def _disclosure(body):
    active = [row for row in body["expenses"] if not row["excluded"]]
    result = {"operating_monthly": 0.0, "cogs_monthly": 0.0, "operating_cash_monthly": 0.0,
              "non_operating_monthly": 0.0, "one_time_total": 0.0, "active_count": len(active),
              "excluded_count": len(body["expenses"]) - len(active), "missing_inputs": [], "cost_issues": [], "cost_mix": []}
    categories = {}
    for row in active:
        category = categories.setdefault(row["category"], {"category": row["category"], "annual_amount": 0.0, "missing_count": 0, "count": 0, "quantified_count": 0})
        category["count"] += 1
        value = cost_amount(row)
        missing = expense_missing(row)
        issues = _cost_issues(row)
        result["missing_inputs"].extend(f"expenses.{row['id']}.{key}" for key in missing)
        result["cost_issues"].extend({"id": row["id"], "label": row["label"], "message": issue} for issue in issues)
        if missing or issues:
            category["missing_count"] += 1
        if value is None:
            continue
        category["quantified_count"] += 1
        if row["cadence"] == "one_time":
            result["one_time_total"] += value
            continue
        monthly = _annual(value, row["cadence"]) / 12
        category["annual_amount"] += monthly * 12
        if row["category"] in NON_OPERATING:
            result["non_operating_monthly"] += monthly
        elif row["category"] == "cogs":
            result["cogs_monthly"] += monthly
            result["operating_cash_monthly"] += monthly
        else:
            result["operating_monthly"] += monthly
            if row["category"] not in NON_CASH:
                result["operating_cash_monthly"] += monthly
    result["complete"] = not result["missing_inputs"] and not result["cost_issues"] and bool(active or body["no_operating_expenses_confirmed"])
    result["cost_mix"] = [{**row, "annual_amount": _money(row["annual_amount"]) if row["quantified_count"] else None} for row in categories.values()]
    return {key: _money(value) if isinstance(value, float) else value for key, value in result.items()}


def evaluate(raw, cockpit, *, source_file_ids=None, now=None, include_expense_changes=False):
    body = validate_body(raw)
    original = deepcopy(body)
    from app.services.operating_plan_commands import resolve

    resolved = resolve(body, cockpit)
    body = resolved["body"]
    original_actions = {row["id"]: row for row in original["adjustments"]}
    commands = {row["action_id"]: row for row in resolved["commands"]}
    now = now or datetime.now(UTC)
    if isinstance(now, date) and not isinstance(now, datetime):
        now = datetime.combine(now, datetime.min.time(), UTC)
    expenses = {row["id"]: row for row in body["expenses"]}
    files, allowed = _scope_files(cockpit, source_file_ids)
    active = [row for row in body["adjustments"] if not row["excluded"] and row["status"] != "rejected"]
    conflicts = _conflicts(active, expenses, body)
    approved_candidates = [row for row in active if row["status"] == "approved"]
    approved_conflicts = _conflicts(approved_candidates, expenses, body)
    results = []
    for action in body["adjustments"]:
        result = _impact(action, body, expenses, cockpit, allowed, now, commands.get(action["id"]))
        intent = original_actions[action["id"]]
        from app.services.operating_plan_commands import participants

        participant_ids = {row["id"] for row in participants(intent, original)}
        for split in resolved["itemization"]:
            if participant_ids & set(split["child_expense_row_ids"]) and (split["status"] != "balanced" or split["source_group_key"] and not split["source_comparable"]):
                result["issues"].append(f"Reconcile source itemization for parent {split['parent_expense_row_id']} before applying these costs to earnings; a historical correction remains unresolved for preparer review.")
        if intent.get("command") and participant_ids:
            from app.services.operating_plan_guidance import reconcile_groups

            source_groups = {row["key"]: row for row in reconcile_groups(original, cockpit.get("operating_evidence") or {})}
            references = {row["key"]: row for row in ((cockpit.get("operating_evidence") or {}).get("guidance") or {}).get("expense_groups", [])}
            keys = {expenses[row_id].get("source_group_key") for row_id in participant_ids if expenses[row_id].get("source_group_key")}
            for key in keys:
                reference = references.get(key) or {}
                same_scope = all(reference.get(field) == original.get(field) for field in ("period_start", "period_end", "currency", "basis"))
                same_category = all(expenses[row_id]["category"] == reference.get("category") for row_id in participant_ids if expenses[row_id].get("source_group_key") == key)
                if source_groups.get(key, {}).get("status") != "matches_source" or not same_scope or not same_category:
                    result["issues"].append("Reconcile the command's current source group, category, reporting period, currency and basis before applying its cost change to source earnings or NOI.")
        if action["excluded"]:
            result["review_state"] = "excluded"
        elif action["status"] == "rejected":
            result["review_state"] = "rejected"
        else:
            result["issues"].extend(conflicts.get(action["id"], []))
            result["proposed_math_valid"] = not result["issues"]
            own_issues = [issue for issue in result["issues"] if issue not in conflicts.get(action["id"], [])]
            own_issues.extend(approved_conflicts.get(action["id"], []))
            support_ids = {*action["source_file_ids"], *((expenses.get(action.get("expense_row_id")) or {}).get("source_file_ids") or [])}
            if action["kind"] == "unit_volume":
                support_ids.update(fid for row in body["expenses"] if not row["excluded"] for fid in row["source_file_ids"])
            if action["kind"] == "expense_group_change":
                from app.services.operating_plan_commands import group_rows

                support_ids.update(fid for row in group_rows(action, body) for fid in row["source_file_ids"])
            if not support_ids or not any(files.get(str(file_id), {}).get("eligible_for_review") for file_id in support_ids):
                own_issues.append("Staff review requires linked, current evidence with an accepted business association; an unsupported disclosure remains a proposal.")
            if any(str(file_id) not in allowed or not files.get(str(file_id), {}).get("eligible_for_review") for file_id in support_ids):
                own_issues.append("Resolve each linked source's business association, current version and acceptance before staff review.")
            if action["kind"] == "processing_cost_change" and not action_missing(action):
                own_issues.extend(_merchant_approval_issues(action, cockpit, files))
            result["approval_issues"] = list(dict.fromkeys(own_issues))
            if action["status"] == "approved":
                fresh = bool(action["reviewed_by_user_id"] and action["reviewed_at"] and action["review_reason"]
                             and action["review_source_stamp"] in {
                                 review_stamp(intent, cockpit, expenses.get(action.get("expense_row_id")), original),
                                 _prior_v2_review_stamp(intent, cockpit, expenses.get(action.get("expense_row_id")), original),
                                 _legacy_review_stamp(intent, cockpit, expenses.get(action.get("expense_row_id")), original),
                                 _legacy_review_stamp(intent, cockpit, expenses.get(action.get("expense_row_id")), original, before_guidance=True)})
                if not fresh:
                    result["review_state"] = "stale"
                    result["risk_flags"].append("The proposal, linked expense or financial evidence changed. Staff must review the action again.")
                elif action["baseline_treatment"] not in {"included_in_baseline", "incremental_to_baseline"} or not action["baseline_reconciliation_note"]:
                    result["review_state"] = "needs_baseline_reconciliation"
                    result["risk_flags"].append("Staff must reconcile this action's current amount, business, reporting period and operating treatment to the selected financial baseline.")
                else:
                    result["included_in_approved_projection"] = not own_issues
                    result["review_state"] = "approved" if not own_issues else "blocked"
        results.append(result)

    for command in resolved["commands"]:
        result = next(row for row in results if row["id"] == command["action_id"])
        command.update(included_in_proposed_projection=bool(result.get("proposed_math_valid")),
            included_in_approved_projection=bool(result.get("included_in_approved_projection")),
            projection_issues=list(result.get("issues") or []))
        result["calculation"]["command"] = deepcopy(command)

    def totals(rows):
        return {"annual_impact": _money(sum(row["net_annual_impact"] or 0 for row in rows)),
                "monthly_impact": _money(sum(row["net_annual_impact"] or 0 for row in rows) / 12),
                "first_year_impact": _money(sum(row["first_year_impact"] or 0 for row in rows)),
                "one_time_cost": _money(sum(row["implementation_one_time"] or 0 for row in rows)),
                "complete": bool(rows) and all(row["net_annual_impact"] is not None and row["first_year_impact"] is not None for row in rows), "count": len(rows)}

    proposed_rows = [row for row in results if row.get("proposed_math_valid")]
    approved_rows = [row for row in results if row["included_in_approved_projection"]]
    sums = {"proposed": totals(proposed_rows), "approved": totals(approved_rows)}
    sums["proposed"]["complete"] &= len(proposed_rows) == len(active)
    sums["approved"]["complete"] &= len(approved_rows) == len(approved_candidates)
    for key, expected in (("proposed", len(active)), ("approved", len(approved_candidates))):
        total = sums[key]
        total["expected_count"] = expected
        total["known_annual_subtotal"] = total["annual_impact"] if total["count"] else None
        total["known_first_year_subtotal"] = total["first_year_impact"] if total["count"] else None
        if expected and not total["complete"]:
            # A partial or wholly unquantified plan cannot be represented as a
            # zero savings figure. Known rows remain an explicitly named subtotal.
            for field in ("annual_impact", "monthly_impact", "first_year_impact", "one_time_cost"):
                total[field] = None
    earnings = (cockpit.get("ebitda") or {}).get("headline") or {}
    potential = (cockpit.get("ebitda") or {}).get("potential") or {}
    source_baseline = potential.get("baseline") or {}
    noi = (cockpit.get("noi") or {}).get("headline") or {}
    from app.services.financial_metric_reporting import noi_governance_brief

    noi_governance = noi_governance_brief(cockpit.get("noi"))
    debt = (cockpit.get("debt") or {}).get("service") or {}
    plan = cockpit.get("plan") or {}
    evaluation = plan.get("evaluation") or {}
    current_service = number(debt.get("annual"))
    after_service = number(evaluation.get("debt_service_after_annual")) if evaluation.get("active") and not evaluation.get("coverage_unavailable_reason") else None
    annual_earnings = number(earnings.get("annualized_value"))
    annual_noi = number(noi.get("annualized_value"))
    from app.services.document_associations import identity_matches

    refs = [*(earnings.get("source_refs") or []), *(noi.get("source_refs") or [])]
    expected_business = (cockpit.get("operating_evidence") or {}).get("business_name") or next((
        (ref.get("identity_association") or {}).get("expected_entity") or ref.get("entity") for ref in refs
        if (ref.get("identity_association") or {}).get("expected_entity") or ref.get("entity")), None)
    entity_comparable = bool(expected_business and identity_matches(expected_business, body["business_name"]))
    comparable = bool(entity_comparable and _period_matches(body, earnings) and earnings.get("currency") == body["currency"] == "USD" and earnings.get("basis") == body["basis"] and not (cockpit.get("ebitda") or {}).get("source_selection", {}).get("stale") and not debt.get("identity_association_conflict"))
    reviewed_noi = bool(noi.get("staff_reviewed") and not (cockpit.get("noi") or {}).get("review", {}).get("stale"))
    noi_comparable = bool(entity_comparable and _period_matches(body, noi) and noi.get("currency") == body["currency"] == "USD"
                          and (noi.get("basis") == body["basis"] or reviewed_noi)
                          and not (cockpit.get("noi") or {}).get("review", {}).get("stale"))
    baseline = {"period": earnings.get("period"), "currency": earnings.get("currency"), "plan_currency": body["currency"], "basis": earnings.get("basis"),
        "revenue": source_baseline.get("revenue"), "earnings": annual_earnings, "earnings_kind": earnings.get("kind"),
        "noi": annual_noi, "noi_kind": noi.get("kind"), "noi_label": noi.get("label"),
        "noi_year": noi.get("year"), "noi_basis": noi.get("basis"), "noi_staff_reviewed": reviewed_noi,
        "noi_governance": noi_governance, "noi_period": noi.get("period"), "noi_currency": noi.get("currency"),
        "dscr": ((cockpit.get("dscr") or {}).get("headline") or {}).get("value"),
        "annual_debt_service": current_service, "provisional": bool(earnings.get("provisional", True)),
        "noi_provisional": bool(noi.get("provisional", True))}
    ratio = number(baseline["dscr"])
    recommended = annual_earnings < 0 if annual_earnings is not None and annual_earnings < 0 else ratio < 1.1 if ratio is not None else None
    recommendation = {"threshold": 1.1, "recommended": recommended,
        "reason": "Current earnings are negative." if annual_earnings is not None and annual_earnings < 0 else "Current DSCR is below 1.10x." if recommended else "Current DSCR meets the 1.10x planning threshold." if recommended is False else "Current earnings and coverage must be established before checking the planning threshold.",
        "earnings_gap_annual": _money(max(0, current_service * 1.1 - annual_earnings)) if annual_earnings is not None and current_service is not None and current_service > 0 else None}

    def projection(total, reviewed):
        available = comparable and annual_earnings is not None and total["complete"]
        noi_available = noi_comparable and annual_noi is not None and total["complete"]
        after = annual_earnings + total["annual_impact"] if available else None
        first_year = annual_earnings + total["first_year_impact"] if available else None
        coverage_rows = approved_rows if reviewed else proposed_rows
        coverage_impact = sum(row["first_year_impact"] or 0 for row in coverage_rows
            if row["kind"] != "one_time_adjustment" and (row.get("calculation") or {}).get("cadence") != "one_time") if available else None
        first_year_coverage = annual_earnings + coverage_impact if coverage_impact is not None else None
        new_operating = number(evaluation.get("annual_new_operating_expenses", 0))
        refinance_after = after - new_operating if after is not None and new_operating is not None else None
        refinance_first_year = first_year_coverage - new_operating if first_year_coverage is not None and new_operating is not None else None
        after_noi = annual_noi + total["annual_impact"] if noi_available else None
        first_year_noi = annual_noi + total["first_year_impact"] if noi_available else None
        return {"status": "awaiting_review" if reviewed and total["count"] == 0 else "no_actions" if not reviewed and not active else "reviewed_illustration" if reviewed and available else "unreviewed_illustration" if available else "unavailable",
            "earnings": _money(after), "noi": _money(after_noi),
            "noi_status": "awaiting_review" if reviewed and total["count"] == 0 else "no_actions" if not reviewed and not active else "reviewed_illustration" if reviewed and noi_available else "unreviewed_illustration" if noi_available else "unavailable",
            "first_year_noi": _money(first_year_noi),
            "first_year_earnings": _money(first_year),
            "first_year_coverage_impact": _money(coverage_impact), "first_year_coverage_earnings": _money(first_year_coverage),
            "first_year_dscr_current": round(first_year_coverage / current_service, 2) if first_year_coverage is not None and current_service is not None and current_service > 0 else None,
            "first_year_dscr_with_refinance": round(refinance_first_year / after_service, 2) if refinance_first_year is not None and after_service is not None and after_service > 0 else None,
            "first_year_earnings_with_refinance": _money(refinance_first_year),
            "annual_new_operating_expenses": new_operating,
            "coverage_basis": "Stabilized run-rate coverage uses annual impact; first-year coverage uses effective-date proration and implementation costs against the selected annual debt-service window. One-time benefits do not increase recurring debt-service coverage.",
            "dscr_current": round(after / current_service, 2) if after is not None and current_service is not None and current_service > 0 else None,
            "dscr_with_refinance": round(refinance_after / after_service, 2) if refinance_after is not None and after_service is not None and after_service > 0 else None,
            "annual_debt_service": current_service, "refinance_annual_debt_service": after_service,
            "target_gap_current": _money(max(0, current_service * 1.25 - after)) if after is not None and current_service is not None and current_service > 0 else None,
            "target_gap_refinance": _money(max(0, after_service * 1.25 - refinance_after)) if refinance_after is not None and after_service is not None and after_service > 0 else None}

    missing = []
    if annual_earnings is None:
        missing.append("A latest-period annual earnings baseline is needed to translate operating changes into coverage.")
    if annual_noi is None:
        missing.append("A supported operating-income statement or an explicit annual staff NOI review is needed for a projected NOI figure.")
    elif not noi_comparable:
        missing.append("Reconcile the NOI business, currency and accounting basis with this operating plan before showing projected NOI. The current NOI keeps its own reporting basis.")
    if current_service is None:
        missing.append("Current annual debt service is not established.")
    if not comparable:
        missing.append("Reconcile the disclosure and current earnings business, currency, accounting basis and period before relying on projected coverage.")
    risks = []
    if noi_governance.get("review_needed"):
        risks.append("NOI basis requires review: " + str(noi_governance.get("status_reason") or "Review the current source, period and operating-income calculation."))
    if reviewed_noi:
        risks.append("Projected NOI starts from an explicit annual staff review; its tax-derived or manual basis is separate from the historical P&L and does not establish program eligibility.")
    if baseline["provisional"]:
        risks.append("The current earnings baseline is provisional; a reviewed plan does not remove its source limitations.")
    if (plan.get("record") or {}).get("add_backs") or ((plan.get("record") or {}).get("overrides") or {}).get("income_annual") is not None:
        risks.append("Legacy financing-plan income overrides and addbacks are not stacked into this operating plan; reconcile overlapping assumptions before presenting a combined case.")
    if (evaluation.get("ebitda") or {}).get("replacement_payment_missing"):
        risks.append("A replacement payment is missing; after-refinance coverage is incomplete.")
    from app.services.released_cash_flow import evaluate as evaluate_released_cash_flow

    released = evaluate_released_cash_flow(body, cockpit)
    missing.extend(released["missing"])
    from app.services.operating_plan_guidance import reconcile_groups
    from app.services.operating_unit_economics import evaluate as unit_economics

    units = unit_economics(body)
    if units is not None:
        volume = next((row for row in results if row["kind"] == "unit_volume" and not row["excluded"] and row.get("calculation", {}).get("unit_economics")), None)
        if volume is not None:
            units = volume["calculation"]["unit_economics"]
    result = {"schema_version": "earnings-improvement-v1", "context_stamp": context_stamp(cockpit), "disclosure": _disclosure(body),
        **({"unit_economics": units} if units is not None else {}),
        "expense_groups": reconcile_groups(body, cockpit.get("operating_evidence") or {}),
        "released_cash_flow": released,
        "actions": results, "totals": sums, "baseline": baseline, "recommendation": recommendation,
        "projection": {"proposed": projection(sums["proposed"], False), "approved": projection(sums["approved"], True)},
        "targets": {"current": [{"ratio": ratio, "earnings_required": _money(current_service * ratio)} for ratio in (1.0, 1.1, 1.25) if current_service is not None and current_service > 0],
                    "refinance": [{"ratio": ratio, "earnings_required": _money(after_service * ratio)} for ratio in (1.0, 1.1, 1.25) if after_service is not None and after_service > 0]},
        "missing": missing, "risk_flags": risks,
        "all_actions_reviewed": bool(active) and all(row["review_state"] in {"approved", "rejected", "excluded"} for row in results),
        "notes": ["Historical earnings, NOI and current DSCR remain unchanged. Every improvement is a conditional operating illustration, not lender-approved income or a funding guarantee.",
                  "Recurring annual coverage excludes one-time benefits. First-year illustrations prorate recurring changes by days active in the twelve-month horizon and deduct implementation costs.",
                  "A refinance changes the debt-service denominator; principal, interest and financing proceeds are not operating earnings improvements."]}
    if include_expense_changes:
        from app.services.operating_expense_changes import evaluate as expense_changes

        result["expense_changes"] = expense_changes(body, results, baseline,
            earnings_comparable=comparable, noi_comparable=noi_comparable,
            refinance_annual_debt_service=after_service, new_operating_annual=number(evaluation.get("annual_new_operating_expenses", 0)),
            source_groups=result["expense_groups"])
        for key in ("commands", "itemization", "historical_corrections"):
            if resolved[key]:
                result[key] = resolved[key]
    if resolved["commands"] or resolved["itemization"] or resolved["historical_corrections"]:
        result["preparer_handoff"] = {"scope": "saved_version_only_when_attached_to_a_saved_version",
            "historical": {"baseline_unchanged": True, "source_groups": deepcopy(result["expense_groups"]),
                "itemization": resolved["itemization"], "corrections_for_preparer_review": resolved["historical_corrections"]},
            "future": {"commands": [row for row in resolved["commands"] if not original_actions[row["action_id"]]["excluded"] and original_actions[row["action_id"]]["status"] != "rejected"],
                "assumptions": list(dict.fromkeys(note for row in resolved["commands"] if not original_actions[row["action_id"]]["excluded"] and original_actions[row["action_id"]]["status"] != "rejected" for note in row["assumptions"]))},
            "notes": ["Historical disclosure variances need preparer review; they do not amend a tax return or replace verified earnings.",
                "Future operating changes are conditional proposals. This package does not file or amend tax returns."]}
    return result


def review_actions(raw, cockpit, action_ids, decision, reason, actor, *, baseline_treatment="unreconciled",
                   baseline_reconciliation_note="", source_file_ids=None, now=None):
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required to review operating adjustments")
    if decision not in {"approved", "rejected", "proposed"} or not reason.strip() or len(reason) > 2000:
        raise HTTPException(422, "Choose a valid review decision and provide a review reason")
    if baseline_treatment not in {"unreconciled", "included_in_baseline", "incremental_to_baseline", "outside_operating_earnings"}:
        raise HTTPException(422, "Choose how the adjustment reconciles to the current baseline")
    body = validate_body(raw)
    ids = [str(UUID(str(value))) for value in action_ids]
    if len(ids) != len(set(ids)) or not ids or set(ids) - {row["id"] for row in body["adjustments"]}:
        raise HTTPException(422, "Choose each existing adjustment once")
    stamp_time = now or datetime.now(UTC)
    expenses = {row["id"]: row for row in body["expenses"]}
    for action in body["adjustments"]:
        if action["id"] not in ids:
            continue
        if action["excluded"]:
            raise HTTPException(422, "Restore an excluded adjustment before reviewing it")
        action.update(status=decision, reviewed_by_user_id=str(actor.id), reviewed_at=stamp_time.isoformat(),
            review_reason=reason.strip(), baseline_treatment=baseline_treatment,
            baseline_reconciliation_note=baseline_reconciliation_note.strip())
        action["review_source_stamp"] = review_stamp(action, cockpit, expenses.get(action.get("expense_row_id")), body)
    if decision == "approved":
        assessment = evaluate(body, cockpit, source_file_ids=source_file_ids, now=stamp_time)
        invalid = [row for row in assessment["actions"] if row["id"] in ids and row.get("approval_issues")]
        if invalid:
            raise HTTPException(422, {"message": "Resolve the adjustment inputs, evidence and overlapping treatments before review", "actions": [{"id": row["id"], "issues": row["approval_issues"]} for row in invalid]})
    return validate_body(body)


def package_summary(evaluation):
    """Shareable explanations, never private review reasons, actors or hashes."""
    return {"baseline": deepcopy(evaluation["baseline"]), "disclosure": deepcopy(evaluation["disclosure"]),
        **({"preparer_handoff": deepcopy(evaluation["preparer_handoff"])} if "preparer_handoff" in evaluation else {}),
        **({"unit_economics": deepcopy(evaluation["unit_economics"])} if "unit_economics" in evaluation else {}),
        "expense_groups": deepcopy(evaluation.get("expense_groups") or []),
        "released_cash_flow": deepcopy(evaluation.get("released_cash_flow")),
        "projection": deepcopy(evaluation["projection"]), "totals": deepcopy(evaluation["totals"]),
        "targets": deepcopy(evaluation["targets"]), "missing": list(evaluation["missing"]),
        "risk_flags": list(evaluation["risk_flags"]), "notes": list(evaluation["notes"]),
        "all_actions_reviewed": evaluation["all_actions_reviewed"], "recommendation": deepcopy(evaluation.get("recommendation")),
        "adjustments": [{**{key: deepcopy(row.get(key)) for key in ("id", "label", "kind", "product_name", "review_state", "gross_annual_impact",
            "net_annual_impact", "first_year_impact", "implementation_recurring_annual", "implementation_one_time",
            "included_in_approved_projection", "effective_date", "explanation", "source_note", "owner_name", "issues", "risk_flags", "source_file_ids", "calculation")},
            **{key: deepcopy(row[key]) for key in ("expense_category", "source_group_key") if row.get(key) not in (None, "")}}
            for row in evaluation["actions"] if not row["excluded"]]}
