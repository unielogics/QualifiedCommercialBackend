"""The Earnings Plan design's sample plan, as a v2 plan body.

The reference numbers are generated from the design's own script by the
dashboard (scripts/design-reference/generate.mjs). This turns the design's
state shape into the body the backend stores, so the same numbers can be asked
of the Python engine.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

REFERENCE = json.loads((Path(__file__).parent / "fixtures" / "earnings_plan_reference.json").read_text())
QUESTIONS = [{"key": "ptx", "label": "Payroll taxes"}, {"key": "trn", "label": "Transportation"}]


def item_id(design_id: str) -> str:
    """A stable UUID for a design item id such as `f:s1:1`, `gr:g1` or `na:a1`."""
    return str(uuid5(NAMESPACE_URL, f"earnings-plan-reference:{design_id}"))


def _amount(value):
    return float(value) if value not in ("", None) else None


def _common(row: dict) -> dict:
    return {
        "start_month": int(row["start"]), "setup_cost": _amount(row.get("setup")),
        "setup_cadence": "monthly" if row.get("setupCad") == "mo" else "one_time",
        "note": row.get("note") or "", "evidence_note": row.get("evid") or "", "owner": row.get("owner") or "",
        "included": row.get("on") is not False, "status": row.get("status") or "proposed",
    }


def _target(key: str) -> dict:
    if key == "pricing":
        return {"kind": "pricing"}
    kind, first, *rest = key.split(":")
    if kind == "f":
        return {"kind": "factor", "stream_id": first, "factor_id": f"f{rest[0]}"}
    if kind == "l":
        return {"kind": "line", "line_id": first}
    return {"kind": "group", "group": first}


def reference_baseline() -> dict:
    source = REFERENCE["baseline"]
    return {
        "revenue": source["revenue"], "cogs": source["cogs"], "operating_expenses": source["operating_expenses"],
        "noi": source["noi"], "ebitda": source["ebitda"],
        "debt_service_current": source["debt_service_current"], "debt_service_with_terms": source["debt_service_with_terms"],
        "released_monthly": 50_000, "source_name": "IRS",
        "groups": [{"key": row["key"], "label": row["label"], "amount": row["return_amount"], "statement_amount": row["statement_amount"]}
                   for row in source["groups"]],
    }


def reference_body() -> dict:
    plan = REFERENCE["plan"]
    return {
        "schema_version": "operating-plan-v2",
        "business_name": "Harbor Auto Detail LLC",
        "currency": "USD",
        "plan_start": plan["plan_start"],
        "source": {"confirmations": {key: "confirmed" for key, value in plan["confirmations"].items() if value}, "corrections": {}},
        "streams": [{"id": stream["id"], "name": stream["name"], "type": stream["type"],
                     "factors": [{"id": f"f{index}", "label": factor["l"], "value": float(factor["v"]), "unit": factor["u"], "volume": bool(factor.get("vol"))}
                                 for index, factor in enumerate(stream["f"])]} for stream in plan["streams"]],
        "lines": [{"id": line["id"], "group": line["g"], "label": line["l"], "base": line["base"], "behavior": line["beh"], "link": line["link"]}
                  for line in plan["lines"]],
        "category_answers": {},
        "changes": [{"id": item_id(key), "target": _target(key), "direction": change["dir"],
                     "mode": "percent" if change["mode"] == "pct" else "amount", "value": _amount(change["val"]), **_common(change)}
                    for key, change in plan["changes"].items()],
        "growth": [{"id": item_id(f"gr:{row['id']}"), "name": row["name"], "monthly_spend": _amount(row["spend"]), "stream_id": row["stream"],
                    "factor_id": f"f{row['factor']}", "added_per_month": _amount(row["add"]), **_common(row)} for row in plan["growth"]],
        "activities": [{"id": item_id(f"na:{row['id']}"), "name": row["name"], "monthly_amount": _amount(row["amt"]), **_common(row)}
                       for row in plan["activities"]],
        "allocations": [{"id": item_id(f"alloc:{row['id']}"), "label": row["label"], "purpose": row["purpose"], "monthly_amount": _amount(row["amt"])}
                        for row in plan["allocations"]],
        "attestation": False,
        "notes": "",
    }


# ---------------------------------------------------------------------------
# The same plan on a file: a cockpit built from the design's return and debts
# ---------------------------------------------------------------------------

# The design's short group keys, as the file's baseline names the same lines.
GROUP_KEYS = {"sal": "salaries_and_wages", "ins": "insurance", "mkt": "advertising", "pro": "professional_fees",
              "utl": "utilities", "rep": "repairs", "sw": "software", "oth": "other_deductions"}
REVIEWED_IN_DESIGN = ("f:s1:1", "l:rent", "l:sw")


def file_cockpit(*, terms: bool = True, goal: float | None = None, analyses=None) -> dict:
    """A cockpit holding the design's file: the 2025 return, five debts and, by default, saved loan terms that retire them."""
    from app.services.financial_cockpit import build_cockpit
    from app.tests.test_financial_baseline import DEBTS, ENTITY, filed_return

    plan = {"retire": [f"row:{row['id']}" for row in DEBTS], "exclude": [], "new_payment_monthly": 14_800, "new_payment_source": "manual",
            "updated_at": "2026-11-10T12:00:00+00:00"} if terms else None
    cockpit = build_cockpit([filed_return()] if analyses is None else analyses, DEBTS, business_entity=ENTITY, plan=plan)
    cockpit["operating_evidence"] = {"files": [], "business_name": ENTITY}
    cockpit["_operating_goal"] = ({"status": "set", "metric": "annual_noi", "amount": goal, "currency": "USD", "revision": 1, "updated_at": None} if goal
                                  else {"status": "awaiting_staff", "metric": "annual_noi", "amount": None, "currency": None, "revision": 0, "updated_at": None})
    return cockpit


def file_body(*, keep_design_statuses: bool = False) -> dict:
    """The design's plan on that file's own group keys. Nothing is approved unless asked: an approval needs a reviewer."""
    body = reference_body()
    for line in body["lines"]:
        line["group"] = GROUP_KEYS.get(line["group"], line["group"])
    body["source"]["confirmations"] = {GROUP_KEYS.get(key, key): value for key, value in body["source"]["confirmations"].items()}
    if not keep_design_statuses:
        for block in ("changes", "growth", "activities"):
            for row in body[block]:
                row["status"] = "proposed"
    return body
