"""Audited calculation recipes over immutable, source-bound financial operands.

A recipe is a reviewed underwriting calculation, not an edit to a PDF, a
reported EBITDA fact, a debt schedule, or the reporting period. Source changes
make the recipe stale instead of carrying an old assumption onto new evidence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException

from app.enums import Role
from app.services.financial_metric_review import number, stamp


def _money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _definition(calculation, review):
    key = calculation["key"]
    inputs = {row["key"]: row for row in review["inputs"]}
    is_dscr = key.startswith("dscr.")
    source_keys = [name for name in calculation.get("input_keys", []) if name in inputs and inputs[name]["unit"] == "currency"]
    if key == "dscr.profit_and_loss" or key == "profit_and_loss":
        fields = [f"profit_and_loss.{name}" for name in ("net_income", "interest", "income_taxes", "depreciation_and_amortization")]
        reported = "profit_and_loss.reported_ebitda"
        selected_reported = reported in source_keys
        keys = [name for name in [reported, *fields] if name in inputs]
        defaults = [reported] if selected_reported else fields
        anchor = "profit_and_loss.net_income"
    elif key == "dscr.bank_cash_flow":
        keys, defaults, anchor = ["bank.avg_net", "debt.monthly"], ["bank.avg_net", "debt.monthly"], "bank.avg_net"
    elif key == "dscr.tax_return" or calculation.get("kind") == "tax_estimate":
        keys = [name for name in source_keys if not name.startswith("debt.")]
        anchor = next((name for name in keys if name.endswith(".income")), None)
        defaults = [name for name in keys if inputs[name]["value"] is not None or name == anchor]
    else:
        return None
    keys = [name for name in keys if name in inputs]
    factor = number(calculation.get("annualization_factor")) if is_dscr else 1.0
    if key == "dscr.tax_return":
        factor = 1.0
    elif key == "dscr.bank_cash_flow":
        factor = 12.0
    components = [{"input_key": name, "label": inputs[name]["label"], "value": inputs[name]["value"],
                   "coefficient": 1, "contribution": inputs[name]["value"],
                   "included": name in defaults, "required": name == anchor,
                   "role": "anchor" if name == anchor else "addend", "source_refs": inputs[name]["source_refs"]}
                  for name in keys]
    # A source-reported value and its reconciliation are alternative recipes.
    if any(name.endswith(".reported_ebitda") for name in defaults):
        for component in components:
            component["required"] = False
    definition = {"components": components, "annualization_factor": factor, "kind": "dscr" if is_dscr else "earnings",
                  "unit": "ratio" if is_dscr else "currency", "period": calculation.get("period"),
                  "base_value": calculation.get("value"), "required_anchor": anchor,
                  "denominator": {"input_key": "debt.annual", "label": "Annual debt service", "value": calculation.get("denominator"), "editable": False} if is_dscr else None}
    definition["source_stamp"] = stamp({"calculation": key, "period": definition["period"], "factor": factor,
        "inputs": [{"key": name, "source_stamp": inputs[name]["source_stamp"], "value": inputs[name]["value"]} for name in keys],
        "denominator": definition["denominator"], "source_refs": calculation.get("source_refs", [])})
    definition["can_edit"] = bool(components and factor is not None and calculation.get("currency_status") != "mismatch"
                                   and any(inputs[name].get("editable") for name in keys))
    return definition


def _calculate(recipe, components, adjustments):
    chosen = {row["input_key"]: row["included"] for row in components}
    values = [row["contribution"] for row in recipe["components"] if chosen.get(row["input_key"], False)]
    subtotal = _money(sum((Decimal(str(value)) for value in values), Decimal(0))
                      + sum((Decimal(str(_money(row["amount"]))) for row in adjustments), Decimal(0))) if values and all(value is not None for value in values) else None
    numerator = _money(Decimal(str(subtotal)) * Decimal(str(recipe["annualization_factor"]))) if subtotal is not None and recipe["annualization_factor"] is not None else None
    denominator = (recipe.get("denominator") or {}).get("value")
    value = round(numerator / denominator, 2) if recipe["unit"] == "ratio" and numerator is not None and denominator is not None and denominator > 0 else numerator if recipe["unit"] != "ratio" else None
    return subtotal, numerator, value


def attach_and_apply(review, dscr, state):
    """Attach editable recipes and apply current DSCR recipes before plan math."""
    state = state or {}
    latest = {event["calculation_key"]: event for event in state.get("events", []) if event.get("calculation_key")}
    stale = []
    for calculation in review["calculations"]:
        recipe = _definition(calculation, review)
        if recipe is None:
            continue
        event = latest.get(calculation["key"])
        active = event if event and event.get("action") == "compose" and event.get("source_stamp") == recipe["source_stamp"] else None
        outdated = bool(event and event.get("action") == "compose" and active is None)
        if outdated:
            stale.append({**event, "stale": True})
        choices = active["components"] if active else [{"input_key": row["input_key"], "included": row["included"]} for row in recipe["components"]]
        choice_map = {row["input_key"]: row["included"] for row in choices}
        for row in recipe["components"]:
            row["included"] = choice_map.get(row["input_key"], row["included"])
        adjustments = active.get("adjustments", []) if active else []
        subtotal, numerator, value = _calculate(recipe, choices, adjustments)
        recipe.update(revision=review["revision"], adjustments=adjustments, current_event=active,
                      stale=outdated, source_value=calculation.get("value"), reviewed_value=value if active else calculation.get("value"),
                      reviewed_subtotal=subtotal, reviewed_numerator=numerator,
                      label="Staff-adjusted calculation" if active else "Source calculation")
        calculation["recipe"] = recipe
        if not active:
            continue
        calculation["source_value"] = calculation.get("value")
        calculation["reviewed_value"] = value
        calculation["staff_adjusted"] = True
        calculation["reviewed_label"] = "Staff-adjusted tax earnings estimate" if calculation.get("kind") == "tax_estimate" else "Staff-adjusted EBITDA" if calculation.get("kind") else "Staff-adjusted DSCR"
        # Source EBITDA is deliberately retained. Only the reviewed DSCR is
        # adopted as the current coverage decision; its assumptions stay named.
        if calculation["key"].startswith("dscr."):
            from app.services.financial_cockpit import dscr_status

            candidate = next((row for row in dscr["candidates"] if f"dscr.{row['key']}" == calculation["key"]), None)
            if candidate is None or candidate.get("currency_status") == "mismatch":
                continue
            candidate.update(source_numerator=candidate.get("numerator"), source_value=candidate.get("value"),
                             numerator=numerator, value=value, status=dscr_status(value), staff_adjusted=True,
                             source_label=candidate["label"], label=candidate["label"] + " · staff-adjusted",
                             calculation_review={"revision": active["revision"], "reason": active["reason"], "adjustments": adjustments,
                                                 "components": choices, "period": calculation.get("period")})
            proposed = dscr.get("proposed") or {}
            proposed_annual = number(proposed.get("annual_payment"))
            denominator = (recipe.get("denominator") or {}).get("value")
            candidate["pro_forma_value"] = round(numerator / (denominator + proposed_annual), 2) if numerator is not None and denominator is not None and proposed_annual is not None and denominator + proposed_annual > 0 else None
            candidate["pro_forma_status"] = dscr_status(candidate["pro_forma_value"])
            candidate["note"] = " ".join(filter(None, [candidate.get("note"), "Staff reviewed this calculation recipe. Original source amounts and published debt are unchanged."]))
            calculation.update(value=value, numerator=numerator, label=candidate["label"] + " DSCR")
    review["stale_compositions"] = stale
    review["composition_history"] = [event for event in state.get("events", []) if event.get("calculation_key")]
    dscr["headline"] = next((row for row in dscr.get("candidates", []) if row.get("value") is not None), None)
    dscr["pro_forma_headline"] = next((row for row in dscr.get("candidates", []) if row.get("pro_forma_value") is not None), None)
    for calculation in review["calculations"]:
        if calculation["key"].startswith("dscr."):
            calculation["selected"] = bool(dscr["headline"] and calculation["key"] == f"dscr.{dscr['headline']['key']}")
    for item in review["inputs"]:
        item["used_by_headline"] = [row["key"] for row in review["calculations"] if row.get("selected") and item["key"] in row.get("input_keys", [])]


def change(state, review, *, calculation_key, action, components, adjustments, reason, expected_revision,
           source_stamp, idempotency_key, actor, now=None):
    """One atomic recipe receipt; all selections are validated before a write."""
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    state = state or {}
    request = {"calculation_key": calculation_key, "action": action, "components": components, "adjustments": adjustments,
               "reason": reason.strip(), "expected_revision": expected_revision, "source_stamp": source_stamp}
    request_stamp = stamp(request)
    for event in state.get("events", []):
        if event.get("idempotency_key") == idempotency_key:
            if event.get("request_stamp") != request_stamp:
                raise HTTPException(409, "Idempotency key already used for a different review")
            return state, False
    if state.get("revision", 0) != expected_revision:
        raise HTTPException(409, "Financial reviews changed; reload before saving")
    target = next((row for row in review["calculations"] if row["key"] == calculation_key), None)
    recipe = target.get("recipe") if target else None
    if not recipe or not recipe["can_edit"]:
        raise HTTPException(422, "This calculation must be corrected in its source workspace")
    if recipe["source_stamp"] != source_stamp:
        raise HTTPException(409, "Calculation evidence changed; review its current inputs before saving")
    if not reason.strip() or len(reason) > 2000 or action not in {"compose", "restore"}:
        raise HTTPException(422, "A review action and reason are required")
    if action == "compose":
        keys = [row.get("input_key") for row in components]
        if len(keys) != len(set(keys)) or set(keys) != {row["input_key"] for row in recipe["components"]}:
            raise HTTPException(422, "Review every calculation component exactly once")
        included = {row["input_key"] for row in components if row.get("included") is True}
        reported = "profit_and_loss.reported_ebitda"
        if reported in included and len(included) != 1:
            raise HTTPException(422, "Choose reported EBITDA or the net-income reconciliation, not both")
        if reported not in included and recipe["required_anchor"] not in included:
            raise HTTPException(422, "The source income anchor is required; correct that source input instead of removing the loss")
        if len(adjustments) > 20 or len({row.get("id") for row in adjustments}) != len(adjustments):
            raise HTTPException(422, "Use at most twenty uniquely identified adjustments")
        for row in adjustments:
            amount = number(row.get("amount"))
            if not row.get("id") or len(str(row["id"])) > 80 or not str(row.get("label") or "").strip() or len(str(row["label"])) > 120 or amount is None or abs(amount) > 1e15:
                raise HTTPException(422, "Each adjustment needs an identity, label and finite signed amount")
        _subtotal, numerator, _value = _calculate(recipe, components, adjustments)
        if numerator is not None and abs(numerator) > 1e15:
            raise HTTPException(422, "The reviewed calculation exceeds the supported amount range")
    normalized = [{"id": row["id"], "label": row["label"].strip(), "amount": _money(row["amount"])} for row in adjustments] if action == "compose" else []
    event = {"id": str(uuid.uuid4()), **request, "adjustments": normalized, "components": components if action == "compose" else [],
             "revision": expected_revision + 1, "actor_id": str(actor.id),
             "actor_name": getattr(actor, "full_name", None) or getattr(actor, "name", None),
             "at": (now or datetime.now(UTC)).isoformat(), "source_refs": target.get("source_refs", []),
             "period": target.get("period"), "previous_value": target.get("reviewed_value", target.get("value")),
             "idempotency_key": idempotency_key, "request_stamp": request_stamp}
    return {"revision": expected_revision + 1, "events": [*state.get("events", []), event]}, True
