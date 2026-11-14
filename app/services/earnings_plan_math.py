"""The earnings plan's arithmetic: one revenue model, costs that follow it, and
what each change does to earnings.

A direct port of the Earnings Plan design's engine (the reference numbers live
in app/tests/fixtures/earnings_plan_reference.json; the TypeScript twin in the
dashboard must give the same figures). Pure: no database, no cockpit, no clock.

The model
---------
* A revenue stream is a set of factors. Volume factors multiply into the
  business units sold in a month; the others multiply into the price of one
  unit. Annual revenue is units x price x 12.
* A cost line is fixed, a dollar amount per unit of one stream, or a percent of
  a stream's (or all) revenue. Its rate is read off today's figures, so with no
  changes the model reproduces the source exactly.
* A change raises or lowers one thing by an amount or a percent: overall
  pricing, a stream factor, a cost line, or a whole cost group. A growth
  activity spends each month to add volume to a factor; a new activity is a
  cost that does not exist today.
* The plan figure is the baseline plus what the model moves. The baseline's own
  NOI and EBITDA are never recomputed here, so a source whose lines are not yet
  fully itemized still starts from its real figures.

Two ways the result is broken down, both from the design:
* the bridge applies the sections in a fixed order (pricing, revenue drivers,
  growth, cost of goods sold, operating expenses, new activities) and adds up
  exactly to the plan figure;
* a change's own effect, and a section's own total, is what the plan loses
  without it. Changes interact, so those do not add up to the bridge.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

AREAS = ("Pricing", "Revenue drivers", "Growth", "Cost of goods sold", "Operating expenses", "New activities")
SECTIONS = (
    ("revenue", "Revenue drivers", (0, 1)),
    ("growth", "Growth activities", (2,)),
    ("cogs", "Cost of goods sold", (3,)),
    ("opex", "Operating expenses", (4,)),
    ("activities", "New activities", (5,)),
)
COGS_GROUP = "cogs"
ALL = "all"
TARGET_DSCR = 1.25
RECONCILE_TOLERANCE = 1.0

Predicate = Callable[[str], bool]


def number(value: Any) -> float:
    """A figure as the model counts it: anything unreadable is zero."""
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result and result not in (float("inf"), float("-inf")) else 0.0


def _sign(change: dict) -> int:
    return 1 if change.get("direction") == "up" else -1


def _apply(base: float, change: dict) -> float:
    """Raise or lower a figure by the change's amount or percent."""
    value = number(change.get("value"))
    if change.get("mode") == "percent":
        return base * (1 + _sign(change) * value / 100)
    return base + _sign(change) * value


def change_key(change: dict) -> str:
    """Where a change lands: one change per place."""
    target = change.get("target") or {}
    kind = target.get("kind")
    if kind == "pricing":
        return "pricing"
    if kind == "factor":
        return f"f:{target.get('stream_id')}:{target.get('factor_id')}"
    if kind == "line":
        return f"l:{target.get('line_id')}"
    return f"g:{target.get('group')}"


def start_month(row: dict) -> int:
    try:
        month = int(row.get("start_month") or 1)
    except (TypeError, ValueError):
        month = 1
    return min(12, max(1, month))


class Plan:
    """A plan body indexed for the engine."""

    def __init__(self, body: dict, baseline: dict):
        self.body = body
        self.baseline = baseline
        self.streams: list[dict] = list(body.get("streams") or [])
        self.lines: list[dict] = list(body.get("lines") or [])
        self.changes: list[dict] = list(body.get("changes") or [])
        self.growth: list[dict] = list(body.get("growth") or [])
        self.activities: list[dict] = list(body.get("activities") or [])
        self.stream_by_id = {row["id"]: row for row in self.streams}
        self.line_by_id = {row["id"]: row for row in self.lines}
        self.group_labels = {row["key"]: row.get("label") or row["key"] for row in baseline.get("groups") or []}
        # One change per place; a later duplicate is ignored rather than stacked.
        self.change_at: dict[str, dict] = {}
        for change in self.changes:
            self.change_at.setdefault(change_key(change), change)
        self.items: list[dict] = []
        for key, change in self.change_at.items():
            self.items.append({"id": str(change["id"]), "kind": "change", "key": key, "row": change, "area": self._change_area(key)})
        for row in self.growth:
            self.items.append({"id": str(row["id"]), "kind": "growth", "key": f"gr:{row['id']}", "row": row, "area": 2})
        for row in self.activities:
            self.items.append({"id": str(row["id"]), "kind": "activity", "key": f"na:{row['id']}", "row": row, "area": 5})
        self.item_by_id = {item["id"]: item for item in self.items}
        self.model = self.revenue(lambda _id: False, 0.0)
        self.rates: dict[str, float] = {}
        for line in self.lines:
            behavior, link = line.get("behavior"), line.get("link")
            if behavior == "unit":
                driver = self.model["volume"].get(link)
            elif behavior == "pct":
                driver = self.model["total"] if link == ALL else self.model["revenue"].get(link)
            else:
                driver = 0
            self.rates[line["id"]] = number(line.get("base")) / driver if driver else 0.0

    # ------------------------------------------------------------------ items
    def _change_area(self, key: str) -> int:
        if key == "pricing":
            return 0
        if key.startswith("f:"):
            return 1
        if key.startswith("g:"):
            return 3 if key[2:] == COGS_GROUP else 4
        return 3 if (self.line_by_id.get(key[2:]) or {}).get("group") == COGS_GROUP else 4

    def proposed(self, item_id: str) -> bool:
        item = self.item_by_id.get(item_id)
        return bool(item) and item["row"].get("included") is not False and item["row"].get("status") != "rejected"

    def reviewed(self, item_id: str) -> bool:
        item = self.item_by_id.get(item_id)
        return bool(item) and item["row"].get("included") is not False and item["row"].get("status") == "approved"

    def label(self, item_id: str) -> str:
        item = self.item_by_id[item_id]
        if item["kind"] != "change":
            return item["row"].get("name") or ("Growth activity" if item["kind"] == "growth" else "New activity")
        target = item["row"].get("target") or {}
        kind = target.get("kind")
        if kind == "pricing":
            return "Overall pricing"
        if kind == "factor":
            stream = self.stream_by_id.get(target.get("stream_id"))
            if not stream:
                return "Metric"
            factor = next((row for row in stream.get("factors") or [] if row.get("id") == target.get("factor_id")), None)
            return f"{stream.get('name') or 'Revenue stream'} · {(factor or {}).get('label') or 'metric'}"
        if kind == "line":
            return (self.line_by_id.get(target.get("line_id")) or {}).get("label") or "Cost"
        return f"Whole group: {self.group_labels.get(target.get('group'), target.get('group'))}"

    # ---------------------------------------------------------------- revenue
    def revenue(self, on: Predicate, base_total: float) -> dict:
        volume: dict[str, float] = {}
        revenue: dict[str, float] = {}
        factors: dict[str, list[float]] = {}
        total = 0.0
        for stream in self.streams:
            rows = stream.get("factors") or []
            values = []
            for factor in rows:
                change = self.change_at.get(f"f:{stream['id']}:{factor.get('id')}")
                current = number(factor.get("value"))
                values.append(max(0.0, _apply(current, change)) if change and on(str(change["id"])) else current)
            for activity in self.growth:
                if activity.get("stream_id") != stream["id"] or not on(str(activity["id"])):
                    continue
                at = next((index for index, factor in enumerate(rows) if factor.get("id") == activity.get("factor_id")), None)
                if at is not None:
                    values[at] += number(activity.get("added_per_month"))
            units, price = 1.0, 1.0
            for factor, value in zip(rows, values, strict=True):
                scaled = value / 100 if factor.get("unit") == "%" else value
                if factor.get("volume"):
                    units *= scaled
                else:
                    price *= scaled
            volume[stream["id"]] = units * 12
            revenue[stream["id"]] = units * price * 12
            factors[stream["id"]] = values
            total += revenue[stream["id"]]
        pricing = self.change_at.get("pricing")
        if pricing and on(str(pricing["id"])) and base_total:
            if pricing.get("mode") == "percent":
                lift = 1 + _sign(pricing) * number(pricing.get("value")) / 100
            else:
                lift = 1 + _sign(pricing) * number(pricing.get("value")) / base_total
            revenue = {key: value * lift for key, value in revenue.items()}
            total *= lift
        return {"volume": volume, "revenue": revenue, "total": total, "factors": factors}

    def behavior(self, line: dict) -> str:
        """A line whose driver is zero today is held fixed."""
        chosen = line.get("behavior") or "fixed"
        return "fixed" if chosen != "fixed" and not self.rates.get(line["id"]) else chosen

    # --------------------------------------------------------------- scenario
    def scenario(self, on: Predicate) -> dict:
        model = self.revenue(on, self.model["total"])
        amounts: dict[str, float] = {}
        for line in self.lines:
            change = self.change_at.get(f"l:{line['id']}")
            active = bool(change) and on(str(change["id"]))
            behavior = self.behavior(line)
            base, rate, link = number(line.get("base")), self.rates[line["id"]], line.get("link")
            if behavior == "fixed":
                value = _apply(base, change) if active else base
            elif behavior == "unit":
                value = (_apply(rate, change) if active else rate) * (model["volume"].get(link) or 0.0)
            else:
                share = rate
                if active:
                    # A percent-of-revenue line moves by a percent of itself, or by points of revenue.
                    share = _apply(rate, change) if change.get("mode") == "percent" else rate + _sign(change) * number(change.get("value")) / 100
                value = share * (model["total"] if link == ALL else model["revenue"].get(link) or 0.0)
            amounts[line["id"]] = max(0.0, value)
        for key, change in self.change_at.items():
            if not key.startswith("g:") or not on(str(change["id"])):
                continue
            members = [line for line in self.lines if line.get("group") == key[2:]]
            subtotal = sum(amounts[line["id"]] for line in members)
            if change.get("mode") == "percent":
                factor = 1 + _sign(change) * number(change.get("value")) / 100
            else:
                factor = 1 + _sign(change) * number(change.get("value")) / subtotal if subtotal else 1.0
            for line in members:
                amounts[line["id"]] = max(0.0, amounts[line["id"]] * factor)
        extra = 0.0
        for row in self.growth:
            if on(str(row["id"])):
                extra += number(row.get("monthly_spend")) * 12 + (number(row.get("setup_cost")) * 12 if row.get("setup_cadence") == "monthly" else 0.0)
        for row in self.activities:
            if on(str(row["id"])):
                extra += number(row.get("monthly_amount")) * 12 + (number(row.get("setup_cost")) * 12 if row.get("setup_cadence") == "monthly" else 0.0)
        for change in self.change_at.values():
            if on(str(change["id"])) and change.get("setup_cadence") == "monthly":
                extra += number(change.get("setup_cost")) * 12
        cogs_lines = [line for line in self.lines if line.get("group") == COGS_GROUP]
        other_lines = [line for line in self.lines if line.get("group") != COGS_GROUP]
        revenue_delta = model["total"] - self.model["total"]
        cogs_delta = sum(amounts[line["id"]] - number(line.get("base")) for line in cogs_lines)
        opex_delta = sum(amounts[line["id"]] - number(line.get("base")) for line in other_lines) + extra
        base = self.baseline
        noi = number(base.get("noi")) + revenue_delta - cogs_delta - opex_delta
        return {
            "revenue": number(base.get("revenue")) + revenue_delta,
            "cogs": number(base.get("cogs")) + cogs_delta,
            "opex": number(base.get("operating_expenses")) + opex_delta,
            "noi": noi,
            "ebitda": noi + number(base.get("ebitda")) - number(base.get("noi")),
            "activity_spend": extra,
            "lines": amounts,
            "stream_revenue": model["revenue"],
            "stream_volume": model["volume"],
            "factors": model["factors"],
        }

    # ------------------------------------------------------------------ timing
    def months(self, included: Predicate) -> list[dict]:
        rows = []
        for month in range(1, 13):
            live = self.scenario(lambda item_id, month=month: included(item_id) and start_month(self.item_by_id[item_id]["row"]) <= month)
            one_time = sum(number(item["row"].get("setup_cost")) for item in self.items
                           if included(item["id"]) and start_month(item["row"]) == month and item["row"].get("setup_cadence") != "monthly")
            rows.append({"ebitda": live["ebitda"] / 12 - one_time, "noi": live["noi"] / 12 - one_time})
        return rows

    def effect(self, item_id: str, proposed: dict | None = None) -> dict:
        """What the plan gains or loses with this one change, whether or not it is switched on."""
        item = self.item_by_id[item_id]
        with_it = proposed if proposed is not None and self.proposed(item_id) else self.scenario(lambda other: self.proposed(other) or other == item_id)
        without = self.scenario(lambda other: self.proposed(other) and other != item_id)
        delta = with_it["ebitda"] - without["ebitda"]
        one_time = 0.0 if item["row"].get("setup_cadence") == "monthly" else number(item["row"].get("setup_cost"))
        moves = []
        if abs(with_it["revenue"] - without["revenue"]) >= 1:
            moves.append({"label": "Revenue", "amount": with_it["revenue"] - without["revenue"]})
        for line in self.lines:
            moved = with_it["lines"][line["id"]] - without["lines"][line["id"]]
            if abs(moved) >= 1:
                moves.append({"label": line.get("label") or "Cost", "amount": moved})
        if abs(with_it["activity_spend"] - without["activity_spend"]) >= 1:
            moves.append({"label": "Activity spend", "amount": with_it["activity_spend"] - without["activity_spend"]})
        return {"ebitda": delta, "noi": with_it["noi"] - without["noi"], "revenue": with_it["revenue"] - without["revenue"],
                "first_year": delta * (13 - start_month(item["row"])) / 12 - one_time, "one_time": one_time, "moves": moves}

    def bridge(self, included: Predicate) -> list[dict]:
        previous = number(self.baseline.get("ebitda"))
        steps = []
        for area, label in enumerate(AREAS):
            figure = self.scenario(lambda item_id, area=area: included(item_id) and self.item_by_id[item_id]["area"] <= area)["ebitda"]
            steps.append({"key": area, "label": label, "value": figure - previous})
            previous = figure
        return steps


def loan_operating(baseline: dict) -> float:
    """Operating costs a year the saved loan terms themselves add; nothing without saved terms."""
    return max(0.0, number(baseline.get("loan_operating_annual"))) if baseline.get("debt_service_with_terms") is not None else 0.0


def metrics(baseline: dict, scenario: dict | None, annual_debt: float | None, charges: float = 0.0) -> dict:
    """The six figures for one scenario against one debt service. `scenario` None is today.

    `charges` are operating costs the loan itself adds (see `loan_operating`). They are not the business's
    earnings history or its plan, so EBITDA, NOI and margin are left alone; they come off what covers the debt.
    """
    ebitda = number(baseline.get("ebitda")) if scenario is None else scenario["ebitda"]
    noi = number(baseline.get("noi")) if scenario is None else scenario["noi"]
    revenue = number(baseline.get("revenue")) if scenario is None else scenario["revenue"]
    covering = ebitda - charges
    return {
        "ebitda": ebitda,
        "noi": noi,
        "dscr": covering / annual_debt if annual_debt else None,
        "free_cash_flow": covering - annual_debt if annual_debt is not None else None,
        "operating_margin": noi / revenue if revenue else None,
        "debt_service": annual_debt,
    }


def to_finish(plan: Plan, *, questions: Iterable[dict] = ()) -> list[dict]:
    """What stands between this draft and a saved version, as one list, in the steps' order."""
    body, baseline = plan.body, plan.baseline
    source = body.get("source") or {}
    confirmed = source.get("confirmations") or {}
    answers = body.get("category_answers") or {}
    name = baseline.get("source_name") or "source"
    items: list[dict] = []
    if baseline.get("available", True) is False:
        # Nothing to confirm or reconcile against yet: the file needs its earnings source first.
        items.append({"code": "no_source", "step": 0, "label": "Add the file's earnings source: a filed business return or a profit and loss statement"})
    else:
        for group in baseline.get("groups") or []:
            if not confirmed.get(group["key"]):
                items.append({"code": "confirm_source", "step": 0, "key": group["key"], "label": f"Confirm {group.get('label') or group['key']} source amount"})
        if abs(plan.model["total"] - number(baseline.get("revenue"))) > RECONCILE_TOLERANCE:
            items.append({"code": "reconcile_revenue", "step": 1, "label": f"Reconcile the revenue model to {name} revenue"})
    for question in questions:
        if not answers.get(question["key"]):
            items.append({"code": "answer_category", "step": 1, "key": question["key"], "label": f"Answer {question['label']}: any current cost?"})
    for item in plan.items:
        if plan.proposed(item["id"]) and not str(item["row"].get("note") or "").strip():
            items.append({"code": "explain_change", "step": 2, "item_id": item["id"], "label": f"Explain {plan.label(item['id'])}"})
    if not body.get("attestation"):
        items.append({"code": "attest", "step": 3, "label": "Confirm these disclosures and proposals"})
    return items


def evaluate(body: dict, baseline: dict, *, target_dscr: float = TARGET_DSCR, goal: float | None = None, questions: Iterable[dict] = ()) -> dict:
    """Everything the plan screens, the saved version and the readers downstream need, from one pass.

    `baseline` carries revenue, cogs, operating_expenses, noi, ebitda, the
    annual debt service today (`debt_service_current`) and with the saved loan
    terms (`debt_service_with_terms`, None when there are none), the monthly
    cash the terms release (`released_monthly`), any operating costs a year the
    terms add (`loan_operating_annual`) and the source's expense groups.
    """
    plan = Plan(body, baseline)
    proposed = plan.scenario(plan.proposed)
    reviewed = plan.scenario(plan.reviewed)
    months = {"proposed": plan.months(plan.proposed), "reviewed": plan.months(plan.reviewed)}
    first_year = {view: {"ebitda": sum(row["ebitda"] for row in rows), "noi": sum(row["noi"] for row in rows)} for view, rows in months.items()}
    current_debt = baseline.get("debt_service_current")
    terms_debt = baseline.get("debt_service_with_terms")
    plan_debt = terms_debt if terms_debt is not None else current_debt
    charges = loan_operating(baseline)
    effects = {item["id"]: plan.effect(item["id"], proposed) for item in plan.items}
    sections = []
    for key, label, areas in SECTIONS:
        without = plan.scenario(lambda item_id, areas=areas: plan.proposed(item_id) and plan.item_by_id[item_id]["area"] not in areas)
        sections.append({"key": key, "label": label, "areas": list(areas), "ebitda": proposed["ebitda"] - without["ebitda"],
                         "items": [item["id"] for item in plan.items if item["area"] in areas]})
    scenarios = {"proposed": proposed, "reviewed": reviewed}
    views = {}
    for view, scenario in scenarios.items():
        views[view] = {
            "current_debt": metrics(baseline, scenario, current_debt),
            "with_terms": metrics(baseline, scenario, terms_debt, charges) if terms_debt is not None else None,
        }
    today = {"current_debt": metrics(baseline, None, current_debt),
             "with_terms": metrics(baseline, None, terms_debt, charges) if terms_debt is not None else None}
    released = number(baseline.get("released_monthly")) if terms_debt is not None else 0.0
    allocated = sum(number(row.get("monthly_amount")) for row in body.get("allocations") or [])
    goal_amount = number(goal) if goal else None
    return {
        "model": {"volume": plan.model["volume"], "revenue": plan.model["revenue"], "total": plan.model["total"],
                  "difference": plan.model["total"] - number(baseline.get("revenue")),
                  "reconciles": abs(plan.model["total"] - number(baseline.get("revenue"))) <= RECONCILE_TOLERANCE},
        "rates": plan.rates,
        "behaviors": {line["id"]: plan.behavior(line) for line in plan.lines},
        "items": [{"id": item["id"], "kind": item["kind"], "key": item["key"], "area": item["area"], "label": plan.label(item["id"]),
                   "start_month": start_month(item["row"]), "included": plan.proposed(item["id"]), "reviewed": plan.reviewed(item["id"]),
                   "status": item["row"].get("status") or "proposed", "on": item["row"].get("included") is not False} for item in plan.items],
        "proposed": proposed,
        "reviewed": reviewed,
        "months": months,
        "first_year": first_year,
        "effects": effects,
        "sections": sections,
        "bridge": {"proposed": plan.bridge(plan.proposed), "reviewed": plan.bridge(plan.reviewed)},
        "metrics": {"today": today, **views},
        "debt_service": {"current": current_debt, "with_terms": terms_debt, "plan": plan_debt, "loan_operating": charges},
        "target": {"ratio": target_dscr,
                   **{view: (max(0.0, target_dscr * plan_debt - (scenario["ebitda"] - charges)) if plan_debt else None) for view, scenario in scenarios.items()}},
        "goal": {"amount": goal_amount, "proposed_gap": goal_amount - proposed["noi"] if goal_amount is not None else None,
                 "reviewed_gap": goal_amount - reviewed["noi"] if goal_amount is not None else None},
        "released": {"available_monthly": released, "allocated_monthly": allocated, "over_allocated": allocated > released,
                     "unallocated_monthly": released - allocated},
        "to_finish": to_finish(plan, questions=questions),
    }
