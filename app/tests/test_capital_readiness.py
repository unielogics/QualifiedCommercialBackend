from __future__ import annotations

import asyncio
import calendar
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.capital_readiness import (
    ApplicationCapitalReadinessSnapshot,
    ApplicationFinancialPeriod,
)
from app.schemas.capital_readiness import FinancialPeriodCreate
from app.services.capital_readiness import (
    _current_evidence_sources,
    _policy_lineage,
    aggregate_period_window,
    build_ai_context,
    classify_gross_margin,
    classify_net_margin,
    compatible_period_windows,
    effective_confidence_pct,
    filter_current_financial_periods,
    gated_margin_display,
    margin_value,
    material_change_comparison,
    normalize_entity_name,
    period_warnings,
    published_policy,
    read_snapshot,
    uses_operating_margin_policy,
)


class _PolicyRows:
    def __init__(self, rows: list[SimpleNamespace]):
        self.rows = rows

    def scalars(self) -> _PolicyRows:
        return self

    def all(self) -> list[SimpleNamespace]:
        return self.rows


def _policy(
    policy_key: str,
    *,
    version: int = 1,
    scope: dict | None = None,
) -> SimpleNamespace:
    thresholds = {}
    if scope is not None:
        thresholds["scope"] = scope
    return SimpleNamespace(
        id=uuid4(),
        policy_key=policy_key,
        version=version,
        status="published",
        metric_thresholds=thresholds,
    )


def test_published_policy_uses_explicit_scope_precedence_and_longest_naics_prefix() -> None:
    firm = _policy("qc_lending_margin_v1")
    vertical = _policy(
        "main_street_override",
        scope={"kind": "vertical", "key": "main_street"},
    )
    industry = _policy(
        "restaurant_override",
        scope={"kind": "industry", "key": "full-service restaurants"},
    )
    naics_sector = _policy(
        "food_service_sector_override",
        scope={"kind": "naics_prefix", "key": "72"},
    )
    naics_exact = _policy(
        "full_service_naics_override",
        scope={"kind": "naics_prefix", "key": "722511"},
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=_PolicyRows([firm, vertical, industry, naics_sector, naics_exact])
        )
    )
    profile = SimpleNamespace(
        vertical="main_street",
        industry="Full-Service Restaurants",
        naics_code="722511",
    )

    selected = asyncio.run(published_policy(db, profile))

    assert selected is naics_exact
    assert _policy_lineage(selected) == {
        "kind": "capital_readiness_policy",
        "policy_id": str(naics_exact.id),
        "policy_key": "full_service_naics_override",
        "policy_version": 1,
        "scope": {"kind": "naics_prefix", "key": "722511"},
    }


def test_published_policy_falls_back_by_industry_vertical_then_firm() -> None:
    firm = _policy("qc_lending_margin_v1")
    vertical = _policy(
        "main_street_override",
        scope={"kind": "vertical", "key": "main_street"},
    )
    industry = _policy(
        "restaurant_override",
        scope={"kind": "industry", "key": "full-service restaurants"},
    )
    rows = _PolicyRows([firm, vertical, industry])
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))

    assert (
        asyncio.run(
            published_policy(
                db,
                SimpleNamespace(
                    vertical="main_street",
                    industry="FULL-SERVICE RESTAURANTS",
                    naics_code=None,
                ),
            )
        )
        is industry
    )
    assert (
        asyncio.run(
            published_policy(
                db,
                SimpleNamespace(
                    vertical="main_street", industry="retail", naics_code=None
                ),
            )
        )
        is vertical
    )
    assert (
        asyncio.run(
            published_policy(
                db,
                SimpleNamespace(
                    vertical="dealer", industry="retail", naics_code=None
                ),
            )
        )
        is firm
    )


@pytest.mark.parametrize(
    "naics_code,provenance",
    [
        ("72251", {"status": "canonical"}),
        ("7225119", {"status": "canonical"}),
        ("72251A", {"status": "canonical"}),
        ("722511", {"status": "pending"}),
        ("722511", {"status": "candidate"}),
        ("722511", {"status": "suggested"}),
        ("722511", {"entry_status": "pending"}),
    ],
)
def test_published_policy_does_not_use_invalid_or_unverified_naics(
    naics_code: str, provenance: dict
) -> None:
    firm = _policy("qc_lending_margin_v1")
    vertical = _policy(
        "main_street_override",
        scope={"kind": "vertical", "key": "main_street"},
    )
    naics = _policy(
        "full_service_naics_override",
        scope={"kind": "naics_prefix", "key": "722511"},
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_PolicyRows([firm, vertical, naics])))
    profile = SimpleNamespace(
        vertical="main_street",
        industry=None,
        naics_code=naics_code,
        classification_provenance=provenance,
        backfill_needs_review=False,
    )

    assert asyncio.run(published_policy(db, profile)) is vertical


def test_unverified_classification_does_not_select_industry_override() -> None:
    firm = _policy("qc_lending_margin_v1")
    vertical = _policy(
        "main_street_override",
        scope={"kind": "vertical", "key": "main_street"},
    )
    industry = _policy(
        "restaurant_override",
        scope={"kind": "industry", "key": "full-service restaurants"},
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_PolicyRows([firm, vertical, industry]))
    )
    profile = SimpleNamespace(
        vertical="main_street",
        industry="Full-Service Restaurants",
        naics_code="722511",
        classification_provenance={"status": "pending"},
        backfill_needs_review=False,
    )

    assert asyncio.run(published_policy(db, profile)) is vertical


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-1", "concerning"),
        ("9.9999", "concerning"),
        ("10", "acceptable"),
        ("12.9999", "acceptable"),
        ("13", "healthy"),
        ("17.9999", "healthy"),
        ("18", "very_strong"),
    ],
)
def test_gross_margin_boundaries_use_unrounded_value(value: str, expected: str) -> None:
    assert classify_gross_margin(Decimal(value)) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-1", "concerning"),
        ("1.9999", "concerning"),
        ("2", "acceptable"),
        ("2.9999", "acceptable"),
        ("3", "healthy"),
        ("4.9999", "healthy"),
        ("5", "very_strong"),
    ],
)
def test_net_margin_boundaries_use_unrounded_value(value: str, expected: str) -> None:
    assert classify_net_margin(Decimal(value)) == expected


def test_missing_and_nonpositive_revenue_remain_unavailable() -> None:
    assert margin_value(Decimal("5"), None) is None
    assert margin_value(Decimal("5"), Decimal("0")) is None
    assert margin_value(Decimal("5"), Decimal("-1")) is None
    assert margin_value(Decimal("0"), Decimal("100")) == Decimal("0")


def _period(
    *,
    month: int,
    year: int,
    revenue: str = "100",
    gross_profit: str = "20",
    net_income: str = "5",
    entity: str = "Example LLC",
    basis: str = "accrual",
    currency: str = "USD",
) -> ApplicationFinancialPeriod:
    last_day = calendar.monthrange(year, month)[1]
    return ApplicationFinancialPeriod(
        id=uuid4(),
        profile_id=uuid4(),
        entity_name=entity,
        accounting_basis=basis,
        currency=currency,
        period_start=date(year, month, 1),
        period_end=date(year, month, last_day),
        months_covered=1,
        source_kind="stated",
        review_status="confirmed",
        cogs_applicability="applicable",
        revenue=Decimal(revenue),
        gross_profit=Decimal(gross_profit),
        net_income=Decimal(net_income),
        confidence=Decimal("0.9"),
        content_hash=(f"{year:04d}{month:02d}" * 11)[:64],
        idempotency_key=f"period-{year}-{month:02d}",
        reconciliation_warnings=[],
        created_at=datetime(year, month, last_day, tzinfo=UTC),
    )


def test_compatible_monthly_windows_are_like_for_like() -> None:
    periods = [
        _period(month=month, year=year)
        for year in (2024, 2025)
        for month in range(1, 13)
    ]
    current, prior = compatible_period_windows(periods)
    assert len(current) == 12
    assert len(prior) == 12
    assert {row.period_end.year for row in current} == {2025}
    assert {row.period_end.year for row in prior} == {2024}


def test_aggregate_uses_ratio_of_sums_not_average_percentages() -> None:
    small = _period(month=1, year=2025, revenue="100", gross_profit="10")
    large = _period(month=2, year=2025, revenue="900", gross_profit="180")
    aggregate = aggregate_period_window([small, large])
    assert aggregate["revenue"] == Decimal("1000")
    assert aggregate["gross_profit"] == Decimal("190")
    assert margin_value(aggregate["gross_profit"], aggregate["revenue"]) == Decimal("19")


def test_incompatible_entities_are_never_aggregated() -> None:
    latest = _period(month=2, year=2025, entity="Operating LLC")
    other = _period(month=1, year=2025, entity="Property LLC")
    current, prior = compatible_period_windows([latest, other])
    assert current == [latest]
    assert prior == []


def test_canonical_entity_selects_borrower_instead_of_newest_related_entity() -> None:
    borrower = _period(month=1, year=2025, entity="Operating Company, LLC")
    related = _period(month=2, year=2025, entity="Property Holding LLC")
    current, prior = compatible_period_windows(
        [related, borrower],
        canonical_entities={normalize_entity_name("Operating Company")},
    )
    assert current == [borrower]
    assert prior == []


def test_missing_canonical_entity_returns_no_scoring_window() -> None:
    related = _period(month=2, year=2025, entity="Property Holding LLC")
    assert compatible_period_windows(
        [related], canonical_entities={normalize_entity_name("Operating Company LLC")}
    ) == ([], [])


def test_current_evidence_filter_preserves_history_but_excludes_stale_sources() -> None:
    manual = _period(month=1, year=2025)
    current = _period(month=2, year=2025)
    stale_bytes = _period(month=3, year=2025)
    stale_analysis = _period(month=4, year=2025)
    current_file_id, stale_file_id, changed_file_id = uuid4(), uuid4(), uuid4()
    current_analysis_id, old_analysis_id, new_analysis_id = uuid4(), uuid4(), uuid4()

    current.source_file_id = current_file_id
    current.source_analysis_id = current_analysis_id
    current.content_hash = "a" * 64
    stale_bytes.source_file_id = changed_file_id
    stale_bytes.content_hash = "b" * 64
    stale_analysis.source_file_id = stale_file_id
    stale_analysis.source_analysis_id = old_analysis_id
    stale_analysis.content_hash = "c" * 64

    files = {
        current_file_id: SimpleNamespace(id=current_file_id, content_hash="a" * 64),
        changed_file_id: SimpleNamespace(id=changed_file_id, content_hash="d" * 64),
        stale_file_id: SimpleNamespace(id=stale_file_id, content_hash="c" * 64),
    }
    analyses = {
        current_file_id: SimpleNamespace(id=current_analysis_id),
        stale_file_id: SimpleNamespace(id=new_analysis_id),
    }
    assert filter_current_financial_periods(
        [manual, current, stale_bytes, stale_analysis],
        files=files,  # type: ignore[arg-type]
        newest_analyses=analyses,  # type: ignore[arg-type]
    ) == [manual, current]


@pytest.mark.asyncio
async def test_current_evidence_sources_awaits_async_session_result() -> None:
    """Keep the production AsyncSession coroutine boundary under regression test."""

    file_id = uuid4()
    bucket_id = uuid4()
    analysis_id = uuid4()
    file_row = SimpleNamespace(id=file_id, content_hash="a" * 64)
    analysis_row = SimpleNamespace(id=analysis_id, bucket_file_id=file_id)

    class CoroutineExecuteSession:
        def __init__(self) -> None:
            self._results = iter(
                [
                    SimpleNamespace(
                        scalars=lambda: SimpleNamespace(all=lambda: [file_row])
                    ),
                    SimpleNamespace(
                        scalars=lambda: SimpleNamespace(all=lambda: [analysis_row])
                    ),
                ]
            )

        async def execute(self, _statement: object) -> object:
            return next(self._results)

    files, analyses = await _current_evidence_sources(
        CoroutineExecuteSession(),  # type: ignore[arg-type]
        SimpleNamespace(  # type: ignore[arg-type]
            primary_bucket_id=bucket_id,
            intake_id=None,
        ),
    )

    assert files == {file_id: file_row}
    assert analyses == {file_id: analysis_row}


def test_monthly_window_stops_at_first_gap() -> None:
    latest = _period(month=5, year=2025)
    april = _period(month=4, year=2025)
    february = _period(month=2, year=2025)
    current, prior = compatible_period_windows([latest, april, february])
    assert current == [latest, april]
    assert prior == []


def test_multi_month_comparison_requires_same_calendar_cutoff() -> None:
    current = _period(month=12, year=2025)
    current.months_covered = 6
    current.period_start = date(2025, 7, 1)
    prior_half = _period(month=6, year=2025)
    prior_half.months_covered = 6
    prior_half.period_start = date(2025, 1, 1)
    current_window, prior_window = compatible_period_windows([current, prior_half])
    assert current_window == [current]
    assert prior_window == []


def test_partial_monthly_window_compares_same_cutoff_prior_year() -> None:
    current = [_period(month=month, year=2025) for month in range(1, 7)]
    prior = [_period(month=month, year=2024) for month in range(1, 7)]
    current_window, prior_window = compatible_period_windows([*current, *prior])
    assert len(current_window) == 6
    assert len(prior_window) == 6
    assert {row.period_end.year for row in prior_window} == {2024}


def test_cogs_not_applicable_requires_review_before_treating_blank_as_zero() -> None:
    submitted = _period(month=1, year=2025, gross_profit="20")
    submitted.gross_profit = None
    submitted.cogs = None
    submitted.cogs_applicability = "not_applicable"
    submitted.review_status = "submitted"
    confirmed = _period(month=2, year=2025, gross_profit="20")
    confirmed.gross_profit = None
    confirmed.cogs = None
    confirmed.cogs_applicability = "not_applicable"
    assert aggregate_period_window([submitted])["gross_profit"] is None
    assert aggregate_period_window([confirmed])["gross_profit"] == Decimal("100")


def test_material_gross_profit_warning_blocks_display_until_review() -> None:
    payload = FinancialPeriodCreate(
        entity_name="Example LLC",
        accounting_basis="accrual",
        currency="USD",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        months_covered=12,
        source_kind="stated",
        revenue=Decimal("100"),
        cogs=Decimal("40"),
        gross_profit=Decimal("50"),
        idempotency_key="financial-period-2025",
    )
    assert period_warnings(payload)[0]["code"] == "gross_profit_reconciliation"
    value, status = gated_margin_display(
        Decimal("50"), "very_strong", blocked=True
    )
    assert value is None
    assert status == "unavailable"


def test_property_only_files_do_not_use_operating_margin_policy() -> None:
    assert uses_operating_margin_policy("dealer") is True
    assert uses_operating_margin_policy("main_street") is True
    assert uses_operating_margin_policy("real_estate") is False


def test_missing_evidence_lowers_confidence_without_changing_known_quality() -> None:
    metrics = [
        {
            "key": "gross_margin_pct",
            "status": "very_strong",
            "confidence_pct": 100,
            "source": {},
        },
        {
            "key": "internal_adjusted_ebitda",
            "status": "healthy",
            "confidence_pct": 100,
            "source": {"informational": True},
        },
    ]
    assert effective_confidence_pct(metrics, Decimal("10")) == Decimal("10")
    assert effective_confidence_pct(metrics, Decimal("80")) == Decimal("80")


def test_ai_context_is_bounded_and_prevents_ai_recalculation() -> None:
    snapshot = ApplicationCapitalReadinessSnapshot(
        id=uuid4(),
        profile_id=uuid4(),
        snapshot_version=1,
        policy_id=uuid4(),
        policy_key="qc_lending_margin_v1",
        policy_version=1,
        evidence_fingerprint="a" * 64,
        idempotency_key="capital-readiness-test",
        as_of=datetime.now(UTC),
        communication_locale="es",
        review_status="provisional",
        score=Decimal("72.5"),
        band="three_to_six_months",
        evidence_coverage_pct=Decimal("80"),
        confidence_pct=Decimal("90"),
        pillars=[],
        metrics=[
            {
                "key": f"metric_{index}",
                "value": index,
                "unit": "%",
                "status": "healthy",
                "source": {},
            }
            for index in range(30)
        ],
        strengths=[],
        blockers=[],
        phases=[{"key": "baseline_health_check", "status": "in_progress"}],
        program_opportunities=[],
        source_manifest=[
            {"kind": "financial_period", "id": str(uuid4()), "content_hash": "b" * 64}
        ],
    )
    context = build_ai_context(snapshot)
    assert len(context["metrics"]) == 20
    assert context["communication_locale"] == "es"
    safeguards = " ".join(context["safeguards"])
    assert "do not recalculate" in safeguards.lower()
    assert "cannot approve" in safeguards.lower()
    assert "Spanish" in safeguards
    assert context["policy"]["formula_version"] == "score_v2"


def test_client_safe_snapshot_removes_internal_action_and_review_identifiers() -> None:
    snapshot = ApplicationCapitalReadinessSnapshot(
        id=uuid4(),
        profile_id=uuid4(),
        snapshot_version=2,
        policy_id=uuid4(),
        policy_key="qc_lending_margin_v1",
        policy_version=1,
        formula_version="score_v2",
        evidence_fingerprint="a" * 64,
        idempotency_key="client-safe-snapshot",
        as_of=datetime.now(UTC),
        communication_locale="en",
        review_status="confirmed",
        score=Decimal("80"),
        band="ready_soon",
        evidence_coverage_pct=Decimal("90"),
        confidence_pct=Decimal("85"),
        pillars=[],
        metrics=[],
        strengths=[],
        blockers=[],
        phases=[
            {
                "key": "baseline_health_check",
                "label": "Baseline Health Check",
                "status": "ready",
                "description": "Review evidence",
                "actions": [
                    {
                        "id": str(uuid4()),
                        "action_key": str(uuid4()),
                        "profile_id": str(uuid4()),
                        "owner_user_id": str(uuid4()),
                        "dependencies": [str(uuid4())],
                        "phase_key": "baseline_health_check",
                        "title": "Reconcile books",
                        "status": "in_progress",
                    }
                ],
            }
        ],
        program_opportunities=[],
        source_manifest=[{"kind": "financial_period", "id": str(uuid4())}],
        reviewed_by_user_id=uuid4(),
        material_change={
            "previous_snapshot_id": str(uuid4()),
            "score_delta": 5,
        },
        created_at=datetime.now(UTC),
    )
    client = read_snapshot(snapshot, client_safe=True)
    action = client.phases[0].actions[0]
    assert "owner_user_id" not in action
    assert "action_key" not in action
    assert client.source_manifest == []
    assert client.reviewed_by_user_id is None
    assert "previous_snapshot_id" not in (client.material_change or {})


def test_spanish_snapshot_localizes_new_banking_metric_labels() -> None:
    snapshot = ApplicationCapitalReadinessSnapshot(
        id=uuid4(),
        profile_id=uuid4(),
        snapshot_version=1,
        policy_id=uuid4(),
        policy_key="qc_lending_margin_v1",
        policy_version=1,
        formula_version="score_v2",
        evidence_fingerprint="a" * 64,
        idempotency_key="spanish-banking-metrics",
        as_of=datetime.now(UTC),
        communication_locale="es",
        review_status="provisional",
        score=None,
        band="insufficient_evidence",
        evidence_coverage_pct=Decimal("0"),
        confidence_pct=Decimal("0"),
        pillars=[],
        metrics=[
            {
                "key": key,
                "label": key,
                "unit": "currency",
                "status": "unavailable",
                "source": {},
            }
            for key in (
                "average_daily_balance",
                "low_balance",
                "deposit_frequency",
                "monthly_debt_payments",
                "cash_runway_months",
            )
        ],
        strengths=[],
        blockers=[],
        phases=[],
        program_opportunities=[],
        source_manifest=[],
        created_at=datetime.now(UTC),
    )
    localized = read_snapshot(snapshot, client_safe=True)
    assert [metric.label for metric in localized.metrics] == [
        "Saldo diario promedio",
        "Saldo observado más bajo",
        "Frecuencia de depósitos",
        "Pagos mensuales de deuda",
        "Meses de reserva de efectivo",
    ]


def test_material_change_reports_score_band_coverage_and_metric_delta() -> None:
    previous = SimpleNamespace(
        id=uuid4(),
        snapshot_version=1,
        score=Decimal("60"),
        band="six_to_twelve_months",
        evidence_coverage_pct=Decimal("65"),
        metrics=[{"key": "net_margin_pct", "value": 2, "status": "acceptable"}],
    )
    comparison = material_change_comparison(
        previous,  # type: ignore[arg-type]
        score=Decimal("70"),
        band="three_to_six_months",
        evidence_coverage_pct=Decimal("80"),
        metrics=[{"key": "net_margin_pct", "value": 4, "status": "healthy"}],
    )
    assert comparison is not None
    assert comparison["score_delta"] == 10.0
    assert comparison["band_changed"] is True
    assert comparison["evidence_coverage_delta"] == 15.0
    assert comparison["changed_metrics"][0]["value_delta"] == 2.0
    assert comparison["is_material"] is True


def test_financial_period_requires_a_source_for_ai_extraction() -> None:
    with pytest.raises(ValueError, match="source_file_id"):
        FinancialPeriodCreate(
            entity_name="Example LLC",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            months_covered=1,
            source_kind="ai_extracted",
            revenue=Decimal("100"),
            idempotency_key="ai-period-2025-01",
        )


def test_monthly_window_ignores_nonfinancial_placeholder_objects() -> None:
    # Defensive contract: callers must pass typed financial periods, not loose
    # prompt-derived facts. A simple namespace that lacks review_status fails
    # immediately instead of being treated as zero-valued evidence.
    with pytest.raises(AttributeError):
        compatible_period_windows([SimpleNamespace(period_end=date.today())])  # type: ignore[list-item]
