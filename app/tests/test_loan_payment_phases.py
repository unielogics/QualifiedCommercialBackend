"""Contractual installments, phase cash and balloon stress stay distinct."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.capital_partners.schemas import ListingProductIn
from app.capital_partners.services.listings import project_product_payments
from app.schemas.application_terms import ClientTermsWrite
from app.services import application_terms, financial_plan, plan_terms_link
from app.services import production_term_structure as production
from app.services.loan_schedule import build_schedule

CONFIG = {"amortization_months": 60, "after_balloon": "keep_payment", "balloons": []}


def quote(**extra):
    return build_schedule(89934, 29.99, 24, structure="amortizing", interest_only_months=6, repayment_config=CONFIG, **extra)


def plan_result(*, obligation_payment=14943, before_service=20000, **extra):
    plan = financial_plan.normalize_plan({"retire": ["old"], "new_loan_amount": 89934, "new_loan_rate_pct": 29.99,
        "new_loan_term_months": 24, "new_loan_structure": "amortizing", "new_loan_interest_only_months": 6,
        "new_loan_repayment_config": CONFIG, **extra})
    return financial_plan.evaluate_plan(plan, obligations_list=[{"id": "old", "monthly": obligation_payment, "in_debt_service": True}],
        recurring={}, months=[{"month": "2026-01", "deposits": 30000, "withdrawals": 25000,
            "net": 5000, "ending_balance": 10000, "low_balance": 1000}],
        dscr={"headline": {"key": "profit_and_loss", "numerator": 120000},
            "candidates": [{"key": "profit_and_loss", "numerator": 120000}]},
        service={"monthly": before_service}, proposed=None, requested_amount=None)


def test_screenshot_regular_installments_are_not_balloon_coverage_average():
    s = quote()
    assert s["payment_io_monthly"] == 2247.6
    assert s["ordinary_payment_monthly"] == s["payment_after_monthly"] == 2909.12
    assert s["coverage_payment_monthly"] == 9169.63
    assert s["coverage_annual_debt_service"] == 110035.55
    assert s["first_year_regular_debt_service"] == 30940.32
    assert s["balloon"] == 75126.11
    assert s["rows"][-1]["cash_payment"] == 78035.23
    assert sum(p["cash_due"] for p in s["payment_phases"]) == pytest.approx(s["total_paid"])
    io, repayment = s["payment_phases"]
    assert (io["phase"], io["start_period"], io["end_period"], io["payment_count"], io["payment"]) == ("interest_only", 1, 6, 6, 2247.6)
    assert (repayment["phase"], repayment["start_period"], repayment["end_period"], repayment["payment_count"], repayment["payment"]) == ("repayment", 7, 24, 18, 2909.12)
    assert repayment["maturity_balloon"] == 75126.11


def test_financial_plan_cash_projection_and_savings_use_regular_payment_without_erasing_stress():
    result = plan_result()
    assert result["new_payment_monthly"] == 9169.63
    assert result["new_regular_payment_monthly"] == 2909.12
    assert result["new_interest_only_payment_monthly"] == 2247.6
    assert result["debt_service_after_regular_monthly"] == 7966.12
    assert result["debt_service_after_interest_only_monthly"] == 7304.6
    assert result["net_regular_monthly_change"] == -12033.88
    assert result["replay"][0]["added"] == 2909.12
    assert result["replay"][0]["net_after"] == 17033.88
    assert result["debt_service_after_annual"] == 5057 * 12 + 110035.55
    assert result["replay_basis"] == "regular_payment_proxy"


def test_unpriced_configured_loan_does_not_claim_zero_cost_cash_improvement():
    result = plan_result(new_loan_rate_pct=None, new_payment_monthly=1000)
    assert result["new_regular_payment_monthly"] is None
    assert result["debt_service_after_regular_monthly"] is None
    assert result["replay_projection_available"] is False
    assert result["replay"][0]["net"] == 5000
    for field in ("net_after", "ending_after", "lowest_after", "added", "cumulative"):
        assert result["replay"][0][field] is None
    assert result["cash_kept"] is None and result["months_dipping_negative_after"] is None


def test_recast_after_earlier_io_curtailment_does_not_label_peak_io_as_repayment():
    s = build_schedule(100000, 12, 24, structure="amortizing", interest_only_months=6,
        repayment_config={"amortization_months": 60, "after_balloon": "recast", "balloons": [{"id": "paydown", "month": 3, "type": "percent_balance", "value": 80}]})
    assert s["payment_io_monthly"] == 1000
    assert s["payment_after_monthly"] == s["ordinary_payment_monthly"] == 444.89
    assert s["regular_coverage_payment_monthly"] == 1000
    assert s["payment_phases"][0]["payment_min"] == 200


def test_client_and_production_terms_keep_the_same_phase_and_stress_economics():
    s = quote()
    block = application_terms.structure_block(amount=89934, apr_pct=29.99, term_months=24, frequency="monthly",
        custom_payments_per_year=None, loan_structure="amortizing", interest_only_months=6, repayment_config=CONFIG)
    assert block["regular_payment_monthly"] == 2909.12
    assert block["interest_only_payment_monthly"] == 2247.6
    assert block["max_annual_debt_service"] == 110035.55
    production_quote = production.calculate({"approved_amount": 89934, "rate_pct": 29.99, "term_months": 24,
        "repayment_structure": "fully_amortizing", "interest_only_months": 6, "payment_frequency": "monthly", "repayment_config": CONFIG})
    assert production_quote["repayment_structure"] == "interest_only_then_amortizing"
    assert production_quote["periodic_payment"] == 2247.6 and production_quote["post_io_payment"] == 2909.12
    assert production_quote["balloon_amount"] == s["balloon"]
    assert production_quote["annual_debt_service"] == s["coverage_annual_debt_service"]


def test_recorded_terms_keep_coverage_scalar_but_supply_actual_phase_amounts():
    row = SimpleNamespace(id=uuid4(), version=1, program_name="Term", amount=89934, apr_pct=29.99, term_months=24,
        loan_structure="amortizing", interest_only_months=6, payments_per_year=12, periodic_payment=2909.12,
        annual_new_debt_service=110035.55, repayment_config=CONFIG)
    recorded = plan_terms_link.recorded_from_client_terms(row)
    assert recorded["payment_monthly"] == 9169.63
    assert recorded["regular_payment_monthly"] == 2909.12
    assert recorded["interest_only_payment_monthly"] == 2247.6
    assert recorded["maturity_balance"] == 75126.11


def test_offer_product_does_not_store_coverage_stress_as_contractual_payment():
    product = ListingProductIn.model_validate({"label": "Term", "amount": 89934, "rate_pct": 29.99,
        "term_months": 24, "structure": "amortizing", "interest_only_months": 6, "repayment_config": CONFIG})
    assert float(product.monthly_payment) == 2909.12
    assert float(product.coverage_payment_monthly) == 9169.63
    assert float(product.interest_only_payment_monthly) == 2247.6


def test_existing_offer_display_is_corrected_without_rewriting_its_saved_json():
    saved = {"label": "Term", "amount": 89934, "rate_pct": 29.99, "term_months": 24,
        "structure": "amortizing", "interest_only_months": 6, "repayment_config": CONFIG, "monthly_payment": 9169.63}
    read = project_product_payments(saved)
    assert float(read["monthly_payment"]) == 2909.12 and float(read["coverage_payment_monthly"]) == 9169.63
    assert saved["monthly_payment"] == 9169.63
    missing_rate = project_product_payments({**saved, "rate_pct": None, "coverage_payment_monthly": 9169.63,
        "payment_phases": [{"phase": "repayment", "payment": 2909.12}]})
    assert missing_rate["monthly_payment"] is None and missing_rate["payment_projection_unavailable"]
    assert missing_rate["coverage_payment_monthly"] is None and missing_rate["payment_phases"] == []


def test_retire_only_projection_is_known_but_replacement_terms_remain_explicitly_missing():
    result = plan_result(new_loan_amount=None, new_loan_rate_pct=None, new_loan_term_months=None,
                         new_loan_repayment_config=None, new_loan_interest_only_months=0)
    assert result["new_payment_monthly"] is None and result["new_regular_payment_monthly"] == 0
    assert result["debt_service_after_regular_monthly"] == 5057
    assert result["ebitda"]["replacement_payment_missing"]


def test_early_payoff_is_not_a_cadence_adjustment():
    s = build_schedule(10000, 12, 24, structure="amortizing", interest_only_months=6,
        repayment_config={"amortization_months": 60, "balloons": [{"id": "full", "month": 3, "type": "percent_balance", "value": 100}]})
    assert s["actual_payoff_month"] == 3 and s["cadence_adjustment_note"] is None


def test_loan_modifications_and_export_impact_do_not_call_balloon_stress_an_installment():
    result = plan_result()
    mods = financial_plan.describe_modifications({}, result, [], {})
    loan = next(line for line in mods if line["kind"] == "loan")
    assert loan["monthly_effect"] == -2909.12
    assert "75,126.11" in loan["detail"] and "coverage stress" in loan["detail"]
    impact = financial_plan.plan_impact(result, {})
    assert impact["loan"]["payment_monthly"] == 2909.12
    assert impact["loan"]["coverage_payment_monthly"] == 9169.63
    assert impact["debt_service_monthly"]["after"] == 7966.12
    assert impact["coverage_debt_service_monthly"]["after"] == 14226.63


@pytest.mark.parametrize("io", [-1, .5, 25, True])
def test_legacy_and_configured_schedules_reject_invalid_io_instead_of_clamping(io):
    for config in (None, CONFIG):
        with pytest.raises(ValueError):
            build_schedule(10000, 12, 24, interest_only_months=io, repayment_config=config)


def test_cadence_mapping_is_visible_and_shorter_than_a_payment_period_is_rejected():
    s = build_schedule(10000, 12, 24, structure="amortizing", interest_only_months=1, periods_per_year=4)
    assert s["requested_interest_only_months"] == 1
    assert s["interest_only_months"] == 0
    assert "nearest scheduled payment period" in s["cadence_adjustment_note"]
    with pytest.raises(ValueError, match="at least one payment period"):
        build_schedule(10000, 12, 1, periods_per_year=4)


@pytest.mark.parametrize("config", [None, {"balloons": []}])
def test_all_io_at_maturity_round_trips_as_io_without_an_invented_amortization(config):
    payload = ClientTermsWrite.model_validate({"expected_version": 0, "loan_type": "term_loan", "amount": 10000,
        "apr_pct": 12, "term_months": 24, "funder_type": "bank", "repayment_frequency": "monthly",
        "loan_structure": "amortizing", "interest_only_months": 24, "repayment_config": config,
        "expiration_days": 30, "closing_estimate_days": 10})
    block = application_terms.structure_block(amount=payload.amount, apr_pct=payload.apr_pct, term_months=payload.term_months,
        frequency=payload.repayment_frequency, custom_payments_per_year=None, loan_structure=payload.loan_structure,
        interest_only_months=payload.interest_only_months, repayment_config=config)
    assert block["loan_structure"] == "interest_only" and block["regular_payment_monthly"] == 100
    result = production.calculate({"approved_amount": 10000, "rate_pct": 12, "term_months": 24,
        "repayment_structure": "interest_only_then_amortizing", "interest_only_months": 24, "repayment_config": config})
    assert result["repayment_structure"] == "interest_only" and result["post_io_payment"] is None
    assert not production.validation_errors(result)


def test_explicit_zero_manual_payment_is_not_replaced_or_treated_as_unknown():
    result = plan_result(new_loan_amount=None, new_loan_rate_pct=None, new_loan_term_months=None,
        new_loan_repayment_config=None, new_loan_interest_only_months=0, new_payment_monthly=0, new_payment_source="manual")
    assert result["new_payment_monthly"] == result["new_regular_payment_monthly"] == 0
    assert result["replay_projection_available"] is True and result["replay"][0]["added"] == 0


def test_operating_net_change_respects_clamped_retirements_and_unknown_retirement_cash():
    result = plan_result(obligation_payment=2000, before_service=1000)
    assert result["debt_service_after_regular_monthly"] == 2909.12
    assert result["net_regular_monthly_change"] == 1909.12
    unknown = plan_result(obligation_payment=None)
    assert unknown["replay_projection_available"] is False and unknown["replay"][0]["net"] == 5000
    for field in ("removed", "added", "net_after", "ending_after", "lowest_after"):
        assert unknown["replay"][0][field] is None


def test_advanced_plan_does_not_offer_legacy_quotes_that_drop_its_contract_terms():
    result = plan_result()
    plan = financial_plan.normalize_plan({"new_loan_amount": 89934, "new_loan_rate_pct": 29.99,
        "new_loan_term_months": 24, "new_loan_repayment_config": CONFIG, "new_loan_interest_only_months": 6})
    levers = financial_plan.coverage_levers(plan, result,
        obligations_list=[{"id": "other", "is_mca": True, "in_debt_service": True, "monthly": 500, "payoff": 5000}],
        recurring={}, dscr={"headline": {"key": "profit_and_loss", "numerator": 120000}}, requested_amount=10000, terms=None)
    assert not any(lever["key"] == "refinance_advances" or lever["key"].startswith("stretch") for lever in levers)
