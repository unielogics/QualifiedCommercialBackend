import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.services.capital_readiness_ingestion import (
    ingest_profile_financial_periods,
    statement_context_warnings,
    statement_period_payload,
    statement_profitability_diagnostics,
)


def _analysis(**overrides):
    facts = {"business_name": "Example LLC", "basis": "accrual", "currency": "USD", "period_start": "2025-01-01", "period_end": "2025-12-31", "gross_revenue": "1,000", "cost_of_goods_sold": "800", "gross_profit": "200", "net_income": "(50)"}
    facts.update(overrides)
    return SimpleNamespace(id=uuid4(), bucket_file_id=uuid4(), status="completed", classification="current_p_and_l", analysis={"key_facts": facts}, content_hash="a" * 64, confidence="high", analysis_version=2)


def test_extracted_period_preserves_source_and_negative_income():
    analysis = _analysis()
    result = statement_period_payload(analysis)
    assert result is not None
    assert result.source_file_id == analysis.bucket_file_id
    assert result.source_analysis_id == analysis.id
    assert result.content_hash == analysis.content_hash
    assert result.net_income == Decimal("-50")
    assert result.ebitda is None
    assert result.confidence == Decimal("0.85")


def test_blank_values_are_unknown_and_explicit_zero_is_preserved():
    result = statement_period_payload(_analysis(net_income="", cost_of_goods_sold="0"))
    assert result.net_income is None
    assert result.cogs == Decimal("0")


def test_unknown_source_labels_preserve_numbers_without_guessing_context():
    analysis = _analysis(business_name="", basis="", currency="")
    payload = statement_period_payload(analysis)
    assert payload.entity_name == f"[Unidentified statement:{analysis.id}:single]"
    assert payload.accounting_basis == "unknown"
    assert payload.currency == "XXX"
    assert payload.revenue == Decimal("1000")
    assert payload.net_income == Decimal("-50")
    assert payload.confidence == Decimal("0.65")
    assert {warning["code"] for warning in statement_context_warnings(analysis.analysis["key_facts"])} == {
        "financial_entity_unidentified", "financial_accounting_basis_unknown", "financial_currency_unknown"
    }
    # Missing dates cannot become invented canonical financial periods.
    for missing in ("period_start", "period_end"):
        assert statement_period_payload(_analysis(**{missing: ""})) is None


def test_invalid_period_and_nonfinancial_classification_are_rejected():
    assert statement_period_payload(_analysis(period_start="2026-01-01", period_end="2025-12-31")) is None
    analysis = _analysis()
    analysis.classification = "tax_return"
    assert statement_period_payload(analysis) is None


def test_multiple_statements_keep_their_own_identity_period_and_values():
    analysis = _analysis()
    facts = analysis.analysis["key_facts"]
    facts["income_statements"] = [
        {**facts, "business_name": "Operating LLC", "gross_revenue": "1000"},
        {**facts, "business_name": "Property LLC", "gross_revenue": "500", "currency": "CAD"},
    ]
    first = statement_period_payload(analysis, statement_index=0)
    second = statement_period_payload(analysis, statement_index=1)
    assert first.entity_name == "Operating LLC" and first.revenue == Decimal("1000")
    assert second.entity_name == "Property LLC" and second.currency == "CAD" and second.revenue == Decimal("500")
    assert first.idempotency_key != second.idempotency_key


def test_unidentified_statement_columns_never_share_a_source_identity():
    analysis = _analysis()
    facts = analysis.analysis["key_facts"]
    facts["income_statements"] = [{**facts, "business_name": ""}, {**facts, "business_name": ""}]
    first = statement_period_payload(analysis, statement_index=0)
    second = statement_period_payload(analysis, statement_index=1)
    assert first.entity_name != second.entity_name
    assert first.source_analysis_id == second.source_analysis_id == analysis.id


def test_printed_entity_name_does_not_have_to_match_the_application():
    payload = statement_period_payload(_analysis(business_name="Related Operating LLC"))
    assert payload.entity_name == "Related Operating LLC"
    assert payload.gross_profit == Decimal("200")
    assert payload.net_income == Decimal("-50")


def test_wrapped_null_label_does_not_hide_a_second_explicit_label():
    payload = statement_period_payload(_analysis(
        business_name={"value": None}, entity_name={"value": "Printed DBA"},
        basis={"value": None}, accounting_basis={"value": "cash"},
    ))
    assert payload.entity_name == "Printed DBA"
    assert payload.accounting_basis == "cash"


@pytest.mark.parametrize("gross,net,gross_status,net_status", [
    ("9.999", "1.999", "concerning", "concerning"),
    ("10", "2", "acceptable", "acceptable"),
    ("12.999", "2.999", "acceptable", "acceptable"),
    ("13", "3", "healthy", "healthy"),
    ("17.999", "4.999", "healthy", "healthy"),
    ("18", "5", "very_strong", "very_strong"),
    ("-3", "-1", "concerning", "concerning"),
])
def test_source_local_diagnostics_classify_exact_unrounded_margin_boundaries(gross, net, gross_status, net_status):
    analysis = _analysis(gross_revenue="100", gross_profit=gross, cost_of_goods_sold=None, net_income=net)
    result = statement_profitability_diagnostics(analysis)[0]
    assert result["gross_margin_status"] == gross_status
    assert result["net_margin_status"] == net_status
    assert Decimal(result["gross_margin_unrounded"]) == Decimal(gross)
    assert Decimal(result["net_margin_unrounded"]) == Decimal(net)
    assert result["state"] == "provisional"
    assert result["source_analysis_id"] == str(analysis.id)


def test_date_unknown_numbers_remain_useful_without_invented_periods():
    analysis = _analysis(period_start="", period_end="", gross_profit=None)
    assert statement_period_payload(analysis) is None
    result = statement_profitability_diagnostics(analysis)[0]
    assert result["period_start"] is None and result["period_end"] is None
    assert result["gross_profit"] == 200
    assert result["gross_profit_source"] == "derived"
    assert result["gross_margin_pct"] == 20
    assert result["gross_margin_status"] == "very_strong"
    assert result["net_margin_pct"] == -5
    assert {warning["code"] for warning in result["warnings"]} == {"financial_period_unknown"}


def test_oversized_entity_label_does_not_fail_or_erase_readable_financial_evidence():
    analysis = _analysis(business_name="A" * 201)
    assert statement_period_payload(analysis) is None
    result = statement_profitability_diagnostics(analysis)[0]
    assert result["entity_name"] == "A" * 201
    assert result["gross_margin_pct"] == 20
    assert result["period_start"] == "2025-01-01"
    assert result["period_end"] == "2025-12-31"
    assert {warning["code"] for warning in result["warnings"]} == {"financial_source_context_invalid"}


@pytest.mark.parametrize("invalid", ["1e999999", "1e-999999", "9" * 2000, "NaN", "Infinity"])
def test_invalid_or_unbounded_numbers_never_overflow_storage_or_json(invalid):
    analysis = _analysis(gross_revenue=invalid)
    payload = statement_period_payload(analysis)
    assert payload.revenue is None
    result = statement_profitability_diagnostics(analysis)[0]
    assert result["revenue"] is None
    assert result["gross_margin_pct"] is None


def test_printed_vs_derived_profit_difference_is_visible_not_silently_rewritten():
    result = statement_profitability_diagnostics(_analysis(gross_profit="300"))[0]
    assert result["gross_profit"] == 300
    assert result["derived_gross_profit"] == 200
    warning = next(warning for warning in result["warnings"] if warning["code"] == "gross_profit_reconciliation")
    assert warning["difference"] == 100
    assert warning["severity"] == "requires_review"


@pytest.mark.parametrize("revenue", ["0", "-100", "", None])
def test_missing_or_nonpositive_revenue_cannot_produce_margin_percentages(revenue):
    result = statement_profitability_diagnostics(_analysis(gross_revenue=revenue))[0]
    assert result["gross_margin_pct"] is None
    assert result["net_margin_pct"] is None
    assert result["gross_margin_status"] == "unavailable"
    assert result["net_margin_status"] == "unavailable"


def test_bank_inflows_and_arbitrary_income_aliases_are_never_margin_inputs():
    analysis = _analysis(gross_revenue=None, cost_of_goods_sold=None, gross_profit=None, net_income=None,
                         total_deposits_and_credits=10000, total_withdrawals_and_debits=8000,
                         ending_balance=2000, ordinary_income=1000, operating_income=1000)
    assert statement_profitability_diagnostics(analysis) == []
    analysis.classification = "bank_statement"
    analysis.analysis["key_facts"]["gross_revenue"] = 10000
    assert statement_profitability_diagnostics(analysis) == []


def test_multiple_statement_diagnostics_never_borrow_values_from_other_columns():
    analysis = _analysis()
    analysis.analysis["key_facts"]["income_statements"] = [
        {"business_name": "A LLC", "gross_revenue": "1000", "gross_profit": "130", "net_income": None},
        {"business_name": "B LLC", "gross_revenue": "2000", "gross_profit": None, "net_income": "100"},
    ]
    first, second = statement_profitability_diagnostics(analysis)
    assert first["gross_margin_pct"] == 13 and first["net_margin_pct"] is None
    assert second["gross_margin_pct"] is None and second["net_margin_pct"] == 5
    assert first["statement_index"] == 0 and second["statement_index"] == 1


def test_explicit_linked_statement_imports_without_a_primary_bucket_and_replays_once():
    analysis = _analysis()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=None, intake_id=uuid4())
    def result(rows):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[result([analysis]), result([])]), add=Mock(), flush=AsyncMock())
    selected = AsyncMock(return_value=[SimpleNamespace(id=analysis.bucket_file_id)])
    with patch("app.services.capital_readiness_ingestion.selected_files_for_intake", selected):
        assert asyncio.run(ingest_profile_financial_periods(db, profile)) == 1
    period = db.add.call_args.args[0]
    assert period.profile_id == profile.id and period.source_analysis_id == analysis.id
    assert period.source_file_id == analysis.bucket_file_id
    assert period.review_status == "submitted"
    assert period.content_hash == analysis.content_hash
    db.execute = AsyncMock(side_effect=[result([analysis]), result([period.idempotency_key])])
    db.add.reset_mock()
    with patch("app.services.capital_readiness_ingestion.selected_files_for_intake", selected):
        assert asyncio.run(ingest_profile_financial_periods(db, profile)) == 0
    db.add.assert_not_called()


def test_missing_labels_ingest_as_provisional_source_local_row_with_warnings():
    analysis = _analysis(business_name="", currency="", basis="")
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), intake_id=None)
    def result(rows):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[result([analysis]), result([])]), add=Mock(), flush=AsyncMock())
    assert asyncio.run(ingest_profile_financial_periods(db, profile)) == 1
    row = db.add.call_args.args[0]
    assert row.review_status == "submitted"
    assert row.accounting_basis == "unknown" and row.currency == "XXX"
    assert row.revenue == Decimal("1000")
    assert {warning["code"] for warning in row.reconciliation_warnings} == {
        "financial_entity_unidentified", "financial_accounting_basis_unknown", "financial_currency_unknown"
    }
