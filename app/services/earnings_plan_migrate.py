"""Starting a v2 earnings plan: from the file's baseline, or from a v1 plan.

A new plan is seeded from the one ladder: a cost line per source group (so the
lines add up to the source), and one revenue stream that reproduces the
source's revenue until the business describes how its revenue is really made.

A v1 plan is carried over where the new model can say the same thing: source
confirmations, itemized costs that reconcile to their group, plain cost
changes, a percent price change, cash allocations. Anything else is kept as a
carried-over item with its label and annual effect for re-entry. Nothing is
dropped, and nothing carried over keeps a staff approval: the arithmetic
changed, so a reviewer looks at each change again.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from app.schemas import earnings_plan_v2 as schema
from app.services import earnings_plan_envelope as envelope
from app.services.financial_metric_review import number

log = logging.getLogger(__name__)
SEED_STREAM = "s1"
RECONCILE = 1.0
_PURPOSES = {"existing_cost": "existing", "planned_increment": "planned", "reserve": "reserve", "other": "other"}
_ANSWERS = {"confirmed": "included", "not_applicable": "not_applicable", "needs_help": "help"}
_RE_ENTER = {
    "marketing_investment": "A marketing campaign. Re-enter it as a growth activity: the monthly spend and the volume it adds.",
    "unit_volume": "A change in units sold. Re-enter it on the revenue stream's volume metric.",
    "revenue_change": "A revenue change. Re-enter it on the revenue stream it belongs to, or as overall pricing.",
    "price_change": "A price change per unit. Re-enter it on the stream's price metric.",
    "product_cost_change": "A product cost change. Link the cost to its revenue stream, then lower or raise its cost per unit.",
    "processing_cost_change": "A card processing change. Re-enter it on the card processing line as a change in points of revenue.",
    "one_time_adjustment": "A one-time item. One-time items are not part of run-rate earnings; add a setup cost to the change it belongs to if it still applies.",
    "expense_group_change": "A change to a whole cost group. Re-enter it as a whole-group change.",
    "expense_reduction": "A cost reduction. Re-enter it on the cost line it applies to.",
    "expense_increase": "A cost increase. Re-enter it on the cost line it applies to, or as a new activity.",
}


def default_plan_start(today: date | None = None) -> str:
    """The month after next: a plan made in November starts in January."""
    today = today or datetime.now().date()
    index = today.year * 12 + today.month - 1 + 2
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def seed(cockpit: dict, *, business_name: str = "", today: date | None = None) -> dict:
    """A plan with nothing proposed that reproduces the file's baseline exactly."""
    baseline = envelope.engine_baseline(cockpit)
    source = baseline.get("source") or {}
    body: dict[str, Any] = {
        **schema.empty_body(),
        "business_name": business_name or (cockpit.get("operating_evidence") or {}).get("business_name") or "",
        "period_start": source.get("period_start"), "period_end": source.get("period_end"),
        "currency": source.get("currency") or "USD", "plan_start": default_plan_start(today),
    }
    if not baseline["available"]:
        return schema.validate_body(body)
    body["source"] = {"stamp": envelope.baseline_stamp(baseline), "confirmations": {}, "corrections": {}}
    body["streams"] = [{"id": SEED_STREAM, "name": "All revenue", "type": "custom", "factors": [
        {"id": "f0", "label": "Sales", "value": 1, "unit": "count", "volume": True},
        {"id": "f1", "label": "Revenue per month", "value": round(baseline["revenue"] / 12, 4), "unit": "$", "volume": False}]}]
    body["lines"] = [{"id": group["key"], "group": group["key"], "label": group["label"], "base": round(group["amount"], 2),
                      "behavior": "fixed", "link": schema.ALL} for group in baseline["groups"] if group["amount"] > 0]
    return schema.validate_body(body)


def resync(raw: Any, cockpit: dict) -> dict:
    """Bring a plan onto the file's current source figures after they changed.

    A line that is its group's only line takes the group's new amount; groups
    that are new get a line; itemized groups keep their lines for the business
    to rebalance. Confirmations start again, because the amounts are new.
    """
    body = schema.validate_body(raw)
    baseline = envelope.engine_baseline(cockpit)
    if not baseline["available"]:
        return body
    by_group: dict[str, list[dict]] = {}
    for line in body["lines"]:
        by_group.setdefault(line["group"], []).append(line)
    for group in baseline["groups"]:
        members = by_group.get(group["key"])
        if not members and group["amount"] > 0:
            body["lines"].append({"id": _free_id(body, group["key"]), "group": group["key"], "label": group["label"], "base": round(group["amount"], 2),
                                  "behavior": "fixed", "link": schema.ALL, "added": False})
        elif members and len(members) == 1:
            members[0]["base"] = round(group["amount"], 2)
    body["source"] = {"stamp": envelope.baseline_stamp(baseline), "confirmations": {}, "corrections": {}}
    return schema.validate_body(body)


def _free_id(body: dict, wanted: str) -> str:
    taken = {line["id"] for line in body["lines"]}
    return wanted if wanted not in taken else f"{wanted}-{uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# v1 -> v2
# ---------------------------------------------------------------------------

def _annual_cost(row: dict, side: str = "current") -> float | None:
    from app.schemas.earnings_improvement import cost_amount
    from app.services.earnings_improvement import FACTORS

    amount = cost_amount(row, side)
    factor = FACTORS.get(row.get("cadence") or "monthly")
    return amount * factor if amount is not None and factor else None


def _start_month(effective: Any, plan_start: str) -> int:
    try:
        when = date.fromisoformat(str(effective)[:10])
    except (TypeError, ValueError):
        return 1
    year, month = (int(part) for part in plan_start.split("-"))
    return min(12, max(1, (when.year - year) * 12 + when.month - month + 1))


def _setup(row: dict) -> tuple[float | None, str]:
    cost, cadence = number(row.get("implementation_cost")), row.get("implementation_cost_cadence") or "one_time"
    if not cost:
        return None, "one_time"
    if cadence == "annual":
        return round(cost / 12, 2), "monthly"
    return cost, "monthly" if cadence == "monthly" else "one_time"


def _words(row: dict, plan_start: str) -> dict:
    setup, cadence = _setup(row)
    return {"id": row["id"], "start_month": _start_month(row.get("effective_date"), plan_start), "setup_cost": setup, "setup_cadence": cadence,
            "note": row.get("explanation") or "", "evidence_note": row.get("source_note") or "", "owner": row.get("owner_name") or "",
            "included": True, "source_file_ids": list(row.get("source_file_ids") or [])}


def v1_to_v2(raw: Any, cockpit: dict, *, today: date | None = None) -> dict:
    """The same plan in the new model, with whatever does not fit kept for re-entry."""
    from app.schemas import earnings_improvement as v1_schema

    old = v1_schema.validate_body(raw)
    body = seed(cockpit, business_name=old.get("business_name") or "", today=today)
    body["notes"] = old.get("notes") or ""
    for key in ("period_start", "period_end"):
        body[key] = old.get(key) or body.get(key)
    if old.get("currency"):
        body["currency"] = old["currency"]
    if old.get("projection_start"):
        body["plan_start"] = str(old["projection_start"])[:7]
    plan_start = body["plan_start"]
    carried: list[dict] = []
    impacts = _v1_impacts(old, cockpit)

    def carry(kind: str, label: str, detail: str, amount: float | None = None, identifier: str | None = None):
        carried.append({"id": identifier or str(uuid4()), "kind": kind, "label": label[:240] or "Item from the earlier plan", "detail": detail[:4000],
                        "annual_amount": round(amount, 2) if amount is not None else None})

    groups = {line["group"]: line for line in body["lines"]}
    active = [row for row in old["expenses"] if not row.get("excluded")]
    line_for_expense: dict[str, str] = {}

    # Costs: an itemization that adds up to its source group replaces the group's single line.
    by_group: dict[str, list[dict]] = {}
    for row in active:
        by_group.setdefault(row.get("source_group_key") or "", []).append(row)
    for key, rows in by_group.items():
        amounts = [_annual_cost(row) for row in rows]
        seeded = groups.get(key)
        if not key or seeded is None:
            for row, amount in zip(rows, amounts, strict=True):
                carry("expense", row.get("label") or row.get("category") or "Cost", "A cost the earlier plan listed outside the source's lines. "
                      "Confirm it is inside one of the source lines, or add it as a new activity if it is a cost the business does not have yet.", amount, row["id"])
            continue
        total = sum(amount for amount in amounts if amount is not None)
        if len(rows) > 1 and all(amount is not None for amount in amounts) and abs(total - seeded["base"]) <= RECONCILE:
            at = body["lines"].index(seeded)
            body["lines"][at:at + 1] = [{"id": row["id"], "group": key, "label": row.get("label") or seeded["label"], "base": round(amount, 2),
                                         "behavior": "fixed", "link": schema.ALL} for row, amount in zip(rows, amounts, strict=True)]
            line_for_expense.update({row["id"]: row["id"] for row in rows})
        else:
            line_for_expense.update({row["id"]: seeded["id"] for row in rows})
            if len(rows) > 1:
                carry("itemization", f"Itemization of {seeded['label']}", f"The earlier plan split this source line into {len(rows)} items totalling "
                      f"{total:,.0f} against {seeded['base']:,.0f} on the source. They do not add up, so the line was kept whole. Split it again in step 2 if needed.")

    # Source confirmations and corrections.
    reasons = {row["source_group_key"]: row for row in old.get("historical_corrections") or []}
    for key, decision in (old.get("source_group_confirmations") or {}).items():
        if key not in groups:
            continue
        if decision == "confirmed":
            body["source"]["confirmations"][key] = "confirmed"
            continue
        reason = (reasons.get(key) or {}).get("reason") or ""
        disclosed = sum(amount for amount in (_annual_cost(row) for row in by_group.get(key, [])) if amount is not None)
        if len(reason.strip()) >= 3:
            body["source"]["corrections"][key] = {"amount": round(disclosed or groups[key]["base"], 2), "reason": reason.strip()}
        else:
            carry("correction", f"Correction to {groups[key]['label']}", "The earlier plan marked this source amount as needing a correction without saying why. "
                  "Confirm the amount, or note the correction and its reason in step 1.")
    for category, decision in (old.get("category_confirmations") or {}).items():
        if any(category == key for key, _label in envelope.QUESTION_CATEGORIES):
            body["category_answers"][category] = _ANSWERS.get(decision, "help")

    # Changes.
    used: set[str] = set()
    for row in old["adjustments"]:
        if row.get("excluded"):
            continue
        label, kind = row.get("label") or row.get("kind", "").replace("_", " ").capitalize(), row.get("kind")
        change = _change(row, kind, line_for_expense, groups, old, plan_start)
        place = None
        if change is not None:
            place = change["target"]["kind"] + ":" + str(change["target"].get("line_id") or change["target"].get("group") or "")
        if change is None or place in used:
            why = _RE_ENTER.get(kind, "A change the new plan describes differently. Re-enter it in step 3.")
            if place in used:
                why = "A second change to the same cost. The new plan keeps one change per cost; combine it with the first one in step 3."
            words = " ".join(part for part in (why, f"Reason given: {row['explanation']}" if row.get("explanation") else "") if part)
            carry("change", label, words, impacts.get(row["id"]), row["id"])
            continue
        used.add(place)
        body["changes"].append(change)

    for row in (old.get("released_cash_flow") or {}).get("allocations") or []:
        body["allocations"].append({"id": row["id"], "label": row.get("label") or "", "purpose": _PURPOSES.get(row.get("purpose"), "other"),
                                    "monthly_amount": row.get("amount_monthly"), "notes": row.get("notes") or ""})
    body["carried_over"] = carried
    return schema.validate_body(body)


def _change(row: dict, kind: str, line_for_expense: dict, groups: dict, old: dict, plan_start: str) -> dict | None:
    """A v1 action as a v2 change, or None when the new model says it another way."""
    command = row.get("command") or {}
    words = _words(row, plan_start)
    if kind in {"expense_reduction", "expense_increase"} and row.get("expense_row_id") in line_for_expense:
        expense = next((item for item in old["expenses"] if item["id"] == row["expense_row_id"]), None)
        if command.get("target") == "total_cost" and command.get("mode") == "percent" and command.get("value") is not None:
            return {**words, "target": {"kind": "line", "line_id": line_for_expense[row["expense_row_id"]]},
                    "direction": "up" if command.get("direction") == "increase" else "down", "mode": "percent", "value": command["value"]}
        before = _annual_cost(row) if row.get("current_amount") is not None or row.get("cost_basis") == "per_unit" else _annual_cost(expense) if expense else None
        after = _annual_cost(row, "proposed")
        if before is None or after is None or abs(after - before) < 0.005:
            return None
        return {**words, "target": {"kind": "line", "line_id": line_for_expense[row["expense_row_id"]]},
                "direction": "up" if after > before else "down", "mode": "amount", "value": round(abs(after - before), 2)}
    if kind == "expense_group_change" and command.get("mode") == "percent" and command.get("value") is not None and row.get("source_group_key") in groups:
        return {**words, "target": {"kind": "group", "group": row["source_group_key"]},
                "direction": "up" if command.get("direction") == "increase" else "down", "mode": "percent", "value": command["value"]}
    if kind == "price_change" and row.get("price_basis") == "annual_revenue" and command.get("mode") == "percent" and command.get("value") is not None:
        return {**words, "target": {"kind": "pricing"}, "direction": "up" if command.get("direction") == "increase" else "down",
                "mode": "percent", "value": command["value"]}
    return None


def _v1_impacts(old: dict, cockpit: dict) -> dict[str, float | None]:
    """What each v1 action was worth, so a carried-over item still shows its size."""
    try:
        from app.services import earnings_improvement as v1

        return {row["id"]: row.get("net_annual_impact") for row in v1.evaluate(old, cockpit)["actions"]}
    except Exception:  # noqa: BLE001 - the amounts are a courtesy; the items carry over without them
        log.exception("earnings plan migration: v1 impacts unavailable")
        return {}
