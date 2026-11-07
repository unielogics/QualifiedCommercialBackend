"""A missing rate never prices a free loan or establishes after-plan coverage."""
from copy import deepcopy

import pytest

from app.services.financial_plan import evaluate_plan, normalize_plan


def evaluate(values, proposed=None):
    return evaluate_plan(normalize_plan(values), obligations_list=[], recurring={"payees": [], "months_read": []},
        months=[], dscr={"headline": {"key": "profit_and_loss", "numerator": 120000, "value": 10},
        "candidates": [{"key": "profit_and_loss", "numerator": 120000, "value": 10}]},
        service={"monthly": 1000, "annual": 12000}, proposed=proposed, requested_amount=None)


@pytest.mark.parametrize("extra", [{}, {"new_loan_structure": "interest_only", "new_loan_interest_only": True},
    {"new_loan_repayment_config": {"amortization_months": 120, "after_balloon": "keep_payment", "balloons": []}}])
def test_missing_rate_never_creates_a_zero_rate_schedule_or_after_plan_dscr(extra):
    values={"new_loan_amount": 100000, "new_loan_term_months": 120, **extra}
    before=deepcopy(values)
    result=evaluate(values)
    assert result["schedule"] is None and result["loan_terms"] is None
    assert result["new_payment_monthly"] is None
    assert result["debt_service_before_new_payment_monthly"] == 1000
    assert result["debt_service_after_monthly"] is None and result["debt_service_after_annual"] is None
    assert result["headline_after"] is None and not result["no_debt_left"]
    assert "new loan payment is unknown" in result["coverage_unavailable_reason"]
    assert values == before


def test_explicit_zero_rate_is_a_valid_zero_interest_loan():
    result=evaluate({"new_loan_amount": 120000, "new_loan_term_months": 120, "new_loan_rate_pct": 0})
    assert result["schedule"]["rate_pct"] == 0
    assert result["new_payment_monthly"] == 1000
    assert result["debt_service_after_annual"] == 24000 and result["headline_after"] == 5


@pytest.mark.parametrize("payment", [0, 500])
def test_reviewed_explicit_payment_remains_usable_without_inventing_a_schedule(payment):
    result=evaluate({"new_loan_amount": 100000, "new_loan_term_months": 120,
                     "new_payment_monthly": payment, "new_payment_source": "manual"})
    assert result["schedule"] is None
    assert result["new_payment_monthly"] == payment
    assert result["debt_service_after_annual"] == (1000+payment)*12
    assert result["headline_after"] == round(120000/((1000+payment)*12), 2)
    assert result["coverage_unavailable_reason"] is None


def test_an_unpriced_custom_loan_cannot_reuse_another_recorded_payment():
    result=evaluate({"new_loan_amount": 100000, "new_loan_term_months": 120, "new_payment_source": "term_sheet"},
                    proposed={"amount": 20000, "rate_pct": 12, "term_months": 60, "monthly_payment": 444})
    assert result["new_payment_monthly"] is None and result["headline_after"] is None


def test_no_new_loan_keeps_the_current_known_coverage():
    result=evaluate({})
    assert result["new_payment_monthly"] is None and result["headline_after"] == 10


def test_orphaned_config_without_a_loan_keeps_a_reviewed_payment_usable():
    result=evaluate({"new_payment_monthly": 500, "new_payment_source": "recorded_terms",
                     "new_loan_term_months": 12, "new_loan_repayment_config": {"balloons": []}})
    assert result["schedule"] is None
    assert result["debt_service_after_annual"] == 18000
    assert result["coverage_unavailable_reason"] is None


def test_adopted_schedule_cannot_price_balloon_cash_with_a_missing_rate():
    result=evaluate({"new_payment_source": "term_sheet"}, proposed={"amount": 100000,
        "rate_pct": None, "term_months": 120, "monthly_payment": 500,
        "repayment_config": {"balloons": [{"id": "balloon", "month": 12, "type": "amount", "value": 50000}]}})
    assert result["new_payment_monthly"] == 500 and result["schedule"] is None
    assert result["debt_service_after_annual"] is None and result["headline_after"] is None


def test_a_regular_manual_payment_cannot_erase_unpriced_contractual_balloon_cash():
    result=evaluate({"new_loan_amount": 100000, "new_loan_term_months": 120,
        "new_payment_monthly": 500, "new_payment_source": "manual",
        "new_loan_repayment_config": {"amortization_months": 120, "after_balloon": "keep_payment",
            "balloons": [{"id": "balloon", "month": 12, "type": "amount", "value": 50000}]}})
    assert result["new_payment_monthly"] == 500 and result["schedule"] is None
    assert result["debt_service_after_annual"] is None and result["headline_after"] is None
