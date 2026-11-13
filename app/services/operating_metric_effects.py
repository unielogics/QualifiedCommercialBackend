"""Separate accounting NOI, coverage earnings and cash for noncash D&A."""
from __future__ import annotations

from app.services.financial_metric_review import number


def noncash_da(action, expenses):
    linked = expenses.get(action.get("expense_row_id")) or {}
    return (linked.get("category") == "depreciation_amortization"
            or action.get("kind") == "expense_group_change" and action.get("expense_category") == "depreciation_amortization")


def basis(cockpit, action=None, body=None):
    noi = cockpit.get("noi") or {}
    headline = noi.get("headline") or {}
    earnings = (cockpit.get("ebitda") or {}).get("headline") or {}
    # The source reporting policy explicitly leaves expensed D&A in business
    # NOI. An arbitrary manual/custom metric does not establish that operand.
    noi_known = bool((noi.get("definition") or {}).get("includes_depreciation_and_amortization") is True
                     and headline.get("kind") in {"business_operating_income", "tax_derived_noi"}
                     and number(headline.get("annualized_value")) is not None)
    if action is not None and body is not None:
        from app.services.operating_plan_commands import participants
        from app.services.operating_plan_guidance import reconcile_groups

        evidence = cockpit.get("operating_evidence") or {}
        references = {row["key"]: row for row in (evidence.get("guidance") or {}).get("expense_groups", [])}
        groups = {row["key"]: row for row in reconcile_groups(body, evidence)}
        files = {str(row.get("file_id")): row for row in evidence.get("files", [])}
        noi_files = {str(row.get("file_id")) for row in headline.get("source_refs", []) if row.get("file_id")}
        rows = participants(action, body)
        for row in rows:
            reference = references.get(row.get("source_group_key")) or {}
            scope = all(reference.get(key) == body.get(key) for key in ("period_start", "period_end", "currency", "basis"))
            accepted = bool(reference.get("source_file_ids") and all(files.get(str(fid), {}).get("eligible_for_review") for fid in reference["source_file_ids"]))
            same_noi_source = bool(noi_files & {str(fid) for fid in reference.get("source_file_ids", [])})
            if not scope or not accepted or not same_noi_source or reference.get("category") != "depreciation_amortization" or groups.get(row.get("source_group_key"), {}).get("status") != "matches_source":
                noi_known = False
        noi_known = bool(noi_known and rows)
    earnings_known = bool(earnings.get("kind") in {"computed_ebitda", "reported_ebitda", "tax_estimate"}
                          and number(earnings.get("annualized_value")) is not None)
    return {"noi": noi_known, "earnings": earnings_known, "cash": True,
            "issues": {
                "noi": [] if noi_known else ["Confirm that the selected source NOI includes this expensed depreciation and amortization."],
                "earnings": [] if earnings_known else ["Confirm the selected coverage earnings basis excludes depreciation and amortization before showing its earnings effect."],
                "cash": []}}


def effects(cockpit, accounting_gross, implementation, recurring_cost, one_time_cost, fraction, *, action=None, body=None):
    policy = basis(cockpit, action, body)
    result = {"effect_type": "noncash_da", "metric_issues": policy["issues"],
              "accounting_gross_annual_impact": round(accounting_gross, 2) if accounting_gross is not None else None}
    for metric in ("noi", "earnings", "cash"):
        gross = accounting_gross if metric == "noi" else 0.0 if accounting_gross is not None else None
        if not policy[metric]:
            gross = None
        net = gross - recurring_cost if gross is not None and implementation is not None else None
        first = net * fraction - one_time_cost if net is not None and fraction else 0.0 if net is not None and fraction == 0 else None
        result[metric + "_gross_annual_impact"] = round(gross, 2) if gross is not None else None
        result[metric + "_net_annual_impact"] = round(net, 2) if net is not None else None
        result[metric + "_first_year_impact"] = round(first, 2) if first is not None else None
    return result


def metric_value(row, metric, period):
    """Presence matters: an explicitly unknown metric never falls back."""
    key = f"{metric}_{period}_impact"
    fallback = {"gross_annual": "gross_annual_impact", "net_annual": "net_annual_impact", "first_year": "first_year_impact"}[period]
    return row.get(key) if key in row else row.get(fallback)
