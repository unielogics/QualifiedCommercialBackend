from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.enums import Role
from app.models.ai_playbook import AICollectionRequirement
from app.models.application_profile import (
    ApplicationProfile,
    ApplicationRequirementEvidence,
    ApplicationRequirementState,
)
from app.models.bucket import BucketFile, BucketFileAnalysis
from app.models.funding_program import ApplicationRequirementEvidenceDecision
from app.routers.application_profiles import RequirementCollectionCompletionWrite
from app.schemas.application_profile import ApplicationRequirementRead
from app.services import application_programs, document_associations, owner_credit_review
from app.services import requirement_collection_completion as completion
from app.tests.test_requirement_library import db, template_database  # noqa: F401

NOW = datetime.now(UTC)


def requirement(**changes):
    return SimpleNamespace(**{"requirement_key": "business_tax_returns_1_years", "canonical_key": None,
        "collection_config": {}, "label": "Most recently filed business tax return", "category": "financials",
        "expiration_days": None, **changes})


def source(year, *, personal=False):
    file = BucketFile(id=uuid4(), bucket_id=uuid4(), file_name=f"{'Personal' if personal else 'Business'}-{year}.pdf",
        s3_key="synthetic", content_hash=f"{year}{'b' if personal else 'a'}".ljust(64, "a"), created_at=NOW)
    facts = {"tax_form": "1040", "filers": "Synthetic household", "tax_year": str(year)} if personal else {
        "entity": "Example LLC", "tax_form": "1120-S", "tax_year": str(year), "period_end": f"{year}-12-31"}
    analysis = BucketFileAnalysis(id=uuid4(), bucket_file_id=file.id, bucket_id=file.bucket_id,
        content_hash=file.content_hash, analysis_version=1, status="completed", classification="tax_return",
        confidence="high", analysis={"key_facts": facts}, analyzed_at=NOW)
    return file, analysis


def state(**changes):
    return ApplicationRequirementState(**{"id": uuid4(), "profile_id": uuid4(), "requirement_key": "business_tax_returns_1_years",
        "label": "Latest tax return", "category": "financials", "required_level": "required", "status": "received_unverified",
        "provenance": {"review_checks": [{"key": "custom_positive", "label": "Positive earnings", "instructions": "Review positive earnings", "severity": "block"}]}, **changes})


def test_latest_business_return_excludes_personal_even_when_association_accepted():
    old, prior = source(2024)
    latest, current = source(2025)
    personal, household = source(2025, personal=True)
    files, analyses = [old, personal, latest], {old.id: prior, latest.id: current, personal.id: household}
    records = {str(file.id): {"status": "accepted", "association_stamp": "same"} for file in files}
    result = completion.snapshot(state(), requirement(), files, analyses, expected_entity="Example LLC", associations=records)
    assert result["can_complete"] and result["eligible_file_ids"] == result["suggested_file_ids"] == [str(latest.id)]
    assert len(files) == 3
    assert not completion.qualification_review(state())["status"] == "met"


def test_multiyear_and_personal_requests_keep_their_distinct_scopes():
    old, prior = source(2024)
    latest, current = source(2025)
    personal, household = source(2025, personal=True)
    files, analyses = [old, personal, latest], {old.id: prior, latest.id: current, personal.id: household}
    two = completion.tax_eligible_files(requirement(requirement_key="business_tax_returns_2_years"), files, analyses)
    assert {file.id for file in two} == {old.id, latest.id}
    owner_request = requirement(requirement_key="personal_tax_return", collection_config={"subject": "owner"})
    assert completion.tax_eligible_files(owner_request, files, analyses) == files


def test_collection_acceptance_does_not_assert_program_financial_conditions():
    item = state(status="verified")
    accepted = SimpleNamespace(decision="accepted", reason_code="staff_collection_complete")
    result = completion.qualification_review(item, {uuid4(): accepted})
    assert result["blocking"] and result["status"] == "pending"
    assert result["checks"][0]["label"] == "Positive earnings"
    accepted.reason_code = "staff_requirement_verified"
    assert completion.qualification_review(item, {uuid4(): accepted})["status"] == "met"


def test_source_replacement_changes_collection_fingerprint():
    file, analysis = source(2025)
    item = state()
    original = completion.snapshot(item, requirement(), [file], {file.id: analysis})
    analysis.analysis_version = 2
    current = completion.snapshot(item, requirement(), [file], {file.id: analysis})
    assert original["fingerprint"] != current["fingerprint"]


def test_legacy_file_without_file_hash_uses_its_current_analysis_hash():
    file, analysis = source(2025)
    file.content_hash = None
    result = completion.snapshot(state(), requirement(), [file], {file.id: analysis})
    assert result["can_complete"] and result["eligible_file_ids"] == [str(file.id)]


def test_bank_collection_uses_and_fingerprints_the_counted_account_cohort():
    from app.services.financial_cockpit import file_account_identity

    first, a = source(2025)
    second, b = source(2025)
    keys = []
    for file, analysis, digits in ((first, a, "1111"), (second, b, "2222")):
        facts = {"bank": "Example Bank", "account_last4": digits, "statement_period": "2026-01"}
        keys.append(file_account_identity(facts)[0])
        analysis.classification, analysis.analysis = "bank_statement", {"key_facts": facts}
        file.statement_period = "2026-01"
    req = requirement(requirement_key="business_bank_statements_1_months", label="One month bank statement", category="banking")
    item = state()
    first_scope = completion.snapshot(item, req, [first, second], {first.id: a, second.id: b}, counted_accounts=[keys[0]])
    second_scope = completion.snapshot(item, req, [first, second], {first.id: a, second.id: b}, counted_accounts=[keys[1]])
    assert first_scope["eligible_file_ids"] == [str(first.id)]
    assert second_scope["eligible_file_ids"] == [str(second.id)]
    assert first_scope["fingerprint"] != second_scope["fingerprint"]


@pytest.mark.asyncio
async def test_role_guard_runs_before_any_collection_reads():
    with pytest.raises(HTTPException) as error:
        await completion.complete(None, None, "tax", None, SimpleNamespace(role=Role.CLIENT))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_route_preserves_profile_access_scope_before_completion(monkeypatch):
    from app.routers import application_profiles as route
    from app.services import application_profiles

    actor = SimpleNamespace(role=Role.LOAN_EXEC)
    payload = RequirementCollectionCompletionWrite(expected_fingerprint="a" * 64, idempotency_key="test-access-scope")
    monkeypatch.setattr(application_profiles, "load_profile", AsyncMock(side_effect=HTTPException(404, "Application not found")))
    action = AsyncMock()
    monkeypatch.setattr(completion, "complete", action)
    with pytest.raises(HTTPException) as error:
        await route.complete_application_requirement_collection(uuid4(), "tax", payload, actor, None)
    assert error.value.status_code == 404
    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_materialization_complete_is_durable_and_not_program_acceptance(db, monkeypatch):  # noqa: F811
    from app.models.user import User
    from app.tests.test_requirement_library import _playbook, _requirement

    actor = User(clerk_id="collection-review", email="collection@example.test", name="Reviewer", role=Role.LOAN_EXEC)
    profile = ApplicationProfile(vertical="main_street", funding_category="general")
    playbook = _playbook("platform", "evidence_policy", "completion_policy")
    db.add_all([actor, profile, playbook])
    await db.flush()
    req = _requirement(playbook, "business_tax_returns_1_years", "Most recently filed business tax return")
    req.review_checks = [{"key": "custom_positive", "label": "Positive earnings", "instructions": "Verify earnings are positive", "severity": "block"}]
    db.add(req)
    db.add(AICollectionRequirement(playbook_id=playbook.id, requirement_key="other_unused", label="Unused",
        category="financials", required_level="optional", visibility=["underwriter"], completion_mode="ai_can_complete"))
    old, prior = source(2024)
    latest, current = source(2025)
    personal, household = source(2025, personal=True)
    files, analyses = [old, personal, latest], {old.id: prior, latest.id: current, personal.id: household}
    db.add_all([*files, *analyses.values()])
    await db.flush()
    old_links = [ApplicationRequirementEvidence(requirement_state_id=uuid4(), file_id=file.id,
        source="operator", linked_at=NOW, verified_at=NOW, verified_by_user_id=actor.id) for file in (old, personal)]
    policy = SimpleNamespace(playbook_id=playbook.id, playbook_version=3, policy_key="completion_policy")
    monkeypatch.setattr(application_programs, "_evidence_inventory", AsyncMock(return_value=(files, analyses, {"tax_return"})))
    monkeypatch.setattr(document_associations, "expected_business", AsyncMock(return_value="Example LLC"))
    locks = AsyncMock()
    monkeypatch.setattr(owner_credit_review, "lock_review_sources", locks)

    async def readiness(_db, _profile):
        states, _ = await application_programs._materialize_requirements(db, profile, [], {}, [policy], {playbook.id: [req]})
        row = states[0]
        view = ApplicationRequirementRead(requirement_key=row.requirement_key, label=row.label, category=row.category,
            required_level=row.required_level, status=row.status, collection_config={}, canonical_key=row.requirement_key, provenance=row.provenance,
            collection_completion=row.provenance["collection_completion"], qualification_review=row.provenance["qualification_review"])
        return SimpleNamespace(requirements=[view])

    monkeypatch.setattr(application_programs, "get_program_readiness", readiness)
    before = (await readiness(db, profile)).requirements[0]
    assert before.collection_completion["can_complete"]
    assert before.collection_completion["suggested_file_ids"] == [str(latest.id)]
    target_state = (await db.scalars(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id,
        ApplicationRequirementState.requirement_key == req.requirement_key,
    ))).one()
    for old_link in old_links:
        old_link.requirement_state_id = target_state.id
        db.add(old_link)
    await db.flush()
    await readiness(db, profile)
    assert all(old_link.removed_at is not None for old_link in old_links)
    assert all(old_link.verified_at is not None for old_link in old_links)
    # Explicitly link a file the desk had previously detached. The current
    # published version is 3, not the service's old implicit version 1.
    link = (await db.scalars(select(ApplicationRequirementEvidence).where(
        ApplicationRequirementEvidence.file_id == latest.id,
        ApplicationRequirementEvidence.removed_at.is_(None),
    ))).one()
    link.removed_at, link.removed_by_user_id = NOW, actor.id
    await db.flush()
    payload = RequirementCollectionCompletionWrite(file_ids=[latest.id], expected_fingerprint=before.collection_completion["fingerprint"], idempotency_key="complete-document-1")
    for invalid in (personal.id, old.id, uuid4()):
        with pytest.raises(HTTPException) as wrong_source:
            await completion.complete(db, profile, req.requirement_key, payload.model_copy(update={"file_ids": [invalid]}), actor)
        assert wrong_source.value.status_code == 422
    result = await completion.complete(db, profile, req.requirement_key, payload, actor)
    assert result.requirements[0].status == "verified"
    assert result.requirements[0].collection_completion["complete"]
    assert result.requirements[0].qualification_review["blocking"]
    req.review_checks = [*req.review_checks, {"key": "custom_new", "label": "Another program condition", "instructions": "Review the new published financial condition", "severity": "block"}]
    await db.flush()
    policy_changed = (await readiness(db, profile)).requirements[0]
    assert policy_changed.status == "verified" and policy_changed.collection_completion["complete"]
    assert policy_changed.qualification_review["blocking"]
    accepted = (await db.scalars(select(ApplicationRequirementEvidenceDecision).where(
        ApplicationRequirementEvidenceDecision.reason_code == "staff_collection_complete",
    ))).one()
    assert accepted.policy_version == 3
    again = await completion.complete(db, profile, req.requirement_key, payload, actor)
    assert again.requirements[0].status == "verified"
    payload_b = payload.model_copy(update={"idempotency_key": "complete-document-2", "reason": "Reviewed the current business source again"})
    await completion.complete(db, profile, req.requirement_key, payload_b, actor)
    count = await db.scalar(select(func.count()).select_from(ApplicationRequirementEvidenceDecision))
    replay_a = await completion.complete(db, profile, req.requirement_key, payload, actor)
    assert replay_a.requirements[0].status == "verified"
    assert await db.scalar(select(func.count()).select_from(ApplicationRequirementEvidenceDecision)) == count
    with pytest.raises(HTTPException) as conflict:
        await completion.complete(db, profile, req.requirement_key, payload.model_copy(update={"reason": "Different request reusing an earlier retry key"}), actor)
    assert conflict.value.status_code == 409
    current.analysis_version = 2
    await db.flush()
    refreshed = (await readiness(db, profile)).requirements[0]
    assert refreshed.status == "received_unverified"
    assert not refreshed.collection_completion["complete"]
    with pytest.raises(HTTPException) as stale:
        await completion.complete(db, profile, req.requirement_key,
            payload.model_copy(update={"idempotency_key": "complete-document-3"}), actor)
    assert stale.value.status_code == 409
    assert locks.await_count == 9
