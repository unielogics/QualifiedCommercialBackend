"""Starting a v2 plan from the file, and carrying a v1 plan over without losing anything."""
from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.schemas import earnings_improvement as v1_schema
from app.schemas import earnings_plan_v2 as schema
from app.services import earnings_improvement as v1
from app.services import earnings_plan_envelope as envelope
from app.services import earnings_plan_migrate as migrate
from app.tests.earnings_plan_fixture import file_cockpit
from app.tests.test_financial_baseline import filed_return


def test_a_new_plan_reproduces_the_file_and_adds_up_to_its_source():
    cockpit = file_cockpit()
    body = migrate.seed(cockpit, today=date(2026, 11, 11))
    assert schema.is_v2(body) and body["plan_start"] == "2027-01" and body["business_name"] == "Harbor Auto Detail LLC"
    assert [line["id"] for line in body["lines"]] == [row["key"] for row in cockpit["baseline"]["groups"]["rows"]]
    assert round(sum(line["base"] for line in body["lines"]), 2) == 601886 + 664891
    assert body["source"]["stamp"] == envelope.baseline_stamp(envelope.engine_baseline(cockpit)) and body["source"]["confirmations"] == {}
    result = v1.evaluate(body, cockpit)
    assert result["plan"]["model"]["reconciles"] is True and result["plan"]["source_changed"] is False
    assert result["comparison"]["proposed"]["earnings"] == result["comparison"]["baseline"]["earnings"] == -22377
    assert result["projection"]["proposed"]["status"] == "no_actions"
    # Ten source lines to confirm, two categories the return prints no line for, and the confirmation.
    assert [row["code"] for row in result["plan"]["to_finish"]] == ["confirm_source"] * 10 + ["answer_category"] * 2 + ["attest"]


def test_a_file_without_an_earnings_source_gets_an_empty_plan_not_an_error():
    body = migrate.seed(file_cockpit(analyses=[]), today=date(2026, 11, 11))
    assert body["lines"] == [] and body["streams"] == [] and body["source"]["stamp"] is None


def expense(label, category, amount, group, cadence="annual"):
    return {"id": str(uuid4()), "label": label, "category": category, "current_amount": amount, "cadence": cadence, "source_group_key": group}


def old_plan():
    rent_a, rent_b = expense("Bay 1 lease", "rent", 120000, "rent"), expense("Bay 2 lease", "rent", 76805, "rent")
    salaries = expense("Front desk and technicians", "salaries", 29016.17, "salaries_and_wages", "monthly")
    insurance_a, insurance_b = expense("Garage liability", "insurance", 15000, "insurance"), expense("Workers comp", "insurance", 5000, "insurance")
    vehicle = expense("Loaner van lease", "transportation", 650, "", "monthly")
    body = {**v1_schema.empty_body(), "business_name": "Harbor Auto Detail LLC", "period_start": "2025-01-01", "period_end": "2025-12-31", "currency": "USD",
            "basis": "accrual", "projection_start": "2027-02-01", "notes": "Agreed with the owner on Nov 3.", "client_attestation": True,
            "expenses": [rent_a, rent_b, salaries, insurance_a, insurance_b, vehicle],
            "source_group_confirmations": {"rent": "confirmed", "utilities": "needs_correction", "repairs": "needs_correction", "cogs": "confirmed"},
            "historical_corrections": [{"source_group_key": "utilities", "reason": "Includes a one-off repair to the compressor line."}],
            "category_confirmations": {"payroll_taxes": "confirmed", "transportation": "needs_help", "rent": "confirmed"}}
    body["adjustments"] = [
        {"id": str(uuid4()), "kind": "expense_reduction", "label": "Bay 2 lease amendment", "driver_key": "lease", "expense_row_id": rent_b["id"],
         "current_amount": 76805, "proposed_amount": 48000, "cadence": "annual", "effective_date": "2027-05-01", "explanation": "Amendment signed in October.",
         "owner_name": "Dana", "implementation_cost": 1200, "implementation_cost_cadence": "one_time", "status": "proposed"},
        {"id": str(uuid4()), "kind": "expense_reduction", "label": "Part-time front desk", "driver_key": "staff", "expense_row_id": salaries["id"],
         "command": {"target": "total_cost", "mode": "percent", "direction": "decrease", "value": 8}, "explanation": "Online booking.", "implementation_cost": 0},
        {"id": str(uuid4()), "kind": "expense_reduction", "label": "Second cut to the same lease", "driver_key": "lease-2", "expense_row_id": rent_b["id"],
         "current_amount": 76805, "proposed_amount": 70000, "cadence": "annual", "explanation": "Sublet the storage room.", "implementation_cost": 0},
        {"id": str(uuid4()), "kind": "marketing_investment", "label": "Google Ads", "driver_key": "ads", "product_name": "Detail package",
         "explanation": "Local search campaign.", "implementation_cost": 1500},
        {"id": str(uuid4()), "kind": "price_change", "label": "Price list 2027", "driver_key": "price", "price_basis": "annual_revenue",
         "command": {"target": "annual_price_revenue", "mode": "percent", "direction": "increase", "value": 3}, "explanation": "Competitors charge more.", "implementation_cost": 0},
        {"id": str(uuid4()), "kind": "expense_increase", "label": "Dropped idea", "driver_key": "x", "excluded": True, "explanation": "", "implementation_cost": 0},
    ]
    body["released_cash_flow"] = {"allocations": [{"id": str(uuid4()), "label": "Operating reserve", "amount_monthly": 10000, "purpose": "reserve"}]}
    return v1_schema.validate_body(body)


def test_a_v1_plan_carries_over_what_the_new_model_can_say_and_keeps_the_rest_for_re_entry():
    cockpit = file_cockpit()
    old = old_plan()
    body = migrate.v1_to_v2(old, cockpit, today=date(2026, 11, 11))
    assert schema.is_v2(body) and body["plan_start"] == "2027-02" and body["notes"] == "Agreed with the owner on Nov 3."
    # The rent itemization adds up to the return, so it replaces the single rent line; the insurance split does not, so that line stays whole.
    rent = [line for line in body["lines"] if line["group"] == "rent"]
    assert [(line["label"], line["base"]) for line in rent] == [("Bay 1 lease", 120000), ("Bay 2 lease", 76805)]
    assert [line["base"] for line in body["lines"] if line["group"] == "insurance"] == [22600]
    assert round(sum(line["base"] for line in body["lines"]), 2) == 601886 + 664891

    by_label = {v1_row["label"]: v1_row for v1_row in old["adjustments"]}
    changes = {change["id"]: change for change in body["changes"]}
    lease = changes[by_label["Bay 2 lease amendment"]["id"]]
    assert lease["target"]["line_id"] == old["expenses"][1]["id"] and (lease["direction"], lease["mode"], lease["value"]) == ("down", "amount", 28805)
    assert (lease["start_month"], lease["setup_cost"], lease["setup_cadence"], lease["note"], lease["owner"]) == (4, 1200, "one_time", "Amendment signed in October.", "Dana")
    desk = changes[by_label["Part-time front desk"]["id"]]
    assert desk["target"]["line_id"] == "salaries_and_wages" and (desk["direction"], desk["mode"], desk["value"]) == ("down", "percent", 8)
    price = changes[by_label["Price list 2027"]["id"]]
    assert price["target"]["kind"] == "pricing" and (price["direction"], price["mode"], price["value"]) == ("up", "percent", 3)
    assert all(change["status"] == "proposed" and change["reviewed_by_user_id"] is None for change in body["changes"])

    carried = {row["label"]: row for row in body["carried_over"]}
    assert set(carried) == {"Google Ads", "Second cut to the same lease", "Loaner van lease", "Itemization of Business insurance", "Correction to Repairs and maintenance"}
    assert "growth activity" in carried["Google Ads"]["detail"] and "Local search campaign." in carried["Google Ads"]["detail"]
    assert "one change per cost" in carried["Second cut to the same lease"]["detail"]
    assert carried["Loaner van lease"]["annual_amount"] == 7800
    # Every active v1 change is either a v2 change or waiting to be re-entered; the dropped idea stays dropped.
    active = [row for row in old["adjustments"] if not row["excluded"]]
    assert len(active) == len(body["changes"]) + sum(row["kind"] == "change" for row in body["carried_over"])

    assert body["source"]["confirmations"] == {"rent": "confirmed", "cogs": "confirmed", "utilities": "corrected"}
    assert body["source"]["corrections"]["utilities"]["reason"] == "Includes a one-off repair to the compressor line."
    assert body["category_answers"] == {"payroll_taxes": "included", "transportation": "help"}
    assert [(row["label"], row["purpose"], row["monthly_amount"]) for row in body["allocations"]] == [("Operating reserve", "reserve", 10000)]
    # The words changed meaning, so the business confirms the new plan itself.
    assert body["attestation"] is False

    result = v1.evaluate(body, cockpit)
    assert result["plan"]["model"]["reconciles"] is True
    # Lease -28,805, front desk -8% of 348,194, prices +3% of 1,236,000.
    assert result["totals"]["proposed"]["annual_impact"] == round(28805 + 348194 * 0.08 + 1236000 * 0.03, 2)


def test_carrying_over_never_fails_on_an_empty_plan_or_a_file_without_a_source():
    empty = migrate.v1_to_v2(v1_schema.empty_body(), file_cockpit())
    assert empty["changes"] == [] and empty["carried_over"] == [] and len(empty["lines"]) == 10
    orphan = migrate.v1_to_v2(old_plan(), file_cockpit(analyses=[]))
    # With no source there are no lines to attach to: every cost and cost change is kept for re-entry.
    # The price change needs no line, so it carries over and takes effect once the revenue model is described.
    assert orphan["lines"] == [] and [change["target"]["kind"] for change in orphan["changes"]] == ["pricing"]
    assert sum(row["kind"] == "expense" for row in orphan["carried_over"]) == 6 and sum(row["kind"] == "change" for row in orphan["carried_over"]) == 4


def test_resync_moves_single_lines_to_the_new_source_and_asks_for_confirmation_again():
    cockpit = file_cockpit()
    body = migrate.seed(cockpit)
    body["source"]["confirmations"] = {"rent": "confirmed"}
    amended = file_cockpit(analyses=[filed_return(rent=200000, total_deductions=668086, ordinary_business_income=-25572)])
    stale = v1.evaluate(body, amended)
    assert stale["plan"]["source_changed"] is True
    fresh = migrate.resync(body, amended)
    assert next(line["base"] for line in fresh["lines"] if line["id"] == "rent") == 200000
    assert fresh["source"]["confirmations"] == {} and fresh["source"]["stamp"] != body["source"]["stamp"]
    result = v1.evaluate(fresh, amended)
    assert result["plan"]["source_changed"] is False and all(row["status"] == "matches_source" for row in result["expense_groups"])
