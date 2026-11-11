"""Saved evidence remains distinct from requests, estimates and illustrations."""
import json

from app.capital_partners.services.ppm_financials import financial_sections


def test_tax_income_never_backfills_missing_noi_and_selected_estimate_is_labelled():
    data = {"tax_income": 90000, "tax_year": 2025, "dscr": .8, "dscr_basis": "tax_derived_earnings",
            "operating_earnings": {"noi": {"reason": "A current operating-income source is missing."}}}
    section = financial_sections(data, {"name": "Synthetic Services LLC"}, {}, "USD")[0]
    noi = next(row for row in section["facts"] if row["label"] == "Saved selected NOI")
    assert noi["value"] == "Not established" and "source is missing" in noi["note"]
    tax = next(row for row in section["facts"] if row["label"] == "Tax-return income - separate measure")
    assert "90,000.00" in tax["value"] and "not substituted for NOI" in tax["note"]
    data["operating_earnings"]["noi"] = {"review_needed": True, "historical": {
        "kind": "tax_derived_noi", "label": "Tax-derived NOI estimate", "annualized_value": 12000,
        "currency": "USD", "period": "2025", "tax_derived_estimate": True}}
    section = financial_sections(data, {}, {}, "USD")[0]
    noi = next(row for row in section["facts"] if row["label"] == "Saved selected NOI - annualized")
    assert noi["value"] == "USD 12,000.00" and "Estimate or review pending" in noi["note"]


def test_requested_budget_is_separate_from_proposal_allocations_and_amounts_are_not_recomputed():
    data = {"proposed_terms": {"proposal_use_of_funds": {"debt_refinance": 100000, "working_capital": 70000}},
            "use_of_funds": {"requested_amount": 273000, "unallocated_amount": 0,
                             "category_totals": {"debt_refinance": 130000, "working_capital": 143000}}}
    budget = {"current_allocation_reconciliation": {"scope": "proposed_loan", "status": "allocated"}}
    sections = financial_sections(data, {}, budget, "USD")
    proposal = next(row for row in sections if row["key"] == "proposed_allocations")
    request = next(row for row in sections if row["key"] == "requested_allocations")
    assert proposal["table"]["rows"][0][1] == "USD 100,000.00"
    assert request["facts"][0]["value"] == "USD 273,000.00"
    assert "distinct from proposed loan principal" in request["paragraphs"][0]
    assert request["table"]["rows"][0][1] == "USD 130,000.00"


def test_aggregates_exclude_identifiers_and_unknown_source_currency_stays_unknown():
    data = {"document_summaries": {"bank_activity": [{"month": "2025-12", "currency": None,
        "deposits": 0, "withdrawals": None, "net": 0, "account_number": "PRIVATE_ACCOUNT", "file_name": "PRIVATE_NAME"}],
        "tax_years": [{"year": 2025, "currency": "EUR", "gross_receipts": 120000, "income": 12000,
                       "taxpayer_id": "PRIVATE_TAX_ID"}]}}
    sections = financial_sections(data, {}, {}, "USD")
    bank = next(row for row in sections if row["key"] == "bank_aggregates")
    tax = next(row for row in sections if row["key"] == "tax_aggregates")
    assert bank["table"]["rows"][0][1:] == ["0.00 (currency not confirmed)", "Not established", "0.00 (currency not confirmed)"]
    assert tax["table"]["rows"][0][1] == "EUR 120,000.00"
    assert all(token not in json.dumps(sections) for token in ("PRIVATE_ACCOUNT", "PRIVATE_NAME", "PRIVATE_TAX_ID"))
