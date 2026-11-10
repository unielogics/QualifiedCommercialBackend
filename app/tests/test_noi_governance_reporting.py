"""Governed NOI reports use the actual operands and their own reporting units."""

from types import SimpleNamespace
from unicodedata import normalize

import fitz

from app.services.financial_metric_reporting import current_review_projection
from app.services.investor_presentation_pdf import render_investor_presentation_pdf
from app.services.public_underwriting_packet_pdf import (
    _operating_earnings_lines,
    render_underwriting_packet_pdf,
)
from app.tests.test_financial_metric_review import build
from app.tests.test_investor_presentation import synthetic_financials
from app.tests.test_noi_governance_consumers import annual_pnl, business_tax


def test_tax_noi_packet_uses_known_tax_operands_instead_of_missing_pnl_columns():
    projected = current_review_projection({}, build([business_tax(-15000)]))
    lines = _operating_earnings_lines(projected)
    inputs = next(line for line in lines if line.startswith("NOI inputs:"))
    assert "20,000" in inputs and "5,000" in inputs
    assert "Unavailable" not in inputs and "COGS" not in inputs
    assert "NOI status: provisional" in "\n".join(lines)


def test_deterministic_policy_metadata_is_not_printed_as_an_ai_financial_fact():
    projected = current_review_projection({}, build([business_tax()]))
    raw = render_underwriting_packet_pdf(
        intake=SimpleNamespace(business_name="Synthetic services LLC", variant="mca_refinance_v1"),
        files=[], missing_docs=[], result={}, financials={
            "current_key_metrics": projected["key_metrics"],
            "operating_earnings": projected["operating_earnings"],
        },
    )
    document = fitz.open(stream=raw, filetype="pdf")
    text = normalize("NFKC", " ".join(" ".join(page.get_text().split()) for page in document))
    assert "Financial Calculation Signature" not in text
    assert "Financial Calculation Policy Version" not in text
    assert "Current financial review" in text
    assert "NOI status: provisional" in text


def test_generic_and_annual_noi_aliases_match_while_source_period_stays_separate():
    cockpit = build([annual_pnl(period_end="2025-06-30")])
    projected = current_review_projection({}, cockpit)
    assert cockpit["noi"]["headline"]["value"] == 20000
    assert projected["key_metrics"]["noi"] == projected["key_metrics"]["annual_noi"] == 40000
    assert projected["key_metrics"]["noi_source_period_value"] == 20000
    packet = "\n".join(_operating_earnings_lines(projected))
    assert "40,000" in packet and "Source-period operating result: $20,000" in packet


def test_investor_noi_forecast_review_is_explicitly_labeled_as_an_assumption():
    data = synthetic_financials()
    data["operating_earnings"]["noi"] = {"historical": {
        "forecast_assumption": True, "staff_reviewed": True, "year": 2026,
        "currency": "USD", "annualized_value": 42000,
    }}
    raw = render_investor_presentation_pdf(
        intake=SimpleNamespace(business_name="Synthetic services LLC", variant="mca_refinance_v1",
                               requested_loan_amount=180000, intake_state={}),
        financials=data,
    )
    document = fitz.open(stream=raw, filetype="pdf")
    text = normalize("NFKC", " ".join(" ".join(page.get_text().split()) for page in document))
    assert "NOI - reviewed assumption" in text
