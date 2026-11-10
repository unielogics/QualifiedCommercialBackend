"""Recurring uses of a saved coverage plan's debt-service relief.

This allocation is a cash-management obligation. It never adds financing
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


def source(cockpit):
    plan = cockpit.get("plan") or {}
    record = plan.get("record") or {}
    evaluation = plan.get("evaluation") or {}
    service = (cockpit.get("debt") or {}).get("service") or {}
    earnings = evaluation.get("ebitda") or {}
    saved = bool(record.get("updated_at") and evaluation.get("active"))
    before = number(service.get("monthly"))
    after = number(evaluation.get("debt_service_after_regular_monthly"))
    candidate = round(before - after, 2) if before is not None and after is not None else None
    reasons = []
    if not saved:
        reasons.append("Save an active coverage plan before allocating its recurring released cash flow.")
    if before is None or after is None:
        reasons.append("Establish current and replacement regular monthly debt service; unknown payments are not zero.")
    if evaluation.get("coverage_unavailable_reason"):
        reasons.append(str(evaluation["coverage_unavailable_reason"]))
    if earnings.get("replacement_payment_missing"):
        reasons.append("Complete the replacement loan payment before allocating debt-service relief.")
    if earnings.get("debt_service_override_applied") or (record.get("overrides") or {}).get("debt_service_monthly") is not None:
        reasons.append("Reconcile the hypothetical debt-service override to current obligations before allocating relief.")
    if service.get("identity_association_conflict") or evaluation.get("retirement_replay_unknown"):
        reasons.append("Resolve the debt business association and retirement allocation before using released cash flow.")
    if service.get("currency") not in (None, "USD"):
        reasons.append("Reconcile current debt-service currency to USD before allocating released cash flow.")
    if before is not None and before < 0 or after is not None and after < 0:
        reasons.append("Debt-service amounts must be nonnegative.")
    available = saved and not reasons
    fingerprint = _stamp({"record": record, "service": service,
        "evaluation": {key: evaluation.get(key) for key in (
            "retired_ids", "debt_service_before_monthly", "debt_service_after_regular_monthly",
            "new_regular_payment_monthly", "coverage_unavailable_reason", "retirement_replay_unknown",
            "loan_terms", "schedule")}}) if saved else None
    return {"saved": saved, "available": available, "amount_monthly": max(0, candidate) if available and candidate is not None else None,
        "positive": bool(saved and candidate is not None and candidate > 0),
        "requires_reconciliation": bool(saved and not available and (record.get("retire") or evaluation.get("retired_count"))),
        "fingerprint": fingerprint, "currency": "USD", "reasons": list(dict.fromkeys(reasons)),
        "updated_at": record.get("updated_at"), "before_monthly": before, "after_monthly": after}


def evaluate(body, cockpit):
    current = source(cockpit)
    allocation = body.get("released_cash_flow") or {}
    rows = allocation.get("allocations") or []
    retained = bool(rows or allocation.get("source_fingerprint"))
    required = current["positive"] or current["requires_reconciliation"] or retained
    stale = retained and allocation.get("source_fingerprint") != current["fingerprint"]
    missing = []
    if required:
        if not current["available"]:
            missing.extend(current["reasons"])
        if stale:
            missing.append("The saved coverage plan or debt inputs changed. Reconcile and acknowledge the current released cash flow.")
        if not allocation.get("source_fingerprint"):
            missing.append("Acknowledge the saved coverage plan as the allocation source.")
        if not rows and current["positive"]:
            missing.append("Allocate the full recurring released cash flow to described monthly uses.")
    active_expenses = {row["id"]: row for row in body.get("expenses") or [] if not row.get("excluded")}
    for row in rows:
        if not row.get("label") or row.get("amount_monthly") is None or row["amount_monthly"] <= 0:
            missing.append("Each released-cash-flow allocation needs a described use and a positive monthly amount.")
        if row.get("expense_row_id") and str(row["expense_row_id"]) not in active_expenses:
            missing.append("Link allocations only to active expense rows in this operating plan.")
    allocated = round(sum(number(row.get("amount_monthly")) or 0 for row in rows), 2)
    amount = current["amount_monthly"]
    remaining = round(amount - allocated, 2) if amount is not None else None
    if required and current["available"] and (remaining is None or remaining != 0):
        missing.append("Monthly allocations must reconcile exactly to the saved plan's recurring debt-service relief.")
    if required and body.get("currency") != "USD":
        missing.append("Released cash flow is denominated in USD; reconcile the operating plan currency before allocating it.")
    complete = not missing if required else True
    return {"required": required, "available": current["available"],
        "status": "stale" if stale else "unavailable" if not current["available"] else "not_required" if not required else "complete" if complete else "incomplete",
        "currency": "USD", "source_fingerprint": current["fingerprint"], "amount_monthly": amount,
        "allocated_monthly": allocated, "unallocated_monthly": remaining, "complete": complete,
        "missing": list(dict.fromkeys(missing)), "allocations": rows,
        "source": {key: current[key] for key in ("updated_at", "before_monthly", "after_monthly")},
        "note": "Recurring cash allocation only. Debt-service relief does not increase historical earnings, EBITDA or NOI."}


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
