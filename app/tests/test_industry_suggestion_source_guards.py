"""An early NAICS suggestion must stay current across a provider await."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.services import business_profile, document_associations
from app.services import industry_suggestion as service


def profile(**values):
    return SimpleNamespace(**{"id": uuid4(), "intake_id": uuid4(), "naics_code": None,
        "classified_by_user_id": None, "classification_provenance": {}, **values})


def entries():
    sector = SimpleNamespace(id=uuid4(), parent_id=None, level=2, code="62", label="Health Care", status="official")
    group = SimpleNamespace(id=uuid4(), parent_id=sector.id, level=3, code="621", label="Ambulatory Health Care", status="official")
    activity = SimpleNamespace(id=uuid4(), parent_id=group.id, level=6, code="621340", label="Physical Therapy Services", aliases=[], status="official")
    return activity, group, sector


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_source", ["denied", "stale", "foreign_name", "superseded", "old_analysis", "old_hash", "missing_source"])
async def test_wrong_business_or_obsolete_document_activity_cannot_displace_declared_answers(bad_source):
    current = profile()
    file = SimpleNamespace(id=uuid4(), content_hash="a" * 64, bucket_id=uuid4())
    analysis = SimpleNamespace(id=uuid4(), content_hash=file.content_hash, status="completed", analysis={"key_facts": {"business_name": "Example Therapy LLC"}})
    fact = SimpleNamespace(id=uuid4(), status="accepted", source_file_id=file.id, source_analysis_id=analysis.id, normalized_value="Unrelated trucking business", value={}, extraction_method="document_ai")
    intake = SimpleNamespace(business_name="Example Therapy LLC", intake_state=business_profile.state_entry(SimpleNamespace(business_description="We provide physical therapy services.", industry="professional_practice")))
    association = document_associations.record_for_source(current.id, intake.business_name, file, analysis)
    if bad_source in {"denied", "stale"}:
        association.update(status=bad_source, allows_financial_use=False)
    elif bad_source == "foreign_name":
        association = document_associations.record_for_source(current.id, "Another Business LLC", file, analysis)
    elif bad_source == "old_analysis":
        fact.source_analysis_id = uuid4()
    elif bad_source == "old_hash":
        analysis.content_hash = "b" * 64
    elif bad_source == "missing_source":
        fact.source_file_id = None
    inventory = [] if bad_source == "superseded" else [file]
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [fact]))), get=AsyncMock(return_value=intake))
    with patch("app.services.application_programs._evidence_inventory", AsyncMock(return_value=(inventory, {file.id: analysis}, set()))), patch.object(document_associations, "load_context", AsyncMock(return_value={str(file.id): association})):
        text = await service._activity_text(db, current)
    assert text == "Professional and medical practices — We provide physical therapy services."


@pytest.mark.asyncio
async def test_current_explicitly_associated_source_can_supply_activity_as_a_suggestion():
    current = profile()
    file = SimpleNamespace(id=uuid4(), content_hash="a" * 64, bucket_id=uuid4())
    analysis = SimpleNamespace(id=uuid4(), content_hash=file.content_hash, status="completed")
    fact = SimpleNamespace(id=uuid4(), status="pending", source_file_id=file.id, source_analysis_id=analysis.id, normalized_value="Physical therapy rehabilitation practice", value={})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [fact]))))
    with patch("app.services.application_programs._evidence_inventory", AsyncMock(return_value=([file], {file.id: analysis}, set()))), patch.object(document_associations, "load_context", AsyncMock(return_value={str(file.id): {"allows_financial_use": True, "status": "accepted", "association_stamp": "current"}})):
        text, stamp = await service._activity_snapshot(db, current)
    assert text == fact.normalized_value and stamp


async def run_suggestion(current, refreshed, *, changed_activity=False, provider_failure=False, confidence=0.95, catalog_changed=False, during_catalog_activity=False):
    activity, group, sector = entries()
    statements = []
    async def execute(statement):
        statements.append(statement)
        if len(statements) == 1:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [activity]))
        return SimpleNamespace(scalar_one_or_none=lambda: refreshed)
    async def get(*_args, **_kwargs):
        count = db.get.await_count
        if count == 1 and catalog_changed:
            activity.code = "999999"
        return (activity, group, sector)[count - 1]
    db = SimpleNamespace(execute=execute, get=AsyncMock(side_effect=get), flush=AsyncMock())
    snapshots = [("Physical therapy services", "source-one"), ("Trucking business" if changed_activity else "Physical therapy services", "source-two" if changed_activity else "source-one"), ("Changed later" if during_catalog_activity else "Physical therapy services", "source-three" if during_catalog_activity else "source-one")]
    with patch.object(service, "_activity_snapshot", AsyncMock(side_effect=snapshots)), patch("app.services.ai.bedrock_client.get_client", return_value=object()), patch("app.services.ai.bedrock_client.model_light", return_value="test-model"), patch("app.services.ai.usage.tracked_messages_create", AsyncMock(side_effect=RuntimeError("provider down") if provider_failure else None, return_value=object())), patch("app.services.bucket_ai._text_from_response", return_value=json.dumps({"code": "621340", "confidence": confidence, "reason": "Therapy services"})):
        result = await service.suggest_for_profile(db, current)
    return result, statements, db


@pytest.mark.asyncio
async def test_staff_classification_saved_during_ai_await_wins_including_its_reason():
    before = profile()
    reviewed = profile(id=before.id, naics_code="722511", classified_by_user_id=uuid4(), classification_provenance={"reason": "Staff checked the actual restaurant operations", "status": "confirmed"})
    original = deepcopy(vars(reviewed))
    result, statements, db = await run_suggestion(before, reviewed)
    assert result is None and vars(reviewed) == original
    assert "FOR UPDATE" in str(statements[-1].compile(dialect=postgresql.dialect()))
    assert statements[-1].get_execution_options()["populate_existing"] is True
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_source_or_owner_context_discards_delayed_ai_result():
    before = profile()
    original = deepcopy(vars(before))
    result, _, db = await run_suggestion(before, before, changed_activity=True)
    assert result is None and vars(before) == original
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_current_context_applies_catalog_path_as_suggested_only():
    before = profile()
    result, _, _ = await run_suggestion(before, before)
    assert result["code"] == "621340"
    assert before.naics_code == "621340" and before.classified_by_user_id is None
    assert before.classification_provenance["status"] == "suggested"
    assert before.classification_provenance["source"] == service.SUGGESTION_SOURCE


@pytest.mark.asyncio
async def test_provider_failure_keeps_profile_and_creation_usable_without_guessing_code():
    before = profile()
    result, _, _ = await run_suggestion(before, before, provider_failure=True)
    assert result is None and before.naics_code is None
    assert before.classification_provenance["suggestion_outcome"] == "provider_error"


@pytest.mark.asyncio
async def test_provider_failure_does_not_overwrite_concurrently_reviewed_provenance():
    before = profile()
    reviewed = profile(id=before.id, naics_code="722511", classified_by_user_id=uuid4(), classification_provenance={"reason": "Staff classification", "status": "confirmed"})
    original = deepcopy(vars(reviewed))
    result, _, _ = await run_suggestion(before, reviewed, provider_failure=True)
    assert result is None and vars(reviewed) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -1, 1.1, True])
async def test_invalid_model_confidence_cannot_apply_a_classification(confidence):
    current = profile()
    result, _, db = await run_suggestion(current, current, confidence=confidence)
    assert result is None and current.naics_code is None
    assert current.classification_provenance["suggestion_outcome"] == "low_confidence"
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_definition_changed_during_provider_wait_is_discarded():
    current = profile()
    original = deepcopy(vars(current))
    result, _, _ = await run_suggestion(current, current, catalog_changed=True)
    assert result is None and vars(current) == original


@pytest.mark.asyncio
async def test_activity_changed_during_final_catalog_reads_is_discarded():
    current = profile()
    original = deepcopy(vars(current))
    result, _, _ = await run_suggestion(current, current, during_catalog_activity=True)
    assert result is None and vars(current) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_source", [True, False])
async def test_early_outcomes_do_not_overwrite_a_staff_classification_saved_during_queries(empty_source):
    current = profile()
    reviewed = profile(id=current.id, naics_code="722511", classified_by_user_id=uuid4(), classification_provenance={"reason": "Confirmed by staff"})
    original = deepcopy(vars(reviewed))
    empty = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    locked = SimpleNamespace(scalar_one_or_none=lambda: reviewed)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[locked] if empty_source else [empty, locked]))
    with patch.object(service, "_activity_snapshot", AsyncMock(return_value=(None if empty_source else "Unique activity", "source"))), patch("app.services.ai.bedrock_client.get_client") as provider:
        assert await service.suggest_for_profile(db, current) is None
    assert vars(reviewed) == original
    provider.assert_not_called()
