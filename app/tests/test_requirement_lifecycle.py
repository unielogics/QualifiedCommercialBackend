from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationProfile,
    ApplicationRequirementEvidence,
    ApplicationRequirementState,
)
from app.models.bucket import BucketRequestedDocument
from app.models.funding_program import ApplicationRequirementEvidenceDecision
from app.services import application_programs, collection_tasks, file_requests
from app.services import requirement_lifecycle as lifecycle
from app.tests.test_file_requests import MemoryDB


def owner():
    return SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), extra_requirements=[], intake_id=None,
                           missing_item_email_requirement_key="refinance_debt_schedule", missing_item_email_attempts=2,
                           missing_item_email_next_send_at=datetime.now(UTC) + timedelta(days=1))


def state(profile, key="business_debt_schedule", **values):
    return ApplicationRequirementState(id=uuid4(), profile_id=profile.id, requirement_key=key,
        label="Business debt schedule", category="financials", required_level="required", status="requested",
        source_program_keys=[], source_policy_keys=[], provenance={}, **values)


def task(profile, key, **values):
    return ApplicationCollectionTask(id=uuid4(), profile_id=profile.id,
        canonical_identity=f"document:{key}:business:current", requirement_keys=[key], requirement_state_ids=[],
        action_type="upload_document", label=key, status="requested", config={}, attempts=2,
        cadence_hours=24, max_attempts=3, **values)


def test_known_aliases_share_only_the_same_explicit_context():
    assert lifecycle.debt_identity("refinance_debt_schedule") == "business_debt_schedule"
    assert lifecycle.debt_identity("business_debt_schedule", {"subject": " BUSINESS ", "period": "Current"}) == "business_debt_schedule"
    variants = [{"subject": "Other LLC"}, {"person_id": str(uuid4())}, {"account_id": str(uuid4())},
                {"period": "2025"}, {"as_of_date": "2026-01-01"}, {"quantity": 2}, {"unit": "months", "quantity": 2}]
    identities = [lifecycle.debt_identity("refinance_debt_schedule", config) for config in variants]
    assert len(set(identities)) == len(variants)
    assert "business_debt_schedule" not in identities
    assert lifecycle.debt_identity("custom_debt_looking_label") is None
    assert collection_tasks.canonical_identity("refinance_debt_schedule", "document", {}) == collection_tasks.canonical_identity("business_debt_schedule", "form", {})


def test_definition_normalization_preserves_source_and_reporting_context():
    original = SimpleNamespace(requirement_key="refinance_debt_schedule", canonical_key=None,
                               collection_config={"subject": "Other LLC"}, label="Original")
    before = deepcopy(vars(original))
    normalized = lifecycle.normalize_definition(original)
    assert normalized.requirement_key != "business_debt_schedule"
    assert normalized.original_requirement_key == "refinance_debt_schedule"
    assert vars(original) == before


@pytest.mark.asyncio
async def test_reconciliation_twice_preserves_files_requests_and_reminder_limits():
    db, profile = MemoryDB(), owner()
    first, duplicate = state(profile), state(profile, "refinance_debt_schedule")
    slots = [BucketRequestedDocument(id=uuid4(), bucket_id=profile.primary_bucket_id, name="Schedule", status="uploaded") for _ in range(2)]
    first.requested_document_id, duplicate.requested_document_id = [slot.id for slot in slots]
    request_id = str(uuid4())
    duplicate.provenance = {"source_kind": "staff_request", "file_request_id": request_id, "request_batches": {request_id: {"item": {"name": "Schedule"}, "fingerprint": "original"}}}
    links = [ApplicationRequirementEvidence(id=uuid4(), requirement_state_id=row.id, file_id=uuid4(), source="automatic") for row in (first, duplicate)]
    future = datetime.now(UTC) + timedelta(days=2)
    canonical_task, duplicate_task = task(profile, first.requirement_key, next_attempt_at=future), task(profile, duplicate.requirement_key)
    canonical_task.attempts = 1
    db.rows = [first, duplicate, *slots, *links, canonical_task, duplicate_task]
    for _ in range(2):
        await lifecycle.reconcile(db, profile, [first, duplicate])
        active = await collection_tasks.sync_collection_tasks(db, profile, [first])
    assert len(active) == 1 and active[0].attempts == 2 and active[0].next_attempt_at == future
    assert duplicate_task.status == "cancelled" and canonical_task.status == "requested"
    assert links[1].requirement_state_id == first.id and len({link.file_id for link in links}) == 2
    assert slots[1].status == "closed" and slots[1].requirement_source["redirect_requested_document_id"] == str(slots[0].id)
    assert duplicate.provenance["redirect_requirement_key"] == first.requirement_key
    assert request_id in first.provenance["request_batches"]
    assert profile.missing_item_email_requirement_key == first.requirement_key and profile.missing_item_email_attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("last_decision", ["accepted", "rejected"])
async def test_duplicate_file_keeps_latest_staff_decision_chain_without_rewriting_history(last_decision):
    db, profile, file_id = MemoryDB(), owner(), uuid4()
    first, duplicate = state(profile), state(profile, "refinance_debt_schedule")
    at = datetime.now(UTC)
    links = [ApplicationRequirementEvidence(id=uuid4(), requirement_state_id=row.id, file_id=file_id, source="operator", linked_at=at) for row in (first, duplicate)]
    decisions = [ApplicationRequirementEvidenceDecision(id=uuid4(), requirement_evidence_id=links[0].id,
        actor_kind="staff", decision="accepted", created_at=at - timedelta(days=1)),
        ApplicationRequirementEvidenceDecision(id=uuid4(), requirement_evidence_id=links[1].id,
        actor_kind="staff", decision=last_decision, created_at=at)]
    before = [(row.id, row.decision, row.requirement_evidence_id) for row in decisions]
    db.rows = [first, duplicate, *links, *decisions]
    await lifecycle.reconcile(db, profile, [first, duplicate])
    assert links[0].removed_at is not None and links[1].requirement_state_id == first.id
    assert [(row.id, row.decision, row.requirement_evidence_id) for row in decisions] == before


@pytest.mark.asyncio
async def test_removed_staff_request_survives_repeated_materialization_and_restores_its_schedule():
    db, profile, actor = MemoryDB(), owner(), SimpleNamespace(id=uuid4())
    item = state(profile, "custom_financial_request")
    item.provenance = {"source_kind": "staff_request", "external_definition": {}, "staff_added": {"added_by": str(actor.id)}}
    slot = BucketRequestedDocument(id=uuid4(), bucket_id=profile.primary_bucket_id, status="requested")
    item.requested_document_id = slot.id
    future = datetime.now(UTC) + timedelta(days=2)
    pending = task(profile, item.requirement_key, next_attempt_at=future)
    db.rows = [item, slot, pending]
    with patch.object(application_programs, "get_program_readiness", AsyncMock()):
        await lifecycle.change(db, profile, item.requirement_key, action="remove", reason="Collected elsewhere", actor=actor)
        await lifecycle.change(db, profile, item.requirement_key, action="remove", reason="Repeated request", actor=actor)
    assert item.status == "not_applicable" and pending.status == "cancelled" and pending.next_attempt_at is None
    with (patch.object(application_programs, "_evidence_inventory", AsyncMock(return_value=([], {}, set()))),
          patch.object(application_programs, "_accepted_entity_aliases", AsyncMock(return_value=set())),
          patch.object(collection_tasks, "latest_responses", AsyncMock(return_value={}))):
        for _ in range(2):
            active, _ = await application_programs._materialize_requirements(db, profile, [], {}, [], {})
            assert active == []
    with patch.object(application_programs, "get_program_readiness", AsyncMock()):
        await lifecycle.change(db, profile, item.requirement_key, action="restore", reason="", actor=actor)
    assert pending.status == "requested" and pending.attempts == 2 and pending.next_attempt_at == future
    assert slot.status == "requested" and not lifecycle.is_removed(item)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status,cancelled", [("verified", False), ("waived", False), ("not_applicable", True)])
async def test_restoring_removed_history_never_reopens_completed_or_cancelled_collection(prior_status, cancelled):
    db, profile, actor = MemoryDB(), owner(), SimpleNamespace(id=uuid4())
    item = state(profile, "custom_financial_request")
    item.status = prior_status
    item.provenance = {"source_kind": "staff_request", **({"cancelled_at": "2026-01-01T00:00:00+00:00"} if cancelled else {})}
    pending = task(profile, item.requirement_key)
    pending.status = "cancelled" if cancelled else "completed"
    db.rows = [item, pending]
    with patch.object(application_programs, "get_program_readiness", AsyncMock()):
        await lifecycle.change(db, profile, item.requirement_key, action="remove", reason="Remove from active list", actor=actor)
        await lifecycle.change(db, profile, item.requirement_key, action="restore", reason="", actor=actor)
    assert item.status == prior_status and pending.status == ("cancelled" if cancelled else "completed")
    assert pending.next_attempt_at is None
    assert bool(item.provenance.get("cancelled_at")) == cancelled


@pytest.mark.asyncio
async def test_removing_canonical_alias_suppresses_collection_without_erasing_policy_or_evidence():
    db, profile, actor = MemoryDB(), owner(), SimpleNamespace(id=uuid4())
    canonical = state(profile)
    canonical.source_policy_keys = ["business_evidence", "staff_added"]
    canonical.provenance = {"staff_added": {}, "requirement_aliases": ["refinance_debt_schedule"]}
    duplicate = state(profile, "refinance_debt_schedule")
    duplicate.provenance = {"redirect_requirement_key": canonical.requirement_key}
    pending = task(profile, canonical.requirement_key)
    evidence = ApplicationRequirementEvidence(id=uuid4(), requirement_state_id=canonical.id, file_id=uuid4(), source="operator")
    db.rows = [canonical, duplicate, pending, evidence]
    with patch.object(application_programs, "get_program_readiness", AsyncMock()):
        await lifecycle.change(db, profile, duplicate.requirement_key, action="remove", reason="Not needed twice", actor=actor)
    assert pending.status == "cancelled" and canonical.status == "not_applicable"
    assert canonical.source_policy_keys == ["business_evidence", "staff_added"]
    assert canonical.provenance["staff_removal"]["collection_suppression"] is True
    assert "No published program condition" in canonical.provenance["staff_removal"]["qualification_effect"]
    assert evidence.requirement_state_id == canonical.id and evidence.removed_at is None
    assert canonical.verified_at is None and canonical.verified_by_user_id is None
    assert len(db.rows) == 4


@pytest.mark.parametrize("sources", [
    {"source_program_keys": ["selected_program"]},
    {"source_policy_keys": ["business_evidence"]},
    {"provenance": {"source_kind": "lender"}},
])
@pytest.mark.asyncio
async def test_explicit_collection_removal_survives_derived_refresh_and_restores_original_sources(sources):
    db, profile, actor = MemoryDB(), owner(), SimpleNamespace(id=uuid4())
    item = state(profile, "business_tax_return")
    for key, value in sources.items():
        setattr(item, key, value)
    before_sources = (list(item.source_program_keys), list(item.source_policy_keys))
    slot = BucketRequestedDocument(id=uuid4(), bucket_id=profile.primary_bucket_id, status="uploaded")
    item.requested_document_id = slot.id
    pending = task(profile, item.requirement_key, next_attempt_at=datetime.now(UTC) + timedelta(days=2))
    profile.missing_item_email_requirement_key = item.requirement_key
    next_email = profile.missing_item_email_next_send_at
    db.rows = [item, slot, pending]
    assert lifecycle.lifecycle_fields(item)["can_remove"] is True
    with patch.object(application_programs, "get_program_readiness", AsyncMock()):
        await lifecycle.change(db, profile, item.requirement_key, action="remove", reason="Already collected in the linked source", actor=actor)
        original_receipt = deepcopy(item.provenance["staff_removal"])
        for _ in range(2):
            lifecycle.reactivate_for_derived(item)
            await lifecycle.change(db, profile, item.requirement_key, action="remove", reason="Retry the same removal", actor=actor)
        assert item.provenance["staff_removal"] == original_receipt
        assert lifecycle.is_removed(item) and pending.status == "cancelled" and pending.next_attempt_at is None
        assert slot.status == "not_applicable" and slot.closed_at is not None
        assert profile.missing_item_email_next_send_at is None
        await lifecycle.change(db, profile, item.requirement_key, action="restore", reason="", actor=actor)
    assert not lifecycle.is_removed(item) and item.status == "requested"
    assert (item.source_program_keys, item.source_policy_keys) == before_sources
    assert slot.status == "uploaded" and slot.closed_at is None and slot.closed_reason is None
    assert pending.status == "requested" and pending.attempts == 2
    assert profile.missing_item_email_next_send_at == next_email
    assert len(item.provenance["staff_removal_history"]) == 1


@pytest.mark.asyncio
async def test_removing_one_shared_collection_item_preserves_other_open_scope():
    db, profile, actor = MemoryDB(), owner(), SimpleNamespace(id=uuid4())
    first, second = state(profile, "tax_business_a"), state(profile, "tax_business_b")
    pending = task(profile, first.requirement_key, next_attempt_at=datetime.now(UTC) + timedelta(days=1))
    pending.requirement_keys = [first.requirement_key, second.requirement_key]
    future = pending.next_attempt_at
    db.rows = [first, second, pending]
    with patch.object(application_programs, "get_program_readiness", AsyncMock()):
        await lifecycle.change(db, profile, first.requirement_key, action="remove", reason="No longer collect this business scope", actor=actor)
    assert pending.status == "requested" and pending.next_attempt_at == future
    assert second.status == "requested" and not lifecycle.is_removed(second)


@pytest.mark.asyncio
async def test_removing_last_unfinished_member_stops_followup_for_completed_shared_scope():
    db, profile, actor = MemoryDB(), owner(), SimpleNamespace(id=uuid4())
    first, second = state(profile, "tax_business_a"), state(profile, "tax_business_b")
    second.status = "verified"
    pending = task(profile, first.requirement_key, next_attempt_at=datetime.now(UTC) + timedelta(days=1))
    pending.requirement_keys = [first.requirement_key, second.requirement_key]
    db.rows = [first, second, pending]
    with patch.object(application_programs, "get_program_readiness", AsyncMock()):
        await lifecycle.change(db, profile, first.requirement_key, action="remove", reason="No longer collect this business scope", actor=actor)
    assert pending.status == "completed" and pending.next_attempt_at is None
    assert second.status == "verified"


@pytest.mark.asyncio
async def test_removal_reloads_profile_after_lock_and_preserves_another_staff_request():
    db, profile_id, actor = MemoryDB(), uuid4(), SimpleNamespace(id=uuid4())
    stale = ApplicationProfile(id=profile_id, primary_bucket_id=uuid4(), extra_requirements=[])
    fresh = ApplicationProfile(id=profile_id, primary_bucket_id=stale.primary_bucket_id,
        extra_requirements=[{"key": "custom_request", "label": "Custom request", "source": "custom"},
                            {"key": "another_staff_request", "label": "A concurrent staff request", "source": "custom"}])
    item = state(fresh, "custom_request")
    item.provenance = {"staff_added": {"added_by": str(actor.id)}}
    db.rows = [fresh, item]

    async def current_readiness(_db, value):
        assert value is fresh

    with patch.object(application_programs, "get_program_readiness", AsyncMock(side_effect=current_readiness)):
        await lifecycle.change(db, stale, item.requirement_key, action="remove", reason="Current request no longer needed", actor=actor)
    assert [entry["key"] for entry in fresh.extra_requirements] == ["another_staff_request"]


@pytest.mark.asyncio
async def test_cancelling_staff_batch_keeps_shared_derived_requirement_and_task_active():
    db, profile, actor, request_id = MemoryDB(), owner(), SimpleNamespace(id=uuid4()), uuid4()
    canonical = state(profile)
    canonical.source_policy_keys = ["business_evidence"]
    canonical.provenance = {"source_kind": "staff_request", "file_request_id": str(request_id),
        "request_batches": {str(request_id): {"item": {"name": "Schedule"}, "cancelled_at": None}}}
    pending = task(profile, canonical.requirement_key)
    db.rows = [canonical, pending]
    with patch.object(file_requests.profiles, "log_profile_action", AsyncMock()):
        await file_requests.cancel_batch(db, profile=profile, request_id=request_id, user=actor)
    assert canonical.status == "requested" and pending.status == "requested"
    assert canonical.provenance["request_batches"][str(request_id)]["cancelled_at"]
    assert not canonical.provenance.get("cancelled_at")


@pytest.mark.asyncio
async def test_own_legal_business_subject_matches_generic_business_but_other_contexts_do_not():
    from app.models.public_underwriting_intake import PublicUnderwritingIntake

    db, profile = MemoryDB(), owner()
    profile.intake_id = uuid4()
    db.rows = [PublicUnderwritingIntake(id=profile.intake_id, business_name="Example Business INC")]
    subjects = await lifecycle.profile_subjects(db, profile)
    normalized = lifecycle.normalize_definition(SimpleNamespace(requirement_key="refinance_debt_schedule", canonical_key=None,
        collection_config={"subject": "Example Business, Inc."}, label="Named schedule"), subjects)
    assert normalized.requirement_key == "business_debt_schedule"
    assert normalized.collection_config["subject_label"] == "Example Business, Inc."
    different = lifecycle.normalize_definition(SimpleNamespace(requirement_key="refinance_debt_schedule", canonical_key=None,
        collection_config={"subject": "Other Business INC"}, label="Other business"), subjects)
    assert different.requirement_key != normalized.requirement_key
    account = lifecycle.normalize_definition(SimpleNamespace(requirement_key="refinance_debt_schedule", canonical_key=None,
        collection_config={"subject": "Example Business, Inc.", "account_id": str(uuid4())}, label="Account schedule"), subjects)
    assert account.requirement_key != normalized.requirement_key


@pytest.mark.asyncio
async def test_maintenance_is_idempotent_and_never_calls_readiness_or_outreach():
    from app.services import missing_item_automation

    db, profile = MemoryDB(), owner()
    canonical, duplicate = state(profile), state(profile, "refinance_debt_schedule")
    db.rows = [canonical, duplicate]
    with (patch.object(application_programs, "get_program_readiness", AsyncMock(side_effect=AssertionError("No readiness side effects"))) as readiness,
          patch.object(missing_item_automation, "send_requirements_email", AsyncMock(side_effect=AssertionError("No outreach"))) as send):
        first = await lifecycle.reconcile_profile(db, profile)
        again = await lifecycle.reconcile_profile(db, profile)
    assert first == again == {"active_debt_schedule_requirements": 1, "retired_duplicate_requirements": 1, "active_debt_schedule_tasks": 1}
    readiness.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_alias_cannot_choose_between_distinct_business_or_period_contexts():
    db, profile, actor = MemoryDB(), owner(), SimpleNamespace(id=uuid4())
    rows = [state(profile, lifecycle.debt_identity("refinance_debt_schedule", {"subject": subject})) for subject in ("Business A", "Business B")]
    for row in rows:
        row.provenance = {"staff_added": {"added_by": str(actor.id)}, "requirement_aliases": ["refinance_debt_schedule"]}
    db.rows = rows
    with patch.object(application_programs, "get_program_readiness", AsyncMock()):
        with pytest.raises(HTTPException) as result:
            await lifecycle.change(db, profile, "refinance_debt_schedule", action="remove", reason="Remove this checklist item", actor=actor)
    assert result.value.status_code == 409 and result.value.detail["code"] == "requirement_context_ambiguous"
    assert all(not lifecycle.is_removed(row) for row in rows)
    assert lifecycle.resolve_requirement_key(rows, rows[1].requirement_key) == rows[1].requirement_key
