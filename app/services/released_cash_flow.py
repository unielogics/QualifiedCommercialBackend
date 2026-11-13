"""Recurring uses of a saved coverage plan's debt-service relief.

Unassigned positive cash is retained; reinvestment is optional. This never adds financing
proceeds or principal/interest savings to historical operating earnings.
"""
from __future__ import annotations

import hashlib
import json

from app.services.financial_metric_review import number

REQUIREMENT_KEY = "operating_expenses_and_earnings_plan"
POLICY_KEY = "coverage_plan"


def _stamp(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _same_money(left, right):
    first, second = number(left), number(right)
    return first is not None and second is not None and round(first, 2) == round(second, 2)


def _reconciled_retained_override(cockpit):
    """A recorded remaining-debt amount must reconcile to this exact schedule."""
    debt, plan = cockpit.get("debt") or {}, cockpit.get("plan") or {}
    service, record = debt.get("service") or {}, plan.get("record") or {}
    evaluation, link = plan.get("evaluation") or {}, plan.get("terms_link") or {}
    recorded, adopted = link.get("recorded") or {}, record.get("terms_ref") or {}
    retained = number((record.get("overrides") or {}).get("debt_service_monthly"))
    if (retained is None or retained < 0 or service.get("source") != "accepted_debt_schedule"
            or service.get("schedule_authoritative") is not True or not service.get("version_id")
            or service.get("identity_association_conflict") or link.get("state") != "mirrored"
            or recorded.get("kind") not in {"client_terms", "production_term_sheet"}
            or not recorded.get("id") or not recorded.get("version")
            or recorded.get("treatment") != "refinance"
            or any(str(adopted.get(key)) != str(recorded.get(key)) for key in ("kind", "id", "version"))):
        return False
    selected = record.get("retire") or []
    rows = debt.get("obligations") or []
    ids = [row.get("id") for row in rows]
    selected_ids = set(selected)
    if (not selected or len(selected) != len(selected_ids) or any(not key for key in ids)
            or len(ids) != len(set(ids)) or not selected_ids.issubset(ids)
            or set(evaluation.get("retired_ids") or []) != selected_ids
            or any(row.get("source") != "debt_schedule_row" or row.get("replay_requires_review") for row in rows)):
        return False
    payments = [number(row.get("monthly")) for row in rows]
    if any(value is None or value < 0 for value in payments):
        return False
    total = sum(payments)
    remaining = sum(value for row, value in zip(rows, payments, strict=True) if row["id"] not in selected_ids)
    annual = number(service.get("annual"))
    recorded_annual = number(recorded.get("retained_annual_debt_service"))
    return bool(_same_money(total, service.get("monthly")) and annual is not None and _same_money(annual / 12, total)
        and _same_money(remaining, retained) and recorded_annual is not None and _same_money(recorded_annual / 12, retained)
        and _same_money(evaluation.get("debt_service_before_new_payment_monthly"), retained))


def source(cockpit):
    plan = cockpit.get("plan") or {}
    record = plan.get("record") or {}
    evaluation = plan.get("evaluation") or {}
    service = (cockpit.get("debt") or {}).get("service") or {}
    earnings = evaluation.get("ebitda") or {}
    saved = bool(record.get("updated_at") and evaluation.get("active"))
    before = number(service.get("monthly"))
    after = number(evaluation.get("debt_service_after_regular_monthly"))
    operating = number(evaluation.get("annual_new_operating_expenses", 0))
    financing = number(evaluation.get("annual_new_financing_fees", 0))
    # Regular debt service already includes the new financing-fee proxy. The
    # separately classified operating costs reduce available cash once here.
    candidate = round(before - after - operating / 12, 2) if before is not None and after is not None and operating is not None else None
    reconciled = _reconciled_retained_override(cockpit)
    reasons = []
    if not saved:
        reasons.append("Save an active coverage plan before allocating its recurring released cash flow.")
    if before is None or after is None:
        reasons.append("Establish current and replacement regular monthly debt service; unknown payments are not zero.")
    if evaluation.get("coverage_unavailable_reason"):
        reasons.append(str(evaluation["coverage_unavailable_reason"]))
    if earnings.get("replacement_payment_missing"):
        reasons.append("Complete the replacement loan payment before allocating debt-service relief.")
    if (earnings.get("debt_service_override_applied") or (record.get("overrides") or {}).get("debt_service_monthly") is not None) and not reconciled:
        reasons.append("Reconcile the hypothetical debt-service override to current obligations before allocating relief.")
    if operating is None or operating < 0:
        reasons.append("Reconcile the new recurring operating expenses before allocating released cash flow; unknown costs are not zero.")
    if financing is None or financing < 0:
        reasons.append("Reconcile the new recurring financing fees included in debt service before allocating released cash flow.")
    principal = evaluation.get("principal_solution") or {}
    if principal and (principal.get("status") != "calculated" or principal.get("requires_apply")):
        reasons.append("Apply and save the current deal costs and their shared loan principal before allocating released cash flow.")
    if service.get("identity_association_conflict") or evaluation.get("retirement_replay_unknown"):
        reasons.append("Resolve the debt business association and retirement allocation before using released cash flow.")
    if service.get("currency") not in (None, "USD"):
        reasons.append("Reconcile current debt-service currency to USD before allocating released cash flow.")
    if before is not None and before < 0 or after is not None and after < 0:
        reasons.append("Debt-service amounts must be nonnegative.")
    available = saved and not reasons
    link = plan.get("terms_link") or {}
    fingerprint = _stamp({"record": record,
        "service": {key: service.get(key) for key in ("source", "version_id", "schedule_authoritative", "monthly", "annual", "currency", "identity_association_conflict")},
        "retained_reconciliation": {"matched": reconciled,
            "obligations": sorted(({key: row.get(key) for key in ("id", "source", "monthly", "balance", "replay_requires_review")}
                for row in (cockpit.get("debt") or {}).get("obligations") or []), key=lambda row: str(row["id"])),
            "link_state": link.get("state"), "recorded": {key: (link.get("recorded") or {}).get(key)
                for key in ("kind", "id", "version", "treatment", "retained_annual_debt_service")}},
        "evaluation": {key: evaluation.get(key) for key in (
            "retired_ids", "debt_service_before_monthly", "debt_service_after_regular_monthly",
            "new_regular_payment_monthly", "coverage_unavailable_reason", "retirement_replay_unknown",
            "loan_terms", "schedule", "annual_new_operating_expenses", "annual_new_financing_fees",
            "financing_budget_fingerprint", "principal_solution")}}) if saved else None
    return {"saved": saved, "available": available, "amount_monthly": max(0, candidate) if available and candidate is not None else None,
        "positive": bool(saved and candidate is not None and candidate > 0),
        "requires_reconciliation": bool(saved and not available and (record.get("retire") or evaluation.get("retired_count"))),
        "fingerprint": fingerprint, "currency": "USD", "reasons": list(dict.fromkeys(reasons)),
        "updated_at": record.get("updated_at"), "before_monthly": before, "after_monthly": after,
        "new_operating_expenses_monthly": round(operating / 12, 2) if operating is not None else None,
        "new_financing_fees_monthly": round(financing / 12, 2) if financing is not None else None,
        "reconciled_retained_override": reconciled}


def evaluate(body, cockpit):
    current = source(cockpit)
    allocation = body.get("released_cash_flow") or {}
    rows = allocation.get("allocations") or []
    required = bool(rows or current["requires_reconciliation"])
    stale = bool(rows and allocation.get("source_fingerprint") != current["fingerprint"])
    missing = []
    if required:
        if not current["available"]:
            missing.extend(current["reasons"])
        if stale:
            missing.append("The saved coverage plan or debt inputs changed. Reconcile and acknowledge the current released cash flow.")
        if rows and not allocation.get("source_fingerprint"):
            missing.append("Acknowledge the saved coverage plan as the allocation source.")
    active_expenses = {row["id"]: row for row in body.get("expenses") or [] if not row.get("excluded")}
    for row in rows:
        if not row.get("label") or row.get("amount_monthly") is None or row["amount_monthly"] <= 0:
            missing.append("Each released-cash-flow allocation needs a described use and a positive monthly amount.")
        if row.get("expense_row_id") and str(row["expense_row_id"]) not in active_expenses:
            missing.append("Link allocations only to active expense rows in this operating plan.")
    allocated = round(sum(number(row.get("amount_monthly")) or 0 for row in rows), 2)
    amount = current["amount_monthly"]
    remaining = round(amount - allocated, 2) if amount is not None else None
    if rows and current["available"] and (remaining is None or remaining < 0):
        missing.append("Monthly allocations cannot exceed the saved plan's recurring debt-service relief.")
    if required and body.get("currency") != "USD":
        missing.append("Released cash flow is denominated in USD; reconcile the operating plan currency before allocating it.")
    complete = not missing if required else True
    return {"required": required, "available": current["available"],
        "status": "stale" if stale else "unavailable" if not current["available"] else "not_required" if not required else "complete" if complete else "incomplete",
        "currency": "USD", "source_fingerprint": current["fingerprint"], "amount_monthly": amount,
        "allocated_monthly": allocated, "unallocated_monthly": remaining, "complete": complete,
        "missing": list(dict.fromkeys(missing)), "allocations": rows,
        "source": {key: current[key] for key in ("updated_at", "before_monthly", "after_monthly",
            "new_operating_expenses_monthly", "new_financing_fees_monthly", "reconciled_retained_override")},
        "note": "Unassigned positive cash is kept as cash; reinvestment is optional. Debt-service relief does not increase historical earnings, EBITDA or NOI."}


async def derived_requirement(db, profile):
    # Most files have no saved plan; avoid an extra financial evidence read.
    if not (getattr(profile, "financial_plan", None) or {}).get("updated_at"):
        return None
    from app.services import funding_program_baselines, requirement_library
    from app.services.financial_cockpit import load_cockpit

    current = source(await load_cockpit(db, profile))
    if not current["positive"] and not current["requires_reconciliation"]:
        return None
    row = await requirement_library.materialize_baseline_row(db, funding_program_baselines.OPERATING_PLAN)
    return requirement_library.ExtraDefinition(requirement_library.transient_copy(row, required_level="required"), 1,
        {"key": REQUIREMENT_KEY, "source_policy_key": POLICY_KEY, "source_fingerprint": current["fingerprint"]})


def apply_requirement(state, requirement, requested, cockpit):
    """An old PDF or receipt cannot satisfy a changed recurring allocation."""
    key = getattr(requirement, "canonical_key", None) or requirement.requirement_key
    if key != REQUIREMENT_KEY:
        return
    from app.schemas.earnings_improvement import empty_body
    from app.services import requirement_lifecycle

    published = (cockpit.get("operating_plan") or {}).get("evaluation") or {}
    allocation = published.get("released_cash_flow") or evaluate(empty_body(), cockpit)
    metadata = {**(state.provenance or {}), "released_cash_flow": allocation}
    state.provenance = metadata
    if not allocation["required"] or allocation["complete"] or state.status in {"waived", "not_applicable"} or requirement_lifecycle.is_removed(state):
        return
    if state.status != "failed":
        state.status = "stale" if allocation["status"] == "stale" else "requested" if requested else "missing"
        state.state_reason = allocation["missing"][0] if allocation["missing"] else "Complete the recurring released-cash-flow allocation."
    state.verified_at = state.verified_by_user_id = None
    for key in ("coverage", "verified_coverage"):
        metadata[key] = {**(metadata.get(key) or {}), "complete": False, "current": 0}
    metadata["collection_completion"] = guard_collection(metadata.get("collection_completion") or {}, state)
    if requested and requested.status not in {"closed", "not_applicable"}:
        requested.status = "requested"


def guard_collection(snapshot, state):
    allocation = (state.provenance or {}).get("released_cash_flow") or {}
    if not allocation.get("required") or allocation.get("complete") or state.status in {"waived", "not_applicable"}:
        return snapshot
    return {**snapshot, "complete": False, "can_complete": False,
        "fingerprint": _stamp({"files": snapshot.get("fingerprint"), "allocation": allocation}),
        "reason": (allocation.get("missing") or ["Complete the recurring released-cash-flow allocation."])[0]}


async def materialize(db, profile, requirement, state, requested):
    if (getattr(requirement, "canonical_key", None) or requirement.requirement_key) != REQUIREMENT_KEY:
        return
    if not ((getattr(profile, "financial_plan", None) or {}).get("updated_at")
            or (state.provenance or {}).get("released_cash_flow", {}).get("required")):
        return
    from app.services.financial_cockpit import load_cockpit

    apply_requirement(state, requirement, requested, await load_cockpit(db, profile))
