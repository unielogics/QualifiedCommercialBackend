"""Reviewed annual NOI remains identifiable across current and shared reports."""

import json
from copy import deepcopy
from types import SimpleNamespace

import fitz
import pytest

from app.capital_partners.services.listings import partner_operating_earnings
from app.services.financial_metric_reporting import current_review_projection, operating_earnings_brief
from app.services.investor_presentation_pdf import render_investor_presentation_pdf
from app.services.public_underwriting_packet_pdf import _operating_earnings_lines, render_underwriting_packet_pdf
from app.tests.test_financial_metric_review import build
from app.tests.test_financial_noi_integration import annual
from app.tests.test_investor_presentation import synthetic_financials


def reviewed_cockpit(kind="tax_derived_noi", value=-12000):
    cockpit = build([annual()])
    cockpit["noi"]["source_headline"] = deepcopy(cockpit["noi"]["headline"])
    cockpit["noi"]["source_reason"] = None
    cockpit["noi"]["headline"] = {
        "key": "noi.reviewed", "kind": kind, "label": "Tax-derived NOI estimate" if kind == "tax_derived_noi" else "Staff-reviewed NOI",
        "value": value, "annualized_value": value, "year": 2024, "period": "2024 annual", "currency": "USD",
        "basis": "cash", "complete": True, "provisional": False, "staff_reviewed": True, "staff_adjusted": True,
        "tax_derived_estimate": kind == "tax_derived_noi", "source_accepted": False,
        "source_refs": [{"file_id": "synthetic-source", "file_name": "Synthetic business return.pdf", "accepted": False}],
        "review_receipt": {"id": "PRIVATE_RECEIPT", "actor_name": "PRIVATE_ACTOR", "reason": "PRIVATE_REASON"},
    }
    cockpit["noi"]["scenario"].update(current=value, plan=value)
    return cockpit


@pytest.mark.parametrize("kind,value", [("tax_derived_noi", -12000), ("staff_reviewed_noi", 0)])
def test_current_and_shared_report_keep_review_method_year_and_raw_source_distinct(kind, value):
    cockpit = reviewed_cockpit(kind, value)
    projected = current_review_projection({}, cockpit)
    metrics = projected["key_metrics"]
    assert metrics["annual_noi"] == value
    assert metrics["noi_kind"] == kind and metrics["noi_period"] == "2024 annual"
    assert metrics["annual_ebitda"] == 30000  # NOI review never substitutes for P&L EBITDA.
    brief = projected["operating_earnings"]
    assert brief["noi"]["historical"]["staff_reviewed"]
    assert brief["noi"]["historical"]["year"] == 2024
    assert brief["noi"]["source_historical"]["value"] == 20000
    assert "PRIVATE_" not in json.dumps(brief)
    shared = partner_operating_earnings(brief)
    assert shared["noi"]["historical"]["value"] == value
    assert shared["noi"]["historical"]["kind"] == kind
    assert shared["noi"]["historical"]["source_accepted"] is False
    assert shared["noi"]["source_historical"]["value"] == 20000
    assert "synthetic-source" not in json.dumps(shared)


def test_packet_names_tax_basis_and_source_without_fabricated_pnl_operands():
    brief = operating_earnings_brief(reviewed_cockpit())
    text = "\n".join(_operating_earnings_lines({"operating_earnings": brief}))
    assert "Tax-derived NOI estimate: $-12,000" in text
    assert "2024 annual" in text and "Synthetic business return.pdf" in text
    assert "Staff-reviewed annual basis" in text and "Tax-derived estimate" in text
    assert "NOI inputs: revenue" not in text
    assert "PRIVATE_" not in text


def test_investor_pdf_prints_reviewed_tax_noi_with_period_and_source():
    data = synthetic_financials()
    current = current_review_projection({}, reviewed_cockpit())
    data["operating_earnings"]["noi"] = current["operating_earnings"]["noi"]
    data["current_key_metrics"].update(annual_noi=-12000)
    pdf = render_investor_presentation_pdf(intake=SimpleNamespace(business_name="Synthetic review LLC"), financials=data)
    document = fitz.open(stream=pdf, filetype="pdf")
    text = " ".join(document[2].get_text().split())
    assert "NOI - reviewed tax estimate (2024)" in text and "-$12,000" in text
    assert "NOI basis: 2024 annual; Synthetic business return.pdf" in text
    assert "PRIVATE_" not in "\n".join(page.get_text() for page in document)


def test_packet_replaces_cached_ai_noi_with_reviewed_basis():
    data = current_review_projection({}, reviewed_cockpit())
    pdf = render_underwriting_packet_pdf(
        intake=SimpleNamespace(business_name="Synthetic review LLC", variant="mca_refinance_v1"),
        files=[], missing_docs=[], result={"key_metrics": {"noi": 999999}}, financials=data,
        executive_summary={"key_metrics": [{"label": "NOI", "value": "999999", "note": "Obsolete AI result"}]},
    )
    document = fitz.open(stream=pdf, filetype="pdf")
    text = " ".join(" ".join(page.get_text().split()) for page in document)
    assert "Tax-derived NOI estimate: $-12,000" in text
    assert "Obsolete AI result" not in text and "999999" not in text
    assert "Noi Kind" not in text and "Noi Currency" not in text
