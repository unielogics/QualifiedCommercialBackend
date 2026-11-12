"""Independent monetary acceptance cases for the source-to-plan workflow."""
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.schemas.earnings_improvement import validate_body
from app.services import earnings_improvement as engine
from app.services.operating_plan_commands import resolve
from app.tests.test_earnings_improvement import NOW, SOURCE, action, approve, body, context, expense


def uid(number):
    return str(UUID(int=number))


def command(target="total_cost", *, mode="percent", direction="decrease", value=10, allocation="none"):
    return dict(target=target, mode=mode, direction=direction, value=value, allocation=allocation)


def positive_context():
    cockpit = context()
    cockpit["ebitda"]["headline"].update(value=25000, annualized_value=25000)
    cockpit["noi"]["headline"].update(value=20000, annualized_value=20000)
    cockpit["dscr"]["headline"]["value"] = 2.08
    return cockpit


def pricing_plan(*, value=10, mode="percent", direction="increase"):
    return body(action(kind="price_change", expense_row_id=None, driver_key="all-existing-sales",
        price_basis="annual_revenue", command=command("annual_price_revenue", value=value, mode=mode, direction=direction)),
        historical_revenue_annual=100000)


def test_restaurant_ten_percent_price_change_flows_to_earnings_noi_and_dscr_without_units():
    raw, cockpit = pricing_plan(), positive_context()
    original = deepcopy((raw, cockpit))
    result = engine.evaluate(raw, cockpit, now=NOW, include_expense_changes=True)
    assert result["totals"]["proposed"]["annual_impact"] == 10000
    assert result["projection"]["proposed"]["earnings"] == 35000
    assert result["projection"]["proposed"]["noi"] == 30000
    assert result["projection"]["proposed"]["dscr_current"] == 2.92
    assert result["projection"]["proposed"]["dscr_with_refinance"] == 5.83
    assert (raw, cockpit) == original
    assert "volume" in " ".join(result["commands"][0]["assumptions"]).lower()


def test_price_change_and_rent_reduction_add_without_applying_a_sales_margin():
    raw = pricing_plan()
    raw["adjustments"].append(action(id=uid(90), command=command()))
    result = engine.evaluate(raw, positive_context(), now=NOW, include_expense_changes=True)
    assert result["totals"]["proposed"]["annual_impact"] == 11200
    assert result["projection"]["proposed"]["noi"] == 31200


@pytest.mark.parametrize("current", [100000, 0])
def test_annual_price_basis_without_explicit_command_cannot_bypass_source_reconciliation(current):
    raw = pricing_plan()
    raw["adjustments"][0].update(command=None, current_amount=current, proposed_amount=110000, cadence="annual")
    result = engine.evaluate(raw, positive_context(), now=NOW)
    assert not result["actions"][0]["proposed_math_valid"]
    assert result["projection"]["proposed"]["noi"] is None


@pytest.mark.parametrize("mode,direction,value,target,effect", [
    ("amount", "decrease", 100, 900, 1200),
    ("percent", "decrease", 10, 900, 1200),
    ("percent", "increase", 20, 1200, -2400),
    ("percent", "decrease", 100, 0, 12000),
    ("amount", "increase", 0, 1000, 0),
])
def test_monthly_dollars_percent_direction_and_explicit_zero(mode, direction, value, target, effect):
    raw = body(action(command=command(mode=mode, direction=direction, value=value)))
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["commands"][0]["proposed_value"] == target
    assert result["totals"]["proposed"]["annual_impact"] == effect


@pytest.mark.parametrize("mode,value", [("percent", 100.01), ("amount", 1000.01), ("amount", None)])
def test_invalid_or_cleared_commands_never_create_a_negative_or_zero_projection(mode, value):
    raw = body(action(command=command(mode=mode, value=value)))
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["commands"][0]["status"] == "incomplete"
    assert result["commands"][0]["proposed_value"] is None
    assert result["projection"]["proposed"]["noi"] is None
    assert result["totals"]["proposed"]["annual_impact"] is None


@pytest.mark.parametrize("target,mode,direction,value,quantity,rate,after,effect", [
    ("unit_quantity", "percent", "decrease", 20, 8, 100, 1300, 2400),
    ("unit_cost", "amount", "increase", 5, 10, 105, 1550, -600),
    ("unit_cost", "percent", "decrease", 10, 10, 90, 1400, 1200),
])
def test_consultation_quantity_and_rate_commands_keep_fixed_cost_once(target, mode, direction, value, quantity, rate, after, effect):
    row = expense(category="cogs", label="Consultations", current_amount=1500,
        cost_basis="per_unit", unit_label="consultation", current_units_per_period=10,
        current_unit_cost=100, current_fixed_amount=500)
    raw = body(action(command=command(target, mode=mode, direction=direction, value=value)), expenses=[row])
    resolved = resolve(raw)
    proposed = resolved["body"]["adjustments"][0]
    assert proposed["proposed_units_per_period"] == quantity
    assert proposed["proposed_unit_cost"] == rate
    assert proposed["proposed_fixed_amount"] == 500
    assert quantity * rate + proposed["proposed_fixed_amount"] == after
    assert engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)["totals"]["proposed"]["annual_impact"] == effect


@pytest.mark.parametrize("missing", ["current_units_per_period", "current_unit_cost", "current_fixed_amount"])
def test_unknown_unit_component_is_not_silently_filled(missing):
    row = expense(current_amount=None, cost_basis="per_unit", unit_label="consultation",
        current_units_per_period=10, current_unit_cost=100, current_fixed_amount=500)
    row[missing] = None
    raw = body(action(command=command("unit_cost")), expenses=[row])
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert not result["disclosure"]["complete"]
    assert result["projection"]["proposed"]["noi"] is None


def test_per_unit_price_amount_means_dollars_per_unit_not_annual_revenue():
    raw = body(action(kind="price_change", expense_row_id=None, current_unit_price=10,
        units_per_period=1000, cadence="annual", command=command("unit_price", mode="amount", direction="increase", value=2)))
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["commands"][0]["proposed_value"] == 12
    assert result["totals"]["proposed"]["annual_impact"] == 2000


def test_group_annual_budget_preserves_total_exactly_under_row_reordering():
    rows = [expense(id=uid(1), cadence="annual", current_amount=10000),
        expense(id=uid(2), cadence="annual", current_amount=20000),
        expense(id=uid(3), cadence="annual", current_amount=0)]
    raw = body(action(kind="expense_group_change", expense_row_id=None, expense_category="rent",
        command=command(mode="amount", value=1000, allocation="proportional")), expenses=rows)
    result = resolve(raw)["commands"][0]
    assert result["current_annual_cost"] == 30000
    assert result["proposed_annual_cost"] == 29000
    allocations = result["allocations"]
    assert sum(Decimal(str(row["proposed_annual_cost"])) for row in allocations) == Decimal("29000")
    assert sum(Decimal(str(row["gross_annual_effect"])) for row in allocations) == Decimal("1000")
    assert allocations[-1]["proposed_annual_cost"] == 0
    reordered = deepcopy(raw)
    reordered["expenses"].reverse()
    assert resolve(reordered)["commands"][0] == result
    assert all(row["current_units_per_period"] is None for row in raw["expenses"])


def split_plan():
    parent = expense(id=uid(11), current_amount=30000, cadence="annual", excluded=True, source_group_key="irs:rent")
    children = [expense(id=uid(12), current_amount=10000, cadence="annual", source_group_key="irs:rent", itemization_parent_id=parent["id"]),
        expense(id=uid(13), current_amount=20000, cadence="annual", source_group_key="irs:rent", itemization_parent_id=parent["id"])]
    return body(expenses=[parent, *children], adjustments=[])


def test_split_children_conserve_source_and_parent_is_counted_only_as_reference():
    raw = split_plan()
    assert resolve(raw)["itemization"][0]["remainder_annual_amount"] == 0
    assert engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)["disclosure"]["operating_monthly"] == 2500
    raw["expenses"][2]["current_amount"] = 19000
    assert resolve(raw)["itemization"][0]["remainder_annual_amount"] == 1000
    raw["expenses"][2]["current_amount"] = None
    assert resolve(raw)["itemization"][0]["remainder_annual_amount"] is None
    raw["expenses"][0]["excluded"] = False
    with pytest.raises(HTTPException):
        validate_body(raw)


def test_changed_parent_cannot_launder_a_new_irs_reference_total():
    raw, cockpit = split_plan(), context()
    raw["expenses"][0]["current_amount"] = 40000
    raw["expenses"][2]["current_amount"] = 30000
    cockpit["operating_evidence"]["guidance"] = {"expense_groups": [{"key": "irs:rent", "category": "rent",
        "baseline_annual_amount": 30000, "period_start": "2025-01-01", "period_end": "2025-12-31",
        "currency": "USD", "basis": "cash", "source_file_ids": [SOURCE], "year": "2025"}]}
    result = resolve(raw, cockpit)["itemization"][0]
    assert result["reference_annual_amount"] == 30000
    assert result["recorded_parent_annual_amount"] == 40000
    assert result["remainder_annual_amount"] == -10000
    assert result["status"] == "overallocated"
    assert not result["source_comparable"]


def test_raw_and_materialized_repeated_reads_do_not_compound_or_invalidate_same_review():
    raw = body(action(command=command()))
    approved = approve(raw)
    original = deepcopy(approved)
    for _ in range(3):
        approved = validate_body(resolve(approved)["body"])
        result = engine.evaluate(approved, context(), now=NOW, include_expense_changes=True)
        assert result["commands"][0]["proposed_value"] == 900
        assert result["actions"][0]["included_in_approved_projection"]
        assert result["projection"]["approved"]["noi"] == -13800
    changed = deepcopy(original)
    changed["adjustments"][0]["command"]["value"] = 20
    assert engine.evaluate(changed, context(), now=NOW, include_expense_changes=True)["actions"][0]["review_state"] == "stale"
    changed = deepcopy(original)
    changed["expenses"][0]["current_amount"] += 1
    assert engine.evaluate(changed, context(), now=NOW, include_expense_changes=True)["actions"][0]["review_state"] == "stale"


def test_documented_historical_correction_does_not_change_current_noi_or_future_impact():
    raw, cockpit = body(action(command=command())), context()
    before = engine.evaluate(raw, cockpit, now=NOW, include_expense_changes=True)
    raw["historical_corrections"] = [{"source_group_key": "irs:rent", "reason": "Confirm prior-year classification",
        "source_file_ids": [SOURCE], "evidence_note": "Compare lease and ledger"}]
    result = engine.evaluate(raw, cockpit, now=NOW, include_expense_changes=True)
    assert result["baseline"] == before["baseline"]
    assert result["projection"] == before["projection"]
    handoff = engine.package_summary(result)["preparer_handoff"]
    assert handoff["historical"]["baseline_unchanged"]
    assert handoff["historical"]["corrections_for_preparer_review"][0]["status"] == "unresolved_preparer_review"
    assert handoff["future"]["commands"][0]["gross_annual_effect"] == 1200
