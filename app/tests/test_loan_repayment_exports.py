from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.capital_partners.schemas import ListingProductIn
from app.capital_partners.services.listings import partner_terms
from app.services.application_terms_pdf import render_terms_html
from app.services.deal_cost import internal_economics
from app.services.loan_repayment_display import repayment_lines
from app.services.loan_schedule import build_schedule
from app.services.public_underwriting_packet_pdf import _schedule_cash, _schedule_principal

CONFIG = {
    "amortization_months": 60,
    "after_balloon": "keep_payment",
    "balloons": [
        {"id": "first", "month": 6, "type": "amount", "value": 12000},
        {"id": "second", "month": 12, "type": "percent_balance", "value": 25},
    ],
}


def _schedule():
    return build_schedule(120000, 0, 24, structure="amortizing", repayment_config=CONFIG)


def _terms(**changes):
    values = dict(
        version=4, status="issued", program_name="Term loan", amount=120000, apr_pct=0,
        term_months=24, funder_type="bank", funder_name="Example bank",
        repayment_frequency="monthly", payments_per_year=12, custom_repayment_label=None,
        debt_service_treatment="additive", periodic_payment=2000, payment_count=24,
        annual_new_debt_service=63000, dscr_before=1.4, dscr_after=1.2,
        dscr_explanation="Stored evidence", dscr_source="Verified earnings",
        closing_estimate_days=7, issued_at=datetime(2026, 11, 6, tzinfo=UTC),
        created_at=datetime(2026, 11, 6, tzinfo=UTC), expires_on=date(2026, 11, 20),
        co_brand_enabled=False, sponsor_name=None, client_note=None, conditions=[],
        loan_structure="amortizing", interest_only_months=0, repayment_config=CONFIG,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_client_pdf_uses_actual_total_and_discloses_each_principal_obligation():
    html = render_terms_html(_terms(), business_name="Fictional Example LLC")
    assert "Estimated total repayment</span><strong>$120,000.00" in html
    assert "Starting regular monthly payment" in html
    assert "Peak rolling 12-month cash due" in html
    assert "25% of remaining principal = $21,000.00" in html
    assert "Month 6, payment 6" in html
    assert "Additional residual principal due at maturity: $39,000.00" in html
    assert "$63,000.00, including scheduled balloons and maturity principal once" in html
    assert "first" not in " ".join(repayment_lines(_schedule())).split("First-year")[0]


def test_listing_saves_config_and_prices_actual_cash_due_instead_of_browser_snapshot():
    product = ListingProductIn(label="Example", amount=120000, rate_pct=0, term_months=24,
        structure="amortizing", repayment_config=CONFIG, monthly_payment=1)
    assert float(product.monthly_payment) == 5250
    assert product.model_dump(mode="json")["repayment_config"]["balloons"][1]["value"] == 25
    terms = partner_terms({"repayment_config": CONFIG, "payments_per_year": 12, "internal_note": "SECRET"})
    assert terms == {"repayment_config": CONFIG, "payments_per_year": 12}


@pytest.mark.parametrize("changes", [{"factor_rate": 1.2}, {"amount": None}, {"term_months": 6}])
def test_listing_rejects_unsupported_or_out_of_maturity_balloons(changes):
    values = dict(label="Example", amount=120000, rate_pct=0, term_months=24,
                  structure="amortizing", repayment_config=CONFIG)
    with pytest.raises(ValidationError):
        ListingProductIn(**{**values, **changes})


def test_internal_cost_and_public_packet_count_balloons_once():
    schedule = build_schedule(120000, 12, 24, structure="amortizing", repayment_config=CONFIG)
    economics = internal_economics({}, {"amount": 120000, "rate_pct": 12,
        "schedule": {key: value for key, value in schedule.items() if key != "rows"},
        "schedule_rows": schedule["rows"]})
    assert economics["structure_rate_pct"] == pytest.approx(12, abs=0.01)
    assert sum(_schedule_cash(row) for row in schedule["rows"]) == pytest.approx(schedule["total_paid"], abs=.01)
    assert sum(_schedule_principal(row) for row in schedule["rows"]) == pytest.approx(120000, abs=.01)
    legacy = {"payment": 1000, "principal": 0, "balloon": 10000}
    assert _schedule_cash(legacy) == 11000
    assert _schedule_principal(legacy) == 10000


def test_internal_cost_can_price_the_entire_long_daily_schedule():
    schedule = build_schedule(100000, 12, 120, structure="amortizing", periods_per_year=252,
        repayment_config={"amortization_months": 180, "balloons": []})
    assert len(schedule["rows"]) == 2520
    economics = internal_economics({}, {"amount": 100000, "rate_pct": 12,
        "schedule": {key: value for key, value in schedule.items() if key != "rows"},
        "schedule_rows": schedule["rows"]})
    assert economics["structure_rate_pct"] == pytest.approx(12, abs=.01)
