from copy import deepcopy

import pytest

from app.services.loan_schedule import (
    build_schedule,
    effective_rate_pct,
    normalize_repayment_config,
    risk_profile,
    schedule_cash_flows,
)


def event(identifier, month, value, kind="amount"):
    return {"id": identifier, "month": month, "type": kind, "value": value}


def assert_conservation(schedule):
    assert schedule["principal_repaid"] == schedule["amount"]
    assert round(sum(row["principal"] for row in schedule["rows"]) + schedule["balloon"], 2) == schedule["amount"]
    assert round(sum(schedule_cash_flows(schedule)), 2) == schedule["total_paid"]
    assert schedule["total_paid"] == round(schedule["amount"] + schedule["total_interest"], 2)
    assert schedule["rows"][-1]["balance_after_cash"] == 0


def test_separate_amortization_horizon_creates_one_residual_at_maturity():
    schedule = build_schedule(100000, 0, 12, structure="amortizing", repayment_config={"amortization_months": 24})
    assert schedule["payment_after"] == 4166.67
    assert schedule["maturity_balloon"] == 49999.96
    assert schedule["rows"][-1]["cash_payment"] == 54166.63
    assert schedule["annual_debt_service"] == schedule["peak_rolling_12_month_debt_service"] == 100000
    assert schedule["coverage_annual_debt_service"] == 100000
    assert schedule["coverage_payment_monthly"] == 8333.33
    assert schedule["regular_coverage_payment_monthly"] == 4166.67
    assert_conservation(schedule)


def test_same_month_percentage_uses_balance_after_regular_payment_and_prior_balloon():
    config = {"balloons": [event("fixed", 3, 20000), event("half", 3, 50, "percent_balance")]}
    before = deepcopy(config)
    schedule = build_schedule(100000, 12, 12, structure="interest_only", repayment_config=config)
    assert config == before
    assert schedule["scheduled_balloons"][0]["balance_before"] == 100000
    assert schedule["scheduled_balloons"][1]["balance_before"] == 80000
    assert schedule["scheduled_balloons"][1]["amount"] == 40000
    assert schedule["rows"][2]["regular_payment"] == 1000 and schedule["rows"][2]["payment"] == 61000
    assert schedule["rows"][3]["regular_payment"] == 400
    assert schedule["total_interest"] == 6600 and schedule["total_paid"] == 106600
    assert schedule["maturity_balloon"] == 40000 and schedule["scheduled_balloons_total"] == 60000
    assert schedule["amortization_months"] is None
    assert effective_rate_pct(100000, schedule_cash_flows(schedule)) == 12
    assert effective_rate_pct(100000, [row["payment"] for row in schedule["rows"]], schedule["balloon"]) == 12
    assert_conservation(schedule)


def test_recast_lowers_future_regular_payment_and_keep_payment_accelerates_recovery():
    settings = {"amortization_months": 360, "balloons": [event("partial", 12, 10000)]}
    keep = build_schedule(100000, 6, 60, structure="amortizing", repayment_config=settings)
    recast = build_schedule(100000, 6, 60, structure="amortizing", repayment_config={**settings, "after_balloon": "recast"})
    assert keep["rows"][12]["regular_payment"] == keep["payment_after"]
    assert recast["rows"][12]["regular_payment"] < recast["payment_after"]
    assert recast["scheduled_balloons"][0]["recast_payment"] == recast["rows"][12]["regular_payment"]
    assert keep["balloon"] < recast["balloon"]
    assert keep["total_interest"] < recast["total_interest"]
    assert_conservation(keep)
    assert_conservation(recast)


def test_explicit_amortization_is_after_interest_only_period():
    schedule = build_schedule(1200, 0, 6, structure="amortizing", interest_only_months=2, repayment_config={"amortization_months": 4})
    assert [row["principal"] for row in schedule["rows"]] == [0, 0, 300, 300, 300, 300]
    assert schedule["weighted_average_life_months"] == 4.5 and schedule["full_exposure_months"] == 2
    assert_conservation(schedule)


def test_flat_simple_interest_is_not_silently_changed_to_declining_interest():
    schedule = build_schedule(1000, 12, 12, structure="simple_interest", repayment_config={"amortization_months": 24, "balloons": [event("partial", 3, 100)]})
    assert all(row["interest"] == 10 for row in schedule["rows"])
    assert schedule["total_interest"] == 120 and schedule["balloon"] == 399.96
    assert_conservation(schedule)


def test_full_early_payoff_stops_future_interest_and_has_no_second_maturity_payment():
    schedule = build_schedule(12000, 12, 12, structure="interest_only", repayment_config={"balloons": [event("all", 3, 100, "percent_balance")]})
    assert schedule["actual_payoff_month"] == 3 and schedule["periods"] == 3
    assert schedule["contractual_periods"] == 12 and schedule["balloon"] == 0
    assert schedule["total_interest"] == 360
    assert_conservation(schedule)


def test_maturity_scheduled_balloons_and_residual_are_not_counted_twice():
    schedule = build_schedule(100000, 0, 12, structure="interest_only", repayment_config={"balloons": [event("fixed", 12, 20000), event("half", 12, 50, "percent_balance")]})
    last = schedule["rows"][-1]
    assert last["payment"] == 60000 and last["maturity_balloon"] == 40000
    assert last["cash_payment"] == 100000 and last["total_principal"] == 100000
    assert schedule["total_paid"] == 100000 and schedule["peak_rolling_12_month_debt_service"] == 100000
    risk = risk_profile(schedule, income_monthly=5000)
    assert risk["lowest_cash_coverage"] == risk["lowest_coverage"] == .05
    assert risk["coverage_basis"] == "actual_cash_due_per_period"
    assert "whole principal stays out" not in risk["words"]
    assert "annual coverage uses peak rolling" in risk["words"]
    assert "what underwriting stands on" not in risk["words"]
    assert_conservation(schedule)


def test_peak_rolling_annual_debt_service_does_not_annualize_one_balloon_twelve_times():
    schedule = build_schedule(100000, 0, 24, structure="interest_only", repayment_config={"balloons": [event("first", 6, 10000), event("second", 18, 20000)]})
    assert schedule["first_year_actual_debt_service"] == 10000
    assert schedule["peak_rolling_12_month_debt_service"] == 90000
    assert schedule["coverage_annual_debt_service"] == 90000
    assert schedule["coverage_payment_monthly"] == 7500
    assert_conservation(schedule)


def test_weekly_mapping_and_long_daily_schedule_never_silently_truncate():
    weekly = build_schedule(10000, 10, 12, structure="amortizing", periods_per_year=52, repayment_config={"amortization_months": 24, "balloons": [event("month1", 1, 100)]})
    applied = weekly["scheduled_balloons"][0]
    assert applied["month"] == 1 and applied["period"] == 4 and applied["due_month"] == .92
    long = build_schedule(100000, 8, 600, structure="amortizing", periods_per_year=252, repayment_config={"amortization_months": 1200})
    legacy = build_schedule(100000, 8, 600, structure="amortizing", periods_per_year=252)
    assert long["periods"] == legacy["periods"] == 12600
    assert long["actual_payoff_month"] == legacy["actual_payoff_month"] == 600
    assert effective_rate_pct(100000, schedule_cash_flows(long), periods_per_year=252) == 8
    assert_conservation(long)


@pytest.mark.parametrize("config,kwargs", [
    ({"amortization_months": 360}, {"structure": "interest_only"}),
    ({"amortization_months": 360}, {"interest_only_months": 12}),
    ({"amortization_months": 1.5}, {}), ({"amortization_months": 1201}, {}),
    ({"after_balloon": "guess"}, {}), ({"unexpected": 1}, {}),
    ({"balloons": [event("bad", 13, 1)]}, {}), ({"balloons": [event("bad", 1, 101, "percent_balance")]}, {}),
    ({"balloons": [event("bad", 1, float("nan"))]}, {}), ({"balloons": [event("same", 1, 1), event("same", 2, 1)]}, {}),
    ({}, {"interest_only_months": .5}), ({}, {"periods_per_year": float("inf")}),
    ({}, {"periods_per_year": .5}),
])
def test_invalid_or_incompatible_configuration_is_rejected(config, kwargs):
    with pytest.raises(ValueError):
        normalize_repayment_config(config, 12, **kwargs)


def test_fixed_overpayment_and_unreachable_later_balloon_are_rejected():
    with pytest.raises(ValueError, match="exceeds remaining principal"):
        build_schedule(2400, 0, 12, repayment_config={"balloons": [event("too-big", 1, 2300)]})
    with pytest.raises(ValueError, match="already been repaid"):
        build_schedule(12000, 0, 12, structure="interest_only", repayment_config={"balloons": [event("full", 3, 100, "percent_balance"), event("later", 4, 100)]})


def test_legacy_omission_and_explicit_null_preserve_existing_basis_and_cash_totals():
    legacy = build_schedule(100000, 12, 12, structure="interest_only")
    assert legacy == build_schedule(100000, 12, 12, structure="interest_only", repayment_config=None)
    assert legacy["payment_io"] == 1000 and legacy["annual_debt_service"] == 12000
    assert legacy["coverage_annual_debt_service"] == 12000
    assert legacy["first_year_actual_debt_service"] == 112000
    assert legacy["total_paid"] == 112000 and legacy["coverage_basis"] == "legacy_regular_payment"
    assert_conservation(legacy)


def test_cashflow_helper_handles_null_projected_cash_fields():
    assert schedule_cash_flows({"cash_flows": [None], "rows": [{"payment": 10, "cash_payment": None}], "balloon": 100}) == [110]
    with pytest.raises(ValueError, match="at least one payment period"):
        normalize_repayment_config({}, 1, periods_per_year=1)


def test_zero_rate_early_payoff_and_recast_during_io_keep_the_right_horizon():
    early = build_schedule(10000, 0, 12, structure="interest_only", repayment_config={"balloons": [event("all", 2, 100, "percent_balance")]})
    assert early["total_paid"] == 10000 and early["total_interest"] == 0 and early["actual_payoff_month"] == 2
    during_io = build_schedule(100000, 6, 60, structure="amortizing", interest_only_months=12,
                               repayment_config={"amortization_months": 360, "after_balloon": "recast", "balloons": [event("during-io", 6, 20000)]})
    applied = during_io["scheduled_balloons"][0]
    assert applied["recast_effective_period"] == 13
    assert during_io["rows"][6]["regular_payment"] == 400
    assert during_io["rows"][12]["regular_payment"] == applied["recast_payment"]
    assert_conservation(early)
    assert_conservation(during_io)
