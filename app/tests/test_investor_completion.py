"""Source-bound completion guidance must not manufacture a requested amount or NOI."""
from types import SimpleNamespace

import fitz

from app.services.investor_presentation_pdf import (
    _financing_card,
    _open_conditions,
    render_investor_presentation_pdf,
)


def test_proposal_is_prominent_without_claiming_it_is_the_application_request():
    assert _financing_card(None, 172_488) == (
        "Proposed financing", "$172,488", "Proposed gross loan; application request not recorded")
    assert _financing_card(100_000, 172_488) == (
        "Requested financing", "$100,000", "Proposed gross loan $172,488")
    assert _financing_card(None, None)[1] == "Not entered"
    assert _financing_card(None, 50_000, "CAD")[1] == "CAD 50,000"


def test_current_source_blocker_explains_missing_noi_and_ebitda():
    data = {"use_of_funds": {"requested_amount": 100_000}, "operating_earnings": {
        "noi": {"reason": "Financial source evidence changed. Review the current business sources before relying on NOI."},
        "historical_unavailable_reason": "Latest P&L is excluded from the earnings selection."}}
    conditions = _open_conditions(data, {}, {})
    assert any("Financial source evidence changed" in item for item in conditions)
    assert any("Latest P&L is excluded" in item for item in conditions)
    assert not any("current application request" in item for item in conditions)


def test_source_period_noi_is_not_silently_annualized_or_replaced_by_tax_income():
    data = {"current_key_metrics": {"tax_net_income": 300_000}, "operating_earnings": {
        "noi": {"historical": {"value": 12_000, "period": "2026-01-01 to 2026-03-31"}}}}
    conditions = _open_conditions(data, {}, {}, requested_amount=100_000)
    assert any("dated six-to-twelve-month P&L" in item for item in conditions)
    assert any("Validated P&L EBITDA is unavailable" in item for item in conditions)
    assert not any("current application request" in item for item in conditions)
    assert "300000" not in str(conditions)


def test_provisional_noi_still_has_a_review_condition_when_its_value_is_visible():
    data = {"current_key_metrics": {"annual_noi": -12_000, "annual_ebitda": 0},
            "operating_earnings": {"noi": {"historical": {"provisional": True}}}}
    assert any("NOI is provisional" in item for item in _open_conditions(data, {}, {}, requested_amount=100_000))


def test_pdf_shows_proposal_and_actionable_missing_figures_without_mutating_inputs():
    data = {"proposed_terms": {"amount": 172_488, "currency": "USD"},
            "operating_earnings": {"noi": {"reason": "The latest business P&L has missing operating-income inputs."}}}
    document = fitz.open(stream=render_investor_presentation_pdf(
        intake=SimpleNamespace(business_name="Synthetic Therapy LLC"), financials=data), filetype="pdf")
    overview = " ".join(document[0].get_text().split())
    conditions = " ".join(document[4].get_text().split())
    assert "PROPOSED FINANCING" in overview and "$172,488" in overview
    assert "application request not recorded" in overview
    assert "latest business P&L has missing operating-income inputs" in conditions
    assert "Confirm the current application request" in conditions
    assert data["proposed_terms"]["amount"] == 172_488
    assert "use_of_funds" not in data
