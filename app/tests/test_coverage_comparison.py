"""A loss ratio and improved payment affordability can coexist truthfully."""

from copy import deepcopy

import pytest

from app.services.coverage_comparison import coverage_comparison
from app.services.financial_cockpit import plan_brief
from app.services.financial_plan import evaluate_plan, normalize_plan, plan_impact


def review(income=-54369, before=210936, after=71736, *, comparable=True):
    return coverage_comparison(
        {"income_annual": income, "debt_service_annual": before},
        {"income_annual": income, "debt_service_annual": after},
        before_comparable=comparable,
        after_comparable=comparable,
    )


def evaluation(plan=None, *, income=-54369, monthly=17578.26, comparable=True):
    candidate = {"key": "tax_return", "label": "Tax return 2025", "numerator": income,
                 "annual_debt_service": monthly * 12 if monthly is not None else None,
                 "value": round(income / (monthly * 12), 2) if income is not None and monthly else None,
                 "currency_comparable": comparable}
    dscr = {"headline": candidate, "candidates": [candidate]}
    result = evaluate_plan(normalize_plan(plan), obligations_list=[
        {"id": "retire", "monthly": 11600.09, "payoff": 65644, "in_debt_service": True}],
        recurring={}, months=[], dscr=dscr, service={"monthly": monthly},
        proposed=None, requested_amount=None)
    return result, dscr


def test_body_good_loss_ratio_stays_signed_while_required_funding_falls():
    result, dscr = evaluation({"retire": ["retire"]})
    comparison = result["coverage_comparison"]
    assert result["headline_before"] == comparison["before"]["dscr"] == -.26
    assert result["headline_after"] == comparison["after"]["dscr"] == -.76
    assert comparison["before"]["cash_shortfall_annual"] == 265308.12
    assert comparison["after"]["cash_shortfall_annual"] == 126107.04
    assert comparison["debt_service_savings_annual"] == comparison["shortfall_reduction_annual"] == 139201.08
    assert comparison["cash_surplus_change_annual"] == 139201.08
    assert comparison["effect"] == "cash_relief"
    assert not comparison["ratio_delta_direction_meaningful"]
    assert result["income_before_annual"] == result["income_after_annual"] == -54369
    impact = plan_impact(result, dscr)
    assert impact["dscr"]["change"] == -.5
    assert impact["coverage_comparison"] == comparison
    brief = plan_brief({"plan": {"evaluation": result, "impact": impact}})
    assert brief["coverage_comparison"] == comparison
    assert "refinancing alone does not change EBITDA" in comparison["note"]


@pytest.mark.parametrize("income", [-24000, 0, 24000])
@pytest.mark.parametrize("after,expected", [(60000, "cash_relief"), (120000, "unchanged"), (180000, "cash_worsens")])
def test_cash_direction_follows_payment_burden_for_negative_zero_and_positive_earnings(income, after, expected):
    comparison = review(income, 120000, after)
    assert comparison["effect"] == expected
    assert comparison["debt_service_savings_annual"] == 120000 - after
    assert comparison["cash_surplus_change_annual"] == 120000 - after
    assert comparison["before"]["dscr"] == round(income / 120000, 2)
    assert comparison["after"]["dscr"] == round(income / after, 2)
    assert comparison["ratio_delta_direction_meaningful"] == (income > 0)
    if income == 0:
        assert "With zero earnings" in comparison["note"]
        assert "With negative earnings" not in comparison["note"]


@pytest.mark.parametrize("income,remaining_shortfall", [(-24000, 24000), (0, 0), (24000, 0)])
def test_complete_retirement_has_no_ratio_and_preserves_operating_loss(income, remaining_shortfall):
    comparison = review(income, 120000, 0)
    assert comparison["after"]["dscr"] is None
    assert comparison["after"]["income_annual"] == income
    assert comparison["after"]["cash_shortfall_annual"] == remaining_shortfall
    assert comparison["debt_service_savings_annual"] == 120000
    assert comparison["effect"] == "cash_relief"
    assert not comparison["ratio_delta_direction_meaningful"]


@pytest.mark.parametrize("income,before,after", [(None, 120000, 60000), (-24000, None, 60000), (-24000, 120000, None)])
def test_unknown_operands_do_not_invent_a_funding_comparison(income, before, after):
    comparison = review(income, before, after)
    assert comparison["cash_surplus_change_annual"] is None
    assert comparison["shortfall_reduction_annual"] is None
    assert comparison["effect"] == "unavailable"
    assert not comparison["ratio_delta_direction_meaningful"]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), True])
def test_nonfinite_or_boolean_inputs_cannot_be_coverage_operands(invalid):
    comparison = review(invalid, 120000, 60000)
    assert comparison["before"]["dscr"] is comparison["after"]["dscr"] is None
    assert comparison["cash_surplus_change_annual"] is None


def test_currency_guards_preserve_provisional_ratio_and_known_payment_relief_only():
    comparison = review(comparable=False)
    assert comparison["before"]["dscr"] == -.26 and comparison["after"]["dscr"] == -.76
    assert comparison["before"]["cash_surplus_annual"] is comparison["after"]["cash_shortfall_annual"] is None
    assert comparison["debt_service_savings_annual"] == 139200
    assert comparison["effect"] == "unavailable"
    assert "No currency conversion is assumed" in comparison["note"]
    assert not review(24000, comparable=False)["ratio_delta_direction_meaningful"]


def test_income_override_confirms_only_the_new_earnings_basis():
    result, _ = evaluation({"overrides": {"income_annual": 24000}}, comparable=False)
    comparison = result["coverage_comparison"]
    assert comparison["before"]["cash_shortfall_annual"] is None
    assert comparison["after"]["cash_shortfall_annual"] == 186939.12
    assert comparison["shortfall_reduction_annual"] is None
    assert comparison["effect"] == "unavailable"


def test_debt_override_compares_with_original_file_debt_service():
    result, _ = evaluation({"overrides": {"debt_service_monthly": 5000}})
    comparison = result["coverage_comparison"]
    assert comparison["before"]["debt_service_annual"] == 210939.12
    assert comparison["after"]["debt_service_annual"] == 60000
    assert comparison["debt_service_savings_annual"] == 150939.12


def test_unknown_replacement_pricing_cannot_claim_an_improved_funding_gap():
    result, _ = evaluation({"retire": ["retire"], "new_loan_amount": 89934,
                            "new_loan_term_months": 24, "new_loan_rate_pct": None,
                            "new_loan_repayment_config": {"amortization_months": 60}})
    comparison = result["coverage_comparison"]
    assert comparison["after"]["debt_service_annual"] is None
    assert comparison["after"]["cash_shortfall_annual"] is None
    assert comparison["effect"] == "unavailable"


def test_comparison_preserves_conservative_balloon_denominator_and_source_inputs():
    plan = {"retire": ["retire"], "new_loan_amount": 89934, "new_loan_rate_pct": 29.99,
            "new_loan_term_months": 24, "new_loan_interest_only_months": 6,
            "new_loan_structure": "amortizing", "new_loan_repayment_config": {"amortization_months": 60}}
    original = deepcopy(plan)
    result, _ = evaluation(plan)
    assert plan == original
    comparison = result["coverage_comparison"]
    assert comparison["after"]["debt_service_annual"] == 181773.59  # 5,978.17 retained + balloon-stress debt service.
    assert comparison["after"]["debt_service_annual"] != result["debt_service_after_regular_monthly"] * 12
    assert comparison["debt_service_savings_annual"] == 29165.53
    assert result["new_regular_payment_monthly"] == 2909.12
    assert result["new_payment_monthly"] == 9169.63
