"""The v2 earnings plan evaluated against a file, in the shape its readers know.

`app/services/earnings_plan_math.py` does the arithmetic on a plan body and a
plain baseline. This module finds that baseline in the cockpit (the one ladder
of app/services/financial_baseline.py), runs the engine, and returns

* the engine's own result under ``plan`` (what the plan screens and the plan
  document use), and
* the same figures under the keys the v1 evaluation used (``baseline``,
  ``projection``, ``totals``, ``actions``, ``comparison``, ``released_cash_flow``,
  ``recommendation``...), so the coverage plan, the investor package, the
  marketplace projection and the file page read a v2 plan without knowing it.

A plan always computes once the file has an earnings baseline. What is missing
before a version can be saved is one list (``plan.to_finish``), never a reason
to blank the figures.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date
from typing import Any

from app.schemas import earnings_plan_v2 as schema
from app.services import earnings_plan_math as engine
from app.services.financial_metric_review import number

RESULT_VERSION = "earnings-plan-v2"
# Costs most businesses carry that a return often folds into another line. Asked only when the source prints none.
QUESTION_CATEGORIES = (("payroll_taxes", "Payroll taxes"), ("transportation", "Transportation"))
# How a v2 item reads to documents that group changes by the v1 action kinds.
PURPOSES = {"existing": "existing_cost", "planned": "planned_increment", "reserve": "reserve", "other": "other"}
NOTES = [
    "Historical earnings, NOI and current DSCR remain unchanged. Every improvement is a conditional operating illustration, not lender-approved income or a funding guarantee.",
    "Run-rate figures show a full year with every change in place. First-year figures count each change from its start month and deduct one-time setup costs.",
    "A refinance changes the debt-service denominator; principal, interest and financing proceeds are not operating earnings improvements.",
]


def _stamp(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _money(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _ratio(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _rounded(value: Any) -> Any:
    """The engine's floats as stored figures: cents for money, six places for rates."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# The baseline the plan starts from
# ---------------------------------------------------------------------------

def engine_baseline(cockpit: dict) -> dict:
    """The file's one ladder as the engine reads it; ``available`` is False until an earnings source is in use."""
    from app.services import released_cash_flow

    block = cockpit.get("baseline") or {}
    figures = block.get("figures") or {}
    current = (block.get("metrics") or {}).get("current") or {}
    debt = block.get("debt_service") or {}
    source = block.get("source") or {}
    groups = [{"key": row["key"], "label": _group_label(row), "amount": number(row.get("amount")) or 0.0,
               "category": row.get("category"), "derived": bool(row.get("derived")),
               # What the latest P&L says for the same line, for the business to compare before it confirms.
               "statement_amount": number(row.get("statement_amount"))}
              for row in (block.get("groups") or {}).get("rows") or []]
    released = released_cash_flow.source(cockpit)
    terms_annual = number(debt.get("with_terms_annual"))
    released_monthly = released["amount_monthly"] if released["available"] else number(debt.get("released_monthly")) if terms_annual is not None else None
    kind = source.get("kind")
    revenue, noi, ebitda = number(figures.get("revenue")), number(current.get("noi")), number(current.get("ebitda"))
    return {
        "available": revenue is not None and noi is not None and ebitda is not None,
        "reason": block.get("reason") if kind is None else None,
        "revenue": revenue, "cogs": number(figures.get("cogs")), "operating_expenses": number(figures.get("operating_expenses")),
        "noi": noi, "ebitda": ebitda,
        "debt_service_current": number(debt.get("current_annual")), "debt_service_with_terms": terms_annual,
        "debt_service_current_monthly": number(debt.get("current_monthly")), "debt_service_with_terms_monthly": number(debt.get("with_terms_monthly")),
        "released_monthly": max(0.0, released_monthly) if released_monthly is not None else None,
        # Recurring operating costs the saved terms add, and whether the budget has confirmed their amount.
        "loan_operating_annual": (number(debt.get("new_operating_annual")) or 0.0) if terms_annual is not None else None,
        "loan_operating_confirmed": debt.get("new_operating_confirmed") is not False,
        "terms_label": (debt.get("terms") or {}).get("label"),
        "statement": (block.get("groups") or {}).get("statement"),
        "source_name": "IRS" if kind == "tax_return" else "P&L" if kind == "profit_and_loss" else "source",
        "source": {key: source.get(key) for key in ("kind", "label", "file_id", "file_name", "form", "year", "period_start", "period_end",
                                                    "period_assumed", "months_covered", "currency", "basis", "accepted", "selected_by", "fallback", "annualized")},
        "groups": groups,
    }


def _group_label(row: dict) -> str:
    return "Other" if row.get("derived") else str(row.get("label") or row["key"])


def baseline_stamp(baseline: dict) -> str | None:
    """Identity of the figures a plan was built on; None before there is a baseline."""
    if not baseline.get("available"):
        return None
    return _stamp({"source": (baseline.get("source") or {}).get("file_id"),
                   "figures": [baseline.get(key) for key in ("revenue", "cogs", "operating_expenses", "noi", "ebitda")],
                   "groups": [[row["key"], round(row["amount"], 2)] for row in baseline.get("groups") or []]})


def questions(baseline: dict) -> list[dict]:
    """The cost categories to ask about: common ones the source prints no line for."""
    present = {row["key"] for row in baseline.get("groups") or []}
    return [{"key": key, "label": label} for key, label in QUESTION_CATEGORIES if key not in present] if baseline.get("available") else []


# ---------------------------------------------------------------------------
# Review stamps: what an approval was given to
# ---------------------------------------------------------------------------

_PROPOSAL_FIELDS = {
    "changes": ("target", "direction", "mode", "value", "start_month", "setup_cost", "setup_cadence", "included"),
    "growth": ("monthly_spend", "stream_id", "factor_id", "added_per_month", "start_month", "setup_cost", "setup_cadence", "included"),
    "activities": ("monthly_amount", "start_month", "setup_cost", "setup_cadence", "included"),
}


def model_stamp(body: dict) -> str:
    """The revenue model and cost lines every change rests on."""
    return _stamp({"streams": [{"id": stream["id"], "factors": [[factor["id"], factor.get("value"), factor.get("unit"), bool(factor.get("volume"))]
                                                               for factor in stream.get("factors") or []]} for stream in body.get("streams") or []],
                   "lines": [[line["id"], line.get("group"), line.get("base"), line.get("behavior"), line.get("link")] for line in body.get("lines") or []]})


def proposal_stamp(block: str, row: dict, body: dict) -> str:
    """What is proposed, without its words: editing a note keeps a review, editing a figure does not."""
    return _stamp({"block": block, "proposal": {key: row.get(key) for key in _PROPOSAL_FIELDS[block]}, "model": model_stamp(body)})


def review_stamp(block: str, row: dict, body: dict, baseline: dict) -> str:
    return _stamp({"proposal": proposal_stamp(block, row, body), "baseline": baseline_stamp(baseline)})


def _with_live_reviews(body: dict, baseline: dict) -> tuple[dict, set[str]]:
    """Approvals given to other figures stop counting until the change is reviewed again."""
    stale: set[str] = set()
    live = deepcopy(body)
    for block, row in schema.items(live):
        if row.get("status") != "approved":
            continue
        fresh = bool(row.get("reviewed_by_user_id") and row.get("reviewed_at") and row.get("review_reason")
                     and row.get("review_stamp") == review_stamp(block, row, live, baseline))
        if not fresh:
            stale.add(str(row["id"]))
            row["status"] = "proposed"
    return live, stale


# ---------------------------------------------------------------------------
# Pieces of the v1-shaped result
# ---------------------------------------------------------------------------

def _effective_date(body: dict, month: int) -> str | None:
    start = body.get("plan_start")
    if not start:
        return None
    year, first = (int(part) for part in start.split("-"))
    index = first - 1 + month - 1
    return date(year + index // 12, index % 12 + 1, 1).isoformat()


def _action_kind(item: dict, effect: dict) -> str:
    if item["kind"] == "growth":
        return "marketing_investment"
    if item["kind"] == "activity":
        return "expense_increase"
    target = item["row"].get("target") or {}
    if target.get("kind") == "pricing":
        return "price_change"
    if target.get("kind") == "factor":
        return "unit_volume"
    if target.get("kind") == "group":
        return "expense_group_change"
    return "expense_increase" if item["row"].get("direction") == "up" else "expense_reduction"


def _actions(plan: engine.Plan, detail: dict, body: dict, stale: set[str]) -> list[dict]:
    rows = []
    for item in plan.items:
        row, effect = item["row"], detail["effects"][item["id"]]
        included = row.get("included") is not False
        approved = plan.reviewed(item["id"])
        state = "excluded" if not included else "rejected" if row.get("status") == "rejected" else "stale" if item["id"] in stale else "approved" if approved else "proposed"
        recurring_setup = number(row.get("setup_cost")) * 12 if row.get("setup_cadence") == "monthly" and row.get("setup_cost") is not None else 0.0
        target = row.get("target") or {}
        group = target.get("group") or (plan.line_by_id.get(target.get("line_id")) or {}).get("group")
        flags = ["The source figures or the plan's model changed after this change was reviewed. Staff must review it again."] if state == "stale" else []
        rows.append({
            "id": item["id"], "label": plan.label(item["id"]), "kind": _action_kind(item, effect), "plan_kind": item["kind"],
            "area": item["area"], "product_name": "", "status": row.get("status") or "proposed", "excluded": not included, "review_state": state,
            "gross_annual_impact": _money(effect["ebitda"] + recurring_setup), "implementation_recurring_annual": _money(recurring_setup),
            "implementation_one_time": _money(effect["one_time"]), "net_annual_impact": _money(effect["ebitda"]),
            "first_year_impact": _money(effect["first_year"]), "monthly_impact": _money(effect["ebitda"] / 12),
            "revenue_change_annual": _money(effect["revenue"]), "noi_annual_impact": _money(effect["noi"]),
            "included_in_approved_projection": approved, "included_in_proposed_projection": plan.proposed(item["id"]), "proposed_math_valid": True,
            "issues": [], "approval_issues": [], "risk_flags": flags, "source_file_ids": list(row.get("source_file_ids") or []),
            "effective_date": _effective_date(body, engine.start_month(row)), "explanation": row.get("note") or "", "source_note": row.get("evidence_note") or "",
            "owner_name": row.get("owner") or "", **({"expense_category": group, "source_group_key": group} if group else {}),
            "calculation": {"model": RESULT_VERSION, "cadence": "annual", "target": item["key"], "start_month": engine.start_month(row),
                            "moves": [{"label": move["label"], "amount": _money(move["amount"])} for move in effect["moves"]]},
        })
    return rows


def _totals(plan: engine.Plan, detail: dict, baseline: dict, view: str) -> dict:
    """The scenario's own change, so the figure adds up even though changes interact."""
    chosen = plan.reviewed if view == "reviewed" else plan.proposed
    rows = [item for item in plan.items if chosen(item["id"])]
    if not rows:
        return {"annual_impact": 0.0, "monthly_impact": 0.0, "first_year_impact": 0.0, "one_time_cost": 0.0, "complete": False, "count": 0,
                "expected_count": 0, "known_annual_subtotal": None, "known_first_year_subtotal": None}
    annual = detail[view]["ebitda"] - (baseline.get("ebitda") or 0.0)
    first = detail["first_year"][view]["ebitda"] - (baseline.get("ebitda") or 0.0)
    one_time = sum(detail["effects"][item["id"]]["one_time"] for item in rows)
    return {"annual_impact": _money(annual), "monthly_impact": _money(annual / 12), "first_year_impact": _money(first), "one_time_cost": _money(one_time),
            "complete": True, "count": len(rows), "expected_count": len(rows), "known_annual_subtotal": _money(annual), "known_first_year_subtotal": _money(first),
            "noi_annual_impact": _money(detail[view]["noi"] - (baseline.get("noi") or 0.0)),
            "noi_first_year_impact": _money(detail["first_year"][view]["noi"] - (baseline.get("noi") or 0.0))}


def _projection(detail: dict, baseline: dict, totals: dict, view: str, *, active: int) -> dict:
    reviewed = view == "reviewed"
    available = bool(baseline.get("available"))
    status = ("awaiting_review" if reviewed and totals["count"] == 0 else "no_actions" if not reviewed and not active
              else ("reviewed_illustration" if reviewed else "unreviewed_illustration") if available else "unavailable")
    scenario, first = detail[view], detail["first_year"][view]
    current, terms = baseline.get("debt_service_current"), baseline.get("debt_service_with_terms")
    after = scenario["ebitda"] if available else None
    first_year = first["ebitda"] if available else None
    # Operating charges the loan itself adds come off earnings once, as they always have, before the with-terms ratios:
    # the same figure the plan's own six metrics use, so every reader gets one coverage with the terms.
    new_operating = engine.loan_operating(baseline)
    with_terms = after - new_operating if after is not None else None
    first_with_terms = first_year - new_operating if first_year is not None else None

    def over(numerator, denominator):
        return _ratio(numerator / denominator) if numerator is not None and denominator else None

    return {
        "status": status, "noi_status": status,
        "earnings": _money(after), "noi": _money(scenario["noi"]) if available else None, "revenue": _money(scenario["revenue"]) if available else None,
        "first_year_earnings": _money(first_year), "first_year_noi": _money(first["noi"]) if available else None,
        "first_year_coverage_impact": _money(totals["first_year_impact"]) if available and totals["count"] else None,
        "first_year_coverage_earnings": _money(first_year) if totals["count"] else None,
        "first_year_dscr_current": over(first_year, current), "first_year_dscr_with_refinance": over(first_with_terms, terms),
        "first_year_earnings_with_refinance": _money(first_with_terms),
        "annual_new_operating_expenses": new_operating if baseline.get("loan_operating_confirmed", True) else None,
        "coverage_basis": "Run-rate coverage uses a full year with every change in place; first-year coverage counts each change from its start month and deducts one-time setup costs.",
        "dscr_current": over(after, current), "dscr_with_refinance": over(with_terms, terms),
        "annual_debt_service": current, "refinance_annual_debt_service": terms,
        "target_gap_current": _money(max(0.0, current * 1.25 - after)) if after is not None and current else None,
        "target_gap_refinance": _money(max(0.0, terms * 1.25 - with_terms)) if with_terms is not None and terms else None,
    }


def _six(figures: dict | None) -> dict:
    """The six figures in the v1 comparison's names and units (margin as a percent)."""
    if not figures:
        return {"earnings": None, "noi": None, "dscr": None, "cash_after_debt": None, "operating_margin": None, "debt_service": None}
    margin = figures.get("operating_margin")
    return {"earnings": _money(figures.get("ebitda")), "noi": _money(figures.get("noi")), "dscr": _ratio(figures.get("dscr")),
            "cash_after_debt": _money(figures.get("free_cash_flow")), "operating_margin": round(margin * 100, 2) if margin is not None else None,
            "debt_service": _money(figures.get("debt_service"))}


def _comparison(detail: dict, baseline: dict) -> dict:
    basis = "with_terms" if baseline.get("debt_service_with_terms") is not None else "current_debt"
    available = bool(baseline.get("available"))
    return {
        "loan_basis": "saved_terms" if basis == "with_terms" else "current_debt",
        "proposed_basis": "after_setup_before_timing",
        "target_gap": {"proposed": _money(detail["target"]["proposed"]) if available else None,
                       "approved": _money(detail["target"]["reviewed"]) if available else None},
        "baseline": _six(detail["metrics"]["today"]["current_debt"] if available else None),
        "proposed": _six(detail["metrics"]["proposed"][basis] if available else None),
        "approved": _six(detail["metrics"]["reviewed"][basis] if available else None),
    }


def _working(detail: dict, baseline: dict, view: str = "proposed") -> dict:
    """The run-rate scenario in the draft summary's names (the marketplace projection reads these)."""
    available = bool(baseline.get("available"))
    scenario, first = detail[view], detail["first_year"][view]
    e0, noi0 = baseline.get("ebitda"), baseline.get("noi")
    current, terms = baseline.get("debt_service_current"), baseline.get("debt_service_with_terms")
    plan_debt = terms if terms is not None else current

    def over(numerator, denominator):
        return _ratio(numerator / denominator) if available and numerator is not None and denominator else None

    delta = scenario["ebitda"] - e0 if available else None
    charges = engine.loan_operating(baseline)
    before_ratio, after_ratio, terms_ratio = over(e0, current), over(scenario["ebitda"], current), over(scenario["ebitda"] - charges, terms)
    cash_before = e0 - current if available and current is not None else None
    cash_after = scenario["ebitda"] - charges - plan_debt if available and plan_debt is not None else None
    loan_cash = (current - terms) / 12 if current is not None and terms is not None else None
    return {
        "basis": "after_setup_before_timing",
        "noi": _money(scenario["noi"]) if available else None, "noi_delta": _money(scenario["noi"] - noi0) if available else None,
        "earnings": _money(scenario["ebitda"]) if available else None, "earnings_delta": _money(delta),
        "cash_delta_annual": _money(delta), "cash_delta_monthly": _money(delta / 12) if delta is not None else None,
        "dscr_current_debt_before": before_ratio, "dscr_current_debt_after": after_ratio,
        "dscr_current_debt_delta": _ratio(after_ratio - before_ratio) if after_ratio is not None and before_ratio is not None else None,
        "first_year_noi": _money(first["noi"]) if available else None, "first_year_noi_delta": _money(first["noi"] - noi0) if available else None,
        "first_year_earnings": _money(first["ebitda"]) if available else None,
        "first_year_earnings_delta": _money(first["ebitda"] - e0) if available else None, "first_year_cash_delta": _money(first["ebitda"] - e0) if available else None,
        "first_year_dscr_current_debt_after": over(first["ebitda"], current),
        "loan_cash_change_monthly": _money(loan_cash), "loan_cash_change_annual": _money(loan_cash * 12) if loan_cash is not None else None,
        "cash_after_debt_basis": "earnings_less_debt_service_illustration",
        "cash_after_debt_mode": "unavailable" if cash_after is None else "saved_refinance_and_operations" if terms is not None else "current_debt_operations_only",
        "cash_after_debt_before_annual": _money(cash_before), "cash_after_debt_after_annual": _money(cash_after),
        "cash_after_debt_delta_annual": _money(cash_after - cash_before) if cash_after is not None and cash_before is not None else None,
        "cash_after_debt_before_monthly": _money(cash_before / 12) if cash_before is not None else None,
        "cash_after_debt_after_monthly": _money(cash_after / 12) if cash_after is not None else None,
        "cash_after_debt_delta_monthly": _money((cash_after - cash_before) / 12) if cash_after is not None and cash_before is not None else None,
        "dscr_with_refinance_after": terms_ratio,
        "dscr_with_refinance_delta": _ratio(terms_ratio - before_ratio) if terms_ratio is not None and before_ratio is not None else None,
        "refinance_annual_debt_service": _money(terms),
    }


def _draft_feedback(detail: dict, baseline: dict, base_block: dict, active: int) -> dict:
    available = bool(baseline.get("available"))
    return {"basis": "after_setup_before_timing", "actions": [], "summary": {
        "status": "no_changes" if not active else "calculated" if available else "source_blocked", "complete": True,
        "currency": base_block.get("plan_currency"), "noi_currency": base_block.get("noi_currency"),
        "current_earnings": base_block.get("earnings"), "earnings_currency": base_block.get("currency"), "earnings_kind": base_block.get("earnings_kind"),
        "source_comparable": available, "current_noi": base_block.get("noi"), "working": _working(detail, baseline),
        "baseline_ready": available, "baseline_missing": [] if available else ["Select an earnings source before planning its changes."],
        "source_issues": [], "missing": [],
        "notes": ["Run-rate figures after recurring setup costs and before timing; not a staff-reviewed forecast unless marked reviewed.",
                  "Loan cash relief changes debt service, never operating income."]}}


def _expense_groups(body: dict, baseline: dict) -> list[dict]:
    rows = []
    for group in baseline.get("groups") or []:
        members = [line for line in body.get("lines") or [] if line.get("group") == group["key"]]
        disclosed = round(sum(number(line.get("base")) or 0.0 for line in members), 2) if members else None
        difference = round(disclosed - group["amount"], 2) if disclosed is not None else None
        rows.append({"key": group["key"], "category": group.get("category") or group["key"], "label": group["label"],
                     "baseline_annual_amount": _money(group["amount"]), "disclosed_annual_amount": disclosed, "difference_annual": difference,
                     "status": "no_detail" if not members else "matches_source" if abs(difference) <= 1 else "differs_from_source", "notes": []})
    return rows


def _disclosure(body: dict) -> dict:
    lines = body.get("lines") or []
    cogs = sum(number(line.get("base")) or 0.0 for line in lines if line.get("group") == engine.COGS_GROUP)
    operating = sum(number(line.get("base")) or 0.0 for line in lines if line.get("group") != engine.COGS_GROUP)
    mix: dict[str, dict] = {}
    for line in lines:
        entry = mix.setdefault(line.get("group"), {"category": line.get("group"), "annual_amount": 0.0, "missing_count": 0, "count": 0, "quantified_count": 0})
        entry["annual_amount"] += number(line.get("base")) or 0.0
        entry["count"] += 1
        entry["quantified_count"] += 1
    return {"operating_monthly": _money(operating / 12), "cogs_monthly": _money(cogs / 12), "operating_cash_monthly": _money((operating + cogs) / 12),
            "non_operating_monthly": 0.0, "one_time_total": 0.0, "active_count": len(lines), "excluded_count": 0, "missing_inputs": [], "cost_issues": [],
            "complete": bool(lines), "cost_mix": [{**row, "annual_amount": _money(row["annual_amount"])} for row in mix.values()]}


def _released(body: dict, cockpit: dict, detail: dict) -> dict:
    """Released cash and how the plan assigns it. Unassigned cash stays as cash; only assigning more than there is is a problem."""
    from app.services import released_cash_flow

    current = released_cash_flow.source(cockpit)
    rows = [{"id": row["id"], "label": row.get("label") or "", "amount_monthly": row.get("monthly_amount"),
             "purpose": PURPOSES.get(row.get("purpose"), "other"), "notes": row.get("notes") or ""} for row in body.get("allocations") or []]
    amount = current["amount_monthly"]
    allocated = round(detail["released"]["allocated_monthly"], 2)
    remaining = round(amount - allocated, 2) if amount is not None else None
    missing = ["Monthly allocations cannot exceed the saved plan's recurring debt-service relief."] if rows and remaining is not None and remaining < 0 else []
    required = bool(rows)
    return {"required": required, "available": current["available"],
            "status": "unavailable" if not current["available"] else "not_required" if not required else "incomplete" if missing else "complete",
            "currency": "USD", "source_fingerprint": current["fingerprint"], "amount_monthly": amount, "allocated_monthly": allocated,
            "unallocated_monthly": remaining, "complete": not missing, "missing": missing, "allocations": rows,
            "source": {key: current[key] for key in ("updated_at", "before_monthly", "after_monthly", "new_operating_expenses_monthly",
                                                     "new_financing_fees_monthly", "reconciled_retained_override")},
            "note": "Unassigned positive cash is kept as cash; reinvestment is optional. Debt-service relief does not increase historical earnings, EBITDA or NOI."}


def _cash_bridge(released: dict, detail: dict, baseline: dict) -> dict:
    current, terms = baseline.get("debt_service_current_monthly"), baseline.get("debt_service_with_terms_monthly")
    relief = _money(current - terms) if current is not None and terms is not None else None
    operations = (detail["proposed"]["ebitda"] - baseline["ebitda"]) / 12 if baseline.get("available") else None
    return {"status": released["status"], "available": released["available"], "required": False, "complete": released["complete"], "stale": False,
            "currency": "USD", "before_debt_service_monthly": current, "after_debt_service_monthly": terms,
            "new_operating_charges_monthly": released["source"].get("new_operating_expenses_monthly"),
            "new_financing_fees_monthly": released["source"].get("new_financing_fees_monthly"),
            "gross_loan_relief_monthly": relief, "net_loan_cash_change_monthly": released["amount_monthly"],
            "recurring_cash_released_monthly": released["amount_monthly"], "modeled_operations_monthly": _money(operations),
            "net_operations_first_year": _money(detail["first_year"]["proposed"]["ebitda"] - baseline["ebitda"]) if baseline.get("available") else None,
            "combined_incremental_monthly": _money((released["amount_monthly"] or 0.0) + operations) if operations is not None else None,
            "allocated_monthly": released["allocated_monthly"], "unallocated_monthly": released["unallocated_monthly"],
            "missing": list(released["missing"]), "source_fingerprint": released["source_fingerprint"],
            "allocations": [{"id": row["id"], "purpose": row["purpose"], "expense_row_id": None, "action_id": None, "amount_monthly": row["amount_monthly"],
                             "capacity_monthly": None, "status": "assigned", "issues": []} for row in released["allocations"]],
            "notes": ["Released cash is the drop in regular monthly debt service under the saved loan terms. It funds cash uses; it is not operating income."]}


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

def evaluate(raw: Any, cockpit: dict, *, source_file_ids=None, now=None, include_expense_changes: bool = False) -> dict:
    """A v2 plan against the file, with the engine's result and the v1 keys its readers use."""
    from app.schemas import earnings_improvement as v1_schema
    from app.services import earnings_improvement as v1

    body = schema.validate_body(raw)
    baseline = engine_baseline(cockpit)
    asked = questions(baseline)
    live, stale = _with_live_reviews(body, baseline)
    stamp = baseline_stamp(baseline)
    source_changed = bool(body["source"].get("stamp") and stamp and body["source"]["stamp"] != stamp)
    if source_changed:
        # Confirmations were given to other amounts; they are asked again.
        live["source"] = {**live["source"], "confirmations": {}}
    goal = cockpit.get("_operating_goal") or {}
    goal_amount = goal.get("amount") if goal.get("status") == "set" else None
    plan = engine.Plan(live, baseline)
    detail = engine.evaluate(live, baseline, goal=goal_amount, questions=asked)
    # Everything that depends only on the file (the source baseline, coverage targets, the planning threshold) is read the way it always was.
    file_view = v1.evaluate(v1_schema.empty_body(), cockpit)
    base_block = {**file_view["baseline"], "plan_currency": body["currency"], "revenue": baseline["revenue"] if baseline["available"] else file_view["baseline"].get("revenue")}
    actions = _actions(plan, detail, live, stale)
    active = [row for row in actions if not row["excluded"] and row["status"] != "rejected"]
    totals = {"proposed": _totals(plan, detail, baseline, "proposed"), "approved": _totals(plan, detail, baseline, "reviewed")}
    projection = {"proposed": _projection(detail, baseline, totals["proposed"], "proposed", active=len(active)),
                  "approved": _projection(detail, baseline, totals["approved"], "reviewed", active=len(active))}
    released = _released(body, cockpit, detail)
    risks = list(file_view["risk_flags"])
    changed_message = "The source figures changed after this plan was built. Confirm the source amounts again and check the cost lines against them."
    if source_changed:
        risks.append(changed_message)
    if stale:
        risks.append(f"{len(stale)} reviewed change(s) need another review: the source figures or the plan's model changed after they were approved.")
    if released["missing"]:
        risks.append(released["missing"][0])
    if not baseline.get("loan_operating_confirmed", True):
        risks.append("The saved loan terms list a recurring operating cost whose amount is not confirmed. Coverage with the terms counts it as zero until the budget is confirmed.")
    missing = [] if baseline["available"] else [baseline.get("reason") or "An earnings source is needed before the plan's figures can be shown against the file."]
    result = {
        "schema_version": RESULT_VERSION, "context_stamp": v1.context_stamp(cockpit),
        "disclosure": _disclosure(body), "expense_groups": _expense_groups(body, baseline), "released_cash_flow": released,
        "actions": actions, "totals": totals, "baseline": base_block, "recommendation": file_view["recommendation"],
        "projection": projection, "targets": file_view["targets"], "missing": missing, "risk_flags": risks,
        "all_actions_reviewed": bool(active) and all(row["review_state"] == "approved" for row in active),
        "notes": list(NOTES),
        # The reviewed scenario as one figure: changes interact, so summing each change's own effect would not add up.
        "additive_totals": {"approved": {key: totals["approved"][key] for key in ("annual_impact", "first_year_impact", "count")},
                            "proposed": {key: totals["proposed"][key] for key in ("annual_impact", "first_year_impact", "count")}},
        "comparison": _comparison(detail, baseline),
        "draft_feedback": _draft_feedback(detail, baseline, base_block, len(active)),
        "plan": {
            **_rounded(detail), "baseline": _rounded(baseline), "questions": asked, "source_stamp": stamp, "source_changed": source_changed,
            "stale_reviews": sorted(stale), "labels": {item["id"]: plan.label(item["id"]) for item in plan.items},
            "warnings": [*([{"code": "over_allocated", "message": released["missing"][0]}] if released["missing"] else []),
                         *([{"code": "source_changed", "message": changed_message}] if source_changed else [])],
        },
    }
    if goal.get("status") == "set":
        from app.services.operating_plan_tracking import goal_progress

        result["goal_progress"] = goal_progress(goal, result)
        result["cash_bridge"] = _cash_bridge(released, detail, baseline)
    return result
