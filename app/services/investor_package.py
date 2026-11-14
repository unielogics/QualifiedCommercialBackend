"""Read-only package choices and a source-bound operating-plan snapshot."""

from copy import deepcopy

from app.models.financial_form_version import FinancialFormDraft, FinancialFormVersion
from app.schemas.earnings_improvement import cost_amount, empty_body
from app.services import earnings_improvement as earnings
from app.services import operating_plan_service


def options(value=None):
    value = value or {}
    return {"include_operating_plan": value.get("include_operating_plan", True) is True,
            "include_working_operating_plan": value.get("include_working_operating_plan", False) is True}


def _unstarted_allocation(cockpit):
    from app.services.released_cash_flow import evaluate

    allocation = evaluate(empty_body(), cockpit)
    if not allocation["required"]:
        return None
    return {"released_cash_flow": allocation, "missing": allocation["missing"], "currency": "USD",
        "risk_flags": [], "notes": [allocation["note"]], "expenses": [], "adjustments": [],
        "snapshot": {"state": "not_started", "label": "Recurring released-cash-flow allocation not started",
                     "historical_figures_unchanged": True}}


async def operating_snapshot(db, profile, cockpit, choices):
    """Never creates a family, publishes a draft or promotes an approval."""
    choices = options(choices)
    if not choices["include_operating_plan"]:
        return None
    from app.services.financial_workspace import existing_family

    family = await existing_family(db, profile.id, "operating_plan")
    if family is None:
        return _unstarted_allocation(cockpit)
    working = choices["include_working_operating_plan"]
    record = await db.get(FinancialFormDraft, family.id) if working else (
        await db.get(FinancialFormVersion, family.current_version_id) if family.current_version_id else None)
    if record is None or not record.body:
        return _unstarted_allocation(cockpit)
    body = deepcopy(record.body)
    if working or getattr(record, "status", None) == "client_submitted":
        body = operating_plan_service.protect_reviews(body, empty_body())
    evaluation = earnings.evaluate(body, cockpit,
        source_file_ids=[row["file_id"] for row in (cockpit.get("operating_evidence") or {}).get("files", [])])
    result = operating_plan_service.shareable_package(evaluation, cockpit.get("operating_evidence"))
    result["currency"] = body.get("currency")
    names = {str(row["file_id"]): row.get("file_name") for row in (cockpit.get("operating_evidence") or {}).get("files", [])}
    result["expenses"] = [{**{key: deepcopy(row.get(key)) for key in (
        "id", "category", "label", "payee", "current_amount", "cadence", "essential", "notes", "source_file_ids",
        "cost_basis", "unit_label", "current_units_per_period", "current_unit_cost", "current_fixed_amount")},
        "effective_period_amount": cost_amount(row),
        "source_names": [names[str(fid)] for fid in row.get("source_file_ids", []) if names.get(str(fid))]}
        for row in body.get("expenses", []) if not row.get("excluded")]
    if operating_plan_service.is_v2(body):
        result["expenses"] = [{"id": row["id"], "category": row.get("group"), "label": row.get("label"), "payee": "", "current_amount": row.get("base"),
            "cadence": "annual", "essential": True, "notes": "", "source_file_ids": [], "cost_basis": "total", "unit_label": "",
            "current_units_per_period": None, "current_unit_cost": None, "current_fixed_amount": None,
            "effective_period_amount": row.get("base"), "source_names": []} for row in body.get("lines") or []]
    result["plan_notes"] = body.get("notes") or ""
    result["snapshot"] = {"state": "working_draft" if working else getattr(record, "status", "published"),
        "family_id": str(family.id), "revision": record.revision if working else None,
        "version_number": None if working else record.version_number,
        "label": "Unreviewed working draft - conditional illustration" if working else
                 "Client-submitted plan - awaiting staff review" if getattr(record, "status", None) == "client_submitted" else "Published operating plan",
        "historical_figures_unchanged": True}
    if working:
        result["risk_flags"] = ["Working-draft actions have not been approved; the package does not establish achieved savings.",
                                *result.get("risk_flags", [])]
    return result
