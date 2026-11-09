"""A readiness lock must also refresh objects cached before another edit."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile, ApplicationRequirementState
from app.models.bucket import BucketFile, BucketRequestedDocument
from app.services import application_programs, document_associations
from app.tests.test_requirement_library import _playbook, _requirement
from app.tests.test_requirement_library import db as db
from app.tests.test_requirement_library import template_database as template_database


@pytest.mark.asyncio
async def test_sqlite_fixture_uuid_storage_preserves_scientific_notation_hex_during_batch_flush(db):
    identifiers = [UUID("11111111-1111-4111-8111-11111111e999"), UUID("11111111-1111-4111-8111-11111111e998")]
    db.add_all([ApplicationProfile(id=identifier, vertical="main_street", funding_category="general")
                for identifier in identifiers])
    await db.flush()
    raw = (await db.execute(text("SELECT id, typeof(id) FROM application_profiles WHERE id IN (:first, :second)"),
                            {"first": identifiers[0].hex, "second": identifiers[1].hex})).all()
    assert {row[0] for row in raw} == {identifier.hex for identifier in identifiers}
    assert {row[1] for row in raw} == {"text"}
    await db.commit()
    async with AsyncSession(db.bind, expire_on_commit=False) as verifier:
        for identifier in identifiers:
            restored = await verifier.get(ApplicationProfile, identifier)
            assert restored.id == identifier and isinstance(restored.id, UUID)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_refresh", [False, True])
async def test_materializer_preserves_receipt_committed_after_its_state_was_cached(db, monkeypatch, commit_refresh):
    profile = ApplicationProfile(vertical="main_street", funding_category="general")
    playbook = _playbook("platform", "evidence_policy", "freshness_policy")
    db.add_all([profile, playbook])
    await db.flush()
    requirement = _requirement(playbook, "business_license", "Business license")
    db.add(requirement)
    await db.flush()
    policy = SimpleNamespace(playbook_id=playbook.id, playbook_version=1, policy_key="freshness_policy")
    monkeypatch.setattr(application_programs, "_evidence_inventory", AsyncMock(return_value=([], {}, set())))
    monkeypatch.setattr(document_associations, "expected_business", AsyncMock(return_value="Example LLC"))

    async def materialize():
        rows, _ = await application_programs._materialize_requirements(
            db, profile, [], {}, [policy], {playbook.id: [requirement]},
        )
        return rows[0]

    cached = await materialize()
    cached.provenance = {**cached.provenance, "staff_review_marker": "before review"}
    state_id = cached.id
    await db.commit()  # expire_on_commit=False deliberately retains the old JSON.
    receipt = {"idempotency_key": "staff-completion-retry", "fingerprint": "source-reviewed",
               "file_ids": [], "qualification_approved": False}
    async with AsyncSession(db.bind, expire_on_commit=False) as writer:
        current = await writer.get(ApplicationRequirementState, state_id)
        current.provenance = {**current.provenance, "staff_review_marker": "completed in another request",
                              "collection_completion_receipt": receipt}
        await writer.commit()
    assert "collection_completion_receipt" not in cached.provenance
    still_cached = (await db.scalars(select(ApplicationRequirementState).where(ApplicationRequirementState.id == state_id))).one()
    assert still_cached is cached and "collection_completion_receipt" not in still_cached.provenance
    refreshed = await materialize()
    assert refreshed is cached
    assert refreshed.provenance["collection_completion_receipt"] == receipt
    assert refreshed.provenance["staff_review_marker"] == "completed in another request"
    if commit_refresh:
        await db.commit()
    else:
        await db.rollback()
    async with AsyncSession(db.bind, expire_on_commit=False) as verifier:
        persisted = await verifier.get(ApplicationRequirementState, state_id)
        assert persisted.provenance["collection_completion_receipt"] == receipt
        assert persisted.provenance["staff_review_marker"] == "completed in another request"


@pytest.mark.asyncio
async def test_materializer_refreshes_user_removed_evidence_before_relinking(db):
    from datetime import UTC, datetime

    from app.models.application_profile import ApplicationRequirementEvidence

    profile = ApplicationProfile(vertical="main_street", funding_category="general")
    db.add(profile)
    await db.flush()
    state = ApplicationRequirementState(profile_id=profile.id, requirement_key="business_license", label="License",
                                        category="compliance", required_level="required", status="missing")
    file = BucketFile(bucket_id=uuid4(), file_name="License.pdf", s3_key="synthetic-license", status="uploaded")
    db.add_all([state, file])
    await db.flush()
    cached = (await application_programs._sync_requirement_evidence(db, state, {file.id: file}, {file.id: "automatic"}))[0]
    link_id, file_id = cached.id, file.id
    await db.commit()
    async with AsyncSession(db.bind, expire_on_commit=False) as writer:
        link = await writer.get(ApplicationRequirementEvidence, link_id)
        link.removed_at, link.removed_by_user_id = datetime.now(UTC), uuid4()
        await writer.commit()
    assert cached.removed_at is None
    assert await application_programs._sync_requirement_evidence(db, state, {file_id: file}, {file_id: "automatic"}) == []
    assert cached.removed_at is not None and cached.removed_by_user_id is not None


@pytest.mark.asyncio
async def test_room_ask_refresh_preserves_signature_scope_changed_in_another_request(db):
    profile = ApplicationProfile(vertical="main_street", funding_category="general", primary_bucket_id=uuid4())
    playbook = _playbook("platform", "evidence_policy", "room_freshness")
    db.add_all([profile, playbook])
    await db.flush()
    requirement = _requirement(playbook, "business_license", "License")
    db.add(requirement)
    cached = BucketRequestedDocument(bucket_id=profile.primary_bucket_id, requirement_key=requirement.requirement_key,
        name="License", category="compliance", status="requested", requires_signature=False)
    db.add(cached)
    await db.commit()
    ask_id = cached.id
    async with AsyncSession(db.bind, expire_on_commit=False) as writer:
        ask = await writer.get(BucketRequestedDocument, ask_id)
        ask.requires_signature, ask.signature_kind = True, "credit_authorization"
        await writer.commit()
    assert cached.requires_signature is False
    refreshed = await application_programs._requested_document(db, profile, requirement, [], [])
    assert refreshed is cached and refreshed.requires_signature is True
    assert refreshed.signature_kind == "credit_authorization"


@pytest.mark.asyncio
async def test_readiness_locks_before_flushing_own_edits_then_refreshes_profile_without_committing(db, monkeypatch):
    profile = ApplicationProfile(vertical="main_street", funding_category="general", primary_bucket_id=uuid4(),
                                 program_selection_mode="auto", extra_requirements=[])
    db.add(profile)
    await db.commit()
    profile_id = profile.id
    async with AsyncSession(db.bind, expire_on_commit=False) as writer:
        current = await writer.get(ApplicationProfile, profile_id)
        current.extra_requirements = [{"key": "new request from another session"}]
        await writer.commit()
    assert profile.extra_requirements == []
    profile.program_selection_mode = "manual"  # The caller's own pending edit must survive refresh.
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    class ReachedFreshContext(Exception):
        pass

    async def stop_after_refresh(_db, current):
        assert current is profile
        assert current.extra_requirements == [{"key": "new request from another session"}]
        assert current.program_selection_mode == "manual"
        raise ReachedFreshContext

    monkeypatch.setattr(application_programs, "profile_fit_context", stop_after_refresh)
    event.listen(db.bind.sync_engine, "before_cursor_execute", record)
    try:
        with pytest.raises(ReachedFreshContext):
            await application_programs.get_program_readiness(db, profile)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", record)
    bucket_lock = next(index for index, sql in enumerate(statements) if "SELECT buckets.id" in sql)
    profile_lock = next(index for index, sql in enumerate(statements) if "SELECT application_profiles.id \n" in sql)
    own_flush = next(index for index, sql in enumerate(statements) if sql.startswith("UPDATE application_profiles"))
    assert bucket_lock < profile_lock < own_flush
    await db.rollback()
    async with AsyncSession(db.bind, expire_on_commit=False) as verifier:
        persisted = await verifier.get(ApplicationProfile, profile_id)
        assert persisted.program_selection_mode == "auto"
        assert persisted.extra_requirements == [{"key": "new request from another session"}]
        assert await verifier.scalar(select(ApplicationProfile.id).where(ApplicationProfile.id == profile_id)) == profile_id
