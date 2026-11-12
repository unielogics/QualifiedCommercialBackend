"""Arithmetic-only linked expense comparisons, excluded from saved source facts."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from app.schemas.earnings_improvement import action_missing, cost_amount, validate_body
from app.services import earnings_improvement as engine

POLICY_VERSION = "operating-expense-changes-v1"
ORDINARY = {"expense_reduction", "expense_increase"}
SCOPE = "linked_expense_changes_only"
NOTE = "Ordinary linked expense changes only, before implementation costs, timing and other activities. This arithmetic illustration is not the complete or staff-reviewed projection."


def _sum(rows, field):
    return engine._money(sum(row[field] for row in rows)) if rows and all(row[field] is not None for row in rows) else None


def _groups(rows, *, by_source=False):
    groups = defaultdict(list)
    for row in rows:
        key = (row["expense_category"], row["source_group_key"] if by_source else "")
        groups[key].append(row)
    return [{"key": key[1] if by_source else key[0], "label": key[0].replace("_", " ").title() + (" · Additional items" if by_source and not key[1] else ""),
        "expense_category": key[0], "source_group_key": key[1], "expense_row_ids": [row["expense_row_id"] for row in items],
        "current_annual_cost": _sum(items, "current_annual_cost"), "proposed_annual_cost": _sum(items, "proposed_annual_cost"),
        "gross_annual_effect": _sum(items, "gross_annual_effect"),
        "known_gross_annual_subtotal": engine._money(sum(row["gross_annual_effect"] for row in items if row["gross_annual_effect"] is not None)),
        "complete": all(row["status"] in {"calculated", "unchanged"} for row in items),
        "status": "calculated" if all(row["status"] in {"calculated", "unchanged"} for row in items) else "incomplete",
        "missing_math": list(dict.fromkeys(path for row in items for path in row["missing_math"]))}
        for key, items in groups.items()]


def evaluate(body, action_results=None, baseline=None, *, earnings_comparable=False, noi_comparable=False,
             refinance_annual_debt_service=None, new_operating_annual=None, source_groups=None):
    """No writes, no assumed setup cost/date, and no alternate eligibility gate."""
    body = validate_body(body)
    baseline = baseline or {}
    results = {row["id"]: row for row in action_results or []}
    active = [row for row in body["adjustments"] if not row["excluded"] and row["status"] != "rejected"]
    expenses = {row["id"]: row for row in body["expenses"] if not row["excluded"]}
    linked = defaultdict(list)
    for action in active:
        if action.get("expense_row_id") in expenses:
            linked[action["expense_row_id"]].append(action)
    conflicts = engine._conflicts(active, expenses)
    rows, used = [], set()
    for expense in expenses.values():
        attached = linked[expense["id"]]
        action = attached[0] if len(attached) == 1 and attached[0]["kind"] in ORDINARY else None
        current = cost_amount(expense)
        annual = engine._annual(current, expense["cadence"])
        outside = expense["category"] in engine.NON_OPERATING | engine.NON_CASH
        nonrecurring = expense["cadence"] == "one_time"
        status = "unchanged"
        proposed, proposed_annual = current, annual
        gross = 0 if annual is not None else None
        net = first_year = None
        missing, issues = [], []
        if current is None or engine._cost_issues(expense):
            missing.append(f"expenses.{expense['id']}.current_cost")
            issues.extend(engine._cost_issues(expense))
            status = "incomplete"
        if not body["currency"]:
            missing.append("currency")
            status = "incomplete"
        if attached:
            proposed = proposed_annual = gross = None
            if len(attached) > 1:
                if any(conflicts.get(row["id"]) for row in attached) or any(row["kind"] in ORDINARY for row in attached):
                    status = "conflict"
                    issues.append(f"Expense row {expense['id']} has multiple actions: {', '.join(row['id'] for row in attached)}. Choose the intended treatment before using one proposed cost.")
                else:
                    status = "specialized"
                    issues.append("These specialized activities retain their separate canonical calculations; they are not one ordinary expense replacement.")
            elif action is None:
                status = "specialized"
                issues.append(f"Action {attached[0]['id']} uses a specialized model; use its canonical action result rather than treating it as an ordinary cost replacement.")
            else:
                prefix = f"adjustments.{action['id']}"
                used.add(action["id"])
                before, proposed = cost_amount(action), cost_amount(action, "proposed")
                proposed_annual = engine._annual(proposed, action["cadence"])
                for side, amount in (("current", before), ("proposed", proposed)):
                    if amount is None:
                        missing.append(prefix + "." + side + "_cost")
                    problems = engine._cost_issues(action, side)
                    if problems:
                        missing.append(prefix + "." + side + "_cost_reconciliation")
                        issues.extend(problems)
                current_annual = engine._annual(before, action["cadence"])
                if annual is None or current_annual is None or not engine._near(annual, current_annual):
                    missing.append(prefix + ".current_source_reconciliation")
                    issues.append(f"Reconcile action {action['id']}'s current cost and frequency to expense row {expense['id']}.")
                if action["cost_basis"] == expense["cost_basis"] == "per_unit":
                    components = (expense["unit_label"].strip().casefold() == action["unit_label"].strip().casefold(),
                        engine._near(expense["current_unit_cost"], action["current_unit_cost"]),
                        engine._near(engine._annual(expense["current_units_per_period"], expense["cadence"]), engine._annual(action["current_units_per_period"], action["cadence"])),
                        engine._near(engine._annual(expense["current_fixed_amount"], expense["cadence"]), engine._annual(action["current_fixed_amount"], action["cadence"])))
                    if not all(components):
                        missing.append(prefix + ".current_unit_reconciliation")
                        issues.append(f"Current unit definition, quantity, rate and fixed cost must match expense row {expense['id']}.")
                if current_annual is not None and proposed_annual is not None:
                    gross = engine._money(current_annual - proposed_annual)
                    if action["kind"] == "expense_reduction" and gross < 0 or action["kind"] == "expense_increase" and gross > 0:
                        missing.append(prefix + ".direction")
                if conflicts.get(action["id"]):
                    issues.extend(conflicts[action["id"]])
                    status = "conflict"
                else:
                    status = "incomplete" if missing else "calculated"
                if status == "calculated":
                    setup = action.get("implementation_cost")
                    if setup is not None:
                        setup_factor = {"one_time": 0, "monthly": 12, "annual": 1}[action["implementation_cost_cadence"]]
                        net = engine._money(gross - setup * setup_factor)
                        if body.get("projection_start") and action.get("effective_date"):
                            fraction = engine._active_days(date.fromisoformat(action["effective_date"]), date.fromisoformat(body["projection_start"]))
                            first_year = engine._money(net * fraction - (setup if action["implementation_cost_cadence"] == "one_time" else 0)) if fraction else 0
                else:
                    gross = None
        if outside or nonrecurring:
            status = "outside_operating" if outside else "nonrecurring"
            gross = net = first_year = None
            if attached:
                issues.append("This row is outside recurring operating-cost changes; use the appropriate separate disclosure or one-time model.")
        result = results.get(action["id"], {}) if action else {}
        rows.append({"expense_row_id": expense["id"], "expense_category": expense["category"], "source_group_key": expense["source_group_key"],
            "label": expense["label"], "cadence": expense["cadence"], "cost_basis": expense["cost_basis"],
            "proposed_cadence": action["cadence"] if action else None if attached else expense["cadence"],
            "action_ids": [row["id"] for row in attached], "action_id": action["id"] if action else None,
            "status": status, "current_period_cost": engine._money(current), "current_annual_cost": engine._money(annual),
            "proposed_period_cost": engine._money(proposed), "proposed_annual_cost": engine._money(proposed_annual),
            "gross_annual_effect": gross, "net_annual_effect": net, "first_year_effect": first_year,
            "review_state": result.get("review_state", "proposed") if attached else "disclosure",
            "included_in_proposed_projection": bool(result.get("proposed_math_valid")),
            "included_in_approved_projection": bool(result.get("included_in_approved_projection")),
            "missing_math": list(dict.fromkeys(missing)), "completion_missing": action_missing(action) if action else [],
            "issues": list(dict.fromkeys(issues))})
    changed = [row for row in rows if row["action_ids"]]
    unlinked = [row for row in active if row["kind"] in ORDINARY and row["id"] not in used]
    complete = bool(changed) and not unlinked and all(row["status"] in {"calculated", "unchanged"} for row in rows
        if row["action_ids"] or row["status"] not in {"outside_operating", "nonrecurring"})
    subtotal = engine._money(sum(row["gross_annual_effect"] for row in changed if row["gross_annual_effect"] is not None))
    gross = subtotal if complete else None
    references = {row["key"]: row for row in source_groups or []}
    source_issues = []
    bound = [row for row in changed if row["action_id"] and row["source_group_key"]]
    for row in bound:
        reference = references.get(row["source_group_key"])
        if reference is None or reference.get("status") != "matches_source":
            status = reference.get("status") if reference else "source_missing"
            source_issues.append(f"Reconcile current source group for expense row {row['expense_row_id']} ({reference.get('label') if reference else row['label']}; {status}) before adding this cost change to source earnings or NOI.")
    source_comparable = bool(not source_issues and (earnings_comparable or noi_comparable)) if bound else None
    earnings, noi = baseline.get("earnings"), baseline.get("noi")
    after = earnings + gross if complete and not source_issues and earnings_comparable and earnings is not None else None
    after_noi = noi + gross if complete and not source_issues and noi_comparable and noi is not None else None
    debt = baseline.get("annual_debt_service")
    refi = after - new_operating_annual if after is not None and new_operating_annual is not None else None
    return {"policy_version": POLICY_VERSION, "scope": SCOPE, "notes": [NOTE], "rows": rows,
        "source_comparable": source_comparable, "source_reconciliation_issues": source_issues,
        "categories": _groups(rows), "source_groups": _groups(rows, by_source=True),
        "summary": {"changed_count": len(changed), "complete": complete, "gross_annual_effect": gross,
            "known_gross_annual_subtotal": subtotal if changed else None,
            "excluded_action_ids": [row["id"] for row in active if row["id"] not in used],
            "issues": [f"Action {row['id']} needs one current expense link or an unambiguous ordinary cost treatment." for row in unlinked]},
        "gross_projection": {"scope": SCOPE, "status": "illustration" if after is not None or after_noi is not None else "unavailable",
            "earnings": engine._money(after), "noi": engine._money(after_noi),
            "dscr_current": round(after / debt, 2) if after is not None and debt is not None and debt > 0 else None,
            "dscr_with_refinance": round(refi / refinance_annual_debt_service, 2)
                if refi is not None and refinance_annual_debt_service is not None and refinance_annual_debt_service > 0 else None}}
