"""Independent cash reconciliation examples for the investor cost schedule."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.schemas.deal_budget import DealBudgetPatch, DealCostRow
from app.services.deal_budget import calculate, update


def cost(**changes):
    return {"id": str(uuid4()), "label": "Synthetic cost", "category": "management", "payer": "investor",
        "method": "fixed", "value": 0, "basis": "loan_amount", "timing": "closing", "settlement": "separate",
        "enabled": True, "reserve_release": "unknown", "recipient_role": "platform", **changes}


def budget(rows, **changes):
    return {"funding_mode": "single_investor", "investor_funding_amount": None,
        "minimum_investor_contribution": None, "currency": "USD", "costs_confirmed": True, "rows": rows, **changes}


def terms(**changes):
    return {"amount": 100000, "rate_pct": 12, "term_months": 12, "structure": "interest_only",
        "payments_per_year": 12, "active_proposal": True, **changes}


def test_half_funded_note_reconciles_points_reserves_and_management_without_principal_as_income():
    result = calculate(budget([
        cost(label="Origination", category="origination", payer="borrower", method="percent", value=2, settlement="withheld"),
        cost(label="Borrower reserve", category="escrow_reserve", payer="borrower", value=5000, settlement="withheld"),
        cost(label="Investor closing charge", value=250),
        cost(label="Annual management", basis="outstanding_principal", method="percent", value=1, timing="annual", settlement="withheld"),
        cost(label="Investor reserve", category="escrow_reserve", value=1000, reserve_release="maturity", recipient_role="escrow"),
    ], investor_funding_amount=50000), terms())
    # Twelve monthly charges of $41.67 total $500.04. Refundable reserves
    # increase cash invested but do not become either fees or profit.
    assert result["borrower_withheld_fees"] == 2000
    assert result["borrower_withheld_reserves"] == 5000
    assert result["net_client_cash"] == 93000
    assert result["investor_gross_interest"] == 6000
    assert result["investor_gross_receipts"] == 56000
    assert result["investor_initial_outlay"] == 51250
    assert result["investor_fees"] == 750.04
    assert result["investor_net_income"] == 5249.96
    assert result["investor_net_roi_pct"] == 10.24
    assert round(sum(row["net_cash_flow"] for row in result["cash_flows"]), 2) == 5249.96


def test_separate_borrower_charge_never_reduces_investor_return_or_borrower_loan_disbursement():
    result = calculate(budget([cost(payer="borrower", category="closing_cost", value=1000)]), terms())
    assert result["net_client_cash"] == 100000
    assert result["borrower_paid_separately"] == 1000
    assert result["investor_fees"] == 0
    assert result["investor_net_income"] == 12000


def test_unknown_reserve_release_keeps_known_cash_outlay_but_does_not_invent_a_refund():
    result = calculate(budget([cost(category="escrow_reserve", value=1000, recipient_role="escrow")]), terms())
    assert result["investor_reserves"] == 1000
    assert result["investor_initial_outlay"] == 101000
    assert result["rows"][0]["total_amount"] == 1000
    assert result["investor_net_roi_pct"] is None
    assert any("release" in issue.lower() for issue in result["issues"])


def test_excluded_unknown_charge_and_unconfirmed_pricing_remain_distinct():
    result = calculate(budget([cost(value=None, enabled=False)]), terms())
    assert result["investor_net_roi_pct"] == 12
    assert result["rows"][0]["status"] == "excluded"
    unreviewed = calculate(budget([], costs_confirmed=False), terms())
    assert unreviewed["investor_gross_roi_pct"] == 12
    assert unreviewed["investor_net_roi_pct"] is None


def test_zero_rate_note_with_a_fee_has_negative_net_return_and_counts_maturity_once():
    result = calculate(budget([cost(value=1000)]), terms(rate_pct=0, term_months=24, structure="amortizing",
        repayment_config={"amortization_months": 60, "balloons": [], "after_balloon": "keep_payment"}))
    assert result["investor_gross_receipts"] == 100000
    assert result["investor_net_income"] == -1000
    assert result["investor_net_roi_pct"] == -0.99
    assert round(sum(row["net_cash_flow"] for row in result["cash_flows"]), 2) == -1000


def test_points_preserve_existing_four_decimal_rates():
    row = DealCostRow.model_validate(cost(category="origination", payer="borrower", method="percent", value="2.1250", settlement="withheld"))
    result = calculate(budget([row.model_dump(mode="json")]), terms())
    assert result["borrower_withheld_fees"] == 2125
    assert result["net_client_cash"] == 97875


def test_share_of_interest_is_applied_once_without_becoming_investor_fee_income():
    result = calculate(budget([cost(payer="borrower", basis="interest", method="percent", value=10, timing="annual")]), terms())
    assert result["borrower_recurring_fees"] == 1200
    assert result["investor_net_income"] == 12000


def test_fee_coverage_aligns_declining_fees_with_the_maturity_year_instead_of_adding_unrelated_peaks():
    # $1,666.67 regular principal for 24 months plus $59,999.92 at maturity.
    # Servicing is $10,900 in year one and $8,500 in the maturity year.
    # The loan's peak cash year is $79,999.96, so combined cash due is $88,499.96.
    loan = terms(rate_pct=0, term_months=24, structure="amortizing",
        repayment_config={"amortization_months": 60, "balloons": [], "after_balloon": "keep_payment"},
        coverage_context={"income_annual": 100000, "debt_service_annual": 79999.96,
            "new_loan_annual_debt_service": 79999.96, "base_dscr": 1.25, "basis": "synthetic_operating_earnings"})
    result = calculate(budget([cost(payer="borrower", method="percent", value=12,
        basis="outstanding_principal", timing="annual")]), loan)
    coverage = result["financing_fee_coverage"]
    assert coverage["method"] == "aligned_rolling_12_month_cash_due"
    assert coverage["annual_debt_service_including_fees"] == 88499.96
    assert coverage["with_financing_fees_dscr"] == pytest.approx(100000 / 88499.96, abs=0.0001)
    assert result["investor_net_income"] == 0


def test_unknown_exchange_rate_prevents_combining_fee_currency_with_note_currency():
    result = calculate(budget([cost(value=1000)], currency="EUR"), terms(currency="USD"))
    assert result["investor_net_income"] is None
    assert result["investor_net_roi_pct"] is None
    assert result["net_client_cash"] is None
    assert result["investor_funding_amount"] is None
    assert result["investor_gross_roi_pct"] is None
    assert result["rows"][0]["total_amount"] is None
    assert result["loan_currency"] == "USD"
    assert any("currenc" in issue.lower() for issue in result["issues"])


@pytest.mark.asyncio
async def test_foreign_cost_budget_cannot_project_unconverted_fees_into_usd_billing_fields():
    with pytest.raises(HTTPException) as error:
        await update(None, None, DealBudgetPatch(expected_revision=0, currency="EUR"), SimpleNamespace(role=Role.SUPER_ADMIN))
    assert error.value.status_code == 422


def test_separately_funded_borrower_escrow_is_cash_required_but_not_fee_income():
    result = calculate(budget([cost(category="escrow_reserve", payer="borrower", value=5000,
        recipient_role="escrow")]), terms())
    assert result["net_client_cash"] == 100000
    assert result["borrower_separate_reserves"] == 5000
    assert result["borrower_cash_after_separate_costs"] == 95000
    assert result["platform_fee_revenue"] == 0
    assert result["investor_net_income"] == 12000
