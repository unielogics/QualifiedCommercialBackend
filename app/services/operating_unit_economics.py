"""Disclosed annual business units and one conditional volume illustration.

These figures never replace a selected P&L, reviewed NOI or historical EBITDA.
Local expense units (seats, employees, hours) remain independent of business units.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal

from app.schemas.earnings_improvement import cost_amount, expense_missing

FACTORS = {"weekly": 52, "biweekly": 26, "semi_monthly": 24, "monthly": 12, "quarterly": 4, "annual": 1}
OUTSIDE_VOLUME = {"loan_payments", "income_taxes", "owner_draws", "transfers", "depreciation_amortization"}
ZERO = Decimal(0)


def _number(value):
    return Decimal(str(value)) if value is not None else None


def _value(value, places=2):
    return float(round(value, places)) if value is not None else None


def _annual(value, cadence):
    return _number(value) * FACTORS[cadence] if value is not None and cadence in FACTORS else None


def enabled(body):
    annual_pricing = any(row.get("kind") == "price_change" and row.get("price_basis") == "annual_revenue"
        for row in body.get("adjustments", []) if not row.get("excluded"))
    return bool(body.get("unit_label") or body.get("historical_units_annual") is not None
        or body.get("historical_revenue_annual") is not None and not annual_pricing
        or any(row.get("volume_behavior", "unknown") != "unknown" for row in body.get("expenses", []) if not row.get("excluded"))
        or any(row.get("kind") == "unit_volume" for row in body.get("adjustments", []) if not row.get("excluded")))


def _annual_period(body):
    if not body.get("historical_units_period_start") or not body.get("historical_units_period_end"):
        return False
    start = date.fromisoformat(str(body["historical_units_period_start"]))
    end = date.fromisoformat(str(body["historical_units_period_end"]))
    # Includes ordinary/leap fiscal years and a 52-week reporting year.
    return 364 <= (end - start).days + 1 <= 366 and all(
        body.get(key) == body.get("historical_units_" + key) for key in ("period_start", "period_end"))


def _group(rows, key, label):
    recurring = [row for row in rows if row["recurring"]]
    fields = ("current_annual_amount", "current_fixed_annual_amount", "current_variable_annual_amount",
              "proposed_annual_amount")
    totals = {field: sum((_number(row[field]) for row in recurring), ZERO)
        if recurring and all(row[field] is not None for row in recurring) else None for field in fields}
    units, proposed = _number(rows[0]["historical_units_annual"]), _number(rows[0]["proposed_units_annual"])
    return {"key": key, "label": label, "expense_row_ids": [row["id"] for row in rows],
        **{field: _value(value) for field, value in totals.items()},
        "one_time_amount": _value(sum((_number(row["one_time_amount"]) for row in rows if not row["recurring"]), ZERO))
            if any(not row["recurring"] for row in rows) and all(row["one_time_amount"] is not None for row in rows if not row["recurring"]) else None,
        "current_average_cost_per_unit": _value(totals["current_annual_amount"] / units, 6)
            if units and totals["current_annual_amount"] is not None and all(row["current_average_cost_per_unit"] is not None for row in recurring) else None,
        "current_variable_cost_per_unit": _value(totals["current_variable_annual_amount"] / units, 6)
            if units and totals["current_variable_annual_amount"] is not None and all(row["current_variable_cost_per_unit"] is not None for row in recurring) else None,
        "proposed_average_cost_per_unit": _value(totals["proposed_annual_amount"] / proposed, 6)
            if proposed and totals["proposed_annual_amount"] is not None else None,
        "missing": list(dict.fromkeys(path for row in rows for path in row["missing"]))}


def evaluate(body, *, action=None):
    """Pure read model. Input is a validated OperatingPlanBody JSON object."""
    if not enabled(body):
        return None
    actions = [row for row in body.get("adjustments", [])
        if row.get("kind") == "unit_volume" and not row.get("excluded") and row.get("status") != "rejected"]
    action = action or (actions[0] if len(actions) == 1 else None)
    modeling = action is not None
    units, revenue = _number(body.get("historical_units_annual")), _number(body.get("historical_revenue_annual"))
    annual_period = _annual_period(body)
    missing = []
    if not body.get("unit_label"):
        missing.append("unit_label")
    if units is None:
        missing.append("historical_units_annual")
    if not annual_period:
        missing.append("historical_unit_reporting_period")
    if len(actions) > 1:
        missing.extend(f"adjustments.{row['id']}.unit_volume_duplicate" for row in actions)
    proposed_units = proposed_revenue = revenue_change = None
    if modeling:
        if not units:
            missing.append("historical_units_annual.positive")
        if revenue is None:
            missing.append("historical_revenue_annual")
        change = _number(action.get("unit_change_value"))
        prefix = f"adjustments.{action['id']}"
        if change is None:
            missing.append(prefix + ".unit_change_value")
        elif action.get("unit_change_mode") == "percent" and change < -100:
            missing.append(prefix + ".unit_change_value.minimum_percent")
        if action.get("cadence") != "annual":
            missing.append(prefix + ".annual_cadence")
        if units is not None and change is not None:
            proposed_units = units * (1 + change / 100) if action.get("unit_change_mode") == "percent" else units + change
            if proposed_units < 0 or proposed_units > Decimal("1e15"):
                missing.append(prefix + ".nonnegative_proposed_units")
                proposed_units = None
        if not annual_period:
            proposed_units = None
        if units and revenue is not None and proposed_units is not None:
            proposed_revenue = revenue * proposed_units / units
            revenue_change = proposed_revenue - revenue
            if proposed_revenue > Decimal("1e15"):
                missing.append(prefix + ".proposed_revenue_limit")
    active = [row for row in body.get("expenses", []) if not row.get("excluded")]
    if modeling and not active and not body.get("no_operating_expenses_confirmed"):
        missing.append("expenses_or_no_expenses_confirmation")
    rows = []
    variable_delta = ZERO
    for row in active:
        operating = row["category"] not in OUTSIDE_VOLUME
        recurring = row["cadence"] in FACTORS
        amount = _annual(cost_amount(row), row["cadence"])
        behavior = row.get("volume_behavior", "unknown")
        row_missing = [f"expenses.{row['id']}.{key}" for key in expense_missing(row)]
        fixed = variable = proposed_amount = None
        prefix = f"expenses.{row['id']}"
        if not recurring:
            pass  # Preserved one-time disclosure, outside recurring volume math.
        elif amount is None:
            row_missing.append(prefix + ".recurring_annual_amount")
        elif not operating or behavior == "fixed":
            fixed, variable = amount, ZERO
        elif behavior == "variable":
            if row.get("cost_basis") == "per_unit" and row.get("current_fixed_amount") not in (None, 0):
                row_missing.append(prefix + ".volume_behavior.mixed_required")
            else:
                fixed, variable = ZERO, amount
        elif behavior == "mixed":
            fixed = _annual(row.get("current_fixed_amount"), row["cadence"])
            if fixed is None or fixed > amount:
                row_missing.append(prefix + ".current_fixed_amount")
                fixed = None
            else:
                variable = amount - fixed
        else:
            row_missing.append(prefix + ".volume_behavior")
        rate = variable / units if variable is not None and units else None
        if modeling and proposed_units is not None and variable is not None:
            proposed_amount = fixed + rate * proposed_units if rate is not None else None
            if not operating or behavior == "fixed":
                proposed_amount = amount
            if operating and proposed_amount is not None:
                variable_delta += proposed_amount - amount
        if modeling and operating and recurring:
            missing.extend(row_missing)
        rows.append({"id": row["id"], "category": row["category"], "label": row["label"],
            "source_group_key": row.get("source_group_key") or "", "volume_behavior": behavior,
            "operating": operating, "recurring": recurring,
            "one_time_amount": _value(_number(cost_amount(row))) if not recurring else None,
            "scope_note": "One-time disclosure; excluded from recurring volume effects. Model a changed one-time cost separately." if not recurring else "",
            "historical_units_annual": _value(units, 6),
            "proposed_units_annual": _value(proposed_units, 6), "current_annual_amount": _value(amount),
            "current_fixed_annual_amount": _value(fixed), "current_variable_annual_amount": _value(variable),
            "current_average_cost_per_unit": _value(amount / units, 6) if amount is not None and units and annual_period else None,
            "current_variable_cost_per_unit": _value(rate, 6) if annual_period else None,
            "proposed_annual_amount": _value(proposed_amount),
            "proposed_average_cost_per_unit": _value(proposed_amount / proposed_units, 6)
                if proposed_amount is not None and proposed_units else None,
            "missing": row_missing})
    missing = list(dict.fromkeys(missing))
    complete = modeling and not missing
    categories, groups = defaultdict(list), defaultdict(list)
    for row in rows:
        categories[row["category"]].append(row)
        if row["source_group_key"]:
            groups[row["source_group_key"]].append(row)
    return {"status": "incomplete" if missing else "calculated" if modeling else "disclosed",
        "unit_label": body.get("unit_label") or "", "period_start": body.get("period_start"), "period_end": body.get("period_end"),
        "historical_units_period_start": body.get("historical_units_period_start"),
        "historical_units_period_end": body.get("historical_units_period_end"),
        "historical_units_annual": _value(units, 6), "historical_revenue_annual": _value(revenue),
        "current_revenue_per_unit": _value(revenue / units, 6) if revenue is not None and units and annual_period else None,
        "action_id": action["id"] if action else None,
        "proposed_units_annual": _value(proposed_units, 6) if annual_period else None,
        "proposed_revenue_annual": _value(proposed_revenue) if complete else None,
        "revenue_change_annual": _value(revenue_change) if complete else None,
        "variable_cost_change_annual": _value(variable_delta) if complete else None,
        "net_annual_impact": _value(revenue_change - variable_delta) if complete else None,
        "rows": rows,
        "categories": [_group(value, key, key.replace("_", " ").title()) for key, value in categories.items()],
        "expense_groups": [_group(value, key, value[0]["label"]) for key, value in groups.items()],
        "missing": missing,
        "notes": ["Historical units and revenue are disclosed planning inputs, not replacements for verified source earnings.",
            "Average cost per business unit includes fixed costs; it does not make those costs variable.",
            "Variable costs hold their historical rate per business unit. Fixed costs remain unchanged; local row quantities are not scaled again."]}


def public_summary(value):
    """Allowlisted offering figures, without source keys, row IDs or review data."""
    if not isinstance(value, dict):
        return None
    keys = ("status", "unit_label", "period_start", "period_end", "historical_units_period_start", "historical_units_period_end",
        "historical_units_annual", "historical_revenue_annual", "current_revenue_per_unit", "proposed_units_annual",
        "proposed_revenue_annual", "revenue_change_annual", "variable_cost_change_annual", "net_annual_impact",
        "source_revenue_annual", "revenue_reconciled", "notes")
    row_keys = ("category", "label", "volume_behavior", "operating", "recurring", "one_time_amount", "scope_note",
        "current_annual_amount", "current_fixed_annual_amount", "current_variable_annual_amount", "current_average_cost_per_unit",
        "current_variable_cost_per_unit", "proposed_annual_amount", "proposed_average_cost_per_unit")
    return {**{key: deepcopy(value[key]) for key in keys if key in value},
        **{block: [{key: deepcopy(row[key]) for key in row_keys if key in row} for row in value.get(block, [])]
            for block in ("rows", "categories", "expense_groups")}}
