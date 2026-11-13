"""Pure, noncompounding commands against disclosed current costs and prices.

The returned body is an ephemeral calculation input, never a replacement for
the saved command intent or verified historical financial statements.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.schemas.earnings_improvement import cost_amount

FACTORS = {"weekly": 52, "biweekly": 26, "semi_monthly": 24, "monthly": 12, "quarterly": 4, "annual": 1}
OUTSIDE = {"loan_payments", "income_taxes", "owner_draws", "transfers"}
CENT = Decimal(".01")
ZERO = Decimal(0)


def _d(value):
    return Decimal(str(value)) if value is not None else None


def _money(value):
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP)) if value is not None else None


def _annual(row):
    value = _d(cost_amount(row))
    if row.get("cost_basis") == "per_unit" and row.get("current_amount") is not None and value is not None and abs(value - _d(row["current_amount"])) > CENT:
        return None
    return value * FACTORS[row["cadence"]] if value is not None and row.get("cadence") in FACTORS else None


def _change(current, command):
    value = _d(command.get("value"))
    if current is None or value is None:
        return None
    delta = current * value / 100 if command["mode"] == "percent" else value
    return current + delta if command["direction"] == "increase" else current - delta


def group_rows(action, body):
    """Exact scope, including unscoped manual rows, excluding references/history."""
    return sorted([row for row in body.get("expenses", []) if not row.get("excluded")
        and row["category"] == action.get("expense_category")
        and row.get("source_group_key", "") == action.get("source_group_key", "")], key=lambda row: str(row["id"]))


def participants(action, body):
    if action.get("kind") == "expense_group_change":
        return group_rows(action, body)
    return [row for row in body.get("expenses", []) if row["id"] == action.get("expense_row_id")]


def itemization(body, cockpit=None):
    result = []
    evidence = (cockpit or {}).get("operating_evidence") or {}
    references = {row["key"]: row for row in (evidence.get("guidance") or {}).get("expense_groups", [])}
    files = {row.get("file_id"): row for row in evidence.get("files", [])}
    for parent in body.get("expenses", []):
        lineage = [row for row in body["expenses"] if row.get("itemization_parent_id") == parent["id"]]
        if not lineage or not parent["excluded"]:
            continue
        children = sorted([row for row in lineage if not row["excluded"]], key=lambda row: row["id"])
        source = references.get(parent["source_group_key"])
        recorded = _annual(parent)
        reference = _d(source.get("baseline_annual_amount")) if source else recorded
        same_scope = bool(source and source.get("category") == parent["category"] and all(source.get(key) == body.get(key) for key in ("period_start", "period_end", "currency", "basis")))
        accepted = bool(same_scope and source.get("source_file_ids") and all(files.get(fid, {}).get("eligible_for_review") for fid in source["source_file_ids"]))
        amounts = [_annual(row) for row in children]
        total = sum(amounts, ZERO) if all(value is not None for value in amounts) else None
        remainder = reference - total if reference is not None and total is not None else None
        result.append({"parent_expense_row_id": parent["id"], "expense_category": parent["category"],
            "source_group_key": parent["source_group_key"], "reference_annual_amount": _money(reference),
            "recorded_parent_annual_amount": _money(recorded),
            "reference_authority": "accepted_current_source" if accepted else "unconfirmed_source" if source else "disclosed_parent_only",
            "source_comparable": bool(accepted and remainder is not None and abs(remainder) < CENT),
            "year": source.get("year") if source else None, "source_label": source.get("source_label") if source else None,
            "source_fact_keys": deepcopy(source.get("source_fact_keys") or []) if source else [],
            "children_annual_amount": _money(total), "remainder_annual_amount": _money(remainder),
            "status": "unknown" if remainder is None else "balanced" if abs(remainder) < CENT else "overallocated" if remainder < 0 else "unallocated",
            "child_expense_row_ids": [row["id"] for row in children]})
    return result


def itemization_missing(body, cockpit=None):
    """A documented correction permits handoff, never source confirmation."""
    corrections = {row["source_group_key"] for row in body.get("historical_corrections") or []
        if len(row.get("reason", "").strip()) >= 3}
    return [f"expenses.{row['parent_expense_row_id']}.itemization.{row['status']}" for row in itemization(body, cockpit)
        if row["status"] != "balanced" and row["source_group_key"] not in corrections]


def correction_missing(body):
    """Incomplete optional notes may autosave, but cannot finalize a handoff."""
    missing = []
    for row in body.get("historical_corrections") or []:
        prefix = f"historical_corrections.{row['source_group_key']}"
        if len(row.get("reason", "").strip()) < 3:
            missing.append(prefix + ".reason")
    return missing


def _allocate(rows, weights, proposed):
    """Allocate one rounded annual target; penny remainder follows stable row ID."""
    target = proposed.quantize(CENT, rounding=ROUND_HALF_UP)
    total = sum(weights, ZERO)
    shares = [(target * value / total).quantize(CENT, rounding=ROUND_DOWN) for value in weights]
    remainder = int((target - sum(shares, ZERO)) / CENT)
    eligible = [index for index, weight in enumerate(weights) if weight > 0]
    for index in range(remainder):
        shares[eligible[index % len(eligible)]] += CENT
    return [{"expense_row_id": row["id"], "current_annual_cost": _money(current),
        "proposed_annual_cost": _money(after), "gross_annual_effect": _money(current - after)}
        for row, current, after in zip(rows, weights, shares, strict=True)]


def resolve(body, cockpit=None):
    """Resolve validated JSON without modifying caller-owned inputs or reviews."""
    resolved = deepcopy(body)
    expenses = {row["id"]: row for row in body.get("expenses", [])}
    commands = []
    for action in resolved.get("adjustments", []):
        command = action.get("command")
        if command is None:
            continue
        target, kind = command["target"], action["kind"]
        linked = expenses.get(action.get("expense_row_id"))
        rows = participants(action, body)
        issues, assumptions, allocations = [], [], []
        current = proposed = annual = after_annual = gross = None
        if command.get("value") is None:
            issues.append("Enter a command value; a cleared amount is unknown, not zero.")
        if action["excluded"] or action["status"] == "rejected":
            issues.append("This command is removed or rejected; restore it explicitly before modeling it.")
        if kind == "expense_group_change":
            if target != "total_cost" or command.get("allocation") != "proportional":
                issues.append("Choose Apply proportionally to current annual leaf costs for a group command.")
            if not action.get("expense_category") or not rows:
                issues.append("Choose a group containing active current expense items.")
            weights = [_annual(row) for row in rows]
            if any(row["category"] in OUTSIDE for row in rows):
                issues.append("A group command can change only recurring operating costs.")
            if any(value is None for value in weights):
                issues.append("Every participating current item needs a known recurring annual cost; unknown allocation weights cannot be treated as zero.")
            elif rows:
                weights = [value.quantize(CENT, rounding=ROUND_HALF_UP) for value in weights]
                current = annual = sum(weights, ZERO)
                if current <= 0:
                    issues.append("A zero-total group has no proportional weights; enter individual item targets instead.")
                proposed = _change(current, command)
                if proposed is not None and 0 <= proposed <= Decimal("1e15") and current > 0:
                    allocations = _allocate(rows, weights, proposed)
                    after_annual = sum((_d(row["proposed_annual_cost"]) for row in allocations), ZERO)
                    gross = annual - after_annual
                action.update(cadence="annual", cost_basis="total", current_amount=_money(current), proposed_amount=_money(after_annual))
            assumptions.append("Explicit proportional annual budget targets; quantities, unit rates and fixed charges are unchanged assumptions, not invented drivers.")
        elif target == "annual_price_revenue":
            if kind != "price_change" or action.get("price_basis") != "annual_revenue":
                issues.append("Use the annual-revenue price basis for an annual pricing command.")
            current = _d(body.get("historical_revenue_annual"))
            if current is None:
                issues.append("Confirm the same-period historical annual revenue before applying a pricing change.")
            elif current <= 0 and command.get("value") not in (None, 0):
                issues.append("A pricing increase needs positive existing revenue; new revenue requires its own explicit sales-volume and contribution assumptions.")
            start, end = body.get("period_start"), body.get("period_end")
            if not start or not end or not 364 <= (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 <= 366:
                issues.append("An annual pricing command requires the confirmed annual reporting period.")
            if cockpit is not None:
                from app.services.earnings_improvement import _period_matches

                earnings = cockpit.get("ebitda") or {}
                head = earnings.get("headline") or {}
                source = (earnings.get("potential") or {}).get("baseline") or {}
                source_revenue = _d(source.get("revenue"))
                from app.services.document_associations import identity_matches

                expected = ((cockpit.get("operating_evidence") or {}).get("business_name") or next((row.get("entity") for row in head.get("source_refs", []) if row.get("entity")), None))
                if not (_period_matches(body, head) and head.get("currency") == body.get("currency") and head.get("basis") == body.get("basis")
                        and not (earnings.get("source_selection") or {}).get("stale") and source_revenue is not None
                        and current is not None and abs(source_revenue - current) <= CENT and expected and identity_matches(expected, body.get("business_name"))):
                    issues.append("Reconcile annual pricing revenue with the selected same-period, same-currency and same-basis financial source.")
            proposed = _change(current, command)
            annual, after_annual = current, proposed
            gross = proposed - current if proposed is not None and current is not None else None
            action.update(cadence="annual", current_amount=_money(current), proposed_amount=_money(proposed))
            assumptions.append("Selling prices change across the stated revenue scope; sales volume, product mix, collection and variable-cost dollars are held constant. Demand response is not forecast.")
        elif target == "unit_price" and kind == "price_change" and action.get("price_basis", "per_unit") == "per_unit":
            current = _d(action.get("current_unit_price"))
            proposed = _change(current, command)
            action["proposed_unit_price"] = float(proposed) if proposed is not None else None
            volume = _d(action.get("units_per_period"))
            if current is None or volume is None or action.get("cadence") not in FACTORS:
                issues.append("Confirm current unit price, fixed unit volume and its recurring cadence.")
            elif proposed is not None:
                gross = (proposed - current) * volume * FACTORS[action["cadence"]]
            assumptions.append("Unit volume is held constant; validate demand and customer retention.")
        elif kind in {"expense_reduction", "expense_increase"} and linked is not None:
            if linked["excluded"] or linked["category"] in OUTSIDE or linked["cadence"] not in FACTORS:
                issues.append("Choose an active recurring operating expense item.")
            action.update(cadence=linked["cadence"], current_amount=cost_amount(linked))
            annual = _annual(linked)
            if target == "total_cost":
                current = _d(cost_amount(linked))
                proposed = _change(current, command)
                action.update(cost_basis="total", proposed_amount=_money(proposed))
                assumptions.append("This is a total budget target; it does not assert new quantities or unit rates.")
            elif target in {"unit_cost", "unit_quantity"} and linked["cost_basis"] == "per_unit":
                field = "unit_cost" if target == "unit_cost" else "units_per_period"
                current = _d(linked.get("current_" + field))
                proposed = _change(current, command)
                action.update(cost_basis="per_unit", unit_label=linked["unit_label"])
                for key in ("units_per_period", "unit_cost", "fixed_amount"):
                    action["current_" + key] = linked.get("current_" + key)
                    action["proposed_" + key] = linked.get("current_" + key)
                action["proposed_" + field] = float(proposed) if proposed is not None else None
                action["proposed_amount"] = None
            else:
                issues.append("Choose a total budget command or a matching disclosed unit quantity/rate.")
            after = _d(cost_amount(action, "proposed"))
            after_annual = after * FACTORS[linked["cadence"]] if after is not None and linked["cadence"] in FACTORS else None
            gross = annual - after_annual if annual is not None and after_annual is not None else None
            if gross is not None:
                action["kind"] = "expense_reduction" if gross >= 0 else "expense_increase"
        else:
            issues.append("This command target does not match its action or current expense item.")
        if current is None:
            issues.append("The command's current baseline is unknown.")
        if proposed is not None and (proposed < 0 or proposed > Decimal("1e15")):
            issues.append("The command must produce a nonnegative target within the supported amount limit.")
        for field in ("current_amount", "proposed_amount", "proposed_unit_price", "proposed_unit_cost", "proposed_units_per_period"):
            value = _d(action.get(field))
            if value is not None and not ZERO <= value <= Decimal("1e15"):
                action[field] = None
                issues.append("A resolved amount exceeds the supported nonnegative input range.")
        issues = list(dict.fromkeys(issues))
        commands.append({"action_id": action["id"], "expense_row_id": action.get("expense_row_id"),
            "expense_category": linked["category"] if linked else action.get("expense_category"),
            "source_group_key": linked["source_group_key"] if linked else action.get("source_group_key", ""),
            "target": target, "status": "incomplete" if issues else "calculated", "command": deepcopy(command),
            "current_value": float(current) if current is not None else None,
            "proposed_value": float(proposed) if proposed is not None and not issues else None,
            "current_annual_cost": _money(annual) if target != "annual_price_revenue" else None,
            "proposed_annual_cost": _money(after_annual) if not issues and target != "annual_price_revenue" else None,
            **({"current_annual_revenue": _money(annual), "proposed_annual_revenue": _money(after_annual) if not issues else None} if target == "annual_price_revenue" else {}),
            "gross_annual_effect": _money(gross) if not issues else None,
            "participant_expense_row_ids": [row["id"] for row in rows],
            "allocations": allocations if not issues else [], "issues": issues, "assumptions": assumptions})
    active = [row for row in resolved.get("adjustments", []) if not row["excluded"] and row["status"] != "rejected"]
    for result in commands:
        action = next(row for row in active if row["id"] == result["action_id"]) if any(row["id"] == result["action_id"] for row in active) else None
        if action is None or action["kind"] != "expense_group_change":
            continue
        ids = set(result["participant_expense_row_ids"])
        overlaps = [row["id"] for row in active if row["id"] != action["id"] and (
            row.get("expense_row_id") in ids or row["kind"] == "expense_group_change" and ids & {item["id"] for item in group_rows(row, body)})]
        if overlaps:
            result["status"] = "incomplete"
            result["issues"].append("Group command overlaps item/group actions: " + ", ".join(overlaps))
            result.update(proposed_value=None, proposed_annual_cost=None, gross_annual_effect=None, allocations=[])
    sources = {row["key"]: row for row in (((cockpit or {}).get("operating_evidence") or {}).get("guidance") or {}).get("expense_groups", [])}
    corrections = [{**deepcopy(row), "status": "unresolved_preparer_review", "year": sources.get(row["source_group_key"], {}).get("year"),
        "source_label": sources.get(row["source_group_key"], {}).get("source_label"),
        "source_fact_keys": deepcopy(sources.get(row["source_group_key"], {}).get("source_fact_keys") or []),
        "source_annual_amount": sources.get(row["source_group_key"], {}).get("baseline_annual_amount")}
        for row in body.get("historical_corrections") or []]
    return {"body": resolved, "commands": commands, "itemization": itemization(body, cockpit),
        "historical_corrections": corrections}
