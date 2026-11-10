"""Loan exhibits must reconcile to the saved schedule and stay client safe."""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.application_terms_pdf import render_terms_html
from app.services.loan_schedule import build_schedule
from app.services.loan_terms_visuals import annual_cash_rows, schedule_visuals_html
from app.services.production_term_sheet_pdf import render_term_sheet_html
from app.tests.test_loan_repayment_exports import _terms
from app.tests.test_production_term_sheet_pdf import _sheet


def test_annual_exhibit_separates_regular_extra_and_maturity_principal_once():
    # Zero interest gives an independent, exact expected cash waterfall.
    schedule = build_schedule(120000, 0, 24, structure="amortizing", repayment_config={
        "amortization_months": 60, "after_balloon": "keep_payment", "balloons": [
            {"id": "fixed", "month": 6, "type": "amount", "value": 12000},
            {"id": "percent", "month": 12, "type": "percent_balance", "value": 25},
        ],
    })
    before = deepcopy(schedule)
    years = annual_cash_rows(schedule)
    assert len(years) == 2
    assert years[0]["regular_principal"] == 24000
    assert years[0]["scheduled_balloon"] == 33000
    assert years[0]["maturity_balloon"] == 0
    assert years[0]["cash_due"] == 57000
    assert years[0]["ending_balance"] == 63000
    assert years[1]["regular_principal"] == 24000
    assert years[1]["scheduled_balloon"] == 0
    assert years[1]["maturity_balloon"] == 39000
    assert years[1]["cash_due"] == 63000
    assert years[1]["ending_balance"] == 0
    assert sum(item["interest"] for item in years) == 0
    assert sum(item["cash_due"] for item in years) == 120000
    assert schedule == before, "Rendering must never mutate a saved schedule"


def test_two_year_maturity_is_independent_of_five_year_amortization():
    amount, annual_rate = 262529, .2999
    schedule = build_schedule(amount, annual_rate * 100, 24, structure="amortizing",
        interest_only_months=6, repayment_config={"amortization_months": 60, "balloons": []})
    years = annual_cash_rows(schedule)
    rate = annual_rate / 12
    payment = amount * rate / (1 - (1 + rate) ** -60)
    residual = amount * (1 + rate) ** 18 - payment * ((1 + rate) ** 18 - 1) / rate
    assert schedule["payment_io"] == pytest.approx(amount * rate, abs=.01)
    assert schedule["payment_after"] == pytest.approx(payment, abs=.01)
    # Engine rounds each period's cash to cents; formula is unrounded.
    assert sum(item["maturity_balloon"] for item in years) == pytest.approx(residual, abs=.10)
    assert years[-1]["ending_balance"] == 0
    assert sum(item["cash_due"] for item in years) == pytest.approx(schedule["total_paid"], abs=.01)
    html = schedule_visuals_html(schedule)
    assert 'data-chart="regular-payments"' in html
    assert 'data-chart="payments"' in html
    assert 'data-chart="principal"' in html
    assert html.count("data-chart=") == 3
    assert 'aria-label="Regular principal and interest by loan month"' in html
    assert 'aria-label="Payments by loan month"' in html
    assert 'aria-label="Outstanding principal and principal paid down"' in html
    assert "Annual amortization and cash due" in html


@pytest.mark.parametrize("cadence", [12, 48, 51.96, 252, 365])
def test_each_cadence_uses_actual_rows_and_preserves_cash_totals(cadence):
    schedule = build_schedule(137688, 29.99, 24, structure="amortizing", periods_per_year=cadence,
        interest_only_months=6, repayment_config={"amortization_months": 60, "balloons": []})
    years = annual_cash_rows(schedule)
    assert len(years) == 2
    assert sum(item["payments"] for item in years) == len(schedule["rows"])
    assert sum(item["cash_due"] for item in years) == pytest.approx(schedule["total_paid"], abs=.01)
    assert sum(item["interest"] for item in years) == pytest.approx(schedule["total_interest"], abs=.01)
    principal = sum(item["regular_principal"] + item["scheduled_balloon"] + item["maturity_balloon"] for item in years)
    assert principal == pytest.approx(137688, abs=.01)
    assert years[-1]["ending_balance"] == 0


def test_long_daily_schedule_is_condensed_without_discarding_cash():
    schedule = build_schedule(100000, 12, 120, structure="amortizing", periods_per_year=252,
        repayment_config={"amortization_months": 180, "balloons": []})
    assert len(schedule["rows"]) == 2520
    years = annual_cash_rows(schedule)
    assert len(years) == 10
    assert sum(item["cash_due"] for item in years) == pytest.approx(schedule["total_paid"], abs=.01)
    html = schedule_visuals_html(schedule)
    assert len(html) < 200000, "PDF charts must aggregate dense daily rows"


@pytest.mark.parametrize("summary", [None, {}, {"rows": []}])
def test_unavailable_schedule_is_explicit_and_does_not_invent_charts(summary):
    html = schedule_visuals_html(summary)
    assert "A payment-by-payment principal and interest allocation was not recorded with this version." in html
    assert "<svg" not in html
    assert annual_cash_rows(summary) == []


def test_application_exhibit_keeps_frozen_collateral_and_client_note_escaped():
    row = _terms(client_note='<script>alert("client note")</script>', collateral_revision=2,
        collateral_snapshot={"enabled": True, "properties": [{
            "address": {"street": "123 Example & Main", "city": "Testville", "state": "FL", "postal_code": "33101"},
            "estimated_value": 700000, "existing_liens_total": 300000,
            "available_lien_amount": 400000, "currency": "USD", "proposed_lien_position": "second",
            "owner_name": "Example Owner", "source_file_ids": ["PRIVATE-DOCUMENT-ID"],
        }]})
    html = render_terms_html(row, business_name="Example <Business>")
    assert "Closing preparation" in html
    assert "Proposed collateral - recorded revision 2" in html
    assert "123 Example &amp; Main" in html
    assert "400,000.00 USD" in html
    assert "PRIVATE-DOCUMENT-ID" not in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Example &lt;Business&gt;" in html


def test_legacy_application_scalar_never_gets_fabricated_principal_interest():
    row = _terms(loan_structure=None, repayment_config=None)
    html = render_terms_html(row, business_name="Example LLC")
    assert "A payment-by-payment principal and interest allocation was not recorded with this version." in html
    assert 'data-chart="payments"' not in html
    assert "$2,000.00" in html


def test_production_exhibit_rebuilds_advanced_rows_using_draw_not_credit_limit():
    sheet = _sheet(approved_amount=500000, rate_pct=29.99, term_months=24, extra={
        "structure_version": 1, "facility_kind": "revolving_loc",
        "repayment_structure": "interest_only_then_amortizing", "interest_only_months": 6,
        "initial_draw_amount": 137688, "payment_basis_amount": 137688,
        "payment_frequency": "custom", "payments_per_year": 48, "rate_structure": "fixed",
        "repayment_config": {"amortization_months": 60, "balloons": []},
        "collateral_revision": 0, "collateral_snapshot": {"enabled": False},
    })
    html = render_term_sheet_html(sheet, business_name="Example LLC")
    assert 'data-chart="payments"' in html
    assert "Closing preparation" in html
    assert "$137,688.00" in html
    assert "INTERNAL: do not show this client note" not in html


@pytest.mark.parametrize("repayment", ["fixed_payment", "custom"])
def test_production_lender_stated_payment_does_not_create_an_allocation(repayment):
    sheet = _sheet(extra={"structure_version": 1, "repayment_structure": repayment,
        "payment_frequency": "monthly", "periodic_payment": 50100, "lender_payment_override": True})
    html = render_term_sheet_html(sheet, business_name="Example LLC")
    assert 'data-chart="payments"' not in html
    assert "A payment-by-payment principal and interest allocation was not recorded with this version." in html
    assert "INTERNAL: do not show this client note" not in html


@pytest.mark.asyncio
async def test_already_issued_pdf_remains_byte_identical_after_renderer_upgrade():
    from app.routers import application_terms as routes

    row = SimpleNamespace(issued_pdf_bytes=b"%PDF-frozen-version-3", issued_filename="Original-v3.pdf")
    with patch.object(routes.application_terms, "issue"), patch.object(routes, "render_terms_pdf") as render:
        pdf, name = await routes._issued_pdf(row, business_name="Example LLC", client_name=None)
    assert pdf == b"%PDF-frozen-version-3"
    assert name == "Original-v3.pdf"
    render.assert_not_called()


def test_rendered_advanced_terms_keep_charts_collateral_and_closing_within_five_pages():
    try:
        pytest.importorskip("weasyprint")
    except OSError:
        pytest.skip("WeasyPrint native Pango runtime is unavailable")
    from pypdf import PdfReader

    from app.services.application_terms_pdf import render_terms_pdf

    config = {"amortization_months": 60, "after_balloon": "keep_payment", "balloons": []}
    schedule = build_schedule(200000, 29.99, 24, structure="amortizing", interest_only_months=6,
        repayment_config=config)
    collateral = {"enabled": True, "properties": [{
        "address": {"street": "100 Example Avenue", "city": "Orlando", "state": "FL", "postal_code": "32801"},
        "estimated_value": 850000, "existing_liens_total": 325000, "available_lien_amount": 175000,
        "proposed_lien_position": "Second", "currency": "USD", "owner_name": "Example Holdings LLC",
        "source_file_ids": ["PRIVATE-SOURCE-ID"],
    }], "assets": [{"asset_type": "equipment", "description": "Business equipment",
        "estimated_value": 75000, "existing_liens_total": 10000, "available_lien_amount": 35000,
        "currency": "USD", "proposed_lien_position": "First", "source_file_ids": []}]}
    budget = {"revision": 2, "loan_amount_mode": "gross_loan", "funding_mode": "single_investor",
        "costs_confirmed": True, "currency": "USD", "rows": [
            {"id": "points", "label": "Origination points", "category": "origination", "payer": "borrower",
                "method": "percent", "value": 4, "basis": "loan_amount", "timing": "closing", "settlement": "financed",
                "enabled": True, "recipient_role": "platform", "recipient": "PRIVATE-RECIPIENT"},
            {"id": "reserve", "label": "Payment reserve", "category": "escrow_reserve", "payer": "borrower",
                "method": "fixed", "value": 8000, "basis": "loan_amount", "timing": "closing", "settlement": "withheld",
                "enabled": True, "recipient_role": "escrow", "reserve_release": "maturity"},
        ]}
    row = _terms(amount=200000, apr_pct=29.99, term_months=24, interest_only_months=6,
        periodic_payment=schedule["payment_io"], payment_count=schedule["periods"],
        annual_new_debt_service=schedule["coverage_annual_debt_service"], repayment_config=config,
        collateral_snapshot=collateral, collateral_revision=3, deal_budget_snapshot=budget, deal_budget_revision=2,
        conditions=["Current payoff letters.", "Funding-source review of title and lien priority."])
    rendered = render_terms_pdf(row, business_name="Synthetic Example LLC", client_name="Example Client")
    reader = PdfReader(BytesIO(rendered))
    assert len(reader.pages) <= 5
    pages = [" ".join(page.extract_text().split()) for page in reader.pages]
    text = " ".join(pages)
    for expected in ("CONFIDENTIAL", "Payment and amortization overview", "Regular payments: principal and interest",
        "Total cash due, including extra and maturity principal", "Outstanding principal / principal paid down",
        "Annual amortization and cash due", "Year 1", "Year 2", "Lifetime total", "100 Example Avenue",
        "Business equipment", "Origination points", "Payment reserve", "Closing preparation"):
        assert expected.lower() in text.lower()
    for page in pages:
        if "Annual amortization and cash due" in page:
            assert "Year 1" in page, "Amortization heading must remain with its first row"
    assert f'${schedule["total_interest"]:,.2f}' in text
    assert f'${schedule["total_paid"]:,.2f}' in text
    assert "PRIVATE-RECIPIENT" not in text
    assert "PRIVATE-SOURCE-ID" not in text
