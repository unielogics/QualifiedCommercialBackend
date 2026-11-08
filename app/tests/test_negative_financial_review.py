"""Signed earnings remain factual; cash shortfalls explain negative-ratio plans."""

from copy import deepcopy

import pytest

from app.services import financial_plan as plans
from app.services.financial_cockpit import dscr_block, proposed_terms_block
from app.services.financial_metric_reporting import validated_annual_pair
from app.services.loan_schedule import build_schedule, risk_profile, schedule_cash_flows
from app.tests.test_financial_metric_review import pnl
from app.tests.test_financial_plan_earnings import planned, projection


def basis(income=-24000, *, comparable=True):
    candidate = {"key": "profit_and_loss", "label": "Reviewed P&L", "numerator": income,
                 "value": round(income / 120000, 2) if income is not None else None,
                 "currency_comparable": comparable}
    return {"headline": candidate, "candidates": [candidate]}


def evaluate(plan=None, *, income=-24000, monthly=10000, comparable=True):
    return plans.evaluate_plan(plans.normalize_plan(plan), obligations_list=[
        {"id": "old", "lender": "Existing advance", "monthly": 6000, "payoff": 30000,
         "in_debt_service": True, "is_mca": True}], recurring={}, months=[],
        dscr=basis(income, comparable=comparable), service={"monthly": monthly},
        proposed=None, requested_amount=None)


@pytest.mark.parametrize("income", [-24000, 0, 24000])
def test_refinance_preserves_signed_earnings_and_divides_by_known_positive_debt(income):
    current = evaluate(income=income)
    refinance = evaluate({"retire": ["old"], "new_payment_monthly": 1000}, income=income)
    assert current["income_after_annual"] == refinance["income_after_annual"] == income
    assert current["headline_after"] == round(income / 120000, 2)
    assert refinance["debt_service_after_monthly"] == 5000
    assert refinance["headline_after"] == round(income / 60000, 2)
    if income < 0:
        assert refinance["headline_after"] < current["headline_after"]
        assert refinance["headline_after_status"] == "concerning"


@pytest.mark.parametrize("income,monthly", [(None, 10000), (-24000, None), (0, None)])
def test_unknown_operands_do_not_become_zero_coverage(income, monthly):
    result = evaluate(income=income, monthly=monthly)
    assert result["headline_after"] is None
    assert not result["no_debt_left"]


@pytest.mark.parametrize("income", [-1000, 0, 1000])
def test_risk_keeps_every_signed_period_and_finds_balloon_funding_shortfall(income):
    schedule = build_schedule(10000, 12, 12, structure="interest_only", repayment_config={"balloons": []})
    risk = risk_profile(schedule, income_monthly=income, existing_service_monthly=200)
    assert risk["income_monthly"] == income
    assert len(risk["coverage_by_period"]) == len(risk["cash_coverage_by_period"]) == 12
    cash = schedule_cash_flows(schedule)
    expected = [round(income / (200 + payment), 2) for payment in cash]
    assert [row["coverage"] for row in risk["cash_coverage_by_period"]] == expected
    assert risk["lowest_cash_coverage"] == min(expected)
    assert risk["peak_cash_shortfall_monthly"] == max(0, 200 + cash[-1] - income)
    assert risk["peak_cash_shortfall_period"] == risk["peak_cash_shortfall_month"] == 12
    assert risk["cash_coverage_by_period"][-1]["debt_service_monthly"] == 10300
    assert "nothing is at risk" not in risk["words"]
    if income <= 0:
        assert "zero or negative" in risk["coverage_note"]
        assert "does not identify the largest payment" in risk["words"]


@pytest.mark.parametrize("income,existing", [(None, 0), (-1000, None), (0, None)])
def test_risk_unknown_income_or_existing_debt_has_no_invented_funding_gap(income, existing):
    schedule = build_schedule(12000, 12, 12)
    risk = risk_profile(schedule, income_monthly=income, existing_service_monthly=existing)
    assert risk["coverage_by_period"] == risk["cash_coverage_by_period"] == []
    assert risk["peak_cash_shortfall_monthly"] is None
    assert risk["coverage_unavailable_reason"]
    assert risk["existing_service_monthly"] == existing


def test_unknown_currency_keeps_provisional_signed_ratio_without_mixing_cash_amounts():
    schedule = build_schedule(12000, 0, 12)
    risk = risk_profile(schedule, income_monthly=-1000, existing_service_monthly=0,
                        income_currency_comparable=False)
    assert risk["lowest_coverage"] == -1
    assert risk["peak_cash_shortfall_monthly"] is None
    assert all(row["cash_surplus_monthly"] is None for row in risk["cash_coverage_by_period"])
    assert "no currency conversion" in risk["coverage_note"]


def test_zero_income_survives_the_plan_to_risk_call():
    result = evaluate({"new_loan_amount": 12000, "new_loan_rate_pct": 0, "new_loan_term_months": 12}, income=0)
    assert result["risk"]["income_monthly"] == 0
    assert result["risk"]["lowest_coverage"] == 0
    assert result["risk"]["peak_cash_shortfall_monthly"] == 11000


def test_known_usd_earnings_and_zero_current_debt_can_fund_a_new_loan_without_a_current_ratio():
    dscr = basis(-12000)
    dscr["headline"] = None
    dscr["candidates"][0]["value"] = None
    result = plans.evaluate_plan(plans.normalize_plan({"new_loan_amount": 12000, "new_loan_rate_pct": 0,
        "new_loan_term_months": 12}), obligations_list=[], recurring={}, months=[], dscr=dscr,
        service={"monthly": 0, "annual": 0}, proposed=None, requested_amount=None)
    assert result["headline_after"] == -1
    assert result["income_before_annual"] == result["income_after_annual"] == -12000
    assert result["risk"]["peak_cash_shortfall_monthly"] == 2000
    assert result["risk"]["coverage_unavailable_reason"] is None


def test_unadopted_proposal_uses_known_earnings_even_when_zero_existing_debt_has_no_ratio():
    dscr = basis(-12000)
    dscr.update(headline=None, annual_debt_service=0, proposed={"amount": 12000, "rate_pct": 0,
        "term_months": 12, "monthly_payment": 1000, "structure": "amortizing", "treatment": "additive"})
    result = proposed_terms_block({"dscr": dscr, "plan": {"record": {}, "evaluation": {"active": False}}})
    assert result["risk"]["income_monthly"] == -1000
    assert result["risk"]["lowest_coverage"] == -1
    assert result["risk"]["peak_cash_shortfall_monthly"] == 2000


@pytest.mark.parametrize("income", [-24000, 0])
def test_negative_and_zero_earnings_levers_rank_cash_relief_not_larger_signed_ratio(income):
    plan = plans.normalize_plan({})
    result = evaluate(income=income)
    dscr = basis(income)
    levers = plans.coverage_levers(plan, result, obligations_list=[
        {"id": "old", "lender": "Existing advance", "monthly": 6000, "payoff": 30000,
         "in_debt_service": True, "is_mca": True}], recurring={}, dscr=dscr,
        requested_amount=None, terms={"monthly_payment": 2000, "label": "Additional loan"})
    payoff = next(row for row in levers if row["key"] == "payoff:old")
    add = next(row for row in levers if row["key"] == "term_sheet:add")
    assert payoff["cash_surplus_change_monthly"] == 6000 and payoff["improves"]
    assert add["cash_surplus_change_monthly"] == -2000 and not add["improves"]
    assert levers.index(payoff) < levers.index(add)
    if income < 0:
        assert payoff["dscr_after"] == -.5 < add["dscr_after"] == -.17
    else:
        assert payoff["dscr_after"] == add["dscr_after"] == 0


def test_unknown_currency_levers_rank_known_debt_relief_without_usd_cash_gap():
    plan = plans.normalize_plan({})
    result = evaluate(comparable=False)
    levers = plans.coverage_levers(plan, result, obligations_list=[
        {"id": "old", "lender": "Existing advance", "monthly": 6000, "payoff": 30000,
         "in_debt_service": True, "is_mca": True}], recurring={}, dscr=basis(comparable=False),
        requested_amount=None, terms={"monthly_payment": 2000})
    payoff = next(row for row in levers if row["key"] == "payoff:old")
    add = next(row for row in levers if row["key"] == "term_sheet:add")
    assert payoff["improves"] and not add["improves"]
    assert levers.index(payoff) < levers.index(add)
    assert payoff["cash_surplus_monthly"] is payoff["cash_surplus_change_monthly"] is None


def test_refinance_and_debt_proceeds_cannot_turn_factual_loss_ebitda_positive():
    sources = [pnl(net_income=-10000)]
    original = deepcopy(sources)
    cockpit = planned(sources, retire=["row:debt"], new_payment_monthly=500)
    result = projection(cockpit)
    assert sources == original
    assert result["before"] == result["after"] == -4000
    assert result["annual_before"] == result["annual_after"] == -8000
    assert result["adjusted_annual"]["before"] == result["adjusted_annual"]["after"] == -8000
    assert result["debt_service_savings_monthly"] == 500
    assert validated_annual_pair(cockpit)["ebitda_annual"] == -8000
    assert cockpit["dscr"]["headline"]["value"] == -.67
    assert cockpit["plan"]["evaluation"]["headline_after"] == -1.33


def test_current_dscr_preserves_loss_and_zero_source_earnings():
    for income in [-24000, 0]:
        result = dscr_block([], {"annualized_ebitda": income, "period_label": "2025", "typed": True}, {},
                            {"monthly": 1000, "annual": 12000}, None)
        assert result["headline"]["numerator"] == income
        assert result["headline"]["value"] == income / 12000
