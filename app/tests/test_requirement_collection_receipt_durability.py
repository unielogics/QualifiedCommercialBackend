"""A replaceable checklist projection cannot be the idempotency authority."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.enums import Role
from app.models.application_profile import ApplicationProfile, ApplicationRequirementState
from app.models.file_event import FileEvent
from app.models.funding_program import ApplicationRequirementEvidenceDecision
from app.models.user import User
from app.routers.application_profiles import RequirementCollectionCompletionWrite
from app.schemas.application_profile import ApplicationRequirementRead
from app.services import (
    application_programs,
    document_associations,
    file_events,
    owner_credit_review,
)
from app.services import requirement_collection_completion as completion
from app.services import requirement_collection_receipts as durable
from app.tests.test_requirement_collection_completion import source
from app.tests.test_requirement_library import (  # noqa: F401
    _playbook,
    _requirement,
    db,
    template_database,
)


async def context(db, monkeypatch, *, initially_complete=True):  # noqa: F811
    actor = User(clerk_id="durable-collection-review", email="durable-collection@example.test", name="Reviewer", role=Role.LOAN_EXEC)
    profile = ApplicationProfile(vertical="main_street", funding_category="general")
    playbook = _playbook("platform", "evidence_policy", "durable_completion_policy")
    db.add_all([actor, profile, playbook])
    await db.flush()
    requirement = _requirement(playbook, "business_tax_returns_1_years", "Most recently filed business tax return")
    requirement.review_checks = [{"key": "custom_positive", "label": "Positive earnings", "instructions": "Verify positive earnings", "severity": "block"}]
    db.add(requirement)
    file, analysis = source(2025)
    files, analyses = [file], {file.id: analysis}
    db.add_all([file, analysis])
    await db.flush()
    policy = SimpleNamespace(playbook_id=playbook.id, playbook_version=3, policy_key="durable_completion_policy")
    monkeypatch.setattr(application_programs, "_evidence_inventory", AsyncMock(return_value=(files, analyses, {"tax_return"})))
    monkeypatch.setattr(document_associations, "expected_business", AsyncMock(return_value="Example LLC"))
    monkeypatch.setattr(owner_credit_review, "lock_review_sources", AsyncMock())
    # Persist the normal sanitized timeline row, without fan-out to recipients.
    monkeypatch.setattr(file_events, "_notify", AsyncMock())

    async def readiness(_db, _profile):
        states, _ = await application_programs._materialize_requirements(db, profile, [], {}, [policy], {playbook.id: [requirement]})
        state = states[0]
        view = ApplicationRequirementRead(requirement_key=state.requirement_key, label=state.label, category=state.category,
            required_level=state.required_level, status=state.status, collection_config={}, canonical_key=state.requirement_key,
            provenance=state.provenance, collection_completion=state.provenance["collection_completion"],
            qualification_review=state.provenance["qualification_review"])
        return SimpleNamespace(requirements=[view])

    monkeypatch.setattr(application_programs, "get_program_readiness", readiness)
    first = (await readiness(db, profile)).requirements[0]
    payload = RequirementCollectionCompletionWrite(file_ids=[file.id], reason="Reviewed the current 2025 business return",
        expected_fingerprint=first.collection_completion["fingerprint"], idempotency_key="durable-completion-retry-key")
    if initially_complete:
        accepted = await completion.complete(db, profile, requirement.requirement_key, payload, actor)
        assert accepted.requirements[0].status == "verified"
    state = (await db.scalars(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id,
        ApplicationRequirementState.requirement_key == requirement.requirement_key))).one()
    receipt = deepcopy(state.provenance.get("collection_completion_receipt"))
    await db.commit()
    assert profile.dealer_id is None, "Nondealer files cannot rely on the dealer-only audit metadata writer"
    return SimpleNamespace(actor=actor, profile=profile, requirement=requirement, analysis=analysis,
                           payload=payload, readiness=readiness, state=state, original_receipt=receipt)


async def erase_projection_receipt(db, state):  # noqa: F811
    state.provenance = {key: value for key, value in (state.provenance or {}).items()
                        if key not in {"collection_completion_receipt", "collection_completion_history"}}
    await db.commit()


async def accepted_count(db):  # noqa: F811
    return await db.scalar(select(func.count()).select_from(ApplicationRequirementEvidenceDecision).where(
        ApplicationRequirementEvidenceDecision.reason_code == "staff_collection_complete"))


async def client_event_count(db, profile):  # noqa: F811
    return await db.scalar(select(func.count()).select_from(FileEvent).where(
        FileEvent.profile_id == profile.id, FileEvent.kind == "requirement.collection_completed", FileEvent.visibility == "client"))


@pytest.mark.asyncio
async def test_exact_retry_after_projection_receipt_loss_rehydrates_without_reaccepting_or_duplicate_event(db, monkeypatch):  # noqa: F811
    case = await context(db, monkeypatch)
    accepted_before, events_before = await accepted_count(db), await client_event_count(db, case.profile)
    await erase_projection_receipt(db, case.state)
    result = await completion.complete(db, case.profile, case.requirement.requirement_key, case.payload, case.actor)
    assert result.requirements[0].status == "verified"
    assert await accepted_count(db) == accepted_before
    assert await client_event_count(db, case.profile) == events_before
    assert case.state.provenance["collection_completion_receipt"] == case.original_receipt
    assert result.requirements[0].collection_completion["completed_at"] == case.original_receipt["at"]


@pytest.mark.asyncio
async def test_normal_readiness_refresh_recovers_collection_proof_after_projection_loss_and_new_financial_checks(db, monkeypatch):  # noqa: F811
    case = await context(db, monkeypatch)
    await erase_projection_receipt(db, case.state)
    case.requirement.review_checks = [*case.requirement.review_checks,
        {"key": "new_revenue_minimum", "label": "Revenue minimum", "instructions": "Verify revenue against the new program minimum", "severity": "block"}]
    await db.flush()
    result = (await case.readiness(db, case.profile)).requirements[0]
    assert result.status == "verified"
    assert result.collection_completion["complete"]
    assert result.qualification_review["blocking"], "Recovered document receipt must not approve financial qualification"
    assert case.state.provenance["collection_completion_receipt"] == case.original_receipt
    assert await accepted_count(db) == 1


@pytest.mark.asyncio
async def test_changed_payload_reusing_durable_retry_key_returns_conflict_after_projection_loss(db, monkeypatch):  # noqa: F811
    case = await context(db, monkeypatch)
    await erase_projection_receipt(db, case.state)
    payload = case.payload.model_copy(update={"reason": "A different review attempting the same retry key"})
    with pytest.raises(HTTPException) as error:
        await completion.complete(db, case.profile, case.requirement.requirement_key, payload, case.actor)
    assert error.value.status_code == 409
    assert await accepted_count(db) == 1


@pytest.mark.asyncio
async def test_recovered_old_receipt_does_not_accept_reanalyzed_source_or_create_new_staff_decision(db, monkeypatch):  # noqa: F811
    case = await context(db, monkeypatch)
    await erase_projection_receipt(db, case.state)
    case.analysis.analysis_version = 2
    await db.flush()
    result = (await case.readiness(db, case.profile)).requirements[0]
    assert result.status == "received_unverified" and not result.collection_completion["complete"]
    replay = await completion.complete(db, case.profile, case.requirement.requirement_key, case.payload, case.actor)
    assert replay.requirements[0].status == "received_unverified"
    assert await accepted_count(db) == 1
    assert case.state.provenance["collection_completion_receipt"]["at"] == case.original_receipt["at"]


@pytest.mark.asyncio
async def test_durable_receipt_is_private_without_notices_and_public_event_stays_sanitized(db, monkeypatch):  # noqa: F811
    case = await context(db, monkeypatch)
    rows = list((await db.scalars(select(FileEvent).where(FileEvent.profile_id == case.profile.id))).all())
    private = [row for row in rows if row.kind == durable.KIND]
    assert len(private) == 1 and private[0].visibility == "desk" and private[0].notice_status == "none"
    assert private[0].meta["receipt"] == case.original_receipt
    public = await file_events.list_events(db, case.profile.id, tier="client")
    assert len(public) == 1
    assert public[0].kind == "requirement.collection_completed"
    assert public[0].meta == {"requirement_key": case.requirement.requirement_key, "collection_complete": True}
    file_events._notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_durable_write_failure_aborts_acceptance_instead_of_being_swallowed_by_timeline_delivery(db, monkeypatch):  # noqa: F811
    case = await context(db, monkeypatch, initially_complete=False)
    original_add = db.add

    def reject_private_receipt(row, *args, **kwargs):
        if isinstance(row, FileEvent) and row.kind == durable.KIND:
            raise RuntimeError("Mandatory receipt storage unavailable")
        return original_add(row, *args, **kwargs)

    monkeypatch.setattr(db, "add", reject_private_receipt)
    with pytest.raises(RuntimeError, match="Mandatory receipt storage unavailable"):
        async with db.begin_nested():
            await completion.complete(db, case.profile, case.requirement.requirement_key, case.payload, case.actor)
    assert await accepted_count(db) == 0
    assert await db.scalar(select(func.count()).select_from(FileEvent)) == 0
    file_events._notify.assert_not_awaited()
