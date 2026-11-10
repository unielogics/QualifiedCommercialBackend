"""Recurring allocation uses fee-inclusive payments and net operating cash once."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.schemas.deal_budget import DealBudgetPatch, DealCostRow
from app.schemas.earnings_improvement import empty_body
from app.services import financial_plan_budget, released_cash_flow
from app.services.loan_schedule import build_schedule


def cost(value, *, treatment="financing_cost", **changes):
    return DealCostRow.model_validate({
        "id": uuid4(), "label": "Synthetic recurring cost", "category": "servicing",
        "payer": "borrower", "method": "fixed", "value": value, "basis": "loan_amount",
        "timing": "monthly", "settlement": "separate", "enabled": True,
        "coverage_treatment": "new_cost", "earnings_treatment": treatment, **changes,
    }).model_dump(mode="json")


def budget(rows, **changes):
    result = DealBudgetPatch.model_validate({
        "expected_revision": 4, "costs_confirmed": True, "currency": "USD",
        "rows": rows, **changes,
    }).model_dump(mode="json", exclude={"expected_revision"})
    return {**result, "revision": 4}


def cockpit(costs, *, principal=100000, principal_solution=None):
    schedule = build_schedule(principal, 12, 12, structure="interest_only")
    payment = schedule["ordinary_payment_monthly"]
    after = 1000 + payment  # Retained debt plus the real ordinary replacement payment.
    evaluation = {
        "active": True, "retired_count": 1, "retired_ids": ["old-loan"],
        "loan_terms": {"amount": principal, "rate_pct": 12, "term_months": 12,
            "structure": "interest_only", "payments_per_year": 12, "active_proposal": True},
        "new_payment_monthly": payment, "new_regular_payment_monthly": payment,
        "income_after_annual": 120000, "headline_key": "profit_and_loss",
        "headline_after": 120000 / (after * 12),
        "debt_service_before_monthly": 3000,
        "debt_service_after_annual": after * 12, "debt_service_after_monthly": after,
        "debt_service_after_regular_monthly": after, "ebitda": {},
        "candidates": [{"key": "profit_and_loss", "numerator_after": 120000}],
    }
    financial_plan_budget.apply_to_evaluation(evaluation, costs, principal_solution)
    return {
        "debt": {"service": {"monthly": 3000, "annual": 36000, "currency": "USD",
            "version_id": "00000000-0000-0000-0000-000000000001"}},
        "plan": {"record": {"updated_at": "2026-11-10T12:00:00Z", "retire": ["old-loan"],
            "new_loan_amount": principal, "overrides": {}}, "evaluation": evaluation},
    }


def allocated(current, amount):
    return {**empty_body(), "released_cash_flow": {
        "source_fingerprint": released_cash_flow.source(current)["fingerprint"],
        "allocations": [{"id": str(uuid4()), "label": "Payroll reserve", "amount_monthly": amount,
            "expense_row_id": None, "notes": ""}],
    }}


def test_new_financing_fee_is_already_in_regular_service_and_not_deducted_twice():
    current = cockpit(budget([cost(100)]))
    evaluation = current["plan"]["evaluation"]
    source = released_cash_flow.source(current)
    assert evaluation["annual_new_financing_fees"] == 1200
    assert evaluation["debt_service_after_regular_monthly"] == 2100
    assert source["available"] and source["amount_monthly"] == 900
    assert source["new_financing_fees_monthly"] == 100
    assert source["new_operating_expenses_monthly"] == 0
    assert released_cash_flow.evaluate(allocated(current, 900), current)["complete"]


def test_new_operating_cost_is_deducted_once_after_fee_inclusive_regular_service():
    current = cockpit(budget([cost(100), cost(200, treatment="operating_expense")]))
    evaluation = current["plan"]["evaluation"]
    source = released_cash_flow.source(current)
    assert evaluation["annual_new_operating_expenses"] == 2400
    assert evaluation["income_after_annual"] == 117600
    assert evaluation["debt_service_after_regular_monthly"] == 2100
    assert source["available"] and source["amount_monthly"] == 700
    assert source["new_operating_expenses_monthly"] == 200
    assert source["new_financing_fees_monthly"] == 100
    allocation = released_cash_flow.evaluate(allocated(current, 700), current)
    assert allocation["complete"] and allocation["unallocated_monthly"] == 0
    assert allocation["source"]["new_operating_expenses_monthly"] == 200


@pytest.mark.parametrize("excluded", [{"coverage_treatment": "already_in_baseline"}, {"enabled": False}])
def test_baseline_or_disabled_charges_do_not_create_another_cash_deduction(excluded):
    current = cockpit(budget([cost(100, **excluded), cost(200, treatment="operating_expense", **excluded)]))
    evaluation = current["plan"]["evaluation"]
    source = released_cash_flow.source(current)
    assert evaluation["annual_new_financing_fees"] == 0
    assert evaluation["annual_new_operating_expenses"] == 0
    assert evaluation["income_after_annual"] == 120000
    assert evaluation["debt_service_after_regular_monthly"] == 2000
    assert source["available"] and source["amount_monthly"] == 1000


def test_unknown_new_operating_expense_blocks_allocation_instead_of_becoming_zero():
    current = cockpit(budget([cost(None, treatment="operating_expense")]))
    source = released_cash_flow.source(current)
    assert current["plan"]["evaluation"]["annual_new_operating_expenses"] is None
    assert not source["available"] and source["amount_monthly"] is None
    assert source["new_operating_expenses_monthly"] is None
    assert any("unknown costs are not zero" in reason for reason in source["reasons"])
    assert not released_cash_flow.evaluate(empty_body(), current)["complete"]


def test_financed_closing_fee_affects_release_only_through_applied_loan_principal():
    costs = budget([cost(5000, timing="closing", settlement="financed", category="origination")],
        loan_amount_mode="base_loan", base_loan_amount=100000)
    plan = {"new_loan_amount": 100000, "new_loan_rate_pct": 12, "new_loan_term_months": 12,
        "new_loan_structure": "interest_only"}
    effective, pending = financial_plan_budget.solve_plan(plan, costs)
    assert effective["new_loan_amount"] == 105000 and pending["requires_apply"]
    uncommitted = released_cash_flow.source(cockpit(costs, principal_solution=pending))
    assert not uncommitted["available"] and uncommitted["amount_monthly"] is None
    _, applied = financial_plan_budget.solve_plan(effective, costs)
    assert not applied["requires_apply"]
    current = cockpit(costs, principal=105000, principal_solution=applied)
    evaluation = current["plan"]["evaluation"]
    assert evaluation["annual_new_financing_fees"] == 0
    assert evaluation["annual_new_operating_expenses"] == 0
    assert evaluation["debt_service_after_regular_monthly"] == 2050
    assert released_cash_flow.source(current)["amount_monthly"] == 950


@pytest.mark.parametrize("change", ["revision", "same_total_classification"])
def test_budget_revision_or_same_total_classification_stales_previous_allocation(change):
    costs = budget([cost(100), cost(200, treatment="operating_expense"),
        cost(200, treatment="operating_expense", coverage_treatment="already_in_baseline")])
    before = cockpit(costs)
    allocation = allocated(before, 700)
    assert released_cash_flow.evaluate(allocation, before)["complete"]
    changed = deepcopy(costs)
    if change == "revision":
        changed["revision"] += 1
    else:
        changed["rows"][1]["coverage_treatment"] = "already_in_baseline"
        changed["rows"][2]["coverage_treatment"] = "new_cost"
    after = cockpit(changed)
    assert released_cash_flow.source(before)["amount_monthly"] == 700
    assert released_cash_flow.source(after)["amount_monthly"] == 700
    assert before["plan"]["evaluation"]["annual_new_operating_expenses"] == after["plan"]["evaluation"]["annual_new_operating_expenses"]
    assert before["plan"]["evaluation"]["financing_budget_fingerprint"] != after["plan"]["evaluation"]["financing_budget_fingerprint"]
    result = released_cash_flow.evaluate(allocation, after)
    assert result["status"] == "stale" and not result["complete"]
