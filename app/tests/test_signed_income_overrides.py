"""Signed staff earnings corrections survive the public plan write contract."""

import pytest
from pydantic import ValidationError

from app.schemas.financial_cockpit import FinancialPlanWrite
from app.services import financial_plan as plans
from app.tests.test_financial_plan_earnings import planned, projection


@pytest.mark.parametrize("income", [-24000.5, 0, 120000, None])
def test_plan_schema_normalization_and_evaluation_preserve_signed_income(income):
    request = FinancialPlanWrite.model_validate({"overrides": {
        "income_annual": income, "income_note": "Reviewed annual earnings correction",
        "debt_service_monthly": 1000, "debt_service_note": "Reviewed debt service",
    }})
    plan = plans.normalize_plan(request.model_dump())
    result = plans.evaluate_plan(plan, obligations_list=[], recurring={}, months=[],
        dscr={"headline": {"key": "profit_and_loss", "numerator": 120000},
              "candidates": [{"key": "profit_and_loss", "numerator": 120000}]},
        service={"monthly": 5000}, proposed=None, requested_amount=None)
    expected = income if income is not None else 120000
    assert plan["overrides"]["income_annual"] == income
    assert plan["overrides"]["income_note"] == "Reviewed annual earnings correction"
    assert result["income_after_annual"] == expected
    assert result["headline_after"] == round(expected / 12000, 2)
    assert result["debt_service_after_monthly"] == 1000
    if income is not None and income <= 0:
        assert result["headline_after_status"] == "concerning"
        assert not result["no_debt_left"]


@pytest.mark.parametrize("income", [float("nan"), float("inf"), -float("inf"), "NaN", "Infinity", "-Infinity"])
def test_nonfinite_staff_income_cannot_enter_a_plan_write(income):
    with pytest.raises(ValidationError, match="finite"):
        FinancialPlanWrite.model_validate({"overrides": {"income_annual": income}})
    normalized = plans.normalize_overrides({"income_annual": income, "debt_service_monthly": 0})
    assert normalized["income_annual"] is None
    assert normalized["debt_service_monthly"] == 0


@pytest.mark.parametrize("income", [None, "invalid"])
def test_unknown_or_invalid_legacy_income_does_not_become_zero(income):
    assert plans.normalize_overrides({"income_annual": income})["income_annual"] is None


def test_allowing_income_losses_does_not_allow_negative_debt_or_loan_amounts():
    for payload in [{"overrides": {"debt_service_monthly": -1}}, {"new_payment_monthly": -1}, {"new_loan_amount": -1}]:
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            FinancialPlanWrite.model_validate(payload)
    assert plans.normalize_overrides({"income_annual": -24000, "debt_service_monthly": -1}) == {
        "income_annual": -24000, "income_note": None, "debt_service_monthly": None, "debt_service_note": None,
    }


def test_negative_coverage_override_does_not_rewrite_source_ebitda():
    cockpit = planned(overrides={"income_annual": -24000, "income_note": "Hypothetical loss basis"})
    result = projection(cockpit)
    assert cockpit["plan"]["evaluation"]["income_after_annual"] == -24000
    assert cockpit["plan"]["evaluation"]["headline_after"] == -2
    assert result["before"] == result["after"] == 26000
    assert result["annual_before"] == result["annual_after"] == 52000
