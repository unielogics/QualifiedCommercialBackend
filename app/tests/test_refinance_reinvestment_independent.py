"""Independent synthetic accounting, saved-loan cash and reinvestment oracles."""

from copy import deepcopy

import pytest

from app.services import earnings_improvement as engine
from app.services import released_cash_flow
from app.tests.test_earnings_improvement import (
    NOW,
    SOURCE,
    action,
    body,
    context,
    expense,
)

KEY = "source:synthetic:noncash"


def source_context():
    current = context()
    current["_operating_goal"] = {"status": "set", "metric": "annual_noi", "amount": 36000, "currency": "USD", "revision": 1}
    current["ebitda"]["headline"].update(
        kind="computed_ebitda", value=30000, annualized_value=30000,
        formula="net income + interest + income taxes + depreciation and amortization",
    )
    current["ebitda"]["potential"]["baseline"].update(
        revenue=120000, net_income=18000, interest=0, income_taxes=0,
        depreciation_and_amortization=12000,
    )
    current["noi"]["headline"].update(
        kind="business_operating_income", value=18000, annualized_value=18000,
        formula="revenue - cost of goods sold - operating expenses",
    )
    current["noi"]["definition"] = {
        "key": "business_operating_income",
        "includes_depreciation_and_amortization": True,
    }
    current["debt"]["service"].update(monthly=1000, annual=12000, currency="USD")
    current["dscr"]["headline"]["value"] = 2.5
    current["operating_evidence"]["guidance"] = {
        "reporting": {"period_start": "2025-01-01", "period_end": "2025-12-31", "currency": "USD", "basis": "cash"},
        "expense_groups": [{"key": KEY, "category": "depreciation_amortization", "label": "Noncash D&A",
            "baseline_annual_amount": 12000, "currency": "USD", "basis": "cash", "year": "2025",
            "period_start": "2025-01-01", "period_end": "2025-12-31", "source_file_ids": [SOURCE]}],
    }
    return current


def accounting_change(value=6000, **patch):
    disclosed = expense(category="depreciation_amortization", label="Noncash D&A",
        current_amount=12000, cadence="annual", source_group_key=KEY)
    planned = action(current_amount=12000, proposed_amount=6000, cadence="annual",
        command={"target": "total_cost", "mode": "amount", "direction": "decrease", "value": value, "allocation": "none"},
        **patch)
    return body(planned, expenses=[disclosed])


def saved_loan():
    current = source_context()
    current["debt"]["service"].update(monthly=10000, annual=120000, currency="USD")
    current["plan"] = {
        "record": {"updated_at": "2026-11-12T12:00:00Z", "retire": ["synthetic-old-loan"], "overrides": {}},
        "evaluation": {"active": True, "retired_count": 1, "retired_ids": ["synthetic-old-loan"],
            "debt_service_after_regular_monthly": 6000, "annual_new_operating_expenses": 6000,
            "annual_new_financing_fees": 1200, "ebitda": {}},
    }
    return current


def evaluate(raw, current=None):
    return engine.evaluate(raw, current or source_context(), now=NOW, include_expense_changes=True)


def acknowledge(raw, current):
    raw = deepcopy(raw)
    raw["released_cash_flow"] = {"source_fingerprint": released_cash_flow.source(current)["fingerprint"], "allocations": []}
    return raw


def test_saved_loan_cash_is_known_even_when_the_operating_action_is_unknown():
    current = saved_loan()
    raw = acknowledge(accounting_change(None, effective_date=None, implementation_cost=None), current)
    result = evaluate(raw, current)
    cash = result["cash_bridge"]
    assert cash["available"] is True
    assert cash["before_debt_service_monthly"] == 10000
    assert cash["after_debt_service_monthly"] == 6000
    assert cash["gross_loan_relief_monthly"] == 4000
    assert cash["new_operating_charges_monthly"] == 500
    assert cash["new_financing_fees_monthly"] == 100
    assert cash["net_loan_cash_change_monthly"] == 3500
    assert cash["recurring_cash_released_monthly"] == 3500
    assert cash["modeled_operations_monthly"] is None
    assert cash["combined_incremental_monthly"] is None
    assert result["baseline"]["noi"] == 18000
    assert result["baseline"]["earnings"] == 30000


@pytest.mark.parametrize("principal", [50000, 200000])
def test_principal_neither_creates_revenue_nor_changes_identical_payment_relief(principal):
    current = saved_loan()
    current["plan"]["evaluation"]["loan_terms"] = {"amount": principal}
    raw = acknowledge(body(expenses=[], adjustments=[]), current)
    result = evaluate(raw, current)
    assert result["baseline"]["revenue"] == 120000
    assert result["baseline"]["noi"] == 18000
    assert result["baseline"]["earnings"] == 30000
    assert result["cash_bridge"]["net_loan_cash_change_monthly"] == 3500
    assert result["cash_bridge"]["combined_incremental_monthly"] == 3500


@pytest.mark.parametrize("financing", [0, 1200, 3600])
def test_financing_charge_already_inside_regular_payment_is_not_subtracted_twice(financing):
    current = saved_loan()
    current["plan"]["evaluation"]["annual_new_financing_fees"] = financing
    result = evaluate(acknowledge(body(expenses=[], adjustments=[]), current), current)
    assert result["cash_bridge"]["net_loan_cash_change_monthly"] == 3500
    assert result["cash_bridge"]["new_financing_fees_monthly"] == financing / 12


@pytest.mark.parametrize("field", ["annual_new_operating_expenses", "annual_new_financing_fees"])
def test_missing_real_charges_do_not_turn_into_zero_or_an_available_capacity(field):
    current = saved_loan()
    current["plan"]["evaluation"][field] = None
    cash = evaluate(acknowledge(body(expenses=[], adjustments=[]), current), current)["cash_bridge"]
    assert cash["before_debt_service_monthly"] == 10000
    assert cash["after_debt_service_monthly"] == 6000
    assert cash["gross_loan_relief_monthly"] == 4000
    assert cash["available"] is False
    assert cash["net_loan_cash_change_monthly"] is None
    assert cash["recurring_cash_released_monthly"] is None


def test_retaining_cash_changes_cash_assignments_not_operating_expenses_or_income():
    current = saved_loan()
    raw = acknowledge(body(expenses=[], adjustments=[]), current)
    retained = deepcopy(raw)
    retained["released_cash_flow"]["allocations"] = [{"id": "70000000-0000-4000-8000-000000000001",
        "label": "Retain recurring cash", "purpose": "reserve", "amount_monthly": 3500}]
    before, after = evaluate(raw, current), evaluate(retained, current)
    assert after["cash_bridge"]["allocated_monthly"] == 3500
    assert after["cash_bridge"]["unallocated_monthly"] == 0
    assert after["baseline"] == before["baseline"]
    assert after["projection"] == before["projection"]
    assert after["draft_feedback"] == before["draft_feedback"]
    level = after["draft_feedback"]["summary"]["working"]
    assert level["cash_after_debt_basis"] == "earnings_less_debt_service_illustration"
    assert level["cash_after_debt_mode"] == "saved_refinance_and_operations"
    assert level["cash_after_debt_before_annual"] == -90000
    assert level["cash_after_debt_after_annual"] == -48000
    assert level["cash_after_debt_delta_annual"] == 42000
    assert level["cash_after_debt_before_monthly"] == -7500
    assert level["cash_after_debt_after_monthly"] == -4000
    assert level["cash_after_debt_delta_monthly"] == 3500
    assert raw["expenses"] == retained["expenses"] == []


def test_overallocation_is_explicit_and_does_not_invent_an_extra_expense():
    current = saved_loan()
    raw = acknowledge(body(expenses=[], adjustments=[]), current)
    raw["released_cash_flow"]["allocations"] = [{"id": "70000000-0000-4000-8000-000000000002",
        "label": "Retain cash", "purpose": "reserve", "amount_monthly": 3501}]
    result = evaluate(raw, current)
    assert result["cash_bridge"]["unallocated_monthly"] == -1
    assert result["cash_bridge"]["complete"] is False
    assert "released_cash_flow.allocations.capacity_exceeded" in result["cash_bridge"]["missing"]
    assert result["baseline"]["noi"] == 18000
    assert result["disclosure"]["active_count"] == 0


@pytest.mark.parametrize("direction,change,after_noi,noi_delta", [
    ("decrease", 6000, 24000, 6000),
    ("increase", 6000, 12000, -6000),
    ("decrease", 0, 18000, 0),
])
def test_noncash_change_moves_source_accounting_noi_but_not_ebitda_coverage_or_cash(direction, change, after_noi, noi_delta):
    raw = accounting_change(change)
    raw["adjustments"][0]["command"]["direction"] = direction
    result = evaluate(raw)
    action_result = result["draft_feedback"]["actions"][0]
    summary = result["draft_feedback"]["summary"]
    assert action_result["math_status"] == "calculated"
    assert action_result["noi_gross_annual_delta"] == noi_delta
    assert action_result["earnings_gross_annual_delta"] == 0
    assert action_result["cash_gross_annual_delta"] == 0
    assert summary["draft_noi"] == after_noi
    assert summary["gross_earnings_annual_delta"] == 0
    assert summary["current_earnings"] == 30000
    assert summary["draft_earnings"] == 30000
    assert summary["draft_earnings_delta"] == 0
    assert summary["gross_cash_annual_delta"] == 0
    assert summary["dscr_current_debt_before"] == 2.5
    assert summary["dscr_current_debt_after"] == 2.5
    assert summary["dscr_current_debt_delta"] == 0
    assert result["projection"]["proposed"]["noi"] == after_noi
    assert result["projection"]["proposed"]["earnings"] == 30000
    assert result["baseline"]["noi"] == 18000
    assert result["baseline"]["earnings"] == 30000


@pytest.mark.parametrize("cadence,cost,annual_noi,annual_earnings,first_noi,first_earnings,net_cash,first_cash", [
    ("monthly", 25, 23700, 29700, 23700, 29700, -300, -300),
    ("one_time", 600, 24000, 30000, 23400, 29400, 0, -600),
])
def test_real_setup_cost_reduces_each_relevant_metric_once(cadence, cost, annual_noi, annual_earnings, first_noi, first_earnings, net_cash, first_cash):
    raw = accounting_change(6000, implementation_cost=cost, implementation_cost_cadence=cadence)
    result = evaluate(raw)
    proposed = result["projection"]["proposed"]
    summary = result["draft_feedback"]["summary"]
    assert proposed["noi"] == annual_noi
    assert proposed["earnings"] == annual_earnings
    assert proposed["first_year_noi"] == first_noi
    assert proposed["first_year_earnings"] == first_earnings
    assert summary["net_cash_annual_delta"] == net_cash
    assert summary["first_year_cash_delta"] == first_cash
    assert result["draft_feedback"]["actions"][0]["cash_net_annual_delta"] == net_cash


@pytest.mark.parametrize("metric", ["noi", "earnings", "both"])
def test_unknown_metric_basis_does_not_borrow_the_other_metrics_result(metric):
    current = source_context()
    if metric in {"noi", "both"}:
        current["noi"]["headline"]["kind"] = "custom_metric"
    if metric in {"earnings", "both"}:
        current["ebitda"]["headline"]["kind"] = "custom_metric"
    result = evaluate(accounting_change(), current)
    row = result["draft_feedback"]["actions"][0]
    if metric in {"noi", "both"}:
        assert row["noi_gross_annual_delta"] is None
        assert row["metric_issues"]["noi"]
        assert result["draft_feedback"]["summary"]["draft_noi"] is None
    else:
        assert row["noi_gross_annual_delta"] == 6000
    if metric in {"earnings", "both"}:
        assert row["earnings_gross_annual_delta"] is None
        assert row["metric_issues"]["earnings"]
        assert result["draft_feedback"]["summary"]["dscr_current_debt_after"] is None
    else:
        assert row["earnings_gross_annual_delta"] == 0
    assert row["cash_gross_annual_delta"] == 0


@pytest.mark.parametrize("mutation", ["unaccepted", "different_period", "variance"])
def test_da_source_reconciliation_remains_required_without_masking_saved_loan_cash(mutation):
    current = saved_loan()
    if mutation == "unaccepted":
        current["operating_evidence"]["files"][0]["eligible_for_review"] = False
    elif mutation == "different_period":
        current["operating_evidence"]["guidance"]["expense_groups"][0]["period_end"] = "2024-12-31"
    else:
        current["operating_evidence"]["guidance"]["expense_groups"][0]["baseline_annual_amount"] = 14000
    result = evaluate(acknowledge(accounting_change(), current), current)
    assert result["draft_feedback"]["summary"]["draft_noi"] is None
    assert result["draft_feedback"]["actions"][0]["noi_gross_annual_delta"] is None
    assert result["cash_bridge"]["net_loan_cash_change_monthly"] == 3500


def test_saved_loan_monthly_and_annual_values_remain_independent_of_stale_allocation_acknowledgement():
    current = saved_loan()
    raw = acknowledge(accounting_change(None), current)
    raw["released_cash_flow"]["source_fingerprint"] = "a" * 64
    raw["released_cash_flow"]["allocations"] = [{"id": "70000000-0000-4000-8000-000000000002",
        "label": "Retain cash", "purpose": "reserve", "amount_monthly": 100}]
    cash = evaluate(raw, current)["cash_bridge"]
    assert cash["stale"] is True
    assert cash["loan_status"] == "calculated"
    assert cash["loan_missing"] == []
    assert cash["before_debt_service_annual"] == 120000
    assert cash["after_debt_service_annual"] == 72000
    assert cash["gross_loan_relief_annual"] == 48000
    assert cash["new_operating_charges_annual"] == 6000
    assert cash["new_financing_fees_annual"] == 1200
    assert cash["net_loan_cash_change_annual"] == 42000
    assert cash["recurring_cash_released_annual"] == 42000
    assert cash["modeled_operations_annual"] is None
    assert cash["combined_incremental_annual"] is None


def test_funding_growth_does_not_count_the_same_spend_twice_or_turn_loan_relief_into_income():
    current = saved_loan()
    marketing_id = "70000000-0000-4000-8000-000000000010"
    marketing_key = "source:synthetic:marketing"
    current["operating_evidence"]["guidance"]["expense_groups"].append({
        **current["operating_evidence"]["guidance"]["expense_groups"][0],
        "key": marketing_key, "category": "marketing", "label": "Disclosed marketing", "baseline_annual_amount": 1200,
    })
    cost = expense(id=marketing_id, category="marketing", label="Disclosed marketing",
        current_amount=100, source_group_key=marketing_key)
    growth = action(id="70000000-0000-4000-8000-000000000011", kind="marketing_investment",
        label="Proposed documented campaign", expense_row_id=marketing_id, current_amount=100,
        proposed_amount=150, revenue_return_multiple=3, contribution_margin_pct=50,
        product_name="Synthetic consultation service", explanation="Explicit conditional campaign assumptions",
        source_note="Validate campaign demand and contribution margin against the linked source")
    raw = acknowledge(body(growth, expenses=[cost]), current)
    result = evaluate(raw, current)
    assert result["actions"][0]["calculation"]["incremental_revenue"] == 150
    assert result["actions"][0]["calculation"]["incremental_contribution"] == 75
    assert result["projection"]["proposed"]["noi"] == 18300
    assert result["projection"]["proposed"]["earnings"] == 30300
    assert result["cash_bridge"]["net_loan_cash_change_monthly"] == 3500
    assert result["cash_bridge"]["modeled_operations_monthly"] == 25
    assert result["cash_bridge"]["combined_incremental_monthly"] == 3525
    assert result["cash_bridge"]["combined_incremental_annual"] == 42300
    funded = deepcopy(raw)
    funded["released_cash_flow"]["allocations"] = [
        {"id": "70000000-0000-4000-8000-000000000012", "label": "Fund the proposed increment",
            "purpose": "planned_increment", "expense_row_id": marketing_id, "action_id": growth["id"], "amount_monthly": 50},
        {"id": "70000000-0000-4000-8000-000000000013", "label": "Retain the balance", "purpose": "reserve", "amount_monthly": 3450},
    ]
    after = evaluate(funded, current)
    assert after["cash_bridge"]["allocations"][0]["capacity_monthly"] == 50
    assert after["cash_bridge"]["unallocated_monthly"] == 0
    assert after["cash_bridge"]["combined_incremental_monthly"] == 3525
    assert after["projection"] == result["projection"]
    assert after["draft_feedback"] == result["draft_feedback"]
    assert funded["expenses"] == raw["expenses"]
    assert after["projection"]["approved"]["noi"] is None


@pytest.mark.parametrize("blocker", ["principal", "payments", "currency"])
def test_genuine_saved_loan_blocker_keeps_net_capacity_unavailable_with_direct_reason(blocker):
    current = saved_loan()
    if blocker == "principal":
        current["plan"]["evaluation"]["principal_solution"] = {"status": "calculated", "requires_apply": True}
    elif blocker == "payments":
        current["plan"]["evaluation"]["debt_service_after_regular_monthly"] = None
    else:
        current["debt"]["service"]["currency"] = "EUR"
    result = evaluate(acknowledge(body(expenses=[], adjustments=[]), current), current)
    cash = result["cash_bridge"]
    assert cash["available"] is False
    assert cash["loan_status"] in {"incomplete", "source_blocked"}
    assert cash["loan_missing"]
    assert cash["loan_fix_fields"]
    assert cash["net_loan_cash_change_monthly"] is None
    assert cash["net_loan_cash_change_annual"] is None
    assert cash["recurring_cash_released_monthly"] is None
    assert cash["combined_incremental_monthly"] is None


@pytest.mark.parametrize("explicit_currency,expected_currency", [(None, "USD"), ("EUR", "EUR")])
def test_actual_accepted_first_party_schedule_propagates_its_denomination_into_the_engine(explicit_currency, expected_currency):
    from app.services.financial_cockpit import build_cockpit

    schedule = {"schema_version": "qc_ds.v2", "business_name": "Example LLC", "debts": [{
        "id": "70000000-0000-4000-8000-000000000030", "lender": "Synthetic lender",
        "balance": 100000, "monthly_payment": 10000, "status": "active", "count_in_dscr": True,
    }]}
    if explicit_currency:
        schedule["currency"] = explicit_currency
    actual = build_cockpit([], [], business_entity="Example LLC", accepted_schedule={
        "body": schedule, "key_facts": {}, "version_id": "70000000-0000-4000-8000-000000000031",
        "source": "form", "status": "filed", "schema_version": "qc_ds.v2",
    })
    service = actual["debt"]["service"]
    assert service["schedule_authoritative"] is True
    assert service["source"] == "accepted_debt_schedule"
    assert service["monthly"] == 10000
    assert service["annual"] == 120000
    assert service["currency"] == expected_currency
    if not explicit_currency:
        assert service["currency_basis"] == "first_party_qc_ds_v2_usd"
    current = source_context()
    # The entire authoritative debt model is real; no service currency is injected.
    current["debt"] = actual["debt"]
    current["plan"] = saved_loan()["plan"]
    cash = evaluate(acknowledge(accounting_change(None), current), current)["cash_bridge"]
    assert cash["before_debt_service_monthly"] == 10000
    assert cash["after_debt_service_monthly"] == 6000
    assert cash["net_loan_cash_change_monthly"] == (3500 if expected_currency == "USD" else None)
    assert cash["available"] is (expected_currency == "USD")


def test_working_after_setup_is_known_before_prose_or_dates_without_claiming_a_qualified_forecast():
    raw = accounting_change(6000, implementation_cost=25, implementation_cost_cadence="monthly",
        effective_date=None, explanation="", source_note="")
    raw["projection_start"] = None
    result = evaluate(raw)
    working = result["draft_feedback"]["summary"]["working"]
    assert working["basis"] == "after_setup_before_timing"
    assert working["noi"] == 23700
    assert working["noi_delta"] == 5700
    assert working["goal_gap"] == 12300
    assert working["earnings"] == 29700
    assert working["earnings_delta"] == -300
    assert working["cash_delta_annual"] == -300
    assert working["cash_delta_monthly"] == -25
    assert working["dscr_current_debt_before"] == 2.5
    assert working["dscr_current_debt_after"] == 2.475
    assert working["dscr_current_debt_delta"] == -0.025
    for field in ("first_year_noi", "first_year_noi_delta", "first_year_earnings", "first_year_cash_delta", "first_year_dscr_current_debt_after"):
        assert working[field] is None
    assert result["projection"]["proposed"]["noi"] is None
    assert result["projection"]["approved"]["noi"] is None
