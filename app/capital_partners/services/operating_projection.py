"""A saved-version, public-safe future operating illustration for an offering.

No draft reads, writes, review promotion, source replacement or financial math
in a browser. All amounts and ratios come from the canonical plan evaluator.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from app.models.financial_form_version import FinancialFormVersion
from app.schemas.earnings_improvement import empty_body
from app.services import earnings_improvement as engine
from app.services.operating_plan_service import protect_reviews

POINT_FIELDS = ("status", "basis", "noi_annual", "noi_currency", "earnings_annual", "earnings_currency",
    "earnings_kind", "dscr_current_debt", "dscr_with_refinance", "dscr_current_debt_delta",
    "dscr_with_refinance_delta", "cash_after_debt_annual", "cash_after_debt_monthly", "cash_currency",
    "noi_delta_annual", "earnings_delta_annual", "cash_delta_annual", "cash_delta_monthly")
TERMS_FIELDS = ("terms_state", "recorded_terms_ref", "deal_budget_revision", "program_name", "currency", "amount", "rate_pct", "term_months")


def _point(summary, *, mode="proposed"):
    working = summary.get("working") or {}
    current = mode == "current"
    refinance = mode == "refinance"
    return {"status": summary.get("status"), "basis": working.get("basis"),
        "noi_annual": summary.get("current_noi") if current or refinance else working.get("noi"),
        "noi_currency": summary.get("noi_currency"),
        "earnings_annual": summary.get("current_earnings") if current or refinance else working.get("earnings"),
        "earnings_currency": summary.get("earnings_currency"), "earnings_kind": summary.get("earnings_kind"),
        "dscr_current_debt": working.get("dscr_current_debt_before") if current else working.get("dscr_current_debt_after"),
        "dscr_with_refinance": None if current else working.get("dscr_with_refinance_after"),
        "dscr_current_debt_delta": 0 if current and working.get("dscr_current_debt_before") is not None else working.get("dscr_current_debt_delta"),
        "dscr_with_refinance_delta": None if current else working.get("dscr_with_refinance_delta"),
        "cash_after_debt_annual": working.get("cash_after_debt_before_annual") if current else working.get("cash_after_debt_after_annual"),
        "cash_after_debt_monthly": working.get("cash_after_debt_before_monthly") if current else working.get("cash_after_debt_after_monthly"),
        "cash_currency": summary.get("earnings_currency") if working.get("cash_after_debt_before_annual") is not None else None,
        "noi_delta_annual": 0 if (current or refinance) and summary.get("current_noi") is not None else working.get("noi_delta"),
        "earnings_delta_annual": 0 if (current or refinance) and summary.get("current_earnings") is not None else working.get("earnings_delta"),
        "cash_delta_annual": 0 if current and working.get("cash_after_debt_before_annual") is not None else working.get("cash_after_debt_delta_annual"),
        "cash_delta_monthly": 0 if current and working.get("cash_after_debt_before_monthly") is not None else working.get("cash_after_debt_delta_monthly")}


def project(body, cockpit, saved_version):
    """Pure seam: never exposes action/payee details or private review metadata."""
    body = deepcopy(body)
    saved_version = deepcopy(saved_version)
    if saved_version.get("saved_at"):
        stamp = datetime.fromisoformat(saved_version["saved_at"].replace("Z", "+00:00"))
        saved_version["saved_at"] = stamp.replace(tzinfo=stamp.tzinfo or UTC).astimezone(UTC).isoformat()
    if saved_version.get("state") == "client_submitted":
        body = protect_reviews(body, empty_body())
    sources = [row["file_id"] for row in (cockpit.get("operating_evidence") or {}).get("files", [])]
    result = engine.evaluate(body, cockpit, source_file_ids=sources, include_expense_changes=True)
    base = deepcopy(body)
    from app.schemas import earnings_plan_v2

    if earnings_plan_v2.is_v2(base):
        base = earnings_plan_v2.without_changes(base)
    else:
        base["adjustments"] = []
    baseline = engine.evaluate(base, cockpit, source_file_ids=sources, include_expense_changes=True)
    summary = result["draft_feedback"]["summary"]
    current = baseline["draft_feedback"]["summary"]
    active = [row for row in result["actions"] if not row.get("excluded") and row.get("status") != "rejected"]
    reviewed = bool(active) and all(row.get("included_in_approved_projection") for row in active)
    proposed = _point(summary)
    qualified = any(proposed.get(key) is not None for key in ("noi_annual", "earnings_annual", "dscr_with_refinance"))
    return {"schema_version": "marketplace-operating-projection-v1",
        "status": "conditional" if qualified else "incomplete",
        "saved_version": {key: saved_version.get(key) for key in ("id", "version_number", "state", "saved_at")},
        "qualification": "reviewed" if reviewed else "proposed" if qualified else "needs_inputs",
        "historical_figures_unchanged": True, "basis": "future_operating_plan",
        "saved_terms": {key: deepcopy((cockpit.get("proposed_terms") or {}).get(key)) for key in TERMS_FIELDS},
        "current": _point(current, mode="current"), "refinance_only": _point(current, mode="refinance"),
        "proposed_operating": proposed,
        "reviewed_operating": deepcopy(proposed) if reviewed else {key: None for key in POINT_FIELDS},
        "missing": (["Confirm saved operating-plan inputs before relying on unavailable projected figures."] if summary.get("missing") else [])
            + (["Reconcile the saved plan with the current accepted financial source."] if summary.get("source_issues") else []),
        "notes": ["Future operating underwriting plan; implementation and results remain conditional. These are not actual or amended tax results.",
            "Proposed changes are not staff-approved unless the qualification explicitly says reviewed. A saved version alone is not underwriting approval.",
            "Cash is an earnings-less-debt-service illustration and monthly run rate, not a forecast of ending bank cash. Loan proceeds and repayment relief do not increase operating NOI.",
            "First-year timing and achieved performance are not inferred from annual run rates."]}


async def saved(db, profile, cockpit):
    from app.services.financial_workspace import existing_family

    if not (cockpit.get("operating_plan") or {}).get("available"):
        return None
    family = await existing_family(db, profile.id, "operating_plan")
    record = await db.get(FinancialFormVersion, family.current_version_id) if family and family.current_version_id else None
    if record is None or not record.body:
        return None
    return project(record.body, cockpit, {"id": str(record.id), "version_number": record.version_number,
        "state": record.status, "saved_at": record.created_at.isoformat()})


def public(value):
    if not isinstance(value, dict):
        return None
    return {**{key: deepcopy(value.get(key)) for key in ("schema_version", "status", "qualification",
        "historical_figures_unchanged", "basis")},
        "missing": (["Confirm saved operating-plan inputs before relying on unavailable projected figures."] if value.get("missing") else []),
        "notes": ["Conditional future operating plan; not actual tax results or a guarantee of achieved performance.",
            "Cash figures are earnings-less-debt-service run rates, not forecast ending bank balances."],
        "saved_terms": {key: deepcopy((value.get("saved_terms") or {}).get(key)) for key in TERMS_FIELDS},
        "saved_version": {key: (value.get("saved_version") or {}).get(key) for key in ("id", "version_number", "state", "saved_at")},
        **{key: {field: deepcopy((value.get(key) or {}).get(field)) for field in POINT_FIELDS}
           for key in ("current", "refinance_only", "proposed_operating", "reviewed_operating")}}
