"""Numbers-first diagnostics retain identity warnings without dropping evidence."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.models.capital_readiness import ApplicationFinancialPeriod
from app.schemas.capital_readiness import CapitalReadinessReviewRequest
from app.services import application_programs, capital_readiness, funding_programs


def _result(*, row=None, rows=()):
    return SimpleNamespace(
        scalar_one=lambda: row,
        scalar_one_or_none=lambda: row,
        scalars=lambda: SimpleNamespace(all=lambda: list(rows)),
    )


@pytest.fixture
def computation(monkeypatch):
    now = datetime.now(UTC)
    profile = SimpleNamespace(
        id=uuid4(), vertical="main_street", communication_locale="en",
        industry=None, naics_code=None, underwriting_current_dscr=Decimal("1.4"),
        underwriting_updated_at=now, use_of_funds_revision=1,
        use_of_funds=[{"category": "working_capital", "amount": 50000}],
    )
    period = ApplicationFinancialPeriod(
        id=uuid4(), profile_id=profile.id, entity_name="Trading DBA",
        accounting_basis="accrual", currency="USD", period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31), months_covered=12,
        source_kind="ai_extracted", review_status="submitted", cogs_applicability="applicable",
        revenue=Decimal("1000000"), cogs=Decimal("820000"), gross_profit=Decimal("180000"),
        net_income=Decimal("50000"), confidence=Decimal("0.85"), content_hash="a" * 64,
        idempotency_key="coherent-statement", reconciliation_warnings=[], created_at=now,
    )
    facts = [SimpleNamespace(
        id=uuid4(), field_key=key, value={"value": value}, normalized_value=value,
        status="accepted", confidence=Decimal("0.9"), created_at=now,
        source_file_id=None, source_analysis_id=None,
    ) for key, value in [("nsf_count", "0"), ("books_reconciled", "true"), ("credit_score", "740")]]
    policy = SimpleNamespace(
        id=uuid4(), policy_key="qc_lending_margin_v1", version=1,
        metric_thresholds={}, pillar_weights={}, minimum_coverage_pct=Decimal("60"),
    )
    monkeypatch.setattr(capital_readiness, "ingest_profile_financial_periods", AsyncMock())
    monkeypatch.setattr(capital_readiness, "published_policy", AsyncMock(return_value=policy))
    monkeypatch.setattr(capital_readiness, "latest_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(capital_readiness, "_calculation_inputs", AsyncMock(return_value=([period], facts, [], [], [])))
    monkeypatch.setattr(capital_readiness, "canonical_entity_aliases", AsyncMock(return_value={"legalborrower"}))
    monkeypatch.setattr(application_programs, "published_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(funding_programs, "commercial_terms_by_program", AsyncMock(return_value={}))

    def add(row):
        if row.id is None:
            row.id = uuid4()

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(row=profile), _result(), _result()]),
        add=Mock(side_effect=add), flush=AsyncMock(),
    )
    return db, profile, period


@pytest.mark.asyncio
async def test_name_mismatch_keeps_metrics_score_and_warning(computation):
    db, profile, period = computation
    snapshot = await capital_readiness.recompute(db, profile, idempotency_key="numbers-first-test", expected_snapshot_version=None)
    metrics = {row["key"]: row for row in snapshot.metrics}
    assert metrics["gross_margin_pct"]["value"] == 18
    assert metrics["gross_margin_pct"]["status"] == "very_strong"
    assert metrics["net_margin_pct"]["value"] == 5
    assert metrics["net_margin_pct"]["source"]["identity_warning"] is True
    assert metrics["gross_margin_pct"]["source"]["source_period_ids"] == [str(period.id)]
    assert snapshot.score is not None
    assert snapshot.review_status == "awaiting_review"
    warning = next(row for row in snapshot.blockers if row["key"] == "financial_entity_mismatch")
    assert warning["severity"] == "warning"
    assert warning["blocks_file_acceptance"] is False
    assert "metric_key" not in warning  # Do not replace its identity-specific explanation.
    context = capital_readiness.build_ai_context(snapshot)
    metric = next(row for row in context["metrics"] if row["key"] == "gross_margin_pct")
    assert metric["numerator"] == 180000
    assert metric["denominator"] == 1000000
    assert metric["identity_warning"] is True


@pytest.mark.asyncio
async def test_unknown_context_is_provisional_not_lender_eligibility(computation):
    db, profile, period = computation
    period.accounting_basis = "unknown"
    period.currency = "XXX"
    period.review_status = "confirmed"
    snapshot = await capital_readiness.recompute(db, profile, idempotency_key="unknown-context-test", expected_snapshot_version=None)
    assert next(row for row in snapshot.metrics if row["key"] == "gross_margin_pct")["value"] == 18
    application_programs.published_candidates.assert_awaited_once()
    overrides = application_programs.published_candidates.call_args.kwargs["readiness_metric_overrides"]
    assert overrides == {"gross_margin_pct": None, "net_margin_pct": None}


@pytest.mark.asyncio
async def test_human_confirmed_source_association_can_confirm_snapshot(computation, monkeypatch):
    db, profile, period = computation
    snapshot = await capital_readiness.recompute(db, profile, idempotency_key="confirmed-association-test", expected_snapshot_version=None)
    period.review_status = "confirmed"
    monkeypatch.setattr(capital_readiness, "latest_snapshot", AsyncMock(return_value=snapshot))
    db.execute = AsyncMock(side_effect=[_result(row=profile), _result(rows=[period])])
    result = await capital_readiness.review_snapshot(
        db, profile, CapitalReadinessReviewRequest(expected_snapshot_version=1, status="confirmed", note="Reviewed DBA association."),
        SimpleNamespace(id=uuid4()),
    )
    assert result.review_status == "confirmed"
    assert result.score == snapshot.score
    assert result.supersedes_snapshot_id == snapshot.id


@pytest.mark.asyncio
async def test_material_amount_discrepancy_still_requires_reconciliation(computation):
    db, profile, period = computation
    period.reconciliation_warnings = [{"code": "gross_profit_reconciliation", "severity": "requires_review"}]
    snapshot = await capital_readiness.recompute(db, profile, idempotency_key="amount-discrepancy-test", expected_snapshot_version=None)
    metrics = {row["key"]: row for row in snapshot.metrics}
    assert metrics["gross_margin_pct"]["value"] is None
    assert metrics["gross_margin_pct"]["source"]["candidate_value"] == 18
    assert metrics["net_margin_pct"]["value"] == 5
    assert any(row["key"] == "financial_reconciliation" for row in snapshot.blockers)
