import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.funding_resource_calculations import (
    calculate_funding_report,
    payment,
    present_value,
)


def facts(**overrides):
    now = datetime.now(ZoneInfo("America/New_York"))
    current_month = now.year * 12 + now.month - 1
    months = [f"{value // 12:04d}-{value % 12 + 1:02d}" for value in range(current_month - 3, current_month)]
    return {"requested_amount": 100000, "tax_income": 120000, "tax_year": now.year - 1, "period_months": 12,
            "active_mca_count": 0, "modified_mca": False,
            "debts": [{"type": "other", "balance": 20000, "payment": 2000, "frequency": "monthly", "refinance": False}],
            "bank_months": [{"month": month, "beginning_balance": opening, "ending_balance": closing, "deposits": 50000, "nsf_count": 0}
                           for month, opening, closing in zip(months, [1000, 5000, 3000], [9000, 1000, 8000], strict=True)], **overrides}


def test_capacity_uses_earnings_coverage_and_retained_debt_not_deposits():
    report = calculate_funding_report("term_loan", facts())
    assert report["metrics"]["monthly_payment_capacity"] == 6000
    assert report["metrics"]["average_beginning_balance"] == 3000
    assert report["metrics"]["average_ending_balance"] == 6000
    assert "average_daily_balance" not in report["metrics"]
    assert report["status"] == "preliminary"
    changed = calculate_funding_report("term_loan", facts(annual_revenue=99999999))
    assert changed["scenarios"] == report["scenarios"]


def test_refinancing_does_not_double_count_retired_payments_or_payoffs():
    report = calculate_funding_report("mca_refinance", facts(active_mca_count=1, debts=[
        {"type": "mca", "balance": 50000, "payment": 500, "frequency": "daily", "refinance": True},
        {"type": "other", "balance": 20000, "payment": 1000, "frequency": "monthly", "refinance": False}]))
    assert report["metrics"]["current_monthly_payments"] == 11500
    assert report["metrics"]["retained_monthly_payments"] == 1000
    assert report["metrics"]["payoff_total"] == 50000
    scenario = report["scenarios"][0]
    assert scenario["monthly_difference"] == round(10500 - scenario["monthly_payment"], 2)
    assert scenario["net_cash"] == 50000


def test_zero_interest_and_fee_shortfall():
    assert payment(12000, 0, 12) == 1000
    assert present_value(1000, 0, 12) == 12000
    report = calculate_funding_report("mca_refinance", facts(active_mca_count=1, requested_amount=50000, debts=[
        {"type": "mca", "balance": 50000, "payment": 1000, "frequency": "monthly", "refinance": True}]),
        {"annual_rates_pct": [0, 0, 0], "term_months": 12, "fee_pct": 5})
    assert report["scenarios"][0]["funding_gap"] == 2500
    assert report["scenarios"][0]["net_cash"] == 0


def test_fast_funding_uses_factor_and_never_claims_capacity_or_apr():
    report = calculate_funding_report("merchant_cash_advance", facts())
    assert [item["total_repayment"] for item in report["scenarios"]] == [122000, 130000, 138000]
    assert all(item["modeled_capacity"] is None and item["annual_rate_pct"] is None and item["monthly_payment"] is None for item in report["scenarios"])
    assert report["application_path"] == "/mca-funding-intake"
    duration = calculate_funding_report("merchant_cash_advance", facts(), {"mca_duration_months": 10})
    assert duration["scenarios"][1]["monthly_payment"] == 13000


def test_product_rules_keep_addbacks_and_legacy_rbf_thresholds_separate():
    report = calculate_funding_report("merchant_cash_advance", facts(tax_income=-1, depreciation=100000))
    assert report["status"] == "manual_review"
    # No fabricated revenue/FICO/operating-age floor for new MCA.
    report = calculate_funding_report("merchant_cash_advance", facts(annual_revenue=1, business_age_years=0, credit_band="unknown"))
    assert report["status"] == "preliminary"
    assert calculate_funding_report("mca_refinance", facts(active_mca_count=3))["status"] == "manual_review"
    assert calculate_funding_report("mca_refinance", facts(requested_amount=350001))["status"] == "manual_review"
    older = calculate_funding_report("merchant_cash_advance", facts(tax_year=2024))
    assert older["status"] == "needs_information"
    assert any(item["key"] == "funding_tax_period" for item in older["readiness"])


def test_missing_values_and_short_year_remain_unknown():
    report = calculate_funding_report("term_loan", {})
    assert report["status"] == "needs_information"
    assert report["metrics"]["annual_earnings"] is None
    assert report["metrics"]["retained_monthly_payments"] is None
    assert all(item["modeled_capacity"] is None for item in report["scenarios"])
    assert calculate_funding_report("term_loan", facts(period_months=6))["metrics"]["annual_earnings"] is None
    assert calculate_funding_report("term_loan", facts(tax_income=-100))["status"] == "manual_review"


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, "100", 1e20])
def test_invalid_financial_inputs_are_rejected(value):
    with pytest.raises(ValueError):
        calculate_funding_report("term_loan", facts(requested_amount=value))


def test_duplicate_months_and_fractional_counts_are_rejected():
    data = facts()
    data["bank_months"][1]["month"] = data["bank_months"][0]["month"]
    with pytest.raises(ValueError):
        calculate_funding_report("term_loan", data)
    with pytest.raises(ValueError):
        calculate_funding_report("merchant_cash_advance", facts(active_mca_count=1.5))


def test_scenarios_remain_finite_and_payment_capacity_is_respected():
    report = calculate_funding_report("term_loan", facts(requested_amount=10000000), {"annual_rates_pct": [0, 25, 100], "term_months": 120})
    for scenario in report["scenarios"]:
        assert scenario["monthly_payment"] <= 6000.01
        assert math.isfinite(scenario["modeled_capacity"])


def test_unfunded_payoffs_cannot_claim_payment_relief():
    data = facts(active_mca_count=1, requested_amount=20000, debts=[
        {"type": "mca", "balance": 50000, "payment": 500, "frequency": "daily", "refinance": True}])
    report = calculate_funding_report("mca_refinance", data)
    assert report["status"] == "manual_review"
    assert all(item["monthly_difference"] is None and item["funding_gap"] == 30000 for item in report["scenarios"])


def test_nonconsecutive_future_and_stale_statement_periods_are_not_ready():
    data = facts()
    for row, month in zip(data["bank_months"], ["2024-01", "2026-05", "2099-12"], strict=True):
        row["month"] = month
    report = calculate_funding_report("term_loan", data)
    assert report["status"] == "needs_information"
    assert any(item["key"] == "bank_periods" and item["status"] == "missing" for item in report["readiness"])


def test_zero_refinance_positions_and_incomplete_debt_map_stay_unresolved():
    assert calculate_funding_report("mca_refinance", facts())["status"] == "needs_information"
    report = calculate_funding_report("term_loan", facts(active_mca_count=2))
    assert report["status"] == "needs_information"
    assert report["metrics"]["monthly_payment_capacity"] is None
    assert report["metrics"]["payoff_total"] is None
