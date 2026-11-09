"""Business association changes selection, never immutable source facts."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.enums import Role
from app.models.capital_readiness import ApplicationFinancialPeriod
from app.services import capital_readiness as readiness
from app.services import document_associations


def period(*, entity="J N H Auto LLC", profile_id=None, source_id=None, analysis_id=None):
    return ApplicationFinancialPeriod(
        id=uuid4(), profile_id=profile_id or uuid4(), entity_name=entity,
        accounting_basis="accrual", currency="USD", period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31), months_covered=12, source_kind="ai_extracted",
        review_status="submitted", cogs_applicability="applicable", revenue=Decimal("100000"),
        cogs=Decimal("80000"), gross_profit=Decimal("20000"), net_income=Decimal("10000"),
        confidence=Decimal(".85"), content_hash="a" * 64, idempotency_key=str(uuid4()),
        reconciliation_warnings=[], created_at=datetime.now(UTC),
        source_file_id=source_id or uuid4(), source_analysis_id=analysis_id or uuid4(),
    )


def sources(row, *, status="accepted", **changes):
    file = SimpleNamespace(id=row.source_file_id, content_hash=row.content_hash)
    analysis = SimpleNamespace(id=row.source_analysis_id, content_hash=row.content_hash)
    record = {
        "profile_id": str(row.profile_id), "file_id": str(row.source_file_id),
        "content_hash": row.content_hash, "analysis_id": str(row.source_analysis_id),
        "expected_entity": "JNH Auto LLC", "observed_entity": row.entity_name,
        "status": status, **changes,
    }
    return {file.id: file}, {file.id: analysis}, {str(file.id): record}


def filter_period(row, *, status="accepted", **changes):
    files, analyses, records = sources(row, status=status, **changes)
    selected = readiness.filter_current_financial_periods(
        [row], files=files, newest_analyses=analyses, document_associations=records,
    )
    return selected, records


def fact(row=None, **changes):
    return SimpleNamespace(
        id=uuid4(), profile_id=row.profile_id if row else uuid4(),
        source_file_id=row.source_file_id if row else None,
        source_analysis_id=row.source_analysis_id if row else None,
        field_key="legal_entity_name", status="accepted", value={"value": "J N H Auto LLC"},
        **changes,
    )


def test_explicit_confirmation_selects_only_that_exact_source_without_rewriting_entity_or_verification():
    confirmed = period()
    unrelated_same_name = period(profile_id=confirmed.profile_id)
    files, analyses, records = sources(confirmed)
    other_files, other_analyses, other_records = sources(unrelated_same_name, status="conflict")
    selected = readiness.filter_current_financial_periods(
        [confirmed, unrelated_same_name], files={**files, **other_files},
        newest_analyses={**analyses, **other_analyses}, document_associations={**records, **other_records},
    )
    current, _prior = readiness.compatible_period_windows(
        selected, canonical_entities={readiness.normalize_entity_name("JNH Auto LLC")},
    )
    assert current == [confirmed]
    assert confirmed.entity_name == unrelated_same_name.entity_name == "J N H Auto LLC"
    assert confirmed.review_status == "submitted" and confirmed.confidence == Decimal(".85")
    assert confirmed.revenue == Decimal("100000") and confirmed.content_hash == "a" * 64


def test_real_source_bound_review_receipt_selects_the_printed_period_without_accepting_its_figures():
    row = period()
    file = SimpleNamespace(
        id=row.source_file_id, bucket_id=uuid4(), content_hash=row.content_hash,
        document_family_id=uuid4(), document_version_number=1, document_version_status="current",
    )
    analysis = SimpleNamespace(
        id=row.source_analysis_id, content_hash=row.content_hash, analysis_version=1,
        classification="current_p_and_l", status="completed",
        analysis={"key_facts": {"business_name": row.entity_name}},
    )
    record = document_associations.record_for_source(row.profile_id, "JNH Auto LLC", file, analysis)
    state, _changed = document_associations.change(
        {}, {str(file.id): record}, file_id=file.id, action="accept",
        reason="Inspected this exact source's business association", expected_revision=0,
        source_stamp=record["source_stamp"], idempotency_key="readiness-source-association",
        actor=SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN, name="Reviewer"),
    )
    reviewed = document_associations.record_for_source(row.profile_id, "JNH Auto LLC", file, analysis, state)
    assert reviewed["status"] == "accepted" and not reviewed["document_accepted"]
    selected = readiness.filter_current_financial_periods(
        [row], files={file.id: file}, newest_analyses={file.id: analysis},
        document_associations={str(file.id): reviewed},
    )
    assert readiness.compatible_period_windows(
        selected, canonical_entities={readiness.normalize_entity_name("JNH Auto LLC")},
    )[0] == [row]
    assert row.review_status == "submitted" and row.entity_name == "J N H Auto LLC"


@pytest.mark.parametrize("status", ["denied", "stale"])
def test_denial_and_stale_review_exclude_even_an_exact_matching_name_and_accepted_alias_fact(status):
    row = period(entity="JNH Auto LLC")
    files, analyses, records = sources(row, status=status)
    manual = fact()
    linked = fact(row)
    assert readiness.filter_current_financial_periods(
        [row], files=files, newest_analyses=analyses, document_associations=records,
    ) == []
    assert readiness.filter_current_associated_facts(
        [linked, manual], files=files, newest_analyses=analyses, document_associations=records,
    ) == [manual]
    assert linked.status == "accepted" and linked.value == {"value": "J N H Auto LLC"}


@pytest.mark.parametrize("change", [
    {"content_hash": "b" * 64}, {"analysis_id": str(uuid4())},
    {"file_id": str(uuid4())}, {"profile_id": str(uuid4())},
])
def test_confirmation_from_other_bytes_analysis_document_or_profile_cannot_select_period(change):
    row = period()
    selected, _records = filter_period(row, **change)
    assert selected == [] and row._document_association is None


def test_reset_clears_the_transient_confirmation_and_restores_strict_name_matching():
    row = period()
    assert filter_period(row)[0] == [row]
    assert readiness.period_compatibility_key(row)[0] == readiness.normalize_entity_name("JNH Auto LLC")
    assert filter_period(row, status="conflict")[0] == [row]
    assert readiness.compatible_period_windows(
        [row], canonical_entities={readiness.normalize_entity_name("JNH Auto LLC")},
    ) == ([], [])
    assert row.entity_name == "J N H Auto LLC"


def test_confirmation_does_not_cover_another_entity_nested_in_the_same_file():
    row = period(entity="Different Subsidiary LLC")
    assert filter_period(row, observed_entity="J N H Auto LLC")[0] == [row]
    assert readiness.compatible_period_windows(
        [row], canonical_entities={readiness.normalize_entity_name("JNH Auto LLC")},
    ) == ([], [])


@pytest.mark.parametrize("field,value", [("accounting_basis", "unknown"), ("currency", "XXX")])
def test_confirmed_identity_does_not_waive_basis_or_currency_isolation(field, value):
    row = period()
    setattr(row, field, value)
    assert filter_period(row)[0] == [row]
    assert readiness.period_compatibility_key(row)[3] == str(row.id)


def test_only_explicit_unknown_primary_identity_can_resolve_an_unidentified_source_period():
    row = period(entity="[Unidentified statement: exact-source]")
    assert filter_period(row, observed_entity=None)[0] == [row]
    assert readiness.compatible_period_windows(
        [row], canonical_entities={readiness.normalize_entity_name("JNH Auto LLC")},
    )[0] == [row]
    assert row.entity_name == "[Unidentified statement: exact-source]"


def test_sourced_facts_require_the_current_exact_analysis_while_manual_facts_stay_independent():
    row = period()
    files, analyses, records = sources(row)
    current = fact(row)
    stale = fact(row)
    stale.source_analysis_id = uuid4()
    unbound = fact(row)
    unbound.source_analysis_id = None
    manual = fact()
    assert readiness.filter_current_associated_facts(
        [current, stale, unbound, manual], files=files, newest_analyses=analyses,
        document_associations=records,
    ) == [current, manual]


@pytest.mark.asyncio
async def test_calculation_inputs_filter_denied_periods_and_name_facts_before_aliases_and_citations(monkeypatch):
    denied = period(entity="JNH Auto LLC")
    manual_fact = fact()
    source_fact = fact(denied)
    files, analyses, records = sources(denied, status="denied")
    results = iter([
        [denied], [source_fact, manual_fact], [],
    ])

    async def execute(_statement):
        rows = next(results)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    db = SimpleNamespace(execute=execute)
    profile = SimpleNamespace(id=denied.profile_id)
    monkeypatch.setattr(readiness, "_current_evidence_sources", AsyncMock(return_value=(files, analyses)))
    monkeypatch.setattr(document_associations, "load_context", AsyncMock(return_value=records))
    monkeypatch.setattr(readiness, "attach_financial_period_citations", Mock())
    monkeypatch.setattr(readiness, "list_addbacks", AsyncMock(return_value=[]))
    monkeypatch.setattr(readiness, "list_actions", AsyncMock(return_value=[]))
    periods, facts, _programs, _addbacks, _actions = await readiness._calculation_inputs(db, profile)
    assert periods == [] and facts == [manual_fact]
    document_associations.load_context.assert_awaited_once_with(db, profile, files, analyses)
    readiness.attach_financial_period_citations.assert_called_once_with([], files=files, newest_analyses=analyses)
