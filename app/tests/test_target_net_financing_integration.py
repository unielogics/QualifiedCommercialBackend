from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.capital_partners.services.sync import canonical_financing_fields
from app.services import deal_budget, financial_cockpit, financial_plan
from app.services.financial_plan_budget import (
    apply_to_terms_calculation,
    coverage_context,
    validate_write,
)
from app.services.financing_budget_disclosure import public_budget, render_budget_html


def row(**changes):
    return {"id": str(uuid4()), "label": "Reviewed charge", "category": "management", "payer": "borrower",
        "method": "fixed", "value": 100, "timing": "monthly", "settlement": "separate", "basis": "loan_amount",
        "coverage_treatment": "new_cost", "earnings_treatment": "financing_cost", **changes}


def budget(*rows, **changes):
    return {"revision": 3, "currency": "USD", "costs_confirmed": True, "funding_mode": "single_investor",
        "loan_amount_mode": "gross_loan", "rows": list(rows), **changes}


def evaluate(costs, **plan_changes):
    plan = financial_plan.normalize_plan({"new_loan_amount": 100000, "new_loan_rate_pct": 12,
        "new_loan_term_months": 24, "new_loan_structure": "interest_only", **plan_changes})
    return financial_plan.evaluate_plan(plan, obligations_list=[], recurring={},
        months=[{"month": "2026-01", "deposits": 10000, "withdrawals": 8000, "net": 2000, "ending_balance": 5000, "lowest_balance": 100}],
        dscr={"headline": {"key": "profit_and_loss", "numerator": 120000, "value": 10, "currency_comparable": True},
            "candidates": [{"key": "profit_and_loss", "numerator": 120000, "value": 10, "currency_comparable": True}]},
        service={"monthly": 1000, "annual": 12000}, proposed=None, requested_amount=100000,
        financing_budget=costs)


def test_main_plan_metrics_include_new_costs_once_and_recompute_payment_deltas():
    costs = budget(row(), row(value=200, earnings_treatment="operating_expense"),
        row(value=300, coverage_treatment="already_in_baseline"), row(value=500, payer="investor"))
    result = evaluate(costs)
    assert result["income_after_annual"] == 117600
    assert result["debt_service_after_annual"] == 25200
    assert result["headline_after"] == 4.67
    assert result["debt_service_after_regular_monthly"] == 2100
    assert result["net_regular_monthly_change"] == 1100
    assert result["income_after_annual_before_budget"] == 120000
    assert result["headline_after_before_budget"] == 5
    assert result["risk"]["lowest_coverage"] is None
    assert result["risk"]["loan_schedule_only"]["lowest_coverage"] == 5
    recalculated = deal_budget.calculate(costs, {**result["loan_terms"], "coverage_context": coverage_context(result)})
    assert recalculated["financing_fee_coverage"]["with_financing_fees_dscr"] == pytest.approx(117600 / 25200, abs=.00005)


def test_target_net_uses_solved_gross_payment_and_preserves_unapplied_identity():
    costs = budget(row(timing="closing", value=2000, settlement="financed", category="origination"),
        row(timing="closing", value=1000, settlement="withheld", category="closing_cost"),
        loan_amount_mode="target_net", target_net_amount=100000)
    before = deepcopy(costs)
    result = evaluate(costs)
    assert result["loan_terms"]["amount"] == 103000
    assert result["new_payment_monthly"] == 1030
    assert result["principal_solution"]["requires_apply"] is True
    assert result["principal_solution"]["current_loan_amount"] == 100000
    assert result["financing_budget_calculation"]["net_client_cash"] == 100000
    assert result["annual_new_operating_expenses"] == 0
    assert costs == before


def test_unknown_new_cost_is_not_zero_and_existing_baseline_cost_is_not_double_counted():
    unknown = evaluate(budget(row(value=None)))
    assert unknown["headline_after"] is None
    assert unknown["coverage_unavailable_reason"]
    existing = evaluate(budget(row(value=300, coverage_treatment="already_in_baseline")))
    assert existing["headline_after"] == 5 and existing["debt_service_after_annual"] == 24000


def test_save_requires_current_budget_and_actual_solved_contract_principal():
    costs = budget(row(timing="closing", value=2000, settlement="financed"), loan_amount_mode="base_loan", base_loan_amount=100000)
    for expected, amount in ((None, 102000), (2, 102000), (3, 100000)):
        with pytest.raises(HTTPException) as error:
            validate_write(costs, expected, {"amount": amount})
        assert error.value.status_code == 409
    validate_write(costs, 3, {"amount": 102000})


def test_immutable_terms_snapshot_uses_same_cost_coverage_and_public_treatment():
    from app.services.application_terms import DscrContext, _calculation

    costs = budget(row(), row(value=200, earnings_treatment="operating_expense"),
        row(value=2000, timing="closing", settlement="financed", recipient="PRIVATE RECIPIENT", notes="PRIVATE NOTE"))
    original = _calculation(context=DscrContext("business", 10, 120000, "Annual earnings", 12000, None, "Reviewed P&L"),
        payment=1000, payment_count=24, payments_per_year=12, new_annual_debt_service=12000, amount=100000, total_repayment=124000)
    raw = deal_budget.calculate(costs, {"amount": 100000, "rate_pct": 12, "term_months": 24, "structure": "interest_only",
        "coverage_context": {"income_annual": 120000, "debt_service_annual": 24000, "new_loan_annual_debt_service": 12000}})
    frozen = deepcopy(raw)
    adjusted = apply_to_terms_calculation(original, costs, raw)
    assert adjusted.dscr_after == round(117600 / 25200, 3)
    assert adjusted.projected_annual_debt_service == 25200
    assert original.dscr_after == 5 and raw == frozen
    public = public_budget({"recorded": costs, "basis": "recorded_terms", "calculation": raw})
    html = render_budget_html(public)
    assert "Financed in principal" in html and "operating expense" in html
    assert "PRIVATE RECIPIENT" not in html and "PRIVATE NOTE" not in html
    assert "PRIVATE" not in str(public)


def test_pending_principal_cannot_hydrate_marketplace_offer():
    terms = {"amount": 103000, "rate_pct": 12, "term_months": 24,
        "principal_solution": {"status": "calculated", "requires_apply": True}}
    assert canonical_financing_fields({"proposed_terms": terms}, {}) == {}


def test_external_plan_view_strips_raw_budget_review_details_but_keeps_safe_math():
    from types import SimpleNamespace

    from app.enums import Role
    from app.services.financial_metric_review import redact_for_external

    source = {"deal_budget": budget(row(recipient="PRIVATE RECIPIENT", notes="PRIVATE NOTE")),
        "plan": {"evaluation": {"financing_budget_calculation": {"issues": ["PRIVATE RECIPIENT needs review"]},
            "principal_solution": {"gross_loan_amount": 103000, "issues": ["PRIVATE NOTE"]}, "headline_after": 4.67}}}
    safe = redact_for_external(source, SimpleNamespace(role=Role.BROKER))
    assert "PRIVATE" not in str(safe)
    assert safe["plan"]["evaluation"]["principal_solution"]["gross_loan_amount"] == 103000
    assert safe["plan"]["evaluation"]["headline_after"] == 4.67
    assert "PRIVATE" in str(source)


def test_unsaved_preview_retains_payoffs_and_addbacks_in_cost_coverage():
    cockpit = {"requested_amount": 100000,
        "debt": {"service": {"monthly": 2000}, "obligations": [{"id": "old", "monthly": 1000, "in_debt_service": True}]},
        "bank": {"months": [], "recurring": {}},
        "dscr": {"headline": {"key": "profit_and_loss", "numerator": 120000, "value": 5},
            "candidates": [{"key": "profit_and_loss", "numerator": 120000, "value": 5}]}}
    result = financial_cockpit.financing_preview(cockpit, {"retire": ["old"], "add_backs": [{"kind": "other", "amount_monthly": 100}],
        "new_loan_amount": 100000, "new_loan_rate_pct": 12, "new_loan_term_months": 24, "new_loan_structure": "interest_only"}, budget(row()))
    assert result["evaluation"]["income_after_annual"] == 121200
    assert result["evaluation"]["debt_service_after_annual"] == 25200
    assert result["loan"]["coverage_context"]["debt_service_annual"] == 24000
