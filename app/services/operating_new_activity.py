"""A disclosed new zero-baseline activity is not an extracted historical cost."""


def explicit_new_zero(action, row):
    return bool(row and not row.get("excluded") and action.get("kind") in {"expense_increase", "marketing_investment"}
                and action.get("cost_basis", "total") == row.get("cost_basis", "total") == "total"
                and row.get("current_amount") == action.get("current_amount") == 0
                and row.get("category") not in {"loan_payments", "income_taxes", "owner_draws", "transfers", "depreciation_amortization"}
                and not any(row.get(key) for key in ("source_group_key", "source_candidate_key", "itemization_parent_id", "source_file_ids")))


def baseline_context(body, cockpit, files):
    from app.services.earnings_improvement import comparison_scope

    earnings_ok, noi_ok, _ = comparison_scope(body, cockpit)
    choices = [((cockpit.get("noi") or {}).get("headline") or {}, noi_ok),
               ((cockpit.get("ebitda") or {}).get("headline") or {}, earnings_ok)]
    for source, comparable in choices:
        ids = {str(ref["file_id"]) for ref in source.get("source_refs") or [] if ref.get("file_id")}
        if comparable and ids and all(files.get(fid, {}).get("eligible_for_review") for fid in ids):
            return ids
    return set()
