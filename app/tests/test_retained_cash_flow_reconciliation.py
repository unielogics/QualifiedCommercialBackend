"""Reconcile retained debt to a current accepted schedule and recorded terms."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.schemas.earnings_improvement import empty_body
from app.services import financial_plan, plan_terms_link, released_cash_flow
from app.services.loan_schedule import build_schedule


def recalculate(current, budget):
    plan, debt = current["plan"], current["debt"]
    dscr = {"headline": {"key": "profit_and_loss", "numerator": 120000, "value": .13, "currency_comparable": True},
        "candidates": [{"key": "profit_and_loss", "numerator": 120000, "value": .13, "currency_comparable": True}]}
    plan["evaluation"] = financial_plan.evaluate_plan(plan["record"], obligations_list=debt["obligations"],
        recurring={}, months=[], dscr=dscr, service=debt["service"], proposed=None, requested_amount=273000,
        financing_budget=budget)
    plan["terms_link"] = plan_terms_link.terms_link(plan["record"], plan["evaluation"], plan["terms_link"]["recorded"])
    return current


def case(budget=None):
    amounts = [("advance-a", 6300, 16800), ("advance-b", 5906.25, 18000),
        ("advance-c", 32453.35, 80000), ("advance-d", 21302.04, 40000), ("advance-e", 6295.8, 17688.2),
        ("sba-a", 919, 177337.39), ("sba-b", 2071.84, 117934.99), ("sba-c", 2080.75, 112242.16)]
    obligations = [{"id": f"row:{key}", "kind": "row", "lender": key, "monthly": monthly, "balance": balance,
        "source": "debt_schedule_row", "replay_requires_review": False} for key, monthly, balance in amounts]
    service = {"source": "accepted_debt_schedule", "schedule_authoritative": True, "version_id": str(uuid4()),
        "monthly": 77329.03, "annual": 927948.36, "currency": "USD", "identity_association_conflict": False}
    config = {"balloons": [], "after_balloon": "keep_payment", "amortization_months": 54}
    schedule = build_schedule(200567.44, 29.99, 24, structure="amortizing", interest_only_months=6,
        periods_per_year=48, repayment_config=config)
    recorded = {"kind": "client_terms", "id": str(uuid4()), "version": 3, "label": "Recorded refinancing",
        "amount": 200567.44, "rate_pct": 29.99, "term_months": 24, "structure": "amortizing", "interest_only": False,
        "interest_only_months": 6, "repayment_config": config, "payments_per_year": 48,
        "payment_monthly": round(schedule["coverage_annual_debt_service"] / 12, 2),
        "treatment": "refinance", "retained_annual_debt_service": 60859.08}
    record = plan_terms_link.adopt({"retire": [row["id"] for row in obligations[:5]], "exclude": [], "add_backs": []},
        recorded, by=uuid4())
    record["updated_at"] = "2026-11-10T17:00:00+00:00"
    current = {"debt": {"service": service, "obligations": obligations},
        "plan": {"record": record, "terms_link": {"recorded": recorded}}}
    costs = {"revision": 4, "currency": "USD", "costs_confirmed": True, "funding_mode": "single_investor",
        "loan_amount_mode": "target_net", "target_net_amount": "172488.00", "rows": [
            {"id": "closing", "label": "Recorded closing charge", "category": "closing_cost", "payer": "borrower",
             "method": "percent", "value": "4", "basis": "loan_amount", "timing": "closing", "settlement": "withheld"},
            {"id": "escrow", "label": "Held reserve", "category": "escrow_reserve", "payer": "borrower",
             "method": "percent", "value": "10", "basis": "loan_amount", "timing": "closing", "settlement": "withheld"}]}
    return recalculate(current, costs if budget is None else budget)


def test_eight_current_debts_reconcile_remaining_sba_service_without_editing_source_or_earnings():
    current = case()
    original = deepcopy(current)
    result = released_cash_flow.source(current)
    assert result["reconciled_retained_override"] and result["available"]
    assert result["before_monthly"] == 77329.03 and result["after_monthly"] == 11849.35
    assert result["amount_monthly"] == 65479.68 and not result["reasons"]
    assert current == original
    assert current["plan"]["record"]["overrides"]["debt_service_monthly"] == 5071.59
    allocation = released_cash_flow.evaluate(empty_body(), current)
    assert allocation["required"] and not allocation["complete"]
    assert allocation["source"]["reconciled_retained_override"]


@pytest.mark.parametrize("change", ["ordinary_schedule", "non_authoritative", "no_version", "service_total",
    "annual_service", "unknown_payment", "unknown_remaining", "duplicate_debt", "duplicate_selection", "missing_selected",
    "different_ref", "different_version", "different_terms_amount", "different_remaining", "generic_override",
    "unmirrored", "additive", "replay_review", "identity", "evaluation_remaining"])
def test_partial_unknown_or_mismatched_override_context_stays_blocked(change):
    current = case()
    debt, plan = current["debt"], current["plan"]
    if change == "ordinary_schedule":
        debt["service"]["source"] = "debt_schedule_rows"
    elif change == "non_authoritative":
        debt["service"]["schedule_authoritative"] = False
    elif change == "no_version":
        debt["service"]["version_id"] = None
    elif change == "service_total":
        debt["service"]["monthly"] += .01
    elif change == "annual_service":
        debt["service"]["annual"] += 12
    elif change == "unknown_payment":
        debt["obligations"][0]["monthly"] = None
    elif change == "unknown_remaining":
        debt["obligations"][-1]["monthly"] = None
    elif change == "duplicate_debt":
        debt["obligations"].append(deepcopy(debt["obligations"][0]))
    elif change == "duplicate_selection":
        plan["record"]["retire"].append(plan["record"]["retire"][0])
    elif change == "missing_selected":
        plan["record"]["retire"][0] = "row:missing"
    elif change == "different_ref":
        plan["record"]["terms_ref"]["id"] = str(uuid4())
    elif change == "different_version":
        plan["record"]["terms_ref"]["version"] = 2
    elif change == "different_terms_amount":
        plan["terms_link"]["recorded"]["retained_annual_debt_service"] = 60000
    elif change == "different_remaining":
        plan["record"]["overrides"]["debt_service_monthly"] = 5000
    elif change == "generic_override":
        plan["terms_link"]["recorded"] = {}
        plan["record"]["terms_ref"] = None
    elif change == "unmirrored":
        plan["terms_link"]["state"] = "differs"
    elif change == "additive":
        plan["terms_link"]["recorded"]["treatment"] = "additive"
    elif change == "replay_review":
        debt["obligations"][0]["replay_requires_review"] = True
    elif change == "identity":
        debt["service"]["identity_association_conflict"] = True
    else:
        plan["evaluation"]["debt_service_before_new_payment_monthly"] = 0
    result = released_cash_flow.source(current)
    assert not result["reconciled_retained_override"] and not result["available"]
    assert result["amount_monthly"] is None
    assert any("override" in message for message in result["reasons"])
