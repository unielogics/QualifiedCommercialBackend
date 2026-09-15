from datetime import UTC, date, datetime
from math import isclose
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.application_terms import ClientTermsWrite
from app.services.application_terms import DscrContext, _calculation, repayment_math
from app.services.application_terms_pdf import filename_for, render_terms_html


def _payload(**overrides):
    values = {
        "expected_version": 0,
        "loan_type": "business_term_loan",
        "amount": 750_000,
        "apr_pct": 12.99,
        "term_months": 36,
        "funder_type": "private_fund",
        "repayment_frequency": "monthly",
        "expiration_days": 7,
        "closing_estimate_days": 5,
    }
    values.update(overrides)
    return values


def test_monthly_payment_math_uses_apr_and_term() -> None:
    payment, count, periods, annual, total, cost = repayment_math(
        amount=750_000,
        apr_pct=12.99,
        term_months=36,
        frequency="monthly",
    )

    rate = 0.1299 / 12
    independent_payment = 750_000 * rate / (1 - (1 + rate) ** -36)
    assert count == 36
    assert periods == 12
    assert isclose(payment, independent_payment, abs_tol=0.01)
    assert isclose(annual, payment * 12, abs_tol=0.12)
    assert isclose(total, payment * count, abs_tol=0.36)
    assert isclose(cost, total - 750_000, abs_tol=0.01)


@pytest.mark.parametrize(
    ("frequency", "expected_periods"),
    [("daily", 252), ("weekly", 51.96), ("biweekly", 25.98), ("monthly", 12)],
)
def test_standard_repayment_cadences_are_annualized(frequency: str, expected_periods: float) -> None:
    _, _, periods, annual, _, _ = repayment_math(
        amount=100_000,
        apr_pct=10,
        term_months=24,
        frequency=frequency,
    )

    assert periods == expected_periods
    assert annual > 0


def test_short_term_annual_debt_service_is_capped_at_total_scheduled_payments() -> None:
    payment, count, periods, annual, total, _ = repayment_math(
        amount=120_000,
        apr_pct=0,
        term_months=6,
        frequency="monthly",
    )

    assert (payment, count, periods) == (20_000, 6, 12)
    assert annual == 120_000
    assert total == 120_000


def test_half_payment_counts_use_the_same_half_up_rule_as_the_browser() -> None:
    _, count, periods, _, _, _ = repayment_math(
        amount=100_000,
        apr_pct=10,
        term_months=6,
        frequency="custom",
        custom_payments_per_year=5,
    )

    assert periods == 5
    assert count == 3


def test_real_estate_additive_dscr_keeps_current_pitia() -> None:
    context = DscrContext(
        method="real_estate",
        before=1.2,
        cash_flow=120_000,
        cash_flow_label="Annual gross rent",
        current_annual_debt_service=100_000,
        real_estate_carrying_costs=20_000,
        source="fixture",
    )

    result = _calculation(
        context=context,
        payment=4_166.67,
        payment_count=12,
        payments_per_year=12,
        new_annual_debt_service=50_000,
        amount=50_000,
        total_repayment=50_000,
        treatment="additive",
    )

    assert result.projected_annual_debt_service == 150_000
    assert result.dscr_after == 0.8


def test_real_estate_refinance_dscr_uses_carrying_costs_and_retained_debt() -> None:
    context = DscrContext(
        method="real_estate",
        before=1.2,
        cash_flow=120_000,
        cash_flow_label="Annual gross rent",
        current_annual_debt_service=100_000,
        real_estate_carrying_costs=20_000,
        source="fixture",
    )

    result = _calculation(
        context=context,
        payment=4_166.67,
        payment_count=12,
        payments_per_year=12,
        new_annual_debt_service=50_000,
        amount=50_000,
        total_repayment=50_000,
        treatment="refinance",
        retained_annual_debt_service=10_000,
    )

    assert result.projected_annual_debt_service == 80_000
    assert result.dscr_after == 1.5


def test_custom_repayment_requires_an_explicit_frequency() -> None:
    with pytest.raises(ValidationError, match="payments per year"):
        ClientTermsWrite(**_payload(repayment_frequency="custom"))

    payload = ClientTermsWrite(
        **_payload(
            repayment_frequency="custom",
            custom_payments_per_year=18,
            custom_repayment_label="Every 20 days",
        )
    )
    assert payload.custom_payments_per_year == 18


def test_refinance_requires_explicit_retained_debt_service() -> None:
    with pytest.raises(ValidationError, match="annual debt service"):
        ClientTermsWrite(**_payload(debt_service_treatment="refinance"))

    full_payoff = ClientTermsWrite(
        **_payload(
            debt_service_treatment="refinance",
            retained_annual_debt_service=0,
        )
    )
    assert full_payoff.retained_annual_debt_service == 0


def test_non_refinance_drops_irrelevant_retained_debt_service() -> None:
    payload = ClientTermsWrite(**_payload(retained_annual_debt_service=45_000))
    assert payload.retained_annual_debt_service is None


def test_client_pdf_html_uses_exact_terms_and_urchoice_brand() -> None:
    issued = datetime(2026, 9, 15, tzinfo=UTC)
    row = SimpleNamespace(
        version=2,
        status="issued",
        program_name="Commercial Debt Refinance",
        amount=750_000,
        apr_pct=12.99,
        term_months=36,
        funder_type="private_fund",
        funder_name="Example fund",
        repayment_frequency="monthly",
        custom_repayment_label=None,
        debt_service_treatment="refinance",
        periodic_payment=25_265.32,
        payment_count=36,
        annual_new_debt_service=303_183.84,
        dscr_before=1.20,
        dscr_after=1.50,
        dscr_explanation="Calculated from verified evidence.",
        dscr_source="Verified file evidence",
        closing_estimate_days=7,
        issued_at=issued,
        created_at=issued,
        expires_on=date(2026, 9, 25),
        co_brand_enabled=True,
        sponsor_name="UrChoice",
        client_note="Consolidation structure.",
        conditions=["Final verification."],
    )

    html = render_terms_html(row, business_name="Northstar LLC", client_name="Jordan Morgan")

    assert "Qualified Commercial" in html
    assert "data:image/png;base64," in html
    assert "Commercial Debt Refinance" in html
    assert "$750,000.00" in html
    assert "12.99%" in html
    assert "Before acceptance" in html and "After acceptance" in html
    assert filename_for(row, "Northstar LLC") == "Northstar-LLC-Financing-Terms-v2.pdf"
    assert filename_for(row, "株式会社") == "Client-Financing-Terms-v2.pdf"
