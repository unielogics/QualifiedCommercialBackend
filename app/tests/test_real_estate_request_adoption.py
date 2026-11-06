from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.application_profile import ApplicationRequirementState
from app.models.bucket import BucketRequestedDocument
from app.services import application_programs, real_estate_forms, requirement_library

KIND = "real_estate_schedule"


class Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class MemoryDB:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        for row in self.rows:
            if getattr(row, "id", None) is None:
                row.id = uuid4()
            if getattr(row, "created_at", None) is None:
                row.created_at = datetime.now(UTC)

    async def get(self, model, row_id):
        return next((row for row in self.rows if isinstance(row, model) and row.id == row_id), None)

    async def execute(self, query):
        model = query.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if isinstance(row, model)]
        for attr in ("profile_id", "bucket_id", "requirement_key"):
            values = [value for key, value in query.compile().params.items() if key.startswith(f"{attr}_")]
            if values:
                rows = [row for row in rows if getattr(row, attr) == values[0]]
        return Result(rows)


def context():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), extra_requirements=[])
    actor = SimpleNamespace(id=uuid4(), name="Staff")
    return profile, actor


def slot_for(profile, *, key=None, status="requested"):
    return BucketRequestedDocument(id=uuid4(), bucket_id=profile.primary_bucket_id, name="Real estate schedule",
        category="Real estate", status=status, requirement_key=key, requirement_source={"kind": "intake_template"},
        requires_signature=False, created_at=datetime.now(UTC))


def state_for(profile, slot, *, key=KIND, status="requested", **metadata):
    return ApplicationRequirementState(id=uuid4(), profile_id=profile.id, requested_document_id=slot.id,
        requirement_key=key, label="Schedule of real estate owned", category="property_data", required_level="required",
        status=status, provenance={"requirement_kind": "form", "collection_config": {"form_kind": KIND}, **metadata})


def plan_for(profile):
    return SimpleNamespace(already=False, entry={"key": KIND, "requirement_id": str(uuid4()), "required_level": "required"})


async def materialize(db, profile):
    # Exercise the actual destination selection/adoption rather than simulate a
    # new slot in the mock: library labels differ from intake-template labels.
    requirement = SimpleNamespace(requirement_key=KIND, canonical_key=KIND, requirement_kind="form",
        collection_config={"form_kind": KIND, "document_type": KIND}, category="property_data",
        label="Schedule of real estate owned", required_level="required", visibility=["borrower"],
        playbook_id=uuid4(), objective_text="Complete the real estate schedule", ai_request_message_template=None)
    slot = await application_programs._requested_document(db, profile, requirement, [], [])
    previous = next((row for row in db.rows if isinstance(row, ApplicationRequirementState)
                     and row.profile_id == profile.id and row.requirement_key == KIND), None)
    if previous:
        previous.requested_document_id = slot.id
        previous.status = "requested"
        previous.provenance = {"requirement_kind": "form", "collection_config": {"form_kind": KIND}}
    else:
        db.add(state_for(profile, slot))
    await db.flush()


@pytest.mark.asyncio
async def test_plain_intake_upload_is_adopted_into_persisted_form_without_duplicate_destination():
    profile, actor = context()
    slot = slot_for(profile, key="legacy_real_estate_upload")
    original_id = slot.id
    db = MemoryDB([slot])
    with patch.object(real_estate_forms.financial_workspace, "lock_financial_profile", AsyncMock()) as lock, patch.object(
        requirement_library, "plan_add", AsyncMock(return_value=plan_for(profile))
    ) as pin, patch.object(application_programs, "get_program_readiness", AsyncMock(side_effect=materialize)) as refresh:
        first = await real_estate_forms.ensure_request(db, profile, actor=actor)
        second = await real_estate_forms.ensure_request(db, profile, actor=actor)
    assert first == (slot, False) and second == (slot, True)
    assert slot.id == original_id and slot.requirement_key == KIND
    assert slot.name == "Real estate schedule"
    assert len([row for row in db.rows if isinstance(row, BucketRequestedDocument)]) == 1
    assert len([row for row in db.rows if isinstance(row, ApplicationRequirementState)]) == 1
    assert [entry["key"] for entry in profile.extra_requirements] == [KIND]
    pin.assert_awaited_once()
    refresh.assert_awaited_once()
    assert lock.await_count == 2


@pytest.mark.parametrize("status", ["requested", "received_unverified", "verified", "stale"])
@pytest.mark.asyncio
async def test_active_batch_form_task_is_reused_including_completed_or_expired_evidence(status):
    profile, actor = context()
    slot = slot_for(profile, key="staff_request_batch_0", status="uploaded" if status == "verified" else "requested")
    slot.category = "Other"
    slot.requirement_source = {"kind": "staff_request", "collection_config": {"form_kind": KIND}}
    state = state_for(profile, slot, key=slot.requirement_key, status=status, source_kind="staff_request")
    state.evidence_file_id = uuid4()
    db = MemoryDB([slot, state])
    with patch.object(real_estate_forms.financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(
        requirement_library, "plan_add", AsyncMock()
    ) as pin, patch.object(application_programs, "get_program_readiness", AsyncMock()) as refresh:
        assert await real_estate_forms.ensure_request(db, profile, actor=actor) == (slot, True)
    pin.assert_not_awaited()
    refresh.assert_not_awaited()
    assert profile.extra_requirements == [] and state.status == status and state.evidence_file_id is not None


@pytest.mark.asyncio
async def test_older_plain_slot_cannot_hide_existing_active_batch_form_and_duplicate_ask():
    profile, actor = context()
    plain, batch = slot_for(profile), slot_for(profile, key="staff_request_batch_0")
    batch.requirement_source = {"collection_config": {"form_kind": KIND}}
    db = MemoryDB([plain, batch, state_for(profile, batch, key=batch.requirement_key)])
    with patch.object(real_estate_forms.financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(
        requirement_library, "plan_add", AsyncMock()
    ) as pin:
        assert await real_estate_forms.ensure_request(db, profile, actor=actor) == (batch, True)
    pin.assert_not_awaited()
    assert plain.requirement_key is None and profile.extra_requirements == []


@pytest.mark.parametrize("metadata", [{"cancelled_at": "2026-11-05"}, {"inactive_definition": True}])
@pytest.mark.asyncio
async def test_inactive_or_cancelled_form_definition_does_not_shadow_new_explicit_request(metadata):
    profile, actor = context()
    slot = slot_for(profile, key=KIND)
    state = state_for(profile, slot, **metadata)
    db = MemoryDB([slot, state])
    with patch.object(real_estate_forms.financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(
        requirement_library, "plan_add", AsyncMock(return_value=plan_for(profile))
    ) as pin, patch.object(application_programs, "get_program_readiness", AsyncMock(side_effect=materialize)):
        assert await real_estate_forms.ensure_request(db, profile, actor=actor) == (slot, False)
    pin.assert_awaited_once()
    assert not state.provenance.get("cancelled_at") and not state.provenance.get("inactive_definition")


@pytest.mark.asyncio
async def test_active_state_from_another_profile_cannot_skip_form_materialization():
    profile, actor = context()
    slot = slot_for(profile)
    state = state_for(SimpleNamespace(id=uuid4()), slot)
    db = MemoryDB([slot, state])
    with patch.object(real_estate_forms.financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(
        requirement_library, "plan_add", AsyncMock(return_value=plan_for(profile))
    ) as pin, patch.object(application_programs, "get_program_readiness", AsyncMock(side_effect=materialize)):
        assert await real_estate_forms.ensure_request(db, profile, actor=actor) == (slot, False)
    pin.assert_awaited_once()
    assert len([row for row in db.rows if isinstance(row, ApplicationRequirementState)]) == 2


@pytest.mark.asyncio
async def test_new_form_request_does_not_steal_an_existing_multiple_file_requirement_destination():
    profile, actor = context()
    existing = slot_for(profile, key="staff_request_multi_0")
    existing_state = state_for(profile, existing, key=existing.requirement_key,
        requirement_kind="document", collection_config={"document_type": KIND, "minimum_count": 2})
    db = MemoryDB([existing, existing_state])
    new_slot = slot_for(profile, key=KIND)

    async def refresh(db, profile):
        db.add(new_slot)
        db.add(state_for(profile, new_slot))

    with patch.object(real_estate_forms.financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(
        requirement_library, "plan_add", AsyncMock(return_value=plan_for(profile))
    ), patch.object(application_programs, "get_program_readiness", AsyncMock(side_effect=refresh)):
        assert await real_estate_forms.ensure_request(db, profile, actor=actor) == (new_slot, False)
    assert existing.requirement_key == "staff_request_multi_0"
    assert existing_state.provenance["collection_config"]["minimum_count"] == 2


@pytest.mark.asyncio
async def test_unmaterialized_plain_upload_does_not_claim_form_task_was_prepared():
    profile, actor = context()
    slot = slot_for(profile)
    db = MemoryDB([slot])
    with patch.object(real_estate_forms.financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(
        requirement_library, "plan_add", AsyncMock(return_value=plan_for(profile))
    ), patch.object(application_programs, "get_program_readiness", AsyncMock()):
        with pytest.raises(HTTPException) as error:
            await real_estate_forms.ensure_request(db, profile, actor=actor)
    assert error.value.status_code == 409
