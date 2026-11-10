"""A six-month IO period must be visible as its own payment phase."""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO

import pytest

from app.services.application_terms_pdf import render_terms_html, render_terms_pdf
from app.services.loan_repayment_display import schedule_cadence_label
from app.services.loan_schedule import build_schedule
from app.services.loan_terms_visuals import payment_phases_html
from app.services.production_term_sheet_pdf import render_term_sheet_html
from app.tests.test_loan_repayment_exports import _terms
from app.tests.test_production_term_sheet_pdf import _sheet

CONFIG = {"amortization_months": 54, "after_balloon": "keep_payment", "balloons": []}


def _schedule(**changes):
    values = dict(amount=160102.32, rate_pct=33.99, term_months=24, structure="amortizing",
        interest_only_months=6, periods_per_year=48, repayment_config=CONFIG)
    return build_schedule(**{**values, **changes})


def _row():
    summary = _schedule()
    return _terms(amount=160102.32, apr_pct=33.99, term_months=24, interest_only_months=6,
        payments_per_year=48, repayment_frequency="custom", custom_repayment_label="48 payments per year",
        periodic_payment=1449.41, payment_count=96, annual_new_debt_service=summary["coverage_annual_debt_service"],
        repayment_config=CONFIG)


def test_48_payment_year_displays_24_io_then_72_regular_payments():
    schedule = _schedule()
    before = deepcopy(schedule)
    html = payment_phases_html(schedule)
    for expected in ("Months 1-6", "Months 7-24", "24 payments;", "72 payments;", "$1,133.72", "$1,449.41",
        "$4,534.88 monthly equivalent", "$5,797.64 monthly equivalent", "$130,587.71", "$262,154.51", "$200,159.39",
        "48 payments per year (4 per month equivalent)", "not your regular payment", "In addition to the final scheduled payment"):
        assert expected in html
    assert "Weekly" not in html
    assert schedule == before
    # Independently reconcile the displayed two phases and the separate balloon.
    assert 24 * 1133.72 + 72 * 1449.41 + 130587.71 == pytest.approx(262154.51)
    assert 48 * 1449.41 + 130587.71 == pytest.approx(200159.39)


def test_application_pdf_does_not_use_stale_later_payment_as_the_initial_payment():
    html = render_terms_html(_row(), business_name="Synthetic Example LLC")
    phases = html.split('<section class="loan-payment-phases">')[1].split("</section>")[0]
    assert phases.index("Interest-only payments") < phases.index("$1,133.72") < phases.index("Principal and interest payments") < phases.index("$1,449.41")
    assert "Months 1-6" in phases and "Months 7-24" in phases
    assert "Amortization: 54 months after the interest-only period; contractual maturity: 24 months." in html


def test_monthly_io_phases_have_the_actual_counts_and_monthly_amounts():
    schedule = _schedule(periods_per_year=12)
    html = payment_phases_html(schedule)
    assert "6 payments;" in html and "18 payments;" in html
    assert "Monthly (12 payments per year)" in html
    assert f'${schedule["payment_io"]:,.2f} monthly equivalent' in html
    assert f'${schedule["payment_after"]:,.2f} monthly equivalent' in html


def test_recast_later_payment_range_is_explicit_without_removing_the_balloon():
    schedule = _schedule(repayment_config={"amortization_months": 54, "after_balloon": "recast",
        "balloons": [{"id": "extra", "month": 12, "type": "amount", "value": 10000}]})
    phase = schedule["payment_phases"][1]
    html = payment_phases_html(schedule)
    assert f'${phase["payment_min"]:,.2f} - ${phase["payment_max"]:,.2f}' in html
    assert "Regular payments vary after principal events" in html
    assert f'${schedule["maturity_balloon"]:,.2f}' in html


def test_interest_only_through_maturity_does_not_invent_a_principal_interest_phase():
    html = payment_phases_html(_schedule(structure="interest_only", interest_only_months=0, repayment_config={"balloons": []}))
    assert "Months 1-24" in html
    assert "Principal and interest payments" not in html
    assert "$160,102.32" in html


@pytest.mark.parametrize("summary", [None, {}, {"payment_phases": []}])
def test_missing_schedule_retains_legacy_payment_presentation(summary):
    assert payment_phases_html(summary) == ""


def test_legacy_scalar_has_no_invented_io_schedule():
    html = render_terms_html(_terms(loan_structure=None, repayment_config=None), business_name="Example LLC")
    assert "Your payment phases" not in html
    assert "Estimated monthly payment" in html


def test_48_payments_are_never_called_weekly_and_52_are():
    assert schedule_cadence_label({"periods_per_year": 48}) == "48 payments per year (4 per month equivalent)"
    assert schedule_cadence_label({"periods_per_year": 52}) == "Weekly (52 payments per year)"


def test_production_pdf_has_the_same_io_and_regular_schedule_using_the_draw():
    sheet = _sheet(approved_amount=500000, rate_pct=33.99, term_months=24, extra={
        "structure_version": 1, "facility_kind": "revolving_loc", "repayment_structure": "interest_only_then_amortizing",
        "interest_only_months": 6, "initial_draw_amount": 160102.32, "payment_basis_amount": 160102.32,
        "payment_frequency": "custom", "payments_per_year": 48, "rate_structure": "fixed", "repayment_config": CONFIG})
    html = render_term_sheet_html(sheet, business_name="Synthetic Example LLC")
    for expected in ("Months 1-6", "Months 7-24", "$1,133.72", "$1,449.41", "$130,587.71", "$262,154.51"):
        assert expected in html


def test_rendered_payment_phases_stay_together_on_the_first_page():
    try:
        pytest.importorskip("weasyprint")
    except OSError:
        pytest.skip("Native PDF libraries unavailable")
    from pypdf import PdfReader

    pdf = render_terms_pdf(_row(), business_name="Synthetic Example LLC")
    reader = PdfReader(BytesIO(pdf))
    assert len(reader.pages) <= 5
    first = " ".join(reader.pages[0].extract_text().split())
    for text in ("Your payment phases", "Months 1-6", "Months 7-24", "$1,133.72", "$1,449.41", "$130,587.71"):
        assert text.lower() in first.lower()
