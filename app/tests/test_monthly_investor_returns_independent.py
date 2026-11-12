"""Independent, fictional money examples for the monthly return explorer."""
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.deal_budget import calculate
from app.services.investor_returns import project


def fee(**changes):
    return {"id": str(uuid4()), "label": "Fictional investor charge", "category": "management",
        "payer": "investor", "method": "fixed", "value": 0, "basis": "loan_amount",
        "timing": "closing", "settlement": "separate", "enabled": True,
        "reserve_release": "unknown", "recipient_role": "platform", **changes}


def source(rows=None, **changes):
    return {"funding_mode": "single_investor", "investor_funding_amount": None,
        "minimum_investor_contribution": None, "currency": "USD", "costs_confirmed": True,
        "payoff_terms": {"interest_policy": "accrued_only", "charge_amount": 0},
        "rows": rows or [], **changes}


def contract(**changes):
    return {"amount": 100000, "rate_pct": 12, "term_months": 12,
        "structure": "interest_only", "payments_per_year": 12, "currency": "USD",
        "active_proposal": True, **changes}


def cents(value):
    return Decimal(value).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


@pytest.mark.parametrize("month", [6, 7, 8, 9])
@pytest.mark.parametrize("stake", [100000, 25000])
def test_interest_only_monthly_cash_and_payoff_reconcile_for_the_illustrated_stake(month, stake):
    # $1,000 monthly interest, 10% of that interest in fees, and $250 at closing.
    budget = source([fee(value=250), fee(value=10, method="percent", basis="interest", timing="monthly")])
    result = project(budget, contract(), exit_month=month, investment_amount=stake, curve_work_limit=0)
    share = Decimal(stake) / 100000
    row = next(value for value in result["holding"] if value["month"] == month)
    expected_profit = float((Decimal(900) * month - 250) * share)
    expected_initial = float(Decimal(100250) * share)
    assert row["interest"] == float(1000 * share)
    assert row["principal_returned"] == 0
    assert row["fees"] == float(100 * share)
    assert row["net_distribution"] == float(900 * share)
    assert row["cumulative_fees"] == float((Decimal(100) * month + 250) * share)
    assert row["cumulative_net_profit"] == expected_profit
    assert row["outstanding_principal"] == stake
    assert row["cumulative_net_cash_flow"] == float((Decimal(900) * month - 100250) * share)
    exit_value = result["selected_exit"]
    assert exit_value["principal_returned"] == stake
    assert exit_value["net_income"] == expected_profit
    assert exit_value["initial_outlay"] == expected_initial
    assert exit_value["net_cash_received"] == float((Decimal(100000) + Decimal(900) * month) * share)
    assert exit_value["net_cash_profit"] == expected_profit
    assert exit_value["net_roi_pct"] == round(expected_profit / expected_initial * 100, 2)


def test_amortizing_holding_and_payoff_match_an_independent_declining_balance_recurrence():
    principal, rate, term = Decimal(100000), Decimal(".01"), 36
    payment = cents(principal * rate / (1 - (1 + rate) ** -term))
    collected_interest = Decimal(0)
    for _ in range(6):
        interest = cents(principal * rate)
        collected_interest += interest
        principal -= payment - interest
    result = project(source(), contract(term_months=int(term), structure="amortizing"), curve_work_limit=0)
    sixth = next(row for row in result["holding"] if row["month"] == 6)
    assert sixth["outstanding_principal"] == float(principal)
    assert sixth["cumulative_principal_returned"] == float(Decimal(100000) - principal)
    assert sixth["cumulative_interest"] == sixth["cumulative_net_profit"] == float(collected_interest)
    assert result["selected_exit"]["principal_returned"] == 100000
    assert result["selected_exit"]["net_income"] == float(collected_interest)


def test_fractional_exit_collects_only_the_actual_stub_and_related_percentage_fee():
    budget = source([fee(value=250), fee(value=10, method="percent", basis="interest", timing="monthly")])
    result = project(budget, contract(), exit_month=6.5, curve_work_limit=0)
    assert result["selected_exit"]["payoff_month"] == 6.5
    assert result["selected_exit"]["gross_interest"] == 6500
    assert result["selected_exit"]["total_fees"] == 900
    assert result["selected_exit"]["net_income"] == 5600


def test_every_eager_month_point_matches_the_independent_interest_and_fee_formula():
    budget = source([fee(value=250), fee(value=10, method="percent", basis="interest", timing="monthly")])
    result = project(budget, contract())
    assert result["payoff_curve_status"] == "complete"
    assert [point["month"] for point in result["payoff_curve"]] == list(range(1, 13))
    for point in result["payoff_curve"]:
        month = point["month"]
        assert point["gross_interest"] == 1000 * month
        assert point["total_fees"] == 250 + 100 * month
        assert point["principal_returned"] == 100000
        assert point["net_income"] == 900 * month - 250
    assert result["holding"][0]["cumulative_net_profit"] == -250
    assert result["holding"][0]["net_cash_flow"] == -100250
    assert result["full_term"]["net_income"] == 10550


def test_work_bound_keeps_arbitrary_selected_month_priced_without_an_eager_curve():
    budget = source([fee(value=250), fee(value=10, method="percent", basis="interest", timing="monthly")])
    result = project(budget, contract(), exit_month=9, curve_work_limit=1)
    assert result["payoff_curve_status"] == "on_demand"
    assert result["payoff_curve"] == []
    assert result["performance"]["curve_work_estimate"] > 1
    assert result["performance"]["curve_points"] == 0
    assert result["selected_exit"]["net_income"] == 7850
    assert result["full_term"]["net_income"] == 10550


def test_sparse_cash_schedule_does_not_invent_monthly_interest_collections():
    result = project(source(), contract(payments_per_year=1), exit_month=6, curve_work_limit=0)
    sixth = next(row for row in result["holding"] if row["month"] == 6)
    assert sixth["gross_receipts"] == sixth["interest"] == sixth["net_distribution"] == 0
    assert sixth["cumulative_net_profit"] == 0
    assert sixth["outstanding_principal"] == 100000
    assert result["selected_exit"]["gross_interest"] == result["selected_exit"]["net_income"] == 6000


def test_reserve_is_capital_held_and_released_without_becoming_profit():
    budget = source([fee(value=250), fee(value=10, method="percent", basis="interest", timing="monthly"),
        fee(category="escrow_reserve", value=1000, recipient_role="escrow", reserve_release="maturity")],
        payoff_terms={"interest_policy": "accrued_only", "charge_amount": 0, "reserve_release": "contract_maturity"})
    result = project(budget, contract(), exit_month=6, curve_work_limit=0)
    early = result["selected_exit"]
    assert early["initial_outlay"] == 101250
    assert early["net_income"] == 5150
    assert early["reserve_released"] == 0 and early["reserve_held"] == 1000
    assert early["net_cash_profit"] == 4150
    maturity = result["holding"][-1]
    assert maturity["reserve_release"] == 1000
    assert maturity["cumulative_net_profit"] == maturity["cumulative_net_cash_flow"] == 10550


def test_unpriced_active_fee_preserves_known_interest_but_never_claims_net_earnings():
    result = project(source([fee(value=None, timing="monthly")]), contract(), curve_work_limit=0)
    sixth = next(row for row in result["holding"] if row["month"] == 6)
    assert sixth["interest"] == 1000
    assert sixth["principal_returned"] == 0
    assert sixth["net_distribution"] is sixth["cumulative_net_profit"] is sixth["cumulative_net_roi_pct"] is None
    assert result["selected_exit"]["net_income"] is result["selected_exit"]["net_roi_pct"] is None


def test_short_contract_never_fabricates_month_six_to_nine_collections():
    result = project(source(), contract(term_months=3), exit_month=9, curve_work_limit=0)
    assert [row["month"] for row in result["holding"]] == [0, 1, 2, 3]
    assert result["selected_exit"]["payoff_month"] == 3
    assert result["selected_exit"]["capped_at_maturity"] is True
    assert result["selected_exit"]["net_income"] == 3000


def test_lazy_projection_never_changes_legacy_financials_inputs_or_shared_cached_result():
    budget, terms = source([fee(value=25)]), contract()
    original_budget, original_terms = deepcopy(budget), deepcopy(terms)
    legacy = calculate(budget, terms)
    first = project(budget, terms, exit_month=8, curve_work_limit=0)
    first["holding"][1]["interest"] = 999999
    second = project(budget, terms, exit_month=8, curve_work_limit=0)
    assert second["holding"][1]["interest"] == 1000
    assert budget == original_budget and terms == original_terms
    assert calculate(budget, terms) == legacy


@pytest.mark.parametrize("amount", [0, -1, 100001])
def test_invalid_illustrated_stakes_are_rejected_instead_of_silently_clamped(amount):
    with pytest.raises(HTTPException) as error:
        project(source(), contract(), investment_amount=amount, curve_work_limit=0)
    assert error.value.status_code == 422
