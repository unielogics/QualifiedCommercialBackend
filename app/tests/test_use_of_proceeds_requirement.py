from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import application_profiles as rooms
from app.schemas.application_profile import RoomReminderCreate
from app.services import requirement_collection_completion, use_of_funds, use_of_proceeds
from app.tests.test_requirement_library import db, template_database  # noqa: F401


def test_use_of_funds_reminder_opens_the_existing_budget_tab():
    assert RoomReminderCreate(purpose="use_of_funds").purpose == "use_of_funds"
    assert rooms._room_reminder_copy("use_of_funds") == (
        "tab=budget", "complete the itemized business use-of-funds budget",
    )


@pytest.mark.parametrize("dealer", [False, True])
async def test_preparing_budget_link_never_sends_email_or_sms_and_keeps_pin_separate(dealer):
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=uuid4() if dealer else None)
    link = SimpleNamespace(id=uuid4(), token="private-room-token", status="active", expires_at=None,
                           recipient_email="owner@example.com", completed_at=None)
    actor = SimpleNamespace(role=Role.SUPER_ADMIN)
    session = SimpleNamespace(commit=AsyncMock())
    with (
        patch.object(rooms.profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(rooms, "_profile_room_link", AsyncMock(return_value=link)),
        patch.object(rooms, "_require_training_live_action", AsyncMock()),
        patch.object(rooms, "_deliver_room_request", AsyncMock(return_value=[])) as deliver,
        patch.object(rooms.profiles, "log_profile_action", AsyncMock()) as audit,
    ):
        result = await rooms.send_application_room_reminder(
            profile.id,
            RoomReminderCreate(purpose="use_of_funds", email_room_link=False, sms_reminder=False),
            SimpleNamespace(), actor, session,
        )
    assert result.room_url.endswith("/buckets/request/private-room-token?tab=budget")
    assert result.overall_status == "created"
    from app.routers.buckets import _request_room_kind

    assert await _request_room_kind(session, profile.primary_bucket_id, profile) == ("dealer" if dealer else "application")
    assert "passcode" not in result.room_url and "pin" not in result.room_url
    assert deliver.await_args.kwargs["send_email"] is False
    assert deliver.await_args.kwargs["send_sms"] is False
    assert deliver.await_args.kwargs["query"] == "tab=budget"
    assert audit.await_args.args[3] == "room.link_created"
    assert "without sending" in audit.await_args.args[4]
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER, Role.VENDOR, Role.LENDER])
async def test_nonstaff_cannot_prepare_a_budget_share_link(role):
    profile = SimpleNamespace(id=uuid4())
    with (
        patch.object(rooms.profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(rooms, "_profile_room_link", AsyncMock()) as resolver,
        pytest.raises(HTTPException) as error,
    ):
        await rooms.send_application_room_reminder(profile.id, RoomReminderCreate(purpose="use_of_funds"),
                                                  SimpleNamespace(), SimpleNamespace(role=role), SimpleNamespace())
    assert error.value.status_code == 403
    resolver.assert_not_awaited()


@pytest.mark.parametrize("fields", [
    {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    {"status": "revoked"},
    {"completed_at": datetime.now(UTC), "allow_multiple_sessions": False},
])
async def test_budget_links_reject_unusable_rooms_before_any_delivery(fields):
    profile = SimpleNamespace(id=uuid4())
    link = SimpleNamespace(**{"id": uuid4(), "status": "active", "expires_at": None, "completed_at": None, **fields})
    with (
        patch.object(rooms.profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(rooms, "_profile_room_link", AsyncMock(return_value=link)),
        patch.object(rooms, "_deliver_room_request", AsyncMock()) as deliver,
        pytest.raises(HTTPException) as error,
    ):
        await rooms.send_application_room_reminder(profile.id, RoomReminderCreate(purpose="use_of_funds"),
                                                  SimpleNamespace(), SimpleNamespace(role=Role.SUPER_ADMIN), SimpleNamespace())
    assert error.value.status_code == 409
    deliver.assert_not_awaited()


def _state(**changes):
    from app.models.application_profile import ApplicationRequirementState

    return ApplicationRequirementState(**{
        "id": uuid4(), "profile_id": uuid4(), "requirement_key": "use_of_proceeds",
        "requested_document_id": uuid4(), "label": "Business use of proceeds", "category": "borrower_info",
        "required_level": "required", "status": "requested", "provenance": {}, **changes,
    })


def _profile(**changes):
    return SimpleNamespace(**{
        "id": uuid4(), "use_of_funds": [], "use_of_funds_revision": 0,
        "use_of_funds_updated_at": None, "use_of_funds_updated_by_user_id": None, **changes,
    })


def _budget(profile, amount=1000):
    from decimal import Decimal

    return use_of_funds.summarize(profile, Decimal(str(amount)), "intake.requested_loan_amount")


def _items():
    return [{"id": "working", "category": "working_capital", "label": "Operating costs", "amount": "1000.00"}]


@pytest.mark.parametrize("disposition", ["waived", "not_applicable", "removed"])
async def test_explicit_dispositions_are_preserved_when_budget_is_complete(disposition):
    profile = _profile(use_of_funds=_items(), use_of_funds_revision=1)
    row = _state(status=disposition if disposition != "removed" else "requested")
    if disposition == "removed":
        row.provenance = {"staff_removal": {"removed_at": datetime.now(UTC).isoformat(), "reason": "Staff removed"}}
    old = row.status
    definition = SimpleNamespace(requirement_key="use_of_proceeds", canonical_key=None)
    with patch.object(use_of_funds, "read_budget", AsyncMock(return_value=_budget(profile))):
        assert await use_of_proceeds.materialize(None, profile, definition, row, None, [])
    assert row.status == old and row.provenance["collection_completion"]["can_complete"] is False


def test_unrelated_files_never_become_proceeds_collection_candidates():
    from app.models.bucket import BucketFile, BucketFileAnalysis

    row = _state()
    definition = SimpleNamespace(requirement_key="use_of_proceeds", canonical_key=None, collection_config={},
                                 label="Business use of proceeds", category="borrower_info", expiration_days=None)
    files = [BucketFile(id=uuid4(), file_name=f"Unrelated-{index}.pdf", content_hash="a" * 64,
                        created_at=datetime.now(UTC)) for index in range(19)]
    explicit = BucketFile(id=uuid4(), file_name="Explicit proceeds supporting costs.pdf", content_hash="b" * 64,
                          requested_document_id=row.requested_document_id, created_at=datetime.now(UTC))
    files.append(explicit)
    analyses = {file.id: BucketFileAnalysis(id=uuid4(), status="completed", content_hash=file.content_hash,
                                          analysis_version=1, classification="other", analysis={}) for file in files}
    snapshot = requirement_collection_completion.snapshot(row, definition, files, analyses)
    assert snapshot["eligible_file_ids"] == [str(explicit.id)]
    assert snapshot["suggested_file_ids"] == [str(explicit.id)]


@pytest.mark.parametrize("change", ["budget_revision", "request_revision", "request_amount", "items"])
async def test_budget_or_request_changes_reopen_a_completed_collection(change):
    profile = _profile(use_of_funds=_items(), use_of_funds_revision=1)
    row = _state()
    snapshot = use_of_proceeds.snapshot(profile, row, _budget(profile))
    row.provenance = {"collection_completion_receipt": {
        "source": "use_of_funds", "fingerprint": snapshot["fingerprint"], "actor_id": str(uuid4()),
        "at": datetime.now(UTC).isoformat(),
    }}
    definition = SimpleNamespace(requirement_key="use_of_proceeds", canonical_key=None)
    with patch.object(use_of_funds, "read_budget", AsyncMock(return_value=_budget(profile))):
        await use_of_proceeds.materialize(None, profile, definition, row, None, [])
    assert row.status == "verified" and row.provenance["collection_completion"]["complete"]
    if change == "budget_revision":
        profile.use_of_funds_revision = 2
    elif change == "request_revision":
        profile.current_funding_request = {"requested_amount": "1000.00", "revision": 1}
    elif change == "items":
        profile.use_of_funds = [{**_items()[0], "category": "inventory"}]
    with patch.object(use_of_funds, "read_budget", AsyncMock(return_value=_budget(profile, 2000 if change == "request_amount" else 1000))):
        await use_of_proceeds.materialize(None, profile, definition, row, None, [])
    assert row.status == "received_unverified" and not row.provenance["collection_completion"]["complete"]
    assert row.verified_by_user_id is None


async def test_request_revision_reopens_program_review_without_erasing_its_receipt():
    profile = _profile(use_of_funds=_items(), use_of_funds_revision=1)
    row = _state(provenance={"review_checks": [{"key": "working_capital_only", "severity": "block"}]})
    program_receipt = {"fingerprint": use_of_proceeds.snapshot(profile, row, _budget(profile))["fingerprint"],
                       "actor_id": str(uuid4()), "reason": "Reviewed working-capital-only allocation"}
    row.provenance = {**row.provenance, "use_of_funds_program_review": program_receipt}
    definition = SimpleNamespace(requirement_key="use_of_proceeds", canonical_key=None)
    qualification = {"status": "met", "blocking": False, "reasons": []}
    with patch.object(use_of_funds, "read_budget", AsyncMock(return_value=_budget(profile))):
        await use_of_proceeds.materialize(None, profile, definition, row, None, [], qualification=qualification)
    assert row.provenance["qualification_review"]["status"] == "met"
    profile.current_funding_request = {"requested_amount": "1000.00", "revision": 1}
    with patch.object(use_of_funds, "read_budget", AsyncMock(return_value=_budget(profile))):
        await use_of_proceeds.materialize(None, profile, definition, row, None, [], qualification=qualification)
    assert row.provenance["qualification_review"]["status"] == "pending"
    assert row.provenance["qualification_review"]["blocking"]
    assert row.provenance["use_of_funds_program_review"] == program_receipt


def test_scoped_program_cannot_reuse_old_check_citations_after_allocation_change():
    from app.services.document_qualification_review import check_id
    from app.services.requirement_program_qualification import for_definitions

    first = {"key": "working_capital_only", "label": "Working capital only", "instructions": "Review the allocation", "severity": "block"}
    second = {"key": "permitted_costs", "label": "Permitted costs", "instructions": "Review support", "severity": "block"}
    row = _state(status="verified", provenance={
        "review_checks": [first, second],
        "qualification_review": {"status": "pending", "blocking": True},
        "use_of_funds_collection": {"fingerprint": "new-budget"},
        "use_of_funds_program_review": {"fingerprint": "previous-budget"},
        "ai_document_review": {"checks": [{"id": check_id(first), "status": "pass", "citations": [{"page": 1}]}]},
    })
    scoped = for_definitions(row, [SimpleNamespace(review_checks=[first])])
    assert scoped["status"] == "pending" and scoped["blocking"]


async def test_saved_budget_and_staff_collection_complete_update_the_exact_requirement(db, monkeypatch):  # noqa: F811
    from decimal import Decimal

    from sqlalchemy import func, select

    from app.models.application_profile import (
        ApplicationProfile,
        ApplicationRequirementEvidence,
        ApplicationRequirementState,
    )
    from app.models.bucket import Bucket, BucketFile, BucketFileAnalysis, BucketRequestedDocument
    from app.models.user import User
    from app.routers.application_profiles import RequirementCollectionCompletionWrite
    from app.schemas.application_profile import (
        ApplicationRequirementPatch,
        ApplicationRequirementRead,
    )
    from app.schemas.use_of_funds import UseOfFundsPatch
    from app.services import (
        application_profiles,
        application_programs,
        document_associations,
        file_events,
        owner_credit_review,
    )
    from app.tests.test_requirement_library import _playbook, _requirement

    actor = User(clerk_id="proceeds-review", email="reviewer@example.test", name="Reviewer", role=Role.LOAN_EXEC)
    bucket = Bucket(name="Example application")
    policy = _playbook("platform", "evidence_policy", "proceeds_policy")
    db.add_all([actor, bucket, policy])
    await db.flush()
    profile = ApplicationProfile(vertical="main_street", funding_category="general", primary_bucket_id=bucket.id, is_draft=True)
    definition = _requirement(policy, "use_of_proceeds", "Business use of proceeds", category="borrower_info")
    definition.review_checks = [{"key": "permitted_business_uses", "label": "Permitted uses", "instructions": "Review eligibility and supporting costs separately", "severity": "block"}]
    db.add_all([profile, definition])
    await db.flush()
    selection = SimpleNamespace(playbook_id=policy.id, playbook_version=1, policy_key="proceeds_policy")
    monkeypatch.setattr(use_of_funds, "source_funding_data", AsyncMock(return_value=(Decimal(1000), "intake.requested_loan_amount", None, None)))
    inventory = ([], {}, set())
    monkeypatch.setattr(application_programs, "_evidence_inventory", AsyncMock(return_value=inventory))
    monkeypatch.setattr(document_associations, "load_context", AsyncMock(return_value={}))
    monkeypatch.setattr(document_associations, "expected_business", AsyncMock(return_value="Example LLC"))
    monkeypatch.setattr(owner_credit_review, "lock_review_sources", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(application_profiles, "log_profile_action", audit)
    monkeypatch.setattr(file_events, "emit", AsyncMock())

    async def readiness(_db, _profile):
        states, _ = await application_programs._materialize_requirements(db, profile, [], {}, [selection], {policy.id: [definition]})
        row = states[0]
        view = ApplicationRequirementRead(requirement_key=row.requirement_key, label=row.label, category=row.category,
            required_level=row.required_level, status=row.status, canonical_key=row.requirement_key,
            provenance=row.provenance, collection_completion=row.provenance["collection_completion"],
            qualification_review=row.provenance["qualification_review"])
        return SimpleNamespace(requirements=[view])

    monkeypatch.setattr(application_programs, "get_program_readiness", readiness)
    initial = (await readiness(db, profile)).requirements[0]
    assert initial.status == "requested" and initial.collection_completion["source"] == "use_of_funds"
    assert not initial.collection_completion["can_complete"]
    await use_of_funds.update_budget(db, profile, UseOfFundsPatch(items=_items(), expected_revision=0), actor)
    saved = (await readiness(db, profile)).requirements[0]
    assert saved.status == "received_unverified" and saved.collection_completion["can_complete"]
    assert saved.collection_completion["summary"]["allocated_amount"] == 1000
    assert saved.qualification_review["blocking"]
    row = (await db.scalars(select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == profile.id))).one()
    requested = await db.get(BucketRequestedDocument, row.requested_document_id)
    assert requested.status == "uploaded"
    payload = RequirementCollectionCompletionWrite(expected_fingerprint=saved.collection_completion["fingerprint"],
        idempotency_key="proceeds-review-1", reason="Reviewed the complete itemized budget")
    with pytest.raises(HTTPException) as wrong_source:
        await requirement_collection_completion.complete(db, profile, "use_of_proceeds",
            payload.model_copy(update={"file_ids": [uuid4()], "idempotency_key": "proceeds-wrong-source"}), actor)
    assert wrong_source.value.status_code == 422
    reviewed = (await requirement_collection_completion.complete(db, profile, "use_of_proceeds", payload, actor)).requirements[0]
    assert reviewed.status == "verified" and reviewed.collection_completion["complete"]
    assert reviewed.qualification_review["blocking"] and reviewed.qualification_review["status"] == "pending"
    assert row.provenance["collection_completion_receipt"]["qualification_approved"] is False
    from app.services.requirement_program_qualification import for_definitions

    scoped_conditions = for_definitions(row, [definition])
    assert scoped_conditions["blocking"] and scoped_conditions["status"] == "pending"
    assert await db.scalar(select(func.count()).select_from(ApplicationRequirementEvidence)) == 0
    assert audit.await_args.kwargs["metadata"]["source"] == "use_of_funds"
    replay = await requirement_collection_completion.complete(db, profile, "use_of_proceeds", payload, actor)
    assert replay.requirements[0].collection_completion["complete"]
    support = BucketFile(id=uuid4(), bucket_id=bucket.id, requested_document_id=requested.id,
        file_name="Supporting business costs.pdf", s3_key="fictional/support.pdf", status="uploaded",
        content_hash="c" * 64, created_at=datetime.now(UTC))
    analysis = BucketFileAnalysis(id=uuid4(), bucket_file_id=support.id, bucket_id=bucket.id,
        content_hash=support.content_hash, analysis_version=1, status="completed", classification="other",
        confidence="high", analysis={"key_facts": {}}, analyzed_at=datetime.now(UTC))
    db.add_all([support, analysis])
    await db.flush()
    inventory[0].append(support)
    inventory[1][support.id] = analysis
    with_support = (await readiness(db, profile)).requirements[0]
    assert with_support.collection_completion["complete"] and with_support.qualification_review["blocking"]
    verification = ApplicationRequirementPatch(action="verify", evidence_file_ids=[support.id],
        reason="Reviewed the supporting costs against the current allocation and permitted uses", confirmed=True)
    await application_programs.patch_requirement(db, profile, "use_of_proceeds", verification, actor)
    signed_off = (await readiness(db, profile)).requirements[0]
    assert signed_off.collection_completion["complete"]
    assert signed_off.qualification_review["status"] == "met" and not signed_off.qualification_review["blocking"]
    await application_programs.patch_requirement(db, profile, "use_of_proceeds", ApplicationRequirementPatch(
        action="failed", reason="The supporting costs do not satisfy the program", confirmed=True), actor)
    adverse = (await readiness(db, profile)).requirements[0]
    assert adverse.status == "failed" and adverse.collection_completion["complete"]
    assert adverse.qualification_review["blocking"]
    await application_programs.patch_requirement(db, profile, "use_of_proceeds", verification, actor)
    assert (await readiness(db, profile)).requirements[0].qualification_review["status"] == "met"
    prior_program_review = row.provenance["use_of_funds_program_review"].copy()
    await use_of_funds.update_budget(db, profile, UseOfFundsPatch(items=[{**_items()[0], "amount": "500.00"}], expected_revision=1), actor)
    partial = (await readiness(db, profile)).requirements[0]
    assert partial.status == "received_unverified" and not partial.collection_completion["complete"]
    assert not partial.collection_completion["can_complete"] and requested.status == "requested"
    assert partial.qualification_review["blocking"] and partial.qualification_review["status"] == "pending"
    assert row.provenance["use_of_funds_program_review"] == prior_program_review
    with pytest.raises(HTTPException) as stale:
        await requirement_collection_completion.complete(db, profile, "use_of_proceeds", payload.model_copy(update={"idempotency_key": "proceeds-review-2"}), actor)
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as incomplete:
        await requirement_collection_completion.complete(db, profile, "use_of_proceeds", payload.model_copy(update={
            "idempotency_key": "proceeds-review-3", "expected_fingerprint": partial.collection_completion["fingerprint"],
        }), actor)
    assert incomplete.value.status_code == 422
    await use_of_funds.update_budget(db, profile, UseOfFundsPatch(items=_items(), expected_revision=2), actor)
    restored = (await readiness(db, profile)).requirements[0]
    assert restored.qualification_review["blocking"] and not restored.collection_completion["complete"]
    await application_programs.patch_requirement(db, profile, "use_of_proceeds", verification, actor)
    reverified = (await readiness(db, profile)).requirements[0]
    assert reverified.qualification_review["status"] == "met" and not reverified.collection_completion["complete"]
    assert row.provenance["use_of_funds_program_review"]["fingerprint"] != prior_program_review["fingerprint"]
    assert row.provenance["use_of_funds_program_review_history"][-1] == prior_program_review
