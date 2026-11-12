"""Independent synthetic volume examples, distinct from verified source earnings."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.schemas.earnings_improvement import empty_body, validate_body
from app.services import earnings_improvement as earnings
from app.services.operating_unit_economics import evaluate
from app.tests.test_earnings_improvement import NOW, SOURCE, context


def row(category, total, behavior, **changes):
    return {"id": str(uuid4()), "category": category, "label": f"Fictional {category}",
        "current_amount": total, "cadence": "annual", "volume_behavior": behavior,
        "source_file_ids": [SOURCE], **changes}


def volume(mode="percent", change=20, **changes):
    return {"id": str(uuid4()), "kind": "unit_volume", "label": "Fictional activity change",
        "driver_key": "business_service_volume", "source_note": "Client-disclosed annual visits and explicit expense behavior",
        "unit_change_mode": mode, "unit_change_value": change, "cadence": "annual",
        "implementation_cost": 0, "effective_date": "2026-01-01",
        "explanation": "Client-disclosed service visits, unchanged prices and cost rates",
        "source_file_ids": [SOURCE], **changes}


def plan(*, mode="percent", change=20, actions=True, rows=None, **changes):
    expenses = rows if rows is not None else [
        row("cogs", 30000, "variable"), row("rent", 12000, "fixed"),
        row("salaries", 40000, "mixed", current_fixed_amount=30000),
    ]
    return validate_body({**empty_body(), "business_name": "Example LLC", "currency": "USD", "basis": "cash",
        "period_start": "2025-01-01", "period_end": "2025-12-31", "projection_start": "2026-01-01",
        "unit_label": "service visit", "historical_units_annual": 1000, "historical_revenue_annual": 100000,
        "historical_units_period_start": "2025-01-01", "historical_units_period_end": "2025-12-31",
        "expenses": expenses, "adjustments": [volume(mode, change)] if actions else [], **changes})


@pytest.mark.parametrize(("mode", "change"), [("absolute", 200), ("percent", 20)])
def test_more_visits_scale_only_variable_costs_and_retain_fixed_costs(mode, change):
    raw = plan(mode=mode, change=change)
    before = deepcopy(raw)
    result = evaluate(raw)
    assert result["net_annual_impact"] == 12000
    assert result["proposed_units_annual"] == 1200
    assert result["proposed_revenue_annual"] == 120000
    assert result["revenue_change_annual"] == 20000
    assert result["variable_cost_change_annual"] == 8000
    categories = {value["category"]: value for value in result["rows"]}
    assert categories["cogs"]["proposed_annual_amount"] == 36000
    assert categories["rent"]["proposed_annual_amount"] == 12000
    assert categories["rent"]["current_average_cost_per_unit"] == 12
    assert categories["rent"]["proposed_average_cost_per_unit"] == 10
    assert categories["salaries"]["current_variable_cost_per_unit"] == 10
    assert categories["salaries"]["proposed_annual_amount"] == 42000
    assert raw == before


def test_diagnostic_unit_header_alone_does_not_create_an_earnings_change():
    raw = plan(actions=False)
    result = evaluate(raw)
    assert result["status"] == "disclosed"
    assert result["current_revenue_per_unit"] == 100
    assert result["net_annual_impact"] is None
    calculated = earnings.evaluate(raw, context(), now=NOW)
    assert calculated["totals"]["proposed"]["annual_impact"] == 0
    assert calculated["baseline"]["earnings"] == -10000


def test_monthly_and_quarterly_totals_use_the_same_annual_denominator():
    a = evaluate(plan(rows=[row("cogs", 2500, "variable", cadence="monthly"), row("rent", 1000, "fixed", cadence="monthly")]))
    b = evaluate(plan(rows=[row("cogs", 7500, "variable", cadence="quarterly"), row("rent", 12000, "fixed")]))
    assert a["revenue_change_annual"] == b["revenue_change_annual"] == 20000
    assert a["variable_cost_change_annual"] == b["variable_cost_change_annual"] == 6000
    assert a["net_annual_impact"] == b["net_annual_impact"] == 14000


def test_row_specific_quantity_and_rate_are_not_scaled_or_added_twice():
    cost = row("cogs", 2500, "mixed", cadence="monthly", cost_basis="per_unit",
        unit_label="supply pack", current_units_per_period=100, current_unit_cost=20, current_fixed_amount=500)
    raw = plan(rows=[cost])
    result = evaluate(raw)
    detail = result["rows"][0]
    assert detail["current_annual_amount"] == 30000
    assert detail["current_average_cost_per_unit"] == 30
    assert detail["current_variable_cost_per_unit"] == 24
    assert detail["proposed_annual_amount"] == 34800
    assert result["net_annual_impact"] == 15200
    assert raw["expenses"][0]["current_units_per_period"] == 100


def test_explicit_zero_units_never_produces_a_fake_zero_cost_per_unit():
    disclosed = evaluate(plan(actions=False, historical_units_annual=0))
    assert disclosed["historical_units_annual"] == 0
    assert all(value["current_average_cost_per_unit"] is None for value in disclosed["rows"])
    modeled = evaluate(plan(mode="absolute", change=100, historical_units_annual=0))
    assert modeled["net_annual_impact"] is None
    assert "historical_units_annual.positive" in modeled["missing"]


@pytest.mark.parametrize("missing", ["behavior", "amount"])
def test_unconfirmed_behavior_or_amount_does_not_invent_a_complete_net_benefit(missing):
    raw = plan()
    if missing == "behavior":
        raw["expenses"][0]["volume_behavior"] = "unknown"
    else:
        raw["expenses"][0]["current_amount"] = None
    result = evaluate(raw)
    assert result["status"] == "incomplete"
    assert result["net_annual_impact"] is None
    assert any(raw["expenses"][0]["id"] in path for path in result["missing"])


def test_changed_reporting_period_retains_inputs_but_suppresses_stale_unit_results():
    raw = plan(historical_units_period_start="2024-01-01", historical_units_period_end="2024-12-31")
    result = evaluate(raw)
    assert raw["historical_units_annual"] == 1000
    assert result["net_annual_impact"] is None
    assert result["proposed_units_annual"] is None
    assert all(value["current_average_cost_per_unit"] is None for value in result["rows"])
    assert all(value["proposed_annual_amount"] is None and value["proposed_average_cost_per_unit"] is None for value in result["rows"])


@pytest.mark.parametrize("category", ["loan_payments", "income_taxes", "owner_draws", "transfers", "depreciation_amortization"])
def test_outside_earnings_rows_never_become_volume_earnings_adjustments(category):
    raw = plan()
    raw["expenses"].append(row(category, 12000, "variable"))
    result = evaluate(validate_body(raw))
    assert result["net_annual_impact"] == 12000
    outside = result["rows"][-1]
    assert not outside["operating"]
    assert outside["proposed_annual_amount"] == 12000


def test_one_time_disclosure_remains_one_time_and_does_not_block_recurring_volume():
    raw = plan()
    raw["expenses"].append(row("professional_fees", 2000, "unknown", cadence="one_time"))
    result = evaluate(validate_body(raw))
    assert result["net_annual_impact"] == 12000
    single = result["rows"][-1]
    assert single["one_time_amount"] == 2000
    assert single["current_annual_amount"] is None
    assert not single["recurring"]


def test_volume_delta_feeds_earnings_and_noi_without_rebuilding_the_historical_baseline():
    raw, cockpit = plan(), context()
    before = deepcopy(cockpit)
    result = earnings.evaluate(raw, cockpit, now=NOW)
    assert result["totals"]["proposed"]["annual_impact"] == 12000
    assert result["baseline"]["earnings"] == -10000
    assert result["baseline"]["noi"] == -15000
    assert result["projection"]["proposed"]["earnings"] == 2000
    assert result["projection"]["proposed"]["noi"] == -3000
    assert cockpit == before
