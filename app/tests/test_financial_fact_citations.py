from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.services.capital_readiness_ingestion import statement_period_payload
from app.services.financial_fact_citations import (
    build_financial_citation_manifest,
    ensure_legacy_financial_citation_manifest,
    financial_analysis_citation_status,
    financial_period_citation_status,
    validated_fact_citations_for_analysis,
    validated_fact_citations_for_period,
)


def _citation(field: str, label: str, value: str, **overrides) -> dict:
    return {"page": 1, "line_label": label, "quote": f"{label} {value}",
            "value": value, "measure": field, **overrides}


def _analysis(**overrides):
    facts = {
        "business_name": "Example LLC", "basis": "accrual", "currency": "USD",
        "period_start": "2025-01-01", "period_end": "2025-12-31",
        "gross_revenue": "100000", "cost_of_goods_sold": "87000", "net_income": "3000",
    }
    facts.update(overrides)
    citations = {
        "revenue": [_citation("revenue", "Gross revenue", "100000")],
        "cogs": [_citation("cogs", "Cost of goods sold", "87000")],
        "net_income": [_citation("net_income", "Net income", "3000")],
    }
    return SimpleNamespace(id=uuid4(), bucket_file_id=uuid4(), status="completed",
                           classification="current_p_and_l", content_hash="a" * 64,
                           confidence="high", analysis_version=4,
                           analysis={"key_facts": facts, "fact_citations": citations})


def _validate(analysis, pages=None, **kwargs):
    pages = pages or {1: "Gross revenue 100000\nCost of goods sold 87000\nNet income 3000"}
    manifest = build_financial_citation_manifest(analysis, source_pages=pages,
                                               source_kind="pdf_searchable", source_hash=analysis.content_hash, **kwargs)
    analysis.analysis["financial_fact_citations"] = manifest
    return manifest


def test_exact_source_quote_is_bound_to_ids_hash_value_page_and_field():
    analysis = _analysis()
    manifest = _validate(analysis)
    result = validated_fact_citations_for_analysis(analysis)
    citation = result["revenue"][0]
    assert citation["page"] == 1 and citation["line_label"] == "Gross revenue"
    assert citation["value"] == "100000" and citation["field"] == "revenue"
    assert citation["file_id"] == str(analysis.bucket_file_id)
    assert citation["analysis_id"] == str(analysis.id)
    assert citation["content_hash"] == analysis.content_hash
    assert citation["basis"] == "accrual" and citation["period_end"] == "2025-12-31"
    assert citation["verified"] is True
    assert manifest["validation_hash"]


@pytest.mark.parametrize("overrides,reason", [
    ({"page": 2}, "page_not_searchable"),
    ({"page": 0}, "page_invalid"),
    ({"page": True}, "page_invalid"),
    ({"quote": "Gross revenue 999999"}, "quote_not_found_on_page"),
    ({"line_label": "Net sales"}, "label_not_in_quote"),
    ({"value": "999999"}, "extracted_value_mismatch"),
    ({"measure": "ending_balance"}, "measure_mismatch"),
    ({"period_end": "2024-12-31"}, "statement_context_mismatch"),
    ({"quote": "x" * 321}, "label_or_quote_invalid"),
])
def test_invented_page_quote_line_value_and_context_are_unverified_not_rejected(overrides, reason):
    analysis = _analysis()
    analysis.analysis["fact_citations"]["revenue"][0].update(overrides)
    manifest = _validate(analysis)
    status = manifest["statements"][0]["fact_citation_status"]["revenue"]
    assert status == {"status": "unverified", "reason": reason}
    assert "revenue" not in validated_fact_citations_for_analysis(analysis)
    assert analysis.status == "completed"
    assert analysis.analysis["key_facts"]["gross_revenue"] == "100000"


def test_value_must_be_in_the_quote_not_just_elsewhere_on_the_page():
    analysis = _analysis()
    analysis.analysis["fact_citations"]["revenue"][0]["quote"] = "Gross revenue pending"
    _validate(analysis, {1: "Gross revenue pending\nUnrelated transaction 100000"})
    assert financial_analysis_citation_status(analysis)["revenue"]["reason"] == "value_not_in_quote"


def test_negative_accounting_value_is_not_reversed_or_rounded():
    analysis = _analysis(net_income="(3000.25)")
    analysis.analysis["fact_citations"]["net_income"] = [_citation("net_income", "Net income", "(3,000.25)")]
    _validate(analysis, {1: "Gross revenue 100000\nCost of goods sold 87000\nNet income (3,000.25)"})
    citation = validated_fact_citations_for_analysis(analysis)["net_income"][0]
    assert citation["value"] == "-3000.25"


def test_tax_form_and_line_must_be_printed_next_to_the_actual_line_label():
    analysis = _analysis(ordinary_business_income="3000", net_income=None)
    analysis.analysis["fact_citations"]["ordinary_business_income"] = [
        _citation("ordinary_business_income", "Ordinary business income", "3000",
                  quote="21 Ordinary business income 3000", tax_form="1120-S", tax_line="21")
    ]
    _validate(analysis, {1: "Form 1120-S\n21 Ordinary business income 3000\n2025 Tax return"})
    assert validated_fact_citations_for_analysis(analysis)["ordinary_business_income"][0]["tax_line"] == "21"
    assert "net_income" not in validated_fact_citations_for_analysis(analysis)
    analysis.analysis["fact_citations"]["ordinary_business_income"][0]["tax_line"] = "22"
    _validate(analysis, {1: "Form 1120-S\n21 Ordinary business income 3000\n22 Total income 9000"})
    assert financial_analysis_citation_status(analysis)["ordinary_business_income"]["reason"] == "tax_line_not_found_with_label"


def test_ordinary_taxable_and_book_income_do_not_inherit_net_income_measure():
    for label in ("Ordinary business income", "Taxable income", "Net income per books"):
        analysis = _analysis()
        analysis.analysis["fact_citations"]["net_income"] = [_citation("net_income", label, "3000")]
        _validate(analysis, {1: f"{label} 3000"})
        assert financial_analysis_citation_status(analysis)["net_income"]["reason"] == "measure_label_mismatch"
    analysis = _analysis(net_income=None, net_income_per_books="3000")
    analysis.analysis["fact_citations"]["net_income_per_books"] = [_citation("net_income_per_books", "Net income per books", "3000")]
    _validate(analysis, {1: "Net income per books 3000"})
    assert "net_income_per_books" in validated_fact_citations_for_analysis(analysis)


def test_a_number_elsewhere_on_the_page_is_not_proof_of_a_tax_form():
    analysis = _analysis(gross_revenue="1120")
    analysis.analysis["fact_citations"]["revenue"] = [_citation("revenue", "Gross revenue", "1120", tax_form="1120")]
    _validate(analysis, {1: "Gross revenue 1120"})
    assert financial_analysis_citation_status(analysis)["revenue"]["reason"] == "tax_form_not_found_on_page"


def test_scanned_or_non_pdf_source_keeps_numbers_without_searchable_proof():
    for source_kind, reason in (("pdf_scanned", "source_not_searchable"), ("non_pdf", "source_citation_unavailable")):
        analysis = _analysis()
        manifest = build_financial_citation_manifest(analysis, source_pages={}, source_kind=source_kind, source_hash=analysis.content_hash)
        assert manifest["statements"][0]["fact_citation_status"]["revenue"]["reason"] == reason
        assert manifest["statements"][0]["fact_citations"] == {}
        assert analysis.analysis["key_facts"]["gross_revenue"] == "100000"


def test_legacy_raw_model_citations_cannot_self_verify_during_cache_enrichment():
    analysis = _analysis()
    ensure_legacy_financial_citation_manifest(analysis)
    assert validated_fact_citations_for_analysis(analysis) == {}
    assert financial_analysis_citation_status(analysis)["revenue"]["reason"] == "legacy_analysis_requires_explicit_reanalysis"
    before = deepcopy(analysis.analysis)
    ensure_legacy_financial_citation_manifest(analysis)
    assert analysis.analysis == before


def test_hash_mismatch_blocks_citation_verification_without_rejecting_document():
    analysis = _analysis()
    manifest = build_financial_citation_manifest(analysis, source_pages={1: "Gross revenue 100000"}, source_kind="pdf_searchable", source_hash="b" * 64)
    assert manifest["statements"][0]["fact_citation_status"]["revenue"]["reason"] == "source_hash_mismatch"


def test_fact_and_source_changes_cannot_reuse_old_citations():
    analysis = _analysis()
    _validate(analysis)
    period = statement_period_payload(analysis)
    assert validated_fact_citations_for_period(period, analysis)["revenue"]
    analysis.analysis["key_facts"]["gross_revenue"] = "200000"
    assert "revenue" not in validated_fact_citations_for_analysis(analysis)
    assert "revenue" not in validated_fact_citations_for_period(period, analysis)
    assert financial_period_citation_status(period, analysis)["revenue"]["reason"] == "financial_period_value_mismatch"
    analysis.content_hash = "b" * 64
    assert validated_fact_citations_for_period(period, analysis) == {}


@pytest.mark.parametrize("attribute,value", [("period_end", "2024-12-31"), ("accounting_basis", "cash"), ("currency", "CAD")])
def test_period_context_cannot_borrow_same_value_from_another_context(attribute, value):
    analysis = _analysis()
    _validate(analysis)
    period = statement_period_payload(analysis)
    setattr(period, attribute, value)
    assert validated_fact_citations_for_period(period, analysis) == {}


def test_changed_manifest_is_not_trusted_as_original_server_validation():
    analysis = _analysis()
    _validate(analysis)
    analysis.analysis["financial_fact_citations"]["statements"][0]["fact_citations"]["revenue"][0]["page"] = 99
    assert validated_fact_citations_for_analysis(analysis) == {}
    assert financial_analysis_citation_status(analysis)["revenue"]["reason"] == "citation_manifest_changed"


def test_derived_profit_and_ebitda_cite_inputs_not_invented_printed_lines():
    analysis = _analysis(interest="200", income_taxes="0", depreciation_and_amortization="300")
    for field, label, value in (("interest", "Interest", "200"), ("income_taxes", "Income taxes", "0"), ("depreciation_and_amortization", "Depreciation and amortization", "300")):
        analysis.analysis["fact_citations"][field] = [_citation(field, label, value)]
    _validate(analysis, {1: "Gross revenue 100000\nCost of goods sold 87000\nNet income 3000\nInterest 200\nIncome taxes 0\nDepreciation and amortization 300"})
    period = statement_period_payload(analysis)
    assert period.gross_profit is None and period.ebitda == Decimal("3500")
    citations = validated_fact_citations_for_period(period, analysis)
    assert [item["field"] for item in citations["gross_profit"]] == ["revenue", "cogs"]
    assert [item["field"] for item in citations["ebitda"]] == ["net_income", "interest", "income_taxes", "depreciation_and_amortization"]
    assert financial_period_citation_status(period, analysis)["gross_profit"]["reason"] == "derived_from_verified_inputs"
    period.gross_profit = Decimal("4000")
    assert "gross_profit" not in validated_fact_citations_for_period(period, analysis)
    assert financial_period_citation_status(period, analysis)["gross_profit"]["reason"] == "financial_period_value_mismatch"


def test_multi_statement_index_preserves_exact_column_identity_and_citation():
    analysis = _analysis()
    first = analysis.analysis["key_facts"]
    second = {**first, "gross_revenue": "200000", "net_income": "6000"}
    first["fact_citations"] = deepcopy(analysis.analysis["fact_citations"])
    second["fact_citations"] = {"revenue": [_citation("revenue", "Gross revenue B", "200000", page=2)],
                                "net_income": [_citation("net_income", "Net income B", "6000", page=2)]}
    analysis.analysis["key_facts"] = {"income_statements": [first, second]}
    _validate(analysis, {1: "Gross revenue 100000\nCost of goods sold 87000\nNet income 3000", 2: "Gross revenue B 200000\nNet income B 6000"})
    first_period = statement_period_payload(analysis, statement_index=0)
    second_period = statement_period_payload(analysis, statement_index=1)
    assert validated_fact_citations_for_period(first_period, analysis)["revenue"][0]["page"] == 1
    assert validated_fact_citations_for_period(second_period, analysis)["revenue"][0]["page"] == 2
    second_period.idempotency_key = first_period.idempotency_key
    assert "revenue" not in validated_fact_citations_for_period(second_period, analysis)


def test_citation_manifest_bounds_lists_without_storing_raw_pages():
    analysis = _analysis()
    analysis.analysis["fact_citations"]["revenue"] *= 100
    manifest = _validate(analysis, {1: "Gross revenue 100000\nPrivate unrelated client details"})
    citations = manifest["statements"][0]["fact_citations"]["revenue"]
    assert len(citations) == 4
    assert "Private unrelated client details" not in str(manifest)


@pytest.mark.asyncio
async def test_normal_packet_loader_retains_tax_citations_and_reads_only_current_file_hashes():
    from app.routers import dealer_ai_intake as router

    analysis = _analysis(tax_year="2025", ordinary_business_income="3000", net_income=None)
    analysis.classification = "tax_return"
    analysis.analysis["fact_citations"]["ordinary_business_income"] = [
        _citation("ordinary_business_income", "Ordinary business income", "3000",
                  quote="21 Ordinary business income 3000", tax_form="1120-S", tax_line="21")
    ]
    _validate(analysis, {1: "Form 1120-S\nGross revenue 100000\nCost of goods sold 87000\n21 Ordinary business income 3000"})
    older = SimpleNamespace(**{**vars(analysis), "id": uuid4(), "analysis": {"key_facts": {"tax_year": "2025", "ordinary_business_income": 9000}}})
    foreign = SimpleNamespace(**{**vars(analysis), "id": uuid4(), "bucket_file_id": uuid4()})
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [analysis, older, foreign]))
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))
    intake = SimpleNamespace(bucket_id=uuid4())
    with patch.object(router, "_credit_financials_section", AsyncMock(return_value=None)):
        result = await router._collect_packet_financials(db, intake, active_file_ids={analysis.bucket_file_id})
    assert len(result["tax_years"]) == 1
    measure = result["tax_years"][0]["financial_measures"]["ordinary_business_income"]
    assert measure["value"] == 3000 and measure["measure"] == "ordinary_business_income"
    assert measure["fact_citation_status"]["status"] == "verified"
    assert measure["fact_citations"][0]["tax_line"] == "21"
    assert measure["fact_citations"][0]["analysis_id"] == str(analysis.id)
    assert result["tax_years"][0]["net_income"] is None
    statement = db.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "bucket_file_analyses.content_hash = bucket_files.content_hash" in sql
    assert "bucket_files.deleted_at IS NULL" in sql
    assert "bucket_files.status = 'uploaded'" in sql
    assert "bucket_file_analyses.bucket_id = bucket_files.bucket_id" in sql
    assert str(analysis.bucket_file_id) in sql and str(intake.bucket_id) in sql
