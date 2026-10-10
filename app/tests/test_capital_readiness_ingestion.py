import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.services.capital_readiness_ingestion import (
    ingest_profile_financial_periods,
    statement_period_payload,
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


def test_incomplete_source_context_is_not_guessed_or_merged():
    for missing in ("business_name", "basis", "currency", "period_start", "period_end"):
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
