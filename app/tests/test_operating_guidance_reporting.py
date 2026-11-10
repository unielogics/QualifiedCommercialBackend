"""Shared reports explain operating assumptions without private review data."""

import json
from uuid import uuid4

import fitz

from app.capital_partners.services.listings import partner_operating_earnings
from app.services import earnings_improvement, operating_plan_pdf
from app.tests.test_earnings_improvement import NOW, approve, context, expense
from app.tests.test_operating_marketing import marketing_body


def reporting_fixture():
    cockpit = context()
    cockpit["ebitda"] = {"headline": None}
    group_key = "source:example:marketing"
    cockpit["operating_evidence"]["guidance"] = {"expense_groups": [{"key": group_key,
        "category": "marketing", "label": "Source advertising", "baseline_annual_amount": 12000,
        "currency": "USD", "year": 2025}]}
    raw = marketing_body()
    raw["expenses"][0].update(source_group_key=group_key, label="Online advertising", current_amount=600)
    raw["expenses"].append(expense(id=str(uuid4()), category="marketing", label="Agency fee",
        current_amount=400, source_group_key=group_key))
    # The action refers only to the first channel. The group still reconciles
    # the two independent detail rows against one annual source reference.
    raw["adjustments"][0].update(current_amount=600, proposed_amount=1600)
    raw["adjustments"][0].update(explanation="Increase the online acquisition campaign for the example service.",
        source_note="Campaign assumptions require confirmation against conversion and delivery results.")
    reviewed = approve(raw, cockpit)
    evaluation = earnings_improvement.evaluate(reviewed, cockpit, now=NOW)
    summary = earnings_improvement.package_summary(evaluation)
    financials = {"improvement_plan": summary, "evidence_sources": [
        {"file_id": cockpit["operating_evidence"]["files"][0]["file_id"], "file_name": "Business source.pdf"}]}
    return reviewed, financials


def test_operating_pdf_explains_split_source_group_marketing_return_and_independent_noi():
    body, financials = reporting_fixture()
    document = operating_plan_pdf.build_html(body=body, financial_totals=financials)
    assert "Source expense groups and disclosed detail" in document
    assert "Source annual reference: $12,000.00" in document
    assert "disclosed annual detail: $12,000.00; difference: $0.00" in document
    assert "Online advertising: $600.00 / monthly" in document
    assert "Agency fee: $400.00 / monthly" in document
    assert "Example service" in document
    assert "Assumed revenue per added marketing dollar:</b> 3.00x" in document
    assert "Contribution margin:</b> 50%" in document
    assert "added spend $1,000.00; modeled added revenue $3,000.00" in document
    assert "contribution before added marketing spend $1,500.00" in document
    assert "First-year operating income (NOI)" in document
    assert "-$9,000.00" in document
    assert "staff-reviewed adjustments: reviewed illustration" in document
    assert "Projected NOI can be shown" in document
    assert "PRIVATE REVIEW REASON" not in document
    assert body["adjustments"][0]["review_source_stamp"] not in document
    assert body["adjustments"][0]["reviewed_by_user_id"] not in document
    assert "source:example:marketing" not in document


def test_partner_allowlist_retains_new_assumptions_and_groups_but_removes_private_source_details():
    _, financials = reporting_fixture()
    summary = financials["improvement_plan"]
    summary["expense_groups"][0].update(actor_id="PRIVATE ACTOR", review_reason="PRIVATE REASON",
        source_file_ids=["PRIVATE FILE"], source_fingerprint="PRIVATE STAMP")
    summary["adjustments"][0].update(reviewed_by_user_id="PRIVATE ACTOR", review_reason="PRIVATE REASON")
    summary["adjustments"][0]["calculation"]["private_notes"] = "PRIVATE NOTES"
    output = partner_operating_earnings({"improvement_plan": summary})["improvement_plan"]
    action = output["adjustments"][0]
    assert action["product_name"] == "Example service"
    assert action["calculation"]["revenue_return_multiple"] == 3
    assert action["calculation"]["incremental_spend"] == 1000
    assert action["calculation"]["incremental_revenue"] == 3000
    assert action["calculation"]["incremental_contribution"] == 1500
    assert output["projection"]["approved"]["first_year_noi"] == -9000
    assert output["projection"]["approved"]["noi_status"] == "reviewed_illustration"
    assert output["expense_groups"][0]["status"] == "matches_source"
    assert output["expense_groups"][0]["disclosed_annual_amount"] == 12000
    assert "PRIVATE" not in json.dumps(output)
    assert "source:example:marketing" not in json.dumps(output)


def test_operating_pdf_marks_unknown_assumptions_and_escapes_product_content():
    body, financials = reporting_fixture()
    body["adjustments"][0].update(product_name="<script>private()</script>", revenue_return_multiple=None,
        contribution_margin_pct=None)
    document = operating_plan_pdf.build_html(body=body, financial_totals=financials)
    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "Assumed revenue per added marketing dollar:</b> Not available" in document
    assert "Contribution margin:</b> Not available" in document


def test_rendered_operating_pdf_contains_reconciliation_assumptions_and_page_footers():
    body, financials = reporting_fixture()
    document = operating_plan_pdf.build_html(body=body, financial_totals=financials)
    pdf = operating_plan_pdf._with_footer(operating_plan_pdf._render_pymupdf(document))
    with fitz.open(stream=pdf, filetype="pdf") as rendered:
        content = " ".join(page.get_text() for page in rendered)
        assert 1 <= len(rendered) <= 5
        assert "Source expense groups and disclosed detail" in content
        assert "Source advertising" in content
        assert "Example service" in content
        assert "3.00x" in content
        assert "-$9,000.00" in content
        assert "First-year operating income" in content
        assert "PRIVATE REVIEW REASON" not in content
        assert all(f"Page {index} of {len(rendered)}" in page.get_text() for index, page in enumerate(rendered, 1))
