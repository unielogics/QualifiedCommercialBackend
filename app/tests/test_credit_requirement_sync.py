from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.models.application_profile import ApplicationCollectionTask, ApplicationRequirementState
from app.services import application_profiles, application_programs, collection_tasks
from app.services import credit_requirement_sync as sync

NOW = datetime.now(UTC)


def profile(**changes):
    return SimpleNamespace(
        **{"id": uuid4(), "dealer_id": None, "intake_id": None, "client_id": uuid4(), **changes}
    )


def owner(**changes):
    return SimpleNamespace(
        **{
            "id": uuid4(),
            "first_name": "Pat",
            "last_name": "Owner",
            "ownership_pct": 100,
            "dob": date(1980, 1, 1),
            "email": "pat@example.test",
            "credit_pull_id": uuid4(),
            "archived_at": None,
            "backfill_needs_review": False,
            **changes,
        }
    )


def pull(person, **changes):
    return SimpleNamespace(
        **{
            "id": person.credit_pull_id,
            "application_owner_id": person.id,
            "client_id": uuid4(),
            "legal_first_name": person.first_name,
            "legal_last_name": person.last_name,
            "email": person.email,
            "dob": person.dob,
            "fcra_consent": True,
            "status": "completed",
            "fico": 661,
            "notes": "fico_source=scraped:fico_4",
            "pulled_at": NOW - timedelta(days=6),
            "expires_at": NOW + timedelta(days=84),
            "bureau_response": {
                "report_import_status": "completed",
                "report_s3_key": "report-key",
                "parsed_report": {"scores": [{"model": "TransUnion FICO 4", "score": 661}]},
            },
            **changes,
        }
    )


def requirement(**changes):
    return SimpleNamespace(
        **{
            "id": uuid4(),
            "requirement_key": "guarantor_credit_review",
            "canonical_key": None,
            "collection_config": {},
            "expiration_days": None,
            "review_checks": [{"key": "minimum", "value": 680}],
            "completion_criteria": "Equifax FICO 9, every 20%+ owner",
            "verification_required": True,
            **changes,
        }
    )


def state(prof, **changes):
    return ApplicationRequirementState(
        **{
            "id": uuid4(),
            "profile_id": prof.id,
            "requirement_key": "guarantor_credit_review",
            "label": "Soft credit pull",
            "category": "credit",
            "required_level": "required",
            "status": "requested",
            "source_program_keys": [],
            "source_policy_keys": [],
            "verification_required": True,
            "provenance": {
                "requirement_kind": "document",
                "canonical_key": "guarantor_credit_review",
                "catalog_approved": True,
                "client_visible": True,
                "collection_config": {},
            },
            **changes,
        }
    )


class Rows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


def snapshot(owners, reports, req=None):
    return sync.collection_snapshot(
        profile(), owners, {str(row.id): row for row in reports}, req or requirement(), now=NOW
    )


def test_existing_imported_owner_report_is_collected_without_passing_different_model_score():
    person = owner(backfill_needs_review=True)
    result = snapshot([person], [pull(person)])
    assert result["complete"] is True
    assert result["collected_owner_count"] == result["required_owner_count"] == 1
    assert result["client_action_required"] is False
    assert result["owners"][0]["score"] == 661
    assert result["owners"][0]["required_score_model_present"] is False
    assert result["owner_review_required"] is True
    assert any("different score or model" in reason for reason in result["staff_review_reasons"])
    assert any("imported owner record" in reason for reason in result["staff_review_reasons"])
    assert not {"dob", "email", "last4_ssn", "report_s3_key"}.intersection(result["owners"][0])


def test_every_twenty_percent_owner_is_required_not_the_primary_only():
    first, second, small = (
        owner(ownership_pct=60),
        owner(ownership_pct=20, credit_pull_id=None),
        owner(ownership_pct=20),
    )
    result = snapshot([first, second, small], [pull(first), pull(small)])
    assert result["required_owner_count"] == 3
    assert result["collected_owner_count"] == 2
    assert result["complete"] is False and result["client_action_required"] is True
    second.ownership_pct, small.ownership_pct = 19, 21
    result = snapshot([first, second, small], [pull(first), pull(small)])
    assert result["required_owner_count"] == result["collected_owner_count"] == 2
    assert result["complete"] is True


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"status": "pending"}, "processing"),
        ({"status": "revoked"}, "needs_review"),
        ({"fcra_consent": False}, "needs_review"),
        ({"expires_at": NOW - timedelta(seconds=1)}, "stale"),
        ({"pulled_at": NOW - timedelta(days=91), "expires_at": None}, "stale"),
        ({"pulled_at": None}, "needs_review"),
        ({"pulled_at": NOW + timedelta(hours=1)}, "needs_review"),
        ({"legal_first_name": "Other"}, "needs_review"),
        ({"dob": date(1981, 1, 1)}, "needs_review"),
        (
            {"bureau_response": {"report_import_status": "pending", "report_s3_key": "key"}},
            "processing",
        ),
        ({"bureau_response": {}}, "processing"),
    ],
)
def test_ineligible_source_is_internal_attention_never_duplicate_client_collection(changes, status):
    person = owner()
    result = snapshot([person], [pull(person, **changes)])
    assert result["complete"] is False
    assert result["owners"][0]["status"] == status
    assert result["client_action_required"] is False
    assert result["staff_review_reasons"]


def test_subject_and_explicit_owner_scope_do_not_use_another_owners_report():
    first, second = owner(ownership_pct=60), owner(ownership_pct=40)
    report = pull(first)
    second.credit_pull_id = first.credit_pull_id
    second.first_name = "Other"
    result = snapshot([first, second], [report])
    assert result["collected_owner_count"] == 0
    assert result["complete"] is False
    scoped = snapshot(
        [first, second], [report], requirement(collection_config={"owner_id": str(first.id)})
    )
    assert scoped["complete"] is True and scoped["required_owner_count"] == 1
    missing = snapshot([first], [report], requirement(collection_config={"owner_id": str(uuid4())}))
    assert missing["complete"] is False and missing["ownership_complete"] is False


def test_legacy_same_named_owners_cannot_count_the_same_report_twice():
    first, second = owner(ownership_pct=50), owner(ownership_pct=50)
    second.credit_pull_id = first.credit_pull_id
    result = snapshot([first, second], [pull(first)])
    assert result["collected_owner_count"] == 0 and result["complete"] is False
    assert all(
        row["status"] == "needs_review" and row["report_available"] for row in result["owners"]
    )
    assert result["client_action_required"] is False
    assert any("same report" in text for text in result["staff_review_reasons"])


def test_ownership_scope_unknown_and_archived_owners_cannot_make_credit_complete():
    person = owner(ownership_pct=50)
    assert snapshot([person], [pull(person)])["complete"] is False
    person.ownership_pct = None
    assert snapshot([person], [pull(person)])["complete"] is False
    person.ownership_pct, person.archived_at = 100, NOW
    assert snapshot([person], [pull(person)])["required_owner_count"] == 0


@pytest.mark.asyncio
async def test_known_roster_without_twenty_percent_owner_is_internal_applicability_review(
    monkeypatch,
):
    prof, req = profile(), requirement()
    owners = [owner(ownership_pct=19, credit_pull_id=None) for _ in range(5)] + [
        owner(ownership_pct=5, credit_pull_id=None)
    ]
    row, requested = state(prof), SimpleNamespace(status="requested", requires_signature=False)
    monkeypatch.setattr(application_profiles, "owner_rows", AsyncMock(return_value=owners))
    db = SimpleNamespace(scalars=AsyncMock())
    assert await sync.materialize(db, prof, req, row, requested) is True
    assert row.status == "received_unverified" and requested.status == "uploaded"
    assert row.provenance["coverage"] == {
        "current": 0,
        "required": 0,
        "unit": "owner reports",
        "complete": True,
    }
    assert row.provenance["verified_coverage"]["complete"] is False
    assert row.provenance["credit_collection"]["client_action_required"] is False
    assert "applicability" in row.state_reason and "No client credit upload" in row.state_reason
    db.scalars.assert_not_awaited()
    previous = row.provenance["credit_collection"]["fingerprint"]
    reviewer = uuid4()
    row.status, row.verified_at, row.verified_by_user_id = "verified", NOW, reviewer
    owners[0].ownership_pct, owners[-1].ownership_pct = 18, 6
    await sync.materialize(db, prof, req, row, requested)
    assert row.status == "received_unverified" and row.verified_by_user_id is None
    assert row.provenance["credit_collection"]["fingerprint"] != previous
    owners[0].ownership_pct = None
    assert sync.collection_snapshot(prof, owners, {}, req)["complete"] is False


def test_published_expiry_can_be_stricter_and_missing_or_naive_dates_are_handled():
    person = owner()
    report = pull(person, pulled_at=(NOW - timedelta(days=6)).replace(tzinfo=None), expires_at=None)
    assert snapshot([person], [report])["complete"] is True
    assert (
        snapshot([person], [report], requirement(expiration_days=5))["owners"][0]["status"]
        == "stale"
    )


def test_matching_equifax_model_is_evidence_only_not_automatic_program_acceptance():
    person = owner()
    report = pull(
        person,
        fico=700,
        bureau_response={
            "parsed_report": {"scores": [{"model": "Equifax FICO Score 9", "score": 700}]}
        },
    )
    result = snapshot([person], [report])
    assert result["complete"] is True
    assert result["owners"][0]["required_score_model_present"] is True
    assert result["owner_review_required"] is True


def test_proxy_score_cannot_supply_a_missing_required_model():
    person = owner()
    report = pull(
        person,
        fico=720,
        notes="fico_source=intelligence-proxy",
        bureau_response={"report_s3_key": "key"},
    )
    item = snapshot([person], [report])["owners"][0]
    assert item["status"] == "collected"
    assert item["score_verified"] is False and item["required_score_model_present"] is False


@pytest.mark.parametrize(
    "scores,present",
    [
        ([{"model": "FICO 9", "score": 700, "bureau": "Equifax"}], True),
        ([{"model": "FICO 9", "score": 700, "bureau": "TransUnion"}], False),
        ([{"model": "FICO 9", "score": 700}], False),
        ([{"model": "Equifax FICO 9", "score": 700, "bureau": "TransUnion"}], False),
        ([{"model": "Equifax FICO 9", "score": None}], False),
        ([{"model": "Equifax FICO 9", "score": True}], False),
        ([{"model": "Equifax FICO 9", "score": 999}], False),
        (
            [
                {"model": "FICO 9", "score": 700},
                {"model": "FICO 4", "score": 680, "bureau": "Equifax"},
            ],
            False,
        ),
    ],
)
def test_bureau_provenance_stays_bound_to_its_actual_score_model(scores, present):
    person = owner()
    report = pull(person, bureau_response={"parsed_report": {"scores": scores}})
    item = snapshot([person], [report])["owners"][0]
    assert item["required_score_model_present"] is present
    if scores[0].get("bureau"):
        assert item["score_models"][0]["bureau"] == scores[0]["bureau"]


def test_full_feed_preserves_explicit_bureau_even_when_older_parser_dropped_it():
    person = owner()
    report = pull(
        person,
        bureau_response={
            "parsed_report": {"scores": [{"model": "fico_9", "score": 700}]},
            "full_feed": {"credit_score": {"fico_9": {"score": "700", "bureau": "Equifax"}}},
        },
    )
    item = snapshot([person], [report])["owners"][0]
    assert item["required_score_model_present"] is True
    assert any(model.get("bureau") == "Equifax" for model in item["score_models"])


def test_other_published_credit_requirements_do_not_invent_equifax_fico9_gap():
    person = owner()
    req = requirement(
        completion_criteria="Review the linked TransUnion FICO 4 report", review_checks=[]
    )
    result = snapshot([person], [pull(person)], req)
    assert result["complete"] is True
    assert result["owners"][0]["required_score_model"] is None
    assert not any("required Equifax" in text for text in result["staff_review_reasons"])
    req.collection_config = {"credit_bureau": "Equifax", "credit_model": "fico_9"}
    assert (
        snapshot([person], [pull(person)], req)["owners"][0]["required_score_model"]
        == "Equifax FICO 9"
    )


@pytest.mark.parametrize(
    "req",
    [
        requirement(requirement_key="signed_credit_authorization"),
        requirement(requirement_key="some_credit_report"),
        requirement(collection_config={"period": "2024"}),
        requirement(collection_config={"business_id": str(uuid4())}),
    ],
)
def test_only_exact_current_scoped_requirement_is_supported(req):
    assert sync.supports(req, profile()) is False


def test_credit_collection_scope_accepts_only_exact_current_subject():
    prof = profile()
    req = requirement(collection_config={"subject": "Different Business LLC"})
    assert sync.supports(req, prof) is False
    assert sync.supports(req, prof, expected_subjects=["Current Business LLC"]) is False
    req.collection_config = {"subject": "  CURRENT Business LLC  "}
    assert sync.supports(req, prof, expected_subjects=["Current Business LLC"]) is True
    req.collection_config = {"subject": "business", "business_id": str(prof.id)}
    assert sync.supports(req, prof) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [{"subject": "Different Business LLC"}, {"period": "2024"}, {"business_id": str(uuid4())}],
)
async def test_changed_definition_clears_prior_credit_receipt_before_file_evidence_fallback(
    monkeypatch, scope
):
    prof, person, req = profile(), owner(), requirement()
    report, row = pull(person), state(prof)
    db = SimpleNamespace(scalars=AsyncMock(return_value=Rows([report])))
    monkeypatch.setattr(application_profiles, "owner_rows", AsyncMock(return_value=[person]))
    await sync.materialize(db, prof, req, row, None)
    evidence_id = uuid4()
    row.evidence_file_id = evidence_id
    row.status, row.verified_at, row.verified_by_user_id = "verified", NOW, uuid4()
    req.collection_config = scope
    assert await sync.materialize(db, prof, req, row, None) is False
    assert row.status == "received_unverified" and row.verified_by_user_id is None
    assert row.evidence_file_id == evidence_id
    assert not {"credit_collection", "coverage", "verified_coverage", "source"}.intersection(
        row.provenance
    )
    # An independent waiver is preserved when source association stops applying.
    req.collection_config = {}
    await sync.materialize(db, prof, req, row, None)
    row.status = "waived"
    req.collection_config = scope
    assert await sync.materialize(db, prof, req, row, None) is False
    assert row.status == "waived"


@pytest.mark.asyncio
async def test_materialization_and_collection_reuse_task_and_stop_borrower_reminders(monkeypatch):
    prof, person, req = profile(), owner(backfill_needs_review=True), requirement()
    report = pull(person)
    row = state(prof)
    requested = SimpleNamespace(status="requested", requires_signature=False)
    db = SimpleNamespace(scalars=AsyncMock(return_value=Rows([report])))
    monkeypatch.setattr(application_profiles, "owner_rows", AsyncMock(return_value=[person]))
    assert await sync.materialize(db, prof, req, row, requested) is True
    assert row.status == "received_unverified" and row.verified_at is None
    assert requested.status == "uploaded"
    assert row.provenance["coverage"] == {
        "current": 1,
        "required": 1,
        "unit": "owner reports",
        "complete": True,
    }
    assert row.provenance["verified_coverage"]["complete"] is False
    task = ApplicationCollectionTask(
        id=uuid4(),
        profile_id=prof.id,
        canonical_identity=collection_tasks.canonical_identity(
            "guarantor_credit_review", "document", {}
        ),
        status="escalated",
        assignee="internal",
        action_type="upload_documents",
        label="Credit",
        attempts=3,
        max_attempts=3,
        cadence_hours=24,
        next_attempt_at=NOW,
        config={},
    )
    db.execute, db.flush, db.add = (
        AsyncMock(side_effect=[Rows([task]), Rows([])]),
        AsyncMock(),
        Mock(),
    )
    monkeypatch.setattr(
        application_programs, "profile_requirement_dispositions", AsyncMock(return_value={})
    )
    result = (await collection_tasks.sync_collection_tasks(db, prof, [row]))[0]
    assert result is task and result.status == "in_review" and result.assignee == "internal"
    assert result.next_attempt_at is None and result.attempts == 3
    assert result.action_type == "upload_documents"
    assert row.provenance["collection_status"] == "in_review"
    public = collection_tasks.task_read(result, public=True)
    assert "No duplicate" in public["description"]
    assert "credit_collection" not in public["config"]


@pytest.mark.asyncio
async def test_staff_acceptance_is_bound_to_same_report_and_definition(monkeypatch):
    prof, person, req = profile(), owner(), requirement()
    report, requested = pull(person), SimpleNamespace(status="requested", requires_signature=False)
    row = state(prof)
    db = SimpleNamespace(scalars=AsyncMock(return_value=Rows([report])))
    monkeypatch.setattr(application_profiles, "owner_rows", AsyncMock(return_value=[person]))
    await sync.materialize(db, prof, req, row, requested)
    reviewer = uuid4()
    row.status, row.verified_at, row.verified_by_user_id = "verified", NOW, reviewer
    await sync.materialize(db, prof, req, row, requested)
    assert row.status == "verified" and row.verified_by_user_id == reviewer
    report.bureau_response = {
        **report.bureau_response,
        "parsed_report": {"scores": [{"model": "FICO 4", "score": 662}]},
    }
    await sync.materialize(db, prof, req, row, requested)
    assert row.status == "received_unverified" and row.verified_by_user_id is None
    row.status, row.verified_at, row.verified_by_user_id = "verified", NOW, reviewer
    req.review_checks = [{"key": "new-minimum", "value": 700}]
    await sync.materialize(db, prof, req, row, requested)
    assert row.status == "received_unverified" and row.verified_by_user_id is None


@pytest.mark.asyncio
async def test_staff_credit_failure_remains_until_source_or_policy_changes(monkeypatch):
    prof, person, req = profile(), owner(), requirement()
    report, row = pull(person), state(prof)
    db = SimpleNamespace(scalars=AsyncMock(return_value=Rows([report])))
    monkeypatch.setattr(application_profiles, "owner_rows", AsyncMock(return_value=[person]))
    await sync.materialize(db, prof, req, row, None)
    row.status, row.state_reason = (
        "failed",
        "Staff found the actual required-model score below the published minimum",
    )
    await sync.materialize(db, prof, req, row, None)
    assert row.status == "failed" and row.state_reason.startswith("Staff found")
    req.review_checks = [{"key": "updated-source-policy"}]
    await sync.materialize(db, prof, req, row, None)
    assert row.status == "received_unverified"


@pytest.mark.asyncio
async def test_unlink_returns_to_file_evidence_fallback_and_keeps_waiver(monkeypatch):
    prof, person = profile(), owner()
    row = state(prof, status="waived")
    report = pull(person)
    db = SimpleNamespace(scalars=AsyncMock(return_value=Rows([report])))
    monkeypatch.setattr(application_profiles, "owner_rows", AsyncMock(return_value=[person]))
    await sync.materialize(db, prof, requirement(), row, None)
    assert row.status == "waived"
    person.credit_pull_id = None
    assert await sync.materialize(db, prof, requirement(), row, None) is False
    assert "credit_collection" not in row.provenance


@pytest.mark.asyncio
async def test_internal_review_task_rejects_borrower_response():
    from fastapi import HTTPException

    prof = profile()
    task = SimpleNamespace(
        profile_id=prof.id, status="in_review", assignee="internal", automation_approved=True
    )
    with pytest.raises(HTTPException) as error:
        await collection_tasks.answer_task(
            AsyncMock(), prof, task, value="done", source="borrower_room"
        )
    assert error.value.status_code == 404


@pytest.mark.parametrize("status", ["received_unverified", "failed", "stale"])
def test_linked_credit_staff_review_never_needs_another_client_submission(status):
    from app.services import missing_item_automation

    row = state(profile(), status=status)
    row.provenance = {
        "credit_collection": {
            "source": "linked_owner_credit_pulls",
            "client_action_required": False,
        },
        "coverage": {"complete": False},
    }
    assert sync.staff_review_only(row)
    assert application_programs._requirement_needs_client_evidence(row) is False
    assert missing_item_automation._client_submission_needed(row) is False
    row.provenance["credit_collection"]["client_action_required"] = True
    assert application_programs._requirement_needs_client_evidence(row) is True
    assert missing_item_automation._client_submission_needed(row) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("status", ["received_unverified", "failed", "stale"])
async def test_manual_credit_request_email_cannot_resend_internal_review_item(
    monkeypatch, batch, status
):
    from fastapi import HTTPException

    from app.services import missing_item_automation

    prof = profile()
    prof.client_id = None
    row = SimpleNamespace(
        requirement_key="guarantor_credit_review",
        client_visible=True,
        required_level="required",
        status=status,
        coverage_complete=False,
        provenance={
            "credit_collection": {
                "source": "linked_owner_credit_pulls",
                "client_action_required": False,
            }
        },
    )
    readiness = SimpleNamespace(
        requirements=[row],
        programs=[SimpleNamespace(blocking_requirement_keys=[row.requirement_key])],
    )
    monkeypatch.setattr(
        missing_item_automation, "get_program_readiness", AsyncMock(return_value=readiness)
    )
    send = AsyncMock(
        side_effect=AssertionError(
            "an internal credit review must not request another client report"
        )
    )
    monkeypatch.setattr(missing_item_automation, "send_as_user", send)
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        if batch:
            await missing_item_automation.send_requirements_email(
                db,
                profile=prof,
                requirement_keys=[row.requirement_key],
                user=None,
                initiation_source="staff_requirement_batch_request",
            )
        else:
            await missing_item_automation.send_requirement_email(
                db,
                profile=prof,
                requirement_key=row.requirement_key,
                user=None,
                initiation_source="staff_requirement_request",
            )
    assert error.value.status_code == 409
    send.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_readiness_materializer_uses_owner_report_instead_of_requesting_bucket_upload(
    monkeypatch,
):
    from app.services import document_associations, requirement_lifecycle

    prof, person, req = profile(), owner(), requirement()
    report, row = pull(person), None
    row = state(prof)
    req.playbook_id, req.label, req.category = uuid4(), "Soft credit pull", "credit"
    req.required_level, req.requirement_kind = "required", "document"
    req.visibility, req.completion_mode = ["borrower", "underwriter"], "requires_human_verify"
    req.objective_text, req.ai_request_message_template = "Review required owner credit", None
    selection = SimpleNamespace(
        id=uuid4(), playbook_id=req.playbook_id, playbook_version=1, program_key="published_program"
    )
    requested = SimpleNamespace(id=uuid4(), requires_signature=False, status="requested")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Rows([row])),
        scalars=AsyncMock(return_value=Rows([report])),
        get=AsyncMock(return_value=None),
        flush=AsyncMock(),
        add=Mock(),
    )
    monkeypatch.setattr(application_profiles, "owner_rows", AsyncMock(return_value=[person]))
    monkeypatch.setattr(
        application_programs, "_evidence_inventory", AsyncMock(return_value=([], {}, set()))
    )
    monkeypatch.setattr(
        application_programs, "_accepted_entity_aliases", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        application_programs, "_requested_document", AsyncMock(return_value=requested)
    )
    monkeypatch.setattr(document_associations, "expected_business", AsyncMock(return_value=None))
    monkeypatch.setattr(document_associations, "load_context", AsyncMock(return_value={}))
    monkeypatch.setattr(requirement_lifecycle, "reconcile", AsyncMock())
    monkeypatch.setattr(collection_tasks, "latest_responses", AsyncMock(return_value={}))
    tasks = AsyncMock()
    monkeypatch.setattr(collection_tasks, "sync_collection_tasks", tasks)
    evidence = Mock(
        side_effect=AssertionError("owner reports must not require a second bucket upload")
    )
    monkeypatch.setattr(application_programs, "_matching_evidence_files", evidence)
    result, by_selection = await application_programs._materialize_requirements(
        db,
        prof,
        [selection],
        {req.playbook_id: [req]},
        [],
        {},
    )
    assert result == [row] and by_selection[selection.id] == [req.requirement_key]
    assert (
        row.status == "received_unverified"
        and row.provenance["credit_collection"]["complete"] is True
    )
    assert requested.status == "uploaded"
    tasks.assert_awaited_once_with(db, prof, [row])
    evidence.assert_not_called()


@pytest.mark.asyncio
async def test_effective_program_exemption_completes_task_without_rewriting_requirement(
    monkeypatch,
):
    prof = profile()
    row = state(prof)
    task = ApplicationCollectionTask(
        id=uuid4(),
        profile_id=prof.id,
        canonical_identity=collection_tasks.canonical_identity(
            "guarantor_credit_review", "document", {}
        ),
        status="requested",
        assignee="borrower",
        action_type="upload_documents",
        label="Credit",
        attempts=2,
        max_attempts=3,
        cadence_hours=24,
        next_attempt_at=NOW,
        config={},
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Rows([task]), Rows([])]), flush=AsyncMock(), add=Mock()
    )
    monkeypatch.setattr(
        application_programs,
        "profile_requirement_dispositions",
        AsyncMock(
            return_value={
                row.requirement_key: {"status": "waived", "reason": "Waived for current program"}
            }
        ),
    )
    result = (await collection_tasks.sync_collection_tasks(db, prof, [row]))[0]
    assert result.status == "completed" and result.next_attempt_at is None
    assert result.attempts == 2 and row.status == "requested"
