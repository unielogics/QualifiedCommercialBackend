"""Independent money examples for the preview-only, grouped Step 3 comparison."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.schemas.earnings_improvement import empty_body, validate_body
from app.services import earnings_improvement as engine
from app.tests.test_earnings_improvement import NOW, SOURCE, context


def expense(category, current, **patch):
    return {"id": str(uuid4()), "category": category, "label": f"Example {category}",
        "current_amount": current, "cadence": "monthly", "source_file_ids": [SOURCE], **patch}


def action(row, proposed, **patch):
    return {"id": str(uuid4()), "kind": "expense_reduction", "label": row["label"],
        "expense_row_id": row["id"], "driver_key": row["id"], "current_amount": row["current_amount"],
        "proposed_amount": proposed, "cadence": row["cadence"], "source_file_ids": [SOURCE], **patch}


def body(*, rows=None, actions=None):
    rent = expense("rent", 1000, source_group_key="irs:2025:rents")
    staff = expense("salaries", 2500, source_group_key="irs:2025:wages", cost_basis="per_unit",
        unit_label="hour", current_units_per_period=100, current_unit_cost=20, current_fixed_amount=500)
    base = [rent, staff] if rows is None else rows
    proposed = [action(rent, 800), action(staff, None, cost_basis="per_unit", unit_label="hour",
        current_amount=None, current_units_per_period=100, current_unit_cost=20, current_fixed_amount=500,
        proposed_units_per_period=110, proposed_unit_cost=18, proposed_fixed_amount=500)] if actions is None else actions
    return validate_body({**empty_body(), "business_name": "Example LLC", "period_start": "2025-01-01",
        "period_end": "2025-12-31", "currency": "USD", "basis": "cash", "projection_start": "2026-01-01",
        "expenses": base, "adjustments": proposed,
        "category_confirmations": {"rent": "confirmed", "salaries": "confirmed"}})


def source_context():
    cockpit = context()
    cockpit["operating_evidence"]["guidance"] = {"expense_groups": [
        {"key": key, "category": category, "label": label, "baseline_annual_amount": annual,
         "currency": "USD", "basis": "cash", "year": 2025,
         "period_start": "2025-01-01", "period_end": "2025-12-31", "source_file_ids": [SOURCE]}
        for key, category, label, annual in [
            ("irs:2025:rents", "rent", "Example return - rents", 12000),
            ("irs:2025:wages", "salaries", "Example return - wages", 30000),
            ("irs:2025:advertising", "marketing", "Example return - advertising", 0),
        ]
    ]}
    return cockpit


def preview(raw, cockpit=None):
    return engine.evaluate(raw, cockpit or source_context(), now=NOW, include_expense_changes=True)


def test_grouped_arithmetic_is_live_before_metadata_but_does_not_fill_the_full_projection():
    raw = body()
    original = deepcopy(raw)
    result = preview(raw)
    comparison = result["expense_changes"]
    rows = {row["expense_row_id"]: row for row in comparison["rows"]}
    rent, staff = [rows[row["id"]] for row in raw["expenses"]]
    assert (rent["current_annual_cost"], rent["proposed_annual_cost"], rent["gross_annual_effect"]) == (12000, 9600, 2400)
    assert (staff["current_annual_cost"], staff["proposed_annual_cost"], staff["gross_annual_effect"]) == (30000, 29760, 240)
    assert comparison["summary"]["gross_annual_effect"] == 2640
    assert comparison["gross_projection"]["noi"] == -12360
    assert comparison["gross_projection"]["earnings"] == -7360
    assert comparison["gross_projection"]["dscr_current"] == pytest.approx(-7360 / 12000, abs=.01)
    assert result["projection"]["proposed"]["noi"] is None
    assert result["projection"]["approved"]["noi"] is None
    assert result["baseline"]["noi"] == -15000
    assert raw == original


def test_setup_costs_stay_separate_from_gross_cost_comparison_and_are_not_deducted_twice():
    raw = body()
    for row in raw["adjustments"]:
        row.update(effective_date="2026-01-01", explanation="Disclosed proposed contract change",
            source_note="Example documented current costs and proposed rates", implementation_cost=0)
    raw["adjustments"][0].update(implementation_cost=50, implementation_cost_cadence="monthly")
    result = preview(raw)
    assert result["expense_changes"]["summary"]["gross_annual_effect"] == 2640
    assert result["expense_changes"]["gross_projection"]["noi"] == -12360
    assert result["totals"]["proposed"]["annual_impact"] == 2040
    assert result["projection"]["proposed"]["noi"] == -12960


@pytest.mark.parametrize(("cadence", "current", "proposed"), [("monthly", 1000, 800), ("annual", 12000, 9600), ("quarterly", 3000, 2400)])
def test_same_annual_cost_change_is_independent_of_the_entered_frequency(cadence, current, proposed):
    row = expense("rent", current, cadence=cadence)
    comparison = preview(body(rows=[row], actions=[action(row, proposed)]))["expense_changes"]
    assert comparison["summary"]["gross_annual_effect"] == 2400
    assert comparison["gross_projection"]["noi"] == -12600


def test_an_explicit_new_zero_cost_line_can_offset_savings_without_rewriting_current_costs():
    raw = body()
    future = expense("marketing", 0, source_group_key="irs:2025:advertising",
        notes="User-disclosed new activity; zero current cost, not a reported tax line")
    raw["expenses"].append(future)
    raw["adjustments"].append(action(future, 220, kind="expense_increase"))
    comparison = preview(raw)["expense_changes"]
    assert comparison["summary"]["gross_annual_effect"] == 0
    assert comparison["gross_projection"]["noi"] == -15000
    new = next(row for row in comparison["rows"] if row["expense_row_id"] == future["id"])
    assert new["current_annual_cost"] == 0
    assert new["proposed_annual_cost"] == 2640
    assert future["current_amount"] == 0


@pytest.mark.parametrize("field", ["current_amount", "proposed_amount"])
def test_missing_amount_is_unknown_instead_of_zero_or_an_invented_full_saving(field):
    raw = body()
    raw["adjustments"][0][field] = None
    if field == "current_amount":
        raw["expenses"][0][field] = None
    comparison = preview(raw)["expense_changes"]
    first = comparison["rows"][0]
    assert first["gross_annual_effect"] is None
    assert comparison["summary"]["gross_annual_effect"] is None
    assert comparison["gross_projection"]["noi"] is None


def test_two_actions_for_one_current_line_are_visible_and_never_silently_selected():
    raw = body()
    raw["adjustments"].append({**raw["adjustments"][0], "id": str(uuid4()), "proposed_amount": 700})
    comparison = preview(raw)["expense_changes"]
    rent = next(row for row in comparison["rows"] if row["expense_row_id"] == raw["expenses"][0]["id"])
    assert len(rent["action_ids"]) == 2
    assert rent["gross_annual_effect"] is None
    assert comparison["gross_projection"]["noi"] is None


@pytest.mark.parametrize("category", ["loan_payments", "income_taxes", "owner_draws", "transfers"])
def test_a_changed_non_operating_line_never_creates_noi_savings(category):
    row = expense(category, 1000)
    comparison = preview(body(rows=[row], actions=[action(row, 500)]))["expense_changes"]
    assert comparison["rows"][0]["gross_annual_effect"] is None
    assert comparison["gross_projection"]["noi"] is None


@pytest.mark.parametrize("patch", [{"currency": "EUR"}, {"period_start": "2024-01-01", "period_end": "2024-12-31"}])
def test_incomparable_source_keeps_cost_math_but_never_combines_it_with_the_wrong_noi(patch):
    raw = {**body(), **patch}
    result = preview(raw)
    assert result["expense_changes"]["summary"]["gross_annual_effect"] == 2640
    assert result["expense_changes"]["gross_projection"]["noi"] is None
    assert result["expense_changes"]["gross_projection"]["dscr_current"] is None
    assert result["baseline"]["noi"] == -15000


def test_preview_opt_in_does_not_change_saved_summary_or_existing_projection_results():
    raw = body()
    regular, interactive = engine.evaluate(raw, source_context(), now=NOW), preview(raw)
    assert "expense_changes" not in regular
    assert {key: value for key, value in interactive.items() if key not in {"expense_changes", "draft_feedback"}} == regular
    assert engine.package_summary(interactive) == engine.package_summary(regular)


def test_future_setup_cost_is_not_charged_in_a_year_before_the_change_starts():
    row = expense("rent", 1000)
    proposed = action(row, 800, effective_date="2028-01-01", implementation_cost=600,
        explanation="Future contract renewal", source_note="Example future agreement")
    result = preview(body(rows=[row], actions=[proposed]))
    comparison = result["expense_changes"]["rows"][0]
    assert comparison["gross_annual_effect"] == 2400
    assert comparison["first_year_effect"] == 0
    assert result["actions"][0]["first_year_impact"] == 0


def test_valid_different_proposal_frequency_keeps_its_own_period_label():
    row = expense("rent", 1000)
    proposed = action(row, 9600, current_amount=12000, cadence="annual")
    comparison = preview(body(rows=[row], actions=[proposed]))["expense_changes"]["rows"][0]
    assert comparison["current_period_cost"] == 1000
    assert comparison["cadence"] == "monthly"
    assert comparison["proposed_period_cost"] == 9600
    assert comparison["proposed_cadence"] == "annual"
    assert comparison["gross_annual_effect"] == 2400


def test_inflated_current_disclosure_cannot_create_noi_profit_over_a_different_tax_source_cost():
    raw = body()
    raw["expenses"][0]["current_amount"] = 10000
    raw["adjustments"][0]["current_amount"] = 10000
    comparison = preview(raw)["expense_changes"]
    assert comparison["summary"]["gross_annual_effect"] == 110640
    assert comparison["gross_projection"]["noi"] is None
    assert comparison["gross_projection"]["earnings"] is None
    assert comparison["gross_projection"]["dscr_current"] is None
    assert comparison["source_reconciliation_issues"]
