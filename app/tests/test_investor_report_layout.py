from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import fitz

from app.services.investor_presentation_pdf import render_investor_presentation_pdf
from app.tests.test_investor_presentation import documented_ebitda_financials


def detailed_investor_financials():
    from app.capital_partners.services.sync import proposed_terms

    data = documented_ebitda_financials()
    data["current_key_metrics"]["annual_noi"] = 80_000
    data["operating_earnings"]["noi"] = {"historical": {"currency": "USD"}, "scenario": {"plan": 80_000}}
    data["operating_earnings"]["improvement_plan"]["projection"]["proposed"].update(noi=98_000, dscr_current=1.38)
    data["plan"]["impact"].update(income_annual={"after": 120_000}, dscr={"after": 1.67}, debt_service_monthly={"after": 6000})
    data["proposed_terms"] = proposed_terms({"dscr": {"proposed": {
        "amount": 200_000, "rate_pct": 12, "term_months": 24,
        "structure": "amortizing", "payments_per_year": 12,
        "treatment": "refinance", "retained_annual_debt_service": 0,
        "repayment_config": {"amortization_months": 60, "after_balloon": "keep_payment", "balloons": []}},
        "annual_debt_service": 100_000, "headline": {"numerator": 120_000, "currency_comparable": True}}, "plan": {}})
    references = [{"id": f"S{index}", "label": f"Business statement 2025-{index:02d}.pdf", "kind": "bank_statement", "period": f"2025-{index:02d}", "version": 1, "page": 1, "file_id": f"synthetic-file-{index}", "used_for": ["banking"], "availability": "analyzed"} for index in range(1, 13)]
    for index, (label, used) in enumerate([
        ("2025 business return.pdf", "financials"), ("Saved financing terms", "terms"),
        ("Reviewed use of funds", "use_of_funds"), ("Working operating plan", "operating_plan"),
        ("Linked owner credit report", "credit"), ("Property valuation and title", "collateral")], 13):
        references.append({"id": f"S{index}", "label": label, "kind": used, "period": "2025", "version": 2, "page": None, "used_for": [used], "availability": "linked"})
    data["investor_report"] = {
        "company": {"name": "Synthetic Wellness Services LLC", "address": {"street": "100 Example Avenue", "city": "Miami", "state": "FL", "postal_code": "33101"}, "website": "https://example.com", "industry": "Wellness services", "naics": "621399"},
        "banking": {"account_count": 2, "currency": "USD", "coverage_notes": ["Counted comparable business accounts; internal transfers are not earned revenue."],
            "months": [{"period": f"2025-{index:02d}", "deposits": 80_000 + index * 2000, "withdrawals": 78_000 + index * 1200, "net_cash_flow": 2000 + index * 800, "ending_balance": -2000 if index == 3 else 4000 + index * 1000, "nsf_count": 3 if index == 3 else 0, "source_ids": [f"S{index}"]} for index in range(1, 13)]},
        "credit": {"report_count": 1, "required_owner_count": 1, "score_band": "680-699", "bureaus": ["TransUnion"], "models": ["FICO 4"], "as_of": "2026-10-01", "derogatory_count": 1, "owner_name": "NEVER PRINT THIS PERSON", "ssn": "123-45-6789", "account_number": "4111111111111111", "home_address": "PRIVATE HOME ADDRESS"},
        "collateral": {"enabled": True, "properties": [{"address": "200 Example Road, Miami, FL 33101", "value": 500_000, "existing_liens": 200_000, "available_lien_amount": 150_000, "lien_position": 2, "valuation_date": "2026-10-01", "currency": "USD", "source_ids": ["S18"]}]},
        "references": references,
        "section_reference_ids": {"banking": [f"S{i}" for i in range(1, 13)], "financials": ["S13"], "terms": ["S14"], "use_of_funds": ["S15"], "operating_plan": ["S16"], "credit": ["S17"], "collateral": ["S18"]},
    }
    return data


def stressed_investor_financials():
    data = detailed_investor_financials()
    report = data["investor_report"]
    report["company"]["name"] = "Synthetic Long Company " * 15 + "LLC"
    report["banking"]["months"][4]["ending_balance"] = None
    report["banking"]["months"][8]["deposits"] = None
    report["banking"]["currency"] = None
    report["references"] *= 8
    for index, row in enumerate(report["references"], 1):
        report["references"][index - 1] = {**row, "id": f"S{index}", "label": row["label"] + " additional detailed source context " * 20}
    report["collateral"]["properties"] *= 10
    plan = data["operating_earnings"]["improvement_plan"]
    plan["expenses"] *= 40
    plan["adjustments"] *= 25
    plan["plan_notes"] = "Long confidential plan note 123-45-6789 4111111111111111. " * 2000
    plan["adjustments"][0]["explanation"] = "Review EIN 12-3456789, DOB 01/02/1990, account #123456, routing 12345678, private@example.com, 305-555-0199. " + "Review source statement. " * 400
    data["package_readiness"]["requirements"] *= 30
    return data


def test_five_page_report_includes_monthly_activity_privacy_safe_credit_collateral_and_sources():
    pdf = render_investor_presentation_pdf(intake=SimpleNamespace(business_name="Synthetic LLC"), financials=detailed_investor_financials(), generated_at=datetime(2026, 11, 10, tzinfo=UTC))
    document = fitz.open(stream=pdf, filetype="pdf")
    text = " ".join(" ".join(page.get_text().split()) for page in document)
    assert len(document) == 5
    assert all("CONFIDENTIAL" in page.get_text() for page in document)
    assert all(page.get_images() for page in document)  # The shared QCMark is embedded.
    for expected in ("100 Example Avenue", "$200,000", "Working Capital: $130,000", "2025-12", "104,000", "TransUnion", "680-699", "FICO 4", "200 Example Road", "$150,000", "Sources [S1-S12]", "7 source rows summarize 18 indexed records", "APR 12%", "Amortization 60 months", "regular payment"):
        assert expected in text
    for secret in ("NEVER PRINT THIS PERSON", "123-45-6789", "4111111111111111", "PRIVATE HOME ADDRESS"):
        assert secret not in text
    assert all(f"S{index}" in document[4].get_text() for index in range(13, 19))
    assert all(not rectx.is_empty for page in document for rectx in [page.rect])


def test_large_and_incomplete_reports_stay_five_pages_and_point_to_full_file_detail():
    data = stressed_investor_financials()
    original = deepcopy(data)
    pdf = render_investor_presentation_pdf(intake=SimpleNamespace(), financials=data)
    document = fitz.open(stream=pdf, filetype="pdf")
    text = " ".join(" ".join(page.get_text().split()) for page in document)
    assert len(document) == 5
    assert "144 indexed records" in text
    assert "2 of 10 properties shown" in text
    assert "25 total actions" in text
    assert "currency unconfirmed" in text and "Unknown" in text
    assert "originating file" in text and "full file register" in text
    for secret in ("123-45-6789", "4111111111111111", "12-3456789", "01/02/1990", "#123456", "12345678", "private@example.com", "305-555-0199"):
        assert secret not in text
    assert data == original


def test_valid_long_business_name_and_single_account_scope_are_visible():
    data = detailed_investor_financials()
    name = "Metropolitan Business Wellness and Clinical Care Services for Regional Community Health and Independent Patient Support Operations in the Southeastern United States LLC"
    assert len(name) <= 180
    data["investor_report"]["company"]["name"] = name
    bank = data["investor_report"]["banking"]
    bank.update(account_count=3, displayed_account_count=1, aggregation_scope="single_account_currency_not_comparable", currency=None)
    data["investor_report"]["credit"].update(score_band="620-659, 680-719", score_bands=["620-659", "680-719"], risk_flag_count=3, high_risk_flag_count=1)
    document = fitz.open(stream=render_investor_presentation_pdf(intake=SimpleNamespace(), financials=data), filetype="pdf")
    assert name in " ".join(document[0].get_text().split())
    assert "Only 1 of 3 counted accounts shown" in document[1].get_text()
    credit = " ".join(document[3].get_text().split())
    assert "620-659, 680-719" in credit and "Review flags: 3 (1 high priority)" in credit
