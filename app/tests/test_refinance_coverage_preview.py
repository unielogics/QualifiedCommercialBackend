"""Saved-loan coverage joins working operating math without qualification shortcuts."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.operating_plan_guidance import client_evaluation
from app.tests.test_refinance_reinvestment import da_body, da_context, evaluate


def context():
    value = da_context()
    value["plan"]["evaluation"].update(debt_service_after_annual=90000,
                                       annual_new_operating_expenses=6000,
                                       annual_new_financing_fees=1200)
    return value


def test_coverage_window_and_operating_charges_are_used_once_before_prose_is_complete():
    current = context()
    raw = da_body(implementation_cost=25, implementation_cost_cadence="monthly",
                  effective_date=None, explanation="", source_note="")
    original = deepcopy((raw, current))
    result = evaluate(raw, current)
    working = result["draft_feedback"]["summary"]["working"]
    # 30k EBITDA - 300 recurring setup - 6k new loan operating charges, divided
    # by the saved90k annual stress window. The72k regular-payment equivalent
    # is a different cash calculation; financing fees are not subtracted twice.
    assert working["earnings"] == 29700
    assert working["refinance_annual_debt_service"] == 90000
    assert working["dscr_with_refinance_after"] == 0.2633
    assert working["dscr_with_refinance_delta"] == 0.0133
    assert working["dscr_current_debt_before"] == 0.25
    assert working["refinance_coverage_basis"] == "saved_coverage_window_after_new_operating_charges"
    assert result["projection"]["proposed"]["dscr_with_refinance"] is None
    assert working["first_year_earnings"] is None
    assert (raw, current) == original


def test_depreciation_alone_never_changes_coverage_earnings_or_creates_cash_income():
    result = evaluate(da_body(), context())
    working = result["draft_feedback"]["summary"]["working"]
    assert working["noi_delta"] == 6000
    assert working["earnings_delta"] == working["cash_delta_annual"] == 0
    assert working["dscr_with_refinance_after"] == 0.2667
    assert working["dscr_with_refinance_delta"] == 0.0167


@pytest.mark.parametrize("change", ["principal", "override", "coverage", "currency", "unknown_charge", "unknown_denominator", "not_saved"])
def test_unreconciled_or_unknown_loan_source_cannot_supply_a_planned_ratio(change):
    current = context()
    loan = current["plan"]["evaluation"]
    if change == "principal":
        loan["principal_solution"] = {"status": "calculated", "requires_apply": True}
    elif change == "override":
        current["plan"]["record"]["overrides"] = {"debt_service_monthly": 5071}
    elif change == "coverage":
        loan["coverage_unavailable_reason"] = "Synthetic saved loan needs source reconciliation"
    elif change == "currency":
        current["debt"]["service"]["currency"] = "CAD"
    elif change == "unknown_charge":
        loan["annual_new_operating_expenses"] = None
    elif change == "unknown_denominator":
        loan["debt_service_after_annual"] = None
    else:
        current["plan"]["record"] = {}
    result = evaluate(da_body(), current)
    working = result["draft_feedback"]["summary"]["working"]
    assert working["dscr_with_refinance_after"] is None
    assert working["dscr_with_refinance_delta"] is None
    assert working["refinance_annual_debt_service"] is None
    assert working["noi"] == 24000


def test_unknown_working_metric_keeps_a_known_loan_window_but_not_a_false_ratio():
    result = evaluate(da_body(proposed_amount=None), context())
    value = result["draft_feedback"]["summary"]["working"]
    assert value["refinance_annual_debt_service"] == 90000
    assert value["dscr_with_refinance_after"] is None and value["dscr_with_refinance_delta"] is None


def test_zero_debt_is_a_known_zero_denominator_not_an_infinite_ratio():
    current = context()
    current["plan"]["evaluation"]["debt_service_after_annual"] = 0
    value = evaluate(da_body(), current)["draft_feedback"]["summary"]["working"]
    assert value["refinance_annual_debt_service"] == 0
    assert value["dscr_with_refinance_after"] is None


def test_actual_saved_loan_schedule_uses_balloon_stress_window_and_fee_classification_once():
    from app.services import financial_plan as fp
    from app.tests.test_released_fee_amounts import budget, cost

    current = context()
    service = current["debt"]["service"]
    service["source"] = "debt_schedule_rows"
    obligations = fp.obligations({"rows": [{"id": "old", "lender": "Synthetic bank", "category": "loan", "is_mca": False,
        "monthly_payment": 10000, "balance": 50000, "count_in_dscr": True, "origin": "admin"}], "service": service}, {})
    plan = fp.normalize_plan({"retire": ["row:old"], "new_loan_amount": 100000, "new_loan_rate_pct": 12,
        "new_loan_term_months": 12, "new_loan_structure": "amortizing",
        "new_loan_repayment_config": {"amortization_months": 120, "after_balloon": "keep_payment", "balloons": []}})
    dscr = {"headline": {"key": "tax_return", "numerator": 30000},
            "candidates": [{"key": "tax_return", "label": "Synthetic source earnings", "numerator": 30000, "value": .25}]}
    loan = fp.evaluate_plan(plan, obligations_list=obligations, recurring={}, months=[], dscr=dscr, service=service,
        proposed=None, requested_amount=100000, financing_budget=budget([cost(100, treatment="operating_expense"), cost(50)]))
    assert loan["schedule"]["maturity_balloon"] == 94486.79
    assert loan["debt_service_after_regular_monthly"] == 1484.71
    # Twelve1434.71 installments plus94486.79 maturity principal, then600
    # financing fees. The1200 operating charges belong in the numerator.
    assert loan["debt_service_after_annual"] == 112303.31
    current["plan"] = {"record": {**plan, "updated_at": "2026-01-01T00:00:00Z"}, "evaluation": loan}
    working = evaluate(da_body(), current)["draft_feedback"]["summary"]["working"]
    assert working["refinance_annual_debt_service"] == 112303.31
    assert working["dscr_with_refinance_after"] == .2564
    assert working["dscr_with_refinance_delta"] == .0064


def test_public_projection_and_legacy_saved_summary_have_separate_allowlists():
    from app.services.earnings_improvement import package_summary

    result = evaluate(da_body(), context())
    safe = client_evaluation(result)
    working = safe["draft_feedback"]["summary"]["working"]
    assert working["dscr_with_refinance_after"] == 0.2667
    summary = package_summary(result)
    assert "draft_feedback" not in summary
    assert "refinance_coverage_basis" not in str(summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_real_staff_and_public_evaluate_allowlist_the_canonical_saved_loan_projection(public):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.enums import Role
    from app.routers import application_profiles, operating_plan
    from app.services import financial_workspace
    from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory

    raw = da_body(implementation_cost=0, effective_date=None, explanation="", source_note="")
    db, app = ReadOnlyMemory(), FastAPI()
    db.draft.body = deepcopy(raw)
    routes = application_profiles if public else operating_plan
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    path = f"/application-profiles/public/financial-forms/{TOKEN}/evaluate" if public else f"/application-profiles/{db.profile.id}/operating-plan/evaluate"
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=context())), \
         patch.object(operating_plan.profiles, "load_profile", AsyncMock(return_value=db.profile)), \
         patch.object(financial_workspace, "existing_family", AsyncMock(return_value=db.family)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json={"body": raw})
            assert response.status_code == 200, response.text
            working = response.json()["evaluation"]["draft_feedback"]["summary"]["working"]
            assert working["dscr_with_refinance_after"] == .2667
            assert working["dscr_with_refinance_delta"] == .0167
            assert working["refinance_annual_debt_service"] == 90000
    db.assert_unchanged(raw)
