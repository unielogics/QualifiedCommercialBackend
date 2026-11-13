"""Traceable loan relief and cash-cost savings fund uses, never invented sales."""
from decimal import ROUND_HALF_UP, Decimal

from app.services import earnings_improvement as engine
from app.services.financial_metric_review import number


def _d(value):
    value = number(value)
    return Decimal(str(value)) if value is not None else None


def _money(value):
    return float(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)) if value is not None else None


def _retention(rows, remaining):
    amounts = [(_d(row.get("amount_monthly")), row.get("purpose")) for row in rows]
    known = all(amount is not None for amount, _ in amounts)
    reinvested = sum((amount for amount, purpose in amounts if purpose != "reserve"), Decimal(0)) if known else None
    retained = (sum((amount for amount, purpose in amounts if purpose == "reserve"), Decimal(0)) + remaining
                if known and remaining is not None and remaining >= 0 else None)
    return {"funding_retained_monthly": _money(retained), "funding_retained_annual": _money(retained * 12) if retained is not None else None,
            "funding_reinvested_monthly": _money(reinvested), "funding_reinvested_annual": _money(reinvested * 12) if reinvested is not None else None}


def _math_stamp(action, body):
    action_keys = ("id", "kind", "expense_row_id", "expense_category", "source_group_key", "command", "price_basis",
        "cadence", "cost_basis", "unit_label", "current_amount", "proposed_amount", "current_units_per_period", "proposed_units_per_period",
        "current_unit_cost", "proposed_unit_cost", "current_fixed_amount", "proposed_fixed_amount", "units_per_period",
        "processing_volume", "processing_transaction_count", "current_processing_rate_pct", "proposed_processing_rate_pct",
        "current_processing_rate_basis", "proposed_processing_rate_basis", "current_processing_fixed_fee", "proposed_processing_fixed_fee",
        "current_processing_per_transaction_fee", "proposed_processing_per_transaction_fee", "implementation_cost", "implementation_cost_cadence")
    from app.services.operating_plan_commands import participants

    row_keys = ("id", "category", "source_group_key", "source_candidate_key", "source_file_ids", "itemization_parent_id", "current_amount",
                "cadence", "cost_basis", "unit_label", "current_units_per_period", "current_unit_cost", "current_fixed_amount", "excluded")
    numeric = {key for key in action_keys if key.startswith(("current_", "proposed_")) and key not in {"current_processing_rate_basis", "proposed_processing_rate_basis"}}
    numeric |= {"units_per_period", "processing_volume", "processing_transaction_count", "implementation_cost"}
    def fields(row, keys):
        result = {key: row.get(key) for key in keys}
        for key in numeric & result.keys():
            value = _d(result[key])
            result[key] = "0" if value == 0 else str(value.normalize()) if value is not None else None
        if result.get("command"):
            result["command"] = dict(result["command"])
            value = _d(result["command"].get("value"))
            result["command"]["value"] = "0" if value == 0 else str(value.normalize()) if value is not None else None
        return result
    return engine._stamp({"action": fields(action, action_keys),
        "current_rows": sorted((fields(row, row_keys) for row in participants(action, body)), key=lambda row: row["id"]),
        "scope": {key: body.get(key) for key in ("business_name", "period_start", "period_end", "currency", "basis")}})


def evaluate(body, cockpit, evaluation, *, feedback=None):
    bridge = evaluation.get("cash_bridge") or {}
    active = {row["id"]: row for row in body["adjustments"] if not row["excluded"] and row["status"] != "rejected"}
    candidates = [row for row in active.values() if row["kind"] in {"expense_reduction", "expense_group_change", "product_cost_change", "processing_cost_change"}
                  and not (row.get("command") and row["command"]["direction"] == "increase")]
    if candidates and feedback is None:
        from app.services.operating_draft_feedback import evaluate as draft_feedback

        feedback = draft_feedback(body, cockpit, evaluation)
    results = {row["action_id"]: row for row in (feedback or {}).get("actions", [])}
    earnings_ok, noi_ok, _ = engine.comparison_scope(body, cockpit)
    scope_ok = body["currency"] == "USD" and (earnings_ok or noi_ok)
    known, unknown, bound = Decimal(0), [], []
    for action in candidates:
        row = results.get(action["id"]) or {}
        if row.get("effect_type") == "noncash_da":
            continue
        gross = _d(row.get("cash_gross_annual_delta"))
        if gross is not None and gross <= 0:
            continue
        net = _d(row.get("cash_net_annual_delta"))
        issues = [*row.get("math_missing", []), *row.get("fix_fields", [])]
        if not scope_ok:
            issues.append("operating_source_comparability")
        if row.get("source_issues"):
            issues.extend(row.get("fix_fields") or [f"adjustments.{action['id']}.source_reconciliation"])
        if net is None:
            issues.append(f"adjustments.{action['id']}.implementation_cost")
        if row.get("math_status") != "calculated" or issues:
            unknown.extend(issues or [f"adjustments.{action['id']}.current_proposed_math"])
        else:
            known += max(Decimal(0), net)
        bound.append({"id": action["id"], "math": _math_stamp(action, body), "net_cash_annual": _money(net)})
    uses_savings = bool(known > 0 or unknown)
    if not uses_savings:
        return {"funding_uses_operating_savings": False, "funding_available": bridge.get("available", False),
                "funding_required": bridge.get("required", False), "funding_complete": bridge.get("complete", False),
                "funding_stale": bridge.get("stale", False), "funding_source_fingerprint": bridge.get("source_fingerprint"),
                "operating_savings_monthly": 0.0, "operating_savings_annual": 0.0,
                "operating_known_savings_monthly": 0.0, "operating_known_savings_annual": 0.0,
                **{new: bridge.get(old) for new, old in (("funding_capacity_monthly", "recurring_cash_released_monthly"),
                    ("funding_capacity_annual", "recurring_cash_released_annual"), ("funding_known_capacity_monthly", "recurring_cash_released_monthly"),
                    ("funding_known_capacity_annual", "recurring_cash_released_annual"), ("funding_allocated_monthly", "allocated_monthly"),
                    ("funding_allocated_annual", "allocated_annual"), ("funding_unallocated_monthly", "unallocated_monthly"),
                    ("funding_unallocated_annual", "unallocated_annual"))},
                "funding_missing": list(bridge.get("missing") or []), "funding_fix_fields": list(bridge.get("loan_fix_fields") or []),
                **_retention(body["released_cash_flow"]["allocations"], _d(bridge.get("unallocated_monthly")))}
    saved = bool((cockpit.get("plan") or {}).get("record", {}).get("updated_at") and (cockpit.get("plan") or {}).get("evaluation", {}).get("active"))
    loan = _d(bridge.get("net_loan_cash_change_monthly")) if saved else Decimal(0)
    loan_known = (not saved or bridge.get("loan_status") == "calculated") and loan is not None
    monthly_known = known / 12
    lower = max(Decimal(0), loan + monthly_known).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) if loan_known else None
    capacity = lower if not unknown and scope_ok else None
    source_stamp = engine._stamp({"policy": "cash-cost-funding-v1", "loan": bridge.get("source_fingerprint"),
                                  "no_saved_loan": not saved, "context": engine.context_stamp(cockpit),
                                  "savings": sorted(bound, key=lambda row: row["id"])})
    if known == 0 and loan_known and bridge.get("source_fingerprint"):
        # No validated new money exists yet. Keep the actual existing source
        # acknowledgeable while the speculative proposal remains NeedsInputs.
        source_stamp = bridge["source_fingerprint"]
    allocation = body["released_cash_flow"]
    rows = allocation["allocations"]
    amounts = [_d(row.get("amount_monthly")) for row in rows]
    allocated = sum(amounts, Decimal(0)) if all(value is not None for value in amounts) else None
    remaining = capacity - allocated if capacity is not None and allocated is not None else None
    stale = bool(rows and allocation.get("source_fingerprint") != source_stamp)
    missing = list(dict.fromkeys(unknown))
    if not loan_known:
        missing.extend(bridge.get("loan_missing") or ["coverage_plan.source_reconciliation"])
    if rows and (stale or not allocation.get("source_fingerprint")):
        missing.append("released_cash_flow.source_fingerprint.stale" if stale else "released_cash_flow.source_fingerprint")
    missing.extend(issue for row in bridge.get("allocations") or [] for issue in row.get("issues") or [])
    if rows and capacity is not None and (remaining is None or remaining < 0):
        missing.append("released_cash_flow.allocations.capacity_exceeded")
    missing = list(dict.fromkeys(missing))
    return {"funding_uses_operating_savings": True, "funding_available": capacity is not None,
            "funding_required": bool(rows), "funding_complete": not rows or capacity is not None and not missing,
            "funding_stale": stale, "funding_source_fingerprint": source_stamp, "funding_currency": "USD" if scope_ok else None,
            "operating_savings_monthly": _money(monthly_known) if not unknown and scope_ok else None,
            "operating_savings_annual": _money(known) if not unknown and scope_ok else None,
            "operating_known_savings_monthly": _money(monthly_known), "operating_known_savings_annual": _money(known),
            "funding_capacity_monthly": _money(capacity), "funding_capacity_annual": _money(capacity * 12) if capacity is not None else None,
            "funding_known_capacity_monthly": _money(lower), "funding_known_capacity_annual": _money(lower * 12) if lower is not None else None,
            "funding_allocated_monthly": _money(allocated), "funding_allocated_annual": _money(allocated * 12) if allocated is not None else None,
            "funding_unallocated_monthly": _money(remaining), "funding_unallocated_annual": _money(remaining * 12) if remaining is not None else None,
            "funding_missing": missing, "funding_fix_fields": list(dict.fromkeys([*unknown, *bridge.get("loan_fix_fields", [])] if not loan_known else unknown)),
            **_retention(rows, remaining),
            "funding_note": "Unassigned positive funding is kept as cash; reinvestment is optional. Funding is saved net loan relief plus traceable positive recurring cash-cost savings after recurring setup costs, before linked reinvestment uses. D&A and projected new sales or marketing returns are excluded. Assignments do not change NOI or deduct the same spending twice."}
