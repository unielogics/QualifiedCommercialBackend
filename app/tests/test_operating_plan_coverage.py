from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import earnings_improvement, financial_plan
from app.services.financial_cockpit import financing_preview
from app.tests.test_earnings_improvement import NOW, action, approve, body, context


def cockpit(record=None, *, reviewed=True, implementation_cost=0, effective_date="2026-01-01"):
    current = context()
    current["debt"]["service"].update(monthly=1000)
    current["dscr"] = {"headline": {"key": "tax_return", "value": -.83, "numerator": -10000,
        "currency_comparable": True, "label": "Tax earnings 2025"},
        "candidates": [{"key": "tax_return", "value": -.83, "numerator": -10000, "currency_comparable": True}],
        "annual_debt_service": 12000}
    current["bank"] = {"months": [], "recurring": {}, "accounts": []}
    record = financial_plan.normalize_plan(record or {})
    initial = financial_plan.evaluate_plan(record, obligations_list=[], recurring={}, months=[],
        dscr=current["dscr"], service=current["debt"]["service"], proposed=None, requested_amount=None,
        ebitda=current["ebitda"])
    current["plan"] = {"record": record, "evaluation": initial}
    raw = body()
    raw["adjustments"][0].update(implementation_cost=implementation_cost, effective_date=effective_date)
    raw = approve(raw, current) if reviewed else raw
    evaluation = earnings_improvement.evaluate(raw, current, now=NOW)
    current["operating_plan"] = {"available": True, "state": "published" if reviewed else "client_submitted",
        "version_id": "operating-version-2", "version_number": 2, "evaluation": evaluation}
    return current, raw


def projected(current, budget=None):
    return financing_preview(current, current["plan"]["record"], budget or {})["evaluation"]


def test_published_reviewed_savings_raise_signed_coverage_without_rewriting_history():
    current, _ = cockpit()
    before = deepcopy(current)
    result = projected(current)
    assert result["active"]
    assert result["income_before_annual"] == -10000
    assert result["income_after_annual"] == -7600
    assert result["headline_after"] == -.63
    assert result["operating_plan"]["applied_annual_impact"] == 2400
    assert result["operating_plan"]["version_number"] == 2
    assert current == before


def test_client_proposal_is_visible_but_does_not_change_primary_coverage():
    current, _ = cockpit(reviewed=False)
    result = projected(current)
    assert result["income_after_annual"] == -10000
    assert not result["operating_plan"]["applied"]
    assert result["operating_plan"]["status"] == "awaiting_review"
    assert result["operating_plan"]["proposed_dscr"] == -.63


def test_first_year_proration_and_implementation_cost_are_used_not_stabilized_run_rate():
    current, _ = cockpit(implementation_cost=1000, effective_date="2026-07-01")
    result = projected(current)
    impact = round(2400 * 184 / 365 - 1000, 2)
    assert result["income_after_annual"] == round(-10000 + impact, 2)
    assert result["operating_plan"]["annual_impact"] == 2400
    assert result["operating_plan"]["first_year_impact"] == impact
    approved = current["operating_plan"]["evaluation"]["projection"]["approved"]
    assert approved["first_year_dscr_current"] == result["headline_after"]
    assert approved["first_year_dscr_current"] != approved["dscr_current"]


@pytest.mark.parametrize("change", ["income_override", "addback", "bank_basis"])
def test_conflicting_manual_adjustments_or_different_earnings_basis_do_not_stack(change):
    current, _ = cockpit()
    if change == "income_override":
        current["plan"]["record"]["overrides"]["income_annual"] = -5000
    elif change == "addback":
        current["plan"]["record"]["add_backs"] = [{"id": "manual", "kind": "other", "amount_monthly": 100}]
    else:
        for value in [current["dscr"]["headline"], *current["dscr"]["candidates"]]:
            value["key"] = "bank_cash_flow"
    result = projected(current)
    assert not result["operating_plan"]["applied"]
    assert result["operating_plan"]["status"] == "blocked"


def test_loan_repricing_does_not_stale_operating_review_but_source_change_does():
    current, raw = cockpit()
    changed = deepcopy(current)
    changed["plan"]["evaluation"].update(active=True, debt_service_after_annual=50000,
        loan_terms={"amount": 200000, "rate_pct": 29.99}, schedule={"balloon": 150000})
    result = earnings_improvement.evaluate(raw, changed, now=NOW)
    assert result["actions"][0]["review_state"] == "approved"
    changed["operating_evidence"]["files"][0]["content_hash"] = "new-tax-version"
    result = earnings_improvement.evaluate(raw, changed, now=NOW)
    assert result["actions"][0]["review_state"] == "stale"


def test_budget_charges_and_operating_improvement_are_applied_once_in_shared_engine():
    current, _ = cockpit({"new_loan_amount": 12000, "new_loan_rate_pct": 0,
        "new_loan_term_months": 12, "new_loan_structure": "amortizing"})
    budget = {"currency": "USD", "costs_confirmed": True, "rows": [{"id": "management", "label": "New operating service",
        "enabled": True, "payer": "borrower", "category": "management", "method": "fixed", "value": 50,
        "basis": "loan_amount", "timing": "monthly", "settlement": "separate", "earnings_treatment": "operating_expense",
        "coverage_treatment": "new_cost"}]}
    result = projected(current, budget)
    assert result["income_after_annual_before_budget"] == -7600
    assert result["income_after_annual"] == -8200
    assert result["annual_new_operating_expenses"] == 600
    assert result["debt_service_after_annual"] == 24000
    assert result["headline_after"] == -.34


def test_legacy_review_stays_valid_only_while_original_context_matches():
    current, raw = cockpit()
    action = raw["adjustments"][0]
    action["review_source_stamp"] = earnings_improvement._legacy_review_stamp(action, current, raw["expenses"][0], raw)
    assert earnings_improvement.evaluate(raw, current, now=NOW)["actions"][0]["review_state"] == "approved"
    current["plan"]["evaluation"]["debt_service_after_annual"] = 25000
    assert earnings_improvement.evaluate(raw, current, now=NOW)["actions"][0]["review_state"] == "stale"


def test_wrong_reporting_period_cannot_supply_projected_coverage():
    current, raw = cockpit()
    raw["period_start"], raw["period_end"] = "2024-01-01", "2024-12-31"
    evaluated = earnings_improvement.evaluate(raw, current, now=NOW)
    assert evaluated["projection"]["approved"]["earnings"] is None
    assert evaluated["projection"]["proposed"]["earnings"] is None


def test_unrelated_transfer_exclusion_and_nonoperating_addbacks_do_not_block_tax_savings():
    current, _ = cockpit()
    current["plan"]["record"].update(exclude=["payee:owner-transfer"], add_backs=[
        {"id": "already-reversed", "kind": "interest_refinanced", "amount_monthly": 100}])
    result = projected(current)
    assert result["operating_plan"]["applied"]
    assert result["income_after_annual"] == -7600


def test_rejected_or_excluded_plan_has_no_pending_improvement_review():
    current, raw = cockpit()
    raw["adjustments"][0]["excluded"] = True
    current["operating_plan"]["evaluation"] = earnings_improvement.evaluate(raw, current, now=NOW)
    result = projected(current)
    assert result["operating_plan"]["status"] == "none"
    assert not result["operating_plan"]["applied"]


def test_expense_increase_and_implementation_cost_reduce_projected_earnings():
    current, _ = cockpit()
    raw = body(action(kind="expense_increase", proposed_amount=1200, implementation_cost=600))
    raw = approve(raw, current)
    current["operating_plan"]["evaluation"] = earnings_improvement.evaluate(raw, current, now=NOW)
    result = projected(current)
    assert result["income_after_annual"] == -13000
    assert result["operating_plan"]["first_year_impact"] == -3000
    assert result["headline_after"] == -1.08


def test_one_time_benefit_is_shown_as_cash_but_never_raises_recurring_coverage():
    current, _ = cockpit(reviewed=False)
    raw = body(action(kind="one_time_adjustment", expense_row_id=None, current_amount=1000,
        proposed_amount=0, implementation_cost=100))
    assessed = earnings_improvement.evaluate(raw, current, now=NOW)
    current["operating_plan"]["evaluation"] = assessed
    result = projected(current)
    assert assessed["projection"]["proposed"]["first_year_earnings"] == -9100
    assert assessed["projection"]["proposed"]["first_year_dscr_current"] == -.83
    assert result["operating_plan"]["proposed_dscr"] == -.83


@pytest.mark.asyncio
async def test_terms_context_keeps_reviewed_improvement_before_budget_and_version_reference():
    from app.services import application_terms
    from app.services.financial_plan_operating import refresh_cockpit

    current, _ = cockpit({"new_loan_amount": 12000, "new_loan_rate_pct": 0,
        "new_loan_term_months": 12, "new_loan_structure": "amortizing"})
    refresh_cockpit(current, {})
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)):
        result = await application_terms._cockpit_context(SimpleNamespace(), SimpleNamespace())
    assert result.cash_flow == -7600
    assert "Reviewed operating earnings plan v2" in result.source
    assert result.operating_plan["version_id"] == "operating-version-2"
    assert result.operating_plan["applied_annual_impact"] == 2400


def test_supported_ebitda_and_noi_projections_share_reviewed_improvements_without_rewriting_sources():
    current, _ = cockpit()
    current["ebitda"]["headline"].update(kind="computed_ebitda", annualization_factor=1, months_covered=12)
    current["noi"]["headline"].update(complete=True, provisional=False, reconciliation_required=False)
    current["noi"]["scenario"] = {"current": -15000}
    raw = approve(body(), current)
    current["operating_plan"]["evaluation"] = earnings_improvement.evaluate(raw, current, now=NOW)
    result = projected(current)
    assert result["ebitda"]["annual_before"] == -10000
    assert result["ebitda"]["annual_after"] == -7600
    assert result["noi_scenario"]["current"] == -15000
    assert result["noi_scenario"]["plan"] == -12600
    assert current["ebitda"]["headline"]["annualized_value"] == -10000
    assert current["noi"]["headline"]["annualized_value"] == -15000


def test_legacy_review_does_not_stale_when_operating_only_projection_is_activated():
    from app.services.financial_plan_operating import refresh_cockpit

    current, raw = cockpit()
    action = raw["adjustments"][0]
    action["review_source_stamp"] = earnings_improvement._legacy_review_stamp(action, current, raw["expenses"][0], raw)
    current["operating_plan"]["evaluation"] = earnings_improvement.evaluate(raw, current, now=NOW)
    refresh_cockpit(current, {})
    assert current["plan"]["evaluation"]["operating_plan"]["applied"]
    assert earnings_improvement.evaluate(raw, current, now=NOW)["actions"][0]["review_state"] == "approved"
    current["plan"]["evaluation"]["loan_terms"] = {"amount": 200000, "rate_pct": 29.99}
    assert earnings_improvement.evaluate(raw, current, now=NOW)["actions"][0]["review_state"] == "stale"


def test_operating_plan_first_year_refinance_ratio_matches_main_budget_adjusted_coverage():
    current, raw = cockpit({"new_loan_amount": 12000, "new_loan_rate_pct": 0,
        "new_loan_term_months": 12, "new_loan_structure": "amortizing"})
    budget = {"currency": "USD", "costs_confirmed": True, "rows": [{"id": "service", "label": "Operating service",
        "enabled": True, "payer": "borrower", "category": "management", "method": "fixed", "value": 50,
        "basis": "loan_amount", "timing": "monthly", "settlement": "separate", "earnings_treatment": "operating_expense",
        "coverage_treatment": "new_cost"}]}
    result = projected(current, budget)
    current["plan"]["evaluation"] = result
    assessed = earnings_improvement.evaluate(raw, current, now=NOW)
    assert assessed["actions"][0]["review_state"] == "approved"
    assert assessed["projection"]["approved"]["first_year_dscr_with_refinance"] == result["headline_after"] == -.34
    assert assessed["projection"]["approved"]["first_year_dscr_current"] == -.63
