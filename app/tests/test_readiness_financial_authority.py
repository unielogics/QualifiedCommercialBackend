"""Current source math and reviewed assumptions cannot masquerade as each other."""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.application_profile import ApplicationProfile, ApplicationRequirementState
from app.models.capital_readiness import (
    ApplicationAddBackVerification,
    ApplicationCapitalReadinessSnapshot,
    ApplicationFinancialPeriod,
)
from app.schemas.capital_readiness import CapitalReadinessReviewRequest
from app.services import capital_readiness as readiness
from app.services.financial_calculation_policy import financial_authority
from app.services.financial_cockpit import build_cockpit
from app.tests.test_numbers_first_readiness import computation as base_computation


def current_business_authority():
    analysis = {"file_id": "company-2025", "classification": "tax_return", "accepted": True,
                "key_facts": {"tax_form": "Form 1065", "tax_year": "Calendar year 2025",
                              "entity_name": "Example Capital Partners LLC", "accounting_method": "cash",
                              "ordinary_business_income_loss": -410155, "interest_expense": 163121,
                              "other_deductions_detail": {"amortization": 53333}}}
    body = {"debts": [{"id": "loan", "lender": "Lender", "balance": 748666.03,
                       "monthly_payment": 79879.55}]}
    cockpit = build_cockpit([analysis], [], business_entity="Example Capital Partners",
                            accepted_schedule={"body": body, "version_id": "published-v2"})
    return financial_authority(cockpit)


def test_negative_tax_based_coverage_does_not_replace_a_reviewed_assumption_or_score():
    authority = current_business_authority()
    original = deepcopy(authority)
    profile = SimpleNamespace(underwriting_current_dscr=Decimal("1.4"), underwriting_updated_at=datetime.now(UTC))
    metrics, score_status = readiness.business_dscr_metrics(profile, authority, {}, "en")
    current, assumption = metrics
    assert current["key"] == "current_dscr" and current["value"] == -0.2
    assert current["numerator"] == -193701 and current["denominator"] == 958554.6
    assert current["source"]["basis"] == "tax_return"
    assert current["source"]["provisional"] and current["source"]["informational"]
    assert assumption["key"] == "underwriting_dscr" and assumption["value"] == 1.4
    assert assumption["source"]["assumption"] and assumption["source"]["reviewed"]
    assert score_status == readiness.classify_dscr(Decimal("1.4"))
    assert profile.underwriting_current_dscr == Decimal("1.4") and authority == original


def test_provisional_current_coverage_never_creates_reviewed_score_evidence():
    metrics, status = readiness.business_dscr_metrics(
        SimpleNamespace(underwriting_current_dscr=None, underwriting_updated_at=None),
        current_business_authority(), {}, "en")
    assert len(metrics) == 1 and metrics[0]["value"] == -0.2
    assert status == "unavailable"
    assert readiness._pillar("debt_capital", Decimal("25"), [status])["score"] is None


def test_unknown_current_coverage_stays_unknown_with_an_existing_staff_assumption():
    metrics, _ = readiness.business_dscr_metrics(
        SimpleNamespace(underwriting_current_dscr=Decimal("1.4"), underwriting_updated_at=None),
        {"policy_version": "current", "signature": "a" * 64, "primary_dscr": None}, {}, "en")
    assert metrics[0]["value"] is None and metrics[0]["status"] == "unavailable"
    assert metrics[1]["value"] == 1.4 and metrics[1]["source"]["provisional"]


@pytest.fixture
def computation(monkeypatch):
    db, profile, period = base_computation.__wrapped__(monkeypatch)
    authority = current_business_authority()
    monkeypatch.setattr(readiness, "load_financial_authority", AsyncMock(return_value=authority))
    return db, profile, period, authority


@pytest.mark.asyncio
async def test_recompute_persists_current_business_coverage_without_promoting_tax_estimate_to_ebitda(computation):
    db, profile, _, authority = computation
    snapshot = await readiness.recompute(db, profile, idempotency_key="original-event", expected_snapshot_version=None)
    metrics = {metric["key"]: metric for metric in snapshot.metrics}
    assert metrics["current_dscr"]["value"] == -0.2
    assert metrics["underwriting_dscr"]["value"] == 1.4
    assert metrics["internal_adjusted_ebitda"]["value"] is None
    source = next(item for item in snapshot.source_manifest if item["kind"] == "financial_calculation_authority")
    assert source["signature"] == authority["signature"]
    assert readiness.snapshot_authority_matches(snapshot, authority, profile)
    debt_pillar = next(p for p in snapshot.pillars if p["key"] == "debt_capital")
    assert debt_pillar["status"] == readiness.classify_dscr(Decimal("1.4"))
    context = readiness.build_ai_context(snapshot)
    by_key = {metric["key"]: metric for metric in context["metrics"]}
    assert by_key["current_dscr"]["informational"] and by_key["current_dscr"]["basis"] == "tax_return"
    assert by_key["underwriting_dscr"]["assumption"]


def _receipt_database(db, profile, monkeypatch):
    snapshots = []

    def result(row=None, rows=()):
        return SimpleNamespace(scalar_one=lambda: row, scalar_one_or_none=lambda: row,
                               scalars=lambda: SimpleNamespace(all=lambda: list(rows)))

    async def execute(statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is ApplicationProfile:
            return result(profile)
        if entity is ApplicationCapitalReadinessSnapshot:
            key = next((value for name, value in statement.compile().params.items()
                        if name.startswith("idempotency_key")), None)
            return result(next((row for row in snapshots if row.idempotency_key == key), None))
        assert entity is ApplicationRequirementState
        return result(rows=[])

    def add(row):
        if row.id is None:
            row.id = uuid4()
        if isinstance(row, ApplicationCapitalReadinessSnapshot):
            snapshots.append(row)

    db.execute = AsyncMock(side_effect=execute)
    db.add.side_effect = add
    monkeypatch.setattr(readiness, "latest_snapshot", AsyncMock(side_effect=lambda *_: snapshots[-1] if snapshots else None))
    return snapshots


@pytest.mark.asyncio
async def test_replaying_an_old_event_after_authority_change_appends_a_new_version(computation, monkeypatch):
    db, profile, _, authority = computation
    snapshots = _receipt_database(db, profile, monkeypatch)
    first = await readiness.recompute(db, profile, idempotency_key="same-analysis-event", expected_snapshot_version=None)
    before = deepcopy(first.metrics)
    replay = await readiness.recompute(db, profile, idempotency_key="same-analysis-event", expected_snapshot_version=0)
    assert replay is first  # Exact lost-response retries remain idempotent.
    changed = {**authority, "signature": "b" * 64, "revision": 1,
               "primary_dscr": {**authority["primary_dscr"], "value": -0.1, "numerator": -95855.46}}
    monkeypatch.setattr(readiness, "load_financial_authority", AsyncMock(return_value=changed))
    second = await readiness.recompute(db, profile, idempotency_key="same-analysis-event", expected_snapshot_version=1)
    assert len(snapshots) == 2 and second.snapshot_version == 2
    assert first.idempotency_key != second.idempotency_key
    assert next(metric for metric in second.metrics if metric["key"] == "current_dscr")["value"] == -0.1
    assert first.metrics == before and profile.underwriting_current_dscr == Decimal("1.4")


@pytest.mark.asyncio
async def test_same_event_checks_full_readiness_fingerprint_not_only_cockpit_revision(computation, monkeypatch):
    db, profile, period, _ = computation
    snapshots = _receipt_database(db, profile, monkeypatch)
    first = await readiness.recompute(db, profile, idempotency_key="same-event", expected_snapshot_version=None)
    period.review_status = "confirmed"
    second = await readiness.recompute(db, profile, idempotency_key="same-event", expected_snapshot_version=1)
    assert len(snapshots) == 2 and first.evidence_fingerprint != second.evidence_fingerprint
    assert first.idempotency_key != second.idempotency_key


@pytest.mark.asyncio
async def test_changed_authority_preserves_concurrent_save_check(computation, monkeypatch):
    db, profile, _, authority = computation
    _receipt_database(db, profile, monkeypatch)
    await readiness.recompute(db, profile, idempotency_key="same-event", expected_snapshot_version=None)
    monkeypatch.setattr(readiness, "load_financial_authority", AsyncMock(return_value={**authority, "signature": "c" * 64}))
    with pytest.raises(HTTPException) as caught:
        await readiness.recompute(db, profile, idempotency_key="same-event", expected_snapshot_version=0)
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_current_snapshot_refreshes_derived_history_without_mutating_old_snapshot(computation, monkeypatch):
    db, profile, _, _ = computation
    _receipt_database(db, profile, monkeypatch)
    first = await readiness.recompute(db, profile, idempotency_key="old-event", expected_snapshot_version=None)
    first.formula_version = "old-policy"
    old_metrics = deepcopy(first.metrics)
    fresh = await readiness.current_snapshot(db, profile)
    assert fresh is not first and fresh.snapshot_version == first.snapshot_version + 1
    assert first.formula_version == "old-policy" and first.metrics == old_metrics


@pytest.mark.asyncio
async def test_failed_current_source_read_never_returns_a_cached_ratio(computation, monkeypatch):
    db, profile, _, _ = computation
    _receipt_database(db, profile, monkeypatch)
    first = await readiness.recompute(db, profile, idempotency_key="old-event", expected_snapshot_version=None)
    monkeypatch.setattr(readiness, "load_financial_authority", AsyncMock(side_effect=RuntimeError("source unavailable")))
    with pytest.raises(RuntimeError, match="source unavailable"):
        await readiness.current_snapshot(db, profile)
    assert first.metrics[0] is not None  # Historical evidence is preserved for history reads.


@pytest.mark.asyncio
async def test_current_read_keeps_a_reviewed_successor_instead_of_an_old_receipt(computation, monkeypatch):
    db, profile, _, _ = computation
    snapshots = _receipt_database(db, profile, monkeypatch)
    first = await readiness.current_snapshot(db, profile)
    assert first is None  # Existing no-snapshot behavior remains a 404 at the route.
    first = await readiness.recompute(db, profile, idempotency_key="current-financial-authority", expected_snapshot_version=None)
    reviewed = ApplicationCapitalReadinessSnapshot(
        id=uuid4(), snapshot_version=2, evidence_fingerprint=first.evidence_fingerprint,
        formula_version=first.formula_version, review_status="confirmed",
        source_manifest=deepcopy(first.source_manifest), idempotency_key="reviewed-successor")
    snapshots.append(reviewed)
    assert await readiness.current_snapshot(db, profile) is reviewed
    assert first.review_status == "provisional"


@pytest.mark.asyncio
async def test_review_cannot_confirm_a_snapshot_from_obsolete_financial_sources(computation, monkeypatch):
    db, profile, _, authority = computation
    _receipt_database(db, profile, monkeypatch)
    first = await readiness.recompute(db, profile, idempotency_key="event", expected_snapshot_version=None)
    monkeypatch.setattr(readiness, "load_financial_authority", AsyncMock(return_value={**authority, "signature": "d" * 64}))
    with pytest.raises(HTTPException) as caught:
        await readiness.review_snapshot(db, profile,
            CapitalReadinessReviewRequest(expected_snapshot_version=first.snapshot_version, status="confirmed", note="Reviewed"),
            SimpleNamespace(id=uuid4()))
    assert caught.value.status_code == 409
    assert caught.value.detail["action"] == "recalculate"
    assert first.review_status == "provisional"


@pytest.mark.parametrize("identity", [None, "", "Other Business LLC", "[Unidentified statement:1]"])
def test_unknown_or_unrelated_periods_never_become_current_business_income(identity):
    source = SimpleNamespace(entity_name=identity, review_status="submitted", accounting_basis="cash",
                             currency="USD", id=uuid4(), idempotency_key="period")
    assert readiness.compatible_period_windows([source], canonical_entities={"example capital partners"}) == ([], [])


def manual_period(**extra):
    values = {"id": uuid4(), "profile_id": uuid4(), "entity_name": "Example Capital Partners LLC",
              "period_start": date(2025, 1, 1), "period_end": date(2025, 6, 30), "months_covered": 6,
              "review_status": "confirmed", "source_kind": "stated", "source_file_id": None,
              "source_analysis_id": None, "accounting_basis": "cash", "currency": "USD",
              "ebitda": Decimal("12000"), "reconciliation_warnings": [], "created_at": datetime.now(UTC)}
    return ApplicationFinancialPeriod(**{**values, **extra})


def test_reviewed_manual_period_retains_its_explicit_figure_without_invented_annual_reconciliation():
    row = manual_period()
    result = readiness.reviewed_manual_ebitda([row], {readiness.normalize_entity_name("Example Capital Partners")})
    assert result["value"] == 12000 and result["months_covered"] == 6
    assert result["label"] == "Reviewed manually stated EBITDA"
    assert result["provisional"] and result["annualized"] is False
    assert result["source_period_ids"] == [str(row.id)]


@pytest.mark.parametrize("extra", [
    {"review_status": "submitted"}, {"source_kind": "ai_extracted"},
    {"source_file_id": uuid4()}, {"source_analysis_id": uuid4()},
    {"currency": "CAD"}, {"accounting_basis": "unknown"},
    {"entity_name": "Other Business LLC"}, {"ebitda": None},
    {"months_covered": 5}, {"reconciliation_warnings": [{"code": "unresolved_reconciliation"}]},
])
def test_manual_fallback_never_promotes_unverified_or_source_file_periods(extra):
    row = manual_period(**extra)
    assert readiness.reviewed_manual_ebitda([row], {readiness.normalize_entity_name("Example Capital Partners")}) is None


@pytest.mark.asyncio
async def test_manual_fallback_stays_informational_and_does_not_change_coverage(computation):
    db, profile, period, _ = computation
    period.source_kind = "derived"
    period.review_status = "confirmed"
    period.ebitda = Decimal("12000")
    snapshot = await readiness.recompute(db, profile, idempotency_key="reviewed-manual-period", expected_snapshot_version=None)
    metrics = {row["key"]: row for row in snapshot.metrics}
    assert metrics["internal_adjusted_ebitda"]["value"] == 12000
    assert metrics["internal_adjusted_ebitda"]["source"]["source_basis"] == "reviewed_manual_financial_periods"
    assert metrics["internal_adjusted_ebitda"]["source"]["provisional"]
    assert metrics["internal_adjusted_ebitda"]["source"]["informational"]
    assert metrics["current_dscr"]["value"] == -0.2


@pytest.mark.asyncio
async def test_property_only_policy_does_not_load_business_earnings(monkeypatch):
    from app.services import financial_cockpit

    reader = AsyncMock(side_effect=AssertionError("Property NOI must not become business EBITDA"))
    monkeypatch.setattr(financial_cockpit, "load_cockpit", reader)
    authority = await readiness.load_financial_authority(None, SimpleNamespace(vertical="real_estate"))
    assert authority["primary_dscr"] is None
    reader.assert_not_awaited()


@pytest.mark.asyncio
async def test_property_only_recompute_preserves_existing_underwriting_coverage_policy(computation):
    db, profile, _, _ = computation
    profile.vertical = "real_estate"
    snapshot = await readiness.recompute(db, profile, idempotency_key="property-policy", expected_snapshot_version=None)
    metrics = {row["key"]: row for row in snapshot.metrics}
    assert metrics["current_dscr"]["value"] == 1.4
    assert metrics["current_dscr"]["source"]["kind"] == "underwriting"
    assert "underwriting_dscr" not in metrics


def test_readiness_identity_uses_shared_suffix_rules_without_word_or_conjunction_collisions():
    assert readiness.normalize_entity_name("Example Capital Partners L.L.C.") == "example capital partners"
    assert readiness.normalize_entity_name("A & B LLC") == readiness.normalize_entity_name("A and B")
    assert readiness.normalize_entity_name("A and B LLC") != readiness.normalize_entity_name("A B LLC")
    assert readiness.normalize_entity_name("AB C LLC") != readiness.normalize_entity_name("A BC LLC")


def verified_addback(period_id=None, **extra):
    return ApplicationAddBackVerification(**{
        "id": uuid4(), "profile_id": uuid4(), "financial_period_id": period_id,
        "title": "Nonrecurring operating cost", "category": "one_time_expense",
        "status": "qc_verified", "amount": Decimal("1000"), **extra,
    })


def annual_period_context():
    period = manual_period(source_file_id=uuid4(), source_analysis_id=uuid4(), source_kind="ai_extracted")
    annual = {"ebitda_annual": 52000, "currency": "USD", "ebitda": {"months_covered": 6},
              "source_refs": [{"file_id": str(period.source_file_id), "analysis_id": str(period.source_analysis_id),
                               "accepted": True, "entity": period.entity_name, "currency": "USD", "basis": "cash",
                               "period_start": period.period_start.isoformat(), "period_end": period.period_end.isoformat()}]}
    return period, annual


def scoped_addbacks(rows, periods, annual=None, manual=None):
    return readiness.scoped_business_addbacks(rows, periods, annual_earnings=annual or {}, manual_earnings=manual,
                                             canonical_entities={"example capital partners"})


def test_six_month_operating_addback_annualizes_with_its_exact_ebitda_source_and_unbound_does_not():
    period, annual = annual_period_context()
    bound, unbound = verified_addback(period.id), verified_addback()
    result = scoped_addbacks([bound, unbound], [period], annual)
    assert result["applied_total"] == 2000
    assert result["applied"] == [{"id": str(bound.id), "financial_period_id": str(period.id),
                                  "source_amount": 1000, "annualization_factor": 2, "applied_amount": 2000}]
    assert result["excluded"] == [{"id": str(unbound.id), "financial_period_id": "",
                                   "reason": "not_bound_to_current_ebitda_period"}]


@pytest.mark.parametrize("category", ["interest_refinanced", "Interest expense", "Depreciation / amortization",
                                      "amortisation", "income_taxes", "D&A"])
def test_ebitda_components_are_never_added_back_twice(category):
    period, annual = annual_period_context()
    result = scoped_addbacks([verified_addback(period.id, category=category)], [period], annual)
    assert result["applied_total"] == 0 and not result["applied"]
    assert result["excluded"][0]["reason"] == "already_excluded_from_ebitda"


@pytest.mark.parametrize("extra", [
    {"source_file_id": uuid4()}, {"source_analysis_id": uuid4()}, {"entity_name": "Other Business"},
    {"currency": "CAD"}, {"accounting_basis": "accrual"}, {"period_start": date(2024, 1, 1)},
    {"months_covered": 12},
])
def test_other_sources_contexts_and_periods_cannot_adjust_current_annual_ebitda(extra):
    period, annual = annual_period_context()
    for key, value in extra.items():
        setattr(period, key, value)
    result = scoped_addbacks([verified_addback(period.id)], [period], annual)
    assert result["applied_total"] == 0
    assert result["excluded"][0]["reason"] == "not_bound_to_current_ebitda_period"


def test_reviewed_manual_six_month_addback_keeps_its_nonannual_period_amount():
    period = manual_period()
    manual = readiness.reviewed_manual_ebitda([period], {"example capital partners"})
    result = scoped_addbacks([verified_addback(period.id), verified_addback(uuid4())], [period], manual=manual)
    assert result["applied_total"] == 1000
    assert result["applied"][0]["annualization_factor"] == 1
    assert len(result["excluded"]) == 1 and manual["annualized"] is False


def test_monthly_contribution_within_selected_annual_source_is_not_multiplied_by_twelve():
    period, annual = annual_period_context()
    annual["ebitda"]["months_covered"] = 12
    annual["source_refs"][0]["period_end"] = "2025-12-31"
    period.months_covered = 1
    period.period_end = date(2025, 1, 31)
    result = scoped_addbacks([verified_addback(period.id)], [period], annual)
    assert result["applied_total"] == 1000 and result["applied"][0]["annualization_factor"] == 1


@pytest.mark.asyncio
async def test_adjusted_ebitda_records_raw_and_applied_amounts_without_promoting_or_leaking_ids(computation, monkeypatch):
    db, profile, period, authority = computation
    period.period_end, period.months_covered = date(2025, 6, 30), 6
    period.source_file_id, period.source_analysis_id = uuid4(), uuid4()
    _, annual = annual_period_context()
    annual["source_refs"] = [{"file_id": str(period.source_file_id), "analysis_id": str(period.source_analysis_id),
                               "accepted": True, "entity": period.entity_name, "currency": "USD", "basis": "accrual",
                               "period_start": period.period_start.isoformat(), "period_end": period.period_end.isoformat()}]
    monkeypatch.setattr(readiness, "load_financial_authority", AsyncMock(return_value={**authority, "validated_annual": annual}))
    original = readiness._calculation_inputs.return_value
    adjustments = [verified_addback(period.id), verified_addback(), verified_addback(period.id, category="interest_refinanced")]
    monkeypatch.setattr(readiness, "_calculation_inputs", AsyncMock(return_value=(original[0], original[1], [], adjustments, [])))
    snapshot = await readiness.recompute(db, profile, idempotency_key="period-addbacks", expected_snapshot_version=None)
    metric = next(row for row in snapshot.metrics if row["key"] == "internal_adjusted_ebitda")
    assert metric["value"] == 54000 and metric["source"]["reported_ebitda"] == 52000
    assert metric["source"]["qc_verified_addback_total"] == 3000
    assert metric["source"]["applied_addback_total"] == 2000
    assert len(metric["source"]["excluded_addbacks"]) == 2
    assert metric["source"]["informational"]
    snapshot.created_at = datetime.now(UTC)
    public = readiness.read_snapshot(snapshot, client_safe=True)
    public_metric = next(row for row in public.metrics if row.key == "internal_adjusted_ebitda")
    assert "applied_addbacks" not in public_metric.source and "excluded_addbacks" not in public_metric.source
    assert public_metric.source["applied_addback_total"] == 2000
    assert db.execute.call_args_list[0].args[0].get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
async def test_rebinding_or_reclassifying_addback_changes_receipt_fingerprint(computation, monkeypatch):
    db, profile, period, _ = computation
    original = readiness._calculation_inputs.return_value
    row = verified_addback()
    monkeypatch.setattr(readiness, "_calculation_inputs", AsyncMock(return_value=(original[0], original[1], [], [row], [])))
    snapshots = _receipt_database(db, profile, monkeypatch)
    first = await readiness.recompute(db, profile, idempotency_key="same-event", expected_snapshot_version=None)
    row.financial_period_id = period.id
    second = await readiness.recompute(db, profile, idempotency_key="same-event", expected_snapshot_version=1)
    row.category = "depreciation"
    third = await readiness.recompute(db, profile, idempotency_key="same-event", expected_snapshot_version=2)
    assert len(snapshots) == 3
    assert len({row.evidence_fingerprint for row in [first, second, third]}) == 3


@pytest.mark.asyncio
async def test_property_adjustments_keep_existing_policy(computation, monkeypatch):
    db, profile, period, _ = computation
    profile.vertical, period.ebitda = "real_estate", Decimal("12000")
    original = readiness._calculation_inputs.return_value
    monkeypatch.setattr(readiness, "_calculation_inputs", AsyncMock(return_value=(original[0], original[1], [], [verified_addback()], [])))
    snapshot = await readiness.recompute(db, profile, idempotency_key="property-addback", expected_snapshot_version=None)
    metric = next(row for row in snapshot.metrics if row["key"] == "internal_adjusted_ebitda")
    assert metric["value"] == 13000 and metric["source"]["applied_addback_total"] == 1000


@pytest.mark.asyncio
async def test_embedded_readiness_omits_failed_current_math_in_a_savepoint(monkeypatch):
    events = []

    @asynccontextmanager
    async def savepoint():
        events.append("entered")
        try:
            yield
        finally:
            events.append("exited")

    db = SimpleNamespace(begin_nested=savepoint)
    monkeypatch.setattr(readiness, "current_snapshot", AsyncMock(side_effect=RuntimeError("source unavailable")))
    assert await readiness.advisory_current_snapshot(db, SimpleNamespace(id=uuid4())) is None
    assert events == ["entered", "exited"]


def test_fingerprint_ignores_evidence_and_candidate_order_but_preserves_exact_numeric_observations(computation):
    _, profile, period, authority = computation
    other = manual_period()
    candidate = SimpleNamespace(program_key="a", playbook_version=1, recommendation_status="needs_information",
        reasons=["First reason", "Second reason"], rule_state="unknown",
        rule_results=[{"field": "annual_revenue", "observed": 1263637.63,
                       "source_refs": [{"file_id": "b"}, {"file_id": "a"}]}],
        fact_sources={"annual_revenue": {"source_refs": [{"file_id": "b"}, {"file_id": "a"}]}})
    second = deepcopy(candidate)
    second.program_key = "b"
    periods, facts, _, addbacks, actions = readiness._calculation_inputs.return_value
    policy = readiness.published_policy.return_value

    def calculate(rows, candidates):
        return readiness._fingerprint(profile, rows, facts, policy, addbacks, actions, candidates, [],
                                      {"trading dba"}, {}, financial_authority=authority)

    original = calculate([period, other], [candidate, second])
    candidate.reasons.reverse()
    candidate.rule_results[0]["source_refs"].reverse()
    candidate.fact_sources["annual_revenue"]["source_refs"].reverse()
    assert calculate([other, period], [second, candidate]) == original
    candidate.rule_results[0]["observed"] = 1706065.35
    assert calculate([other, period], [second, candidate]) != original
