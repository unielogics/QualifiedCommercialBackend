"""Independent analytic cases for live draft arithmetic and its qualification boundary."""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.schemas.earnings_improvement import validate_body
from app.services import earnings_improvement as engine
from app.services import released_cash_flow
from app.tests.test_earnings_improvement import NOW, SOURCE, action, body, context, expense

KEY = "source:synthetic:rent"


def source_context(groups=None):
    current = context()
    current["_operating_goal"] = {
        "status": "set",
        "amount": 18000,
        "currency": "USD",
        "revision": 1,
    }
    groups = groups or [(KEY, "rent", 12000)]
    current["operating_evidence"]["guidance"] = {
        "reporting": {
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "currency": "USD",
            "basis": "cash",
        },
        "expense_groups": [
            {
                "key": key,
                "category": category,
                "label": category,
                "baseline_annual_amount": amount,
                "currency": "USD",
                "basis": "cash",
                "year": "2025",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "source_file_ids": [SOURCE],
            }
            for key, category, amount in groups
        ],
    }
    return current


def command(value=10, **extra):
    return {
        "target": "total_cost",
        "mode": "percent",
        "direction": "decrease",
        "value": value,
        "allocation": "none",
        **extra,
    }


def pending(**extra):
    return action(
        effective_date=None, implementation_cost=None, explanation="", source_note="", **extra
    )


def item(value=10, **extra):
    return body(pending(command=command(value), **extra), expenses=[expense(source_group_key=KEY)])


def group(value=10):
    parent = expense(source_group_key=KEY, excluded=True)
    leaves = [
        expense(
            id=str(uuid4()),
            label="Treatment space",
            current_amount=600,
            source_group_key=KEY,
            itemization_parent_id=parent["id"],
        ),
        expense(
            id=str(uuid4()),
            label="Administration space",
            current_amount=400,
            source_group_key=KEY,
            itemization_parent_id=parent["id"],
        ),
    ]
    change = pending(
        kind="expense_group_change",
        expense_row_id=None,
        expense_category="rent",
        source_group_key=KEY,
        command=command(value, allocation="proportional"),
    )
    return body(change, expenses=[parent, *leaves])


def evaluate(raw, current=None):
    return engine.evaluate(
        validate_body(raw), current or source_context(), now=NOW, include_expense_changes=True
    )


@pytest.mark.parametrize("make", [item, group])
def test_same_ten_percent_math_is_live_for_item_and_group_without_inventing_metadata(make):
    raw, current = make(), source_context()
    original, original_source = deepcopy(raw), deepcopy(current)
    result = evaluate(raw, current)
    feedback = result["draft_feedback"]
    row, summary = feedback["actions"][0], feedback["summary"]
    assert row["math_status"] == "calculated"
    assert (row["gross_annual_delta"], row["gross_monthly_run_rate_delta"]) == (1200, 100)
    assert row["first_year_delta"] is None and row["qualification_missing"]
    assert (
        summary["current_noi"],
        summary["draft_noi"],
        summary["draft_noi_delta"],
        summary["draft_gap"],
    ) == (-15000, -13800, 1200, 31800)
    assert (
        summary["qualified_noi_delta"] is None and summary["qualified_first_year_noi_delta"] is None
    )
    assert result["projection"]["proposed"]["noi"] is None
    assert not result["actions"][0]["included_in_approved_projection"]
    assert raw == original and current == original_source
    assert (
        raw["adjustments"][0]["implementation_cost"] is None
        and raw["adjustments"][0]["effective_date"] is None
    )


@pytest.mark.parametrize(
    "value,status,effect",
    [(0, "calculated", 0), (None, "incomplete", None), (101, "incomplete", None)],
)
def test_explicit_zero_unknown_and_impossible_negative_cost_are_distinct(value, status, effect):
    row = evaluate(item(value))["draft_feedback"]["actions"][0]
    assert row["math_status"] == status and row["gross_annual_delta"] == effect
    if value is None or value == 101:
        assert row["math_missing"]


def test_known_subtotal_does_not_turn_an_unknown_second_action_into_a_complete_noi():
    raw = item()
    software = expense(
        id=str(uuid4()),
        category="software",
        label="Software",
        current_amount=100,
        source_group_key="source:synthetic:software",
    )
    raw["expenses"].append(software)
    raw["adjustments"].append(
        pending(
            id=str(uuid4()),
            label="Software change",
            driver_key="software",
            expense_row_id=software["id"],
            current_amount=100,
            proposed_amount=None,
        )
    )
    result = evaluate(
        raw,
        source_context([(KEY, "rent", 12000), (software["source_group_key"], "software", 1200)]),
    )["draft_feedback"]
    assert result["summary"]["known_gross_annual_subtotal"] == 1200
    assert (
        result["summary"]["gross_annual_delta"] is None and result["summary"]["draft_noi"] is None
    )
    assert not result["summary"]["complete"] and result["summary"]["missing"]


def test_source_variance_retains_cost_arithmetic_but_blocks_whole_source_noi():
    raw = item()
    raw["expenses"][0]["current_amount"] = 900
    raw["adjustments"][0]["current_amount"] = 900
    raw["adjustments"][0]["command"] = command(100, mode="amount")
    feedback = evaluate(raw)["draft_feedback"]
    assert feedback["actions"][0]["gross_annual_delta"] == 1200
    assert feedback["actions"][0]["source_issues"] and feedback["actions"][0]["fix_fields"]
    assert feedback["summary"]["gross_annual_delta"] == 1200
    assert (
        feedback["summary"]["draft_noi"] is None and feedback["summary"]["draft_noi_delta"] is None
    )
    assert feedback["summary"]["status"] == "source_blocked"


@pytest.mark.parametrize("currency", ["CAD", ""])
def test_different_or_unknown_plan_currency_never_produces_a_converted_noi(currency):
    raw = item()
    raw["currency"] = currency
    summary = evaluate(raw)["draft_feedback"]["summary"]
    assert summary["draft_noi"] is None and summary["draft_noi_delta"] is None
    assert not summary["source_comparable"]


def test_two_changes_on_the_same_cost_do_not_double_count_known_savings():
    raw = item()
    extra = deepcopy(raw["adjustments"][0])
    extra["id"] = str(uuid4())
    raw["adjustments"].append(extra)
    feedback = evaluate(raw)["draft_feedback"]
    assert all(
        row["math_status"] == "conflict" and row["gross_annual_delta"] is None
        for row in feedback["actions"]
    )
    assert (
        feedback["summary"]["gross_annual_delta"] is None
        and feedback["summary"]["draft_noi"] is None
    )


@pytest.mark.parametrize("state", ["excluded", "rejected"])
def test_removed_or_rejected_history_is_not_a_live_change(state):
    raw = item()
    if state == "excluded":
        raw["adjustments"][0]["excluded"] = True
    else:
        raw["adjustments"][0]["status"] = "rejected"
    feedback = evaluate(raw)["draft_feedback"]
    assert feedback["actions"] == [] and feedback["summary"]["status"] == "no_changes"
    assert (
        feedback["summary"]["gross_annual_delta"] == 0
        and feedback["summary"]["draft_noi"] == -15000
    )


def test_explicit_timing_and_one_time_setup_separate_run_rate_and_first_year_deltas():
    raw = item()
    raw["adjustments"][0].update(
        implementation_cost=500,
        effective_date="2026-01-01",
        explanation="Synthetic renewal plan",
        source_note="Synthetic documented renewal",
    )
    result = evaluate(raw)
    feedback = result["draft_feedback"]
    assert feedback["actions"][0]["gross_annual_delta"] == 1200
    assert feedback["actions"][0]["net_annual_delta"] == 1200
    assert feedback["actions"][0]["first_year_delta"] == 700
    assert feedback["summary"]["qualified_noi_delta"] == 1200
    assert feedback["summary"]["qualified_first_year_noi_delta"] == 700
    raw["projection_start"] = None
    feedback = evaluate(raw)["draft_feedback"]
    assert feedback["actions"][0]["first_year_delta"] is None
    assert feedback["summary"]["qualified_first_year_noi_delta"] is None
    assert "projection_start" in feedback["actions"][0]["qualification_missing"]


def test_increase_is_signed_cost_growth_not_a_positive_profit_gain():
    raw = item()
    raw["adjustments"][0]["command"]["direction"] = "increase"
    feedback = evaluate(raw)["draft_feedback"]
    assert feedback["actions"][0]["gross_annual_delta"] == -1200
    assert (
        feedback["summary"]["draft_noi"] == -16200
        and feedback["summary"]["draft_noi_delta"] == -1200
    )


def test_annual_price_target_needs_no_invented_unit_count_and_keeps_qualification_separate():
    raw = body(
        pending(
            kind="price_change",
            expense_row_id=None,
            price_basis="annual_revenue",
            driver_key="menu-pricing",
            command=command(10, target="annual_price_revenue", direction="increase"),
        ),
        expenses=[expense(source_group_key=KEY)],
        historical_revenue_annual=100000,
    )
    feedback = evaluate(raw)["draft_feedback"]
    assert feedback["actions"][0]["gross_annual_delta"] == 10000
    assert feedback["actions"][0]["gross_monthly_run_rate_delta"] == 833.33
    assert (
        feedback["summary"]["draft_noi"] == -5000
        and feedback["summary"]["draft_noi_delta"] == 10000
    )
    assert feedback["summary"]["qualified_noi_delta"] is None
    assert raw["historical_units_annual"] is None


def test_volume_uses_known_variable_costs_once_and_keeps_fixed_costs_fixed():
    groups = [
        ("source:synthetic:cogs", "cogs", 30000),
        (KEY, "rent", 12000),
        ("source:synthetic:salary", "salaries", 40000),
    ]
    rows = [
        expense(
            id=str(uuid4()),
            category=category,
            label=category,
            current_amount=amount,
            cadence="annual",
            source_group_key=key,
            volume_behavior="variable"
            if category == "cogs"
            else "fixed"
            if category == "rent"
            else "mixed",
            current_fixed_amount=30000 if category == "salaries" else None,
        )
        for key, category, amount in groups
    ]
    raw = body(
        pending(
            kind="unit_volume",
            expense_row_id=None,
            driver_key="business_service_volume",
            current_amount=None,
            proposed_amount=None,
            cadence="annual",
            unit_change_mode="percent",
            unit_change_value=20,
        ),
        expenses=rows,
        unit_label="consultation",
        historical_units_annual=1000,
        historical_revenue_annual=100000,
        historical_units_period_start="2025-01-01",
        historical_units_period_end="2025-12-31",
    )
    feedback = evaluate(raw, source_context(groups))["draft_feedback"]
    assert feedback["actions"][0]["gross_annual_delta"] == 12000
    assert (
        feedback["summary"]["draft_noi"] == -3000
        and feedback["summary"]["draft_noi_delta"] == 12000
    )
    assert feedback["summary"]["qualified_noi_delta"] is None


def test_dscr_delta_uses_the_same_current_debt_basis_instead_of_unrelated_headline_ratio():
    current = source_context()
    current["dscr"]["headline"]["value"] = 999
    summary = evaluate(item(), current)["draft_feedback"]["summary"]
    assert (
        summary["dscr_current_debt_before"],
        summary["dscr_current_debt_after"],
        summary["dscr_current_debt_delta"],
    ) == (-0.8333, -0.7333, 0.10)
    current["debt"]["service"]["annual"] = None
    summary = evaluate(item(), current)["draft_feedback"]["summary"]
    assert all(
        summary[key] is None
        for key in [
            "dscr_current_debt_before",
            "dscr_current_debt_after",
            "dscr_current_debt_delta",
        ]
    )


def test_loan_cash_funding_never_adds_to_operating_effect_or_creates_an_expense():
    current = source_context()
    current["debt"]["service"].update(monthly=10000, annual=120000, currency="USD")
    current["plan"] = {
        "record": {
            "updated_at": "2026-11-12T12:00:00Z",
            "retire": ["synthetic-loan"],
            "overrides": {},
        },
        "evaluation": {
            "active": True,
            "retired_count": 1,
            "retired_ids": ["synthetic-loan"],
            "debt_service_after_regular_monthly": 6000,
            "annual_new_operating_expenses": 6000,
            "annual_new_financing_fees": 1200,
        },
    }
    raw = item()
    before = evaluate(raw, current)
    original_expenses = deepcopy(raw["expenses"])
    raw["released_cash_flow"] = {
        "source_fingerprint": released_cash_flow.source(current)["fingerprint"],
        "allocations": [
            {
                "id": str(uuid4()),
                "label": "Synthetic reserve",
                "purpose": "reserve",
                "amount_monthly": 3500,
            }
        ],
    }
    after = evaluate(raw, current)
    assert after["cash_bridge"]["recurring_cash_released_monthly"] == 3500
    assert after["draft_feedback"] == before["draft_feedback"]
    assert after["draft_feedback"]["summary"]["draft_noi_delta"] == 1200
    assert after["draft_feedback"]["summary"]["draft_noi"] == -13800
    assert raw["expenses"] == original_expenses


def test_qualified_ratios_separate_recurring_setup_and_explicit_first_year_timing():
    raw = item()
    raw["adjustments"][0].update(
        implementation_cost=12,
        implementation_cost_cadence="monthly",
        effective_date="2026-07-01",
        explanation="Synthetic July renewal",
        source_note="Synthetic dated renewal quote",
    )
    summary = evaluate(raw)["draft_feedback"]["summary"]
    assert summary["gross_annual_delta"] == 1200
    assert summary["qualified_noi_delta"] == 1056
    assert summary["qualified_first_year_noi_delta"] == 532.34
    assert summary["dscr_current_debt_before"] == -0.8333
    assert summary["qualified_dscr_current_debt_after"] == -0.7453
    assert summary["qualified_dscr_current_debt_delta"] == 0.0880
    assert summary["qualified_first_year_dscr_current_debt_after"] == -0.7890
    assert summary["qualified_first_year_dscr_current_debt_delta"] == 0.0444
    # The delta uses unrounded common-basis ratios, not rounded displayed values.
    assert summary["qualified_first_year_dscr_current_debt_delta"] != round(
        summary["qualified_first_year_dscr_current_debt_after"]
        - summary["dscr_current_debt_before"],
        4,
    )
