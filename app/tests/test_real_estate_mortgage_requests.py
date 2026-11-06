from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.application_profile import ApplicationCollectionTask
from app.models.bucket import BucketRequestedDocument
from app.schemas.file_request import FileRequestItem
from app.services import application_programs, file_requests
from app.services import real_estate_mortgage_requests as requests


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class MemoryDB:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        for row in self.rows:
            if getattr(row, "id", None) is None:
                row.id = uuid4()
            if getattr(row, "created_at", None) is None:
                row.created_at = datetime.now(UTC)

    async def execute(self, query):
        model = query.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if isinstance(row, model)]
        for key, value in query.compile().params.items():
            if key.startswith("profile_id_"):
                rows = [row for row in rows if row.profile_id == value]
        return Result(rows)

    async def get(self, model, row_id):
        return next((row for row in self.rows if isinstance(row, model) and row.id == row_id), None)


def setup_schedule(*, property_status="active", loan_status="active", loan=True):
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None,
                              funding_category=None, intake_id=None)
    family = SimpleNamespace(id=uuid4(), profile_id=profile.id, kind=requests.FORM_KIND)
    prop = {"id": str(uuid4()), "address": "123 Main Street", "city": "Miami", "state": "FL",
            "mortgage_status": property_status}
    mortgage = {"id": str(uuid4()), "property_row_id": prop["id"], "lender": "Example Bank",
                "loan_reference": "1234", "status": loan_status, "balance": None}
    body = {"properties": [prop], "mortgages": [mortgage] if loan else []}
    version = save_version(family, body)
    return MemoryDB(), profile, family, version


def save_version(family, body, number=1, *, source="form", status="published"):
    version = SimpleNamespace(id=uuid4(), family_id=family.id, version_number=number,
                              body=deepcopy(body), source=source, status=status, actor_name="Client owner")
    family.current_version_id = version.id
    return version


@pytest.fixture
def workflow():
    with ExitStack() as stack:
        stack.enter_context(patch.object(file_requests, "lock_profile", AsyncMock()))
        uses_arf = stack.enter_context(patch("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False)))
        permissions = stack.enter_context(patch("app.services.arf_channel.require_hosted_permission", AsyncMock()))
        log = stack.enter_context(patch.object(file_requests.profiles, "log_profile_action", AsyncMock()))
        email = stack.enter_context(patch.object(file_requests.missing_item_automation, "send_requirements_email", AsyncMock()))
        yield SimpleNamespace(uses_arf=uses_arf, permissions=permissions, log=log, email=email)


@pytest.mark.asyncio
async def test_saved_active_mortgage_creates_one_exact_upload_task_with_truthful_actor(workflow):
    db, profile, family, version = setup_schedule()
    states = await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version)
    state = states[0]
    slot = await db.get(BucketRequestedDocument, state.requested_document_id)
    task = next(row for row in db.rows if isinstance(row, ApplicationCollectionTask))
    assert state.provenance["source_kind"] == "financial_form_request"
    assert state.provenance["requested_by_user_id"] is None
    assert state.provenance["publication_actor_name"] == "Client owner"
    assert state.provenance["source_context"]["mortgage_row_id"] == version.body["mortgages"][0]["id"]
    assert "123 Main Street" in slot.name and "Example Bank" in slot.name
    assert "1234" in slot.description
    assert task.action_type == "upload_documents"
    assert task.config["requested_document_ids"] == [str(slot.id)]
    assert task.config["explicit_assignment_only"] is True
    assert task.config["document_type"] == "mortgage_statement"
    assert task.assignee == "borrower" and task.automation_approved
    assert state.verification_required
    assert state.provenance["external_definition"]["completion_mode"] == "requires_human_verify"
    assert task.next_attempt_at is not None
    workflow.email.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_and_revised_saved_versions_preserve_completion_and_destination(workflow):
    db, profile, family, version = setup_schedule()
    first = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version))[0]
    first.status, first.evidence_file_id = "verified", uuid4()
    first.provenance = {**first.provenance, "verified_coverage": {"current": 1, "required": 1, "complete": True}}
    task = next(row for row in db.rows if isinstance(row, ApplicationCollectionTask))
    task.attempts = 2
    original_ids = first.id, first.requested_document_id, task.id, first.evidence_file_id
    body = deepcopy(version.body)
    body["properties"][0]["address"] = "123 Main St, corrected"
    body["mortgages"][0].update(lender="Example Bank LLC", balance="85000", statement_date="2026-11-01")
    revised = save_version(family, body, 2)
    for _ in range(2):
        current = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=revised))[0]
        assert (current.id, current.requested_document_id, task.id, current.evidence_file_id) == original_ids
        assert current.status == "verified"
        assert current.provenance["verified_coverage"]["current"] == 1
        assert task.status == "completed" and task.next_attempt_at is None
    assert len([row for row in db.rows if isinstance(row, BucketRequestedDocument)]) == 1
    assert "corrected" in current.label and "LLC" in current.label
    workflow.log.assert_awaited_once()
    workflow.email.assert_not_awaited()


@pytest.mark.asyncio
async def test_identical_lender_and_address_mortgages_are_separate_not_semantically_merged(workflow):
    db, profile, family, version = setup_schedule()
    other = {**version.body["mortgages"][0], "id": str(uuid4()), "loan_reference": "5678"}
    version.body["mortgages"].append(other)
    states = await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version)
    assert len({row.id for row in states}) == 2
    assert len({row.requested_document_id for row in states}) == 2
    assert len({row.provenance["canonical_key"] for row in states}) == 2
    assert len({row.label for row in states}) == 2
    assert any("1234" in row.label for row in states) and any("5678" in row.label for row in states)
    assert len([row for row in db.rows if isinstance(row, ApplicationCollectionTask)]) == 2


@pytest.mark.asyncio
async def test_active_property_missing_mortgage_details_still_collects_statement(workflow):
    db, profile, family, version = setup_schedule(loan=False)
    state = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version))[0]
    assert state.provenance["source_context"]["mortgage_row_id"] is None
    assert "not yet identified" in (await db.get(BucketRequestedDocument, state.requested_document_id)).description


@pytest.mark.asyncio
async def test_single_identified_mortgage_adopts_placeholder_without_losing_accepted_statement(workflow):
    db, profile, family, version = setup_schedule(loan=False)
    state = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version))[0]
    state.status, state.evidence_file_id = "verified", uuid4()
    task = next(row for row in db.rows if isinstance(row, ApplicationCollectionTask))
    ids = state.id, state.requested_document_id, task.id
    body = deepcopy(version.body)
    body["mortgages"] = [{"id": str(uuid4()), "property_row_id": body["properties"][0]["id"],
                           "status": "active", "lender": "Identified Bank"}]
    identified = save_version(family, body, 2)
    result = await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=identified)
    assert (result[0].id, result[0].requested_document_id, task.id) == ids
    assert result[0].status == "verified" and task.status == "completed"
    assert result[0].provenance["source_context"]["mortgage_row_id"] == body["mortgages"][0]["id"]
    assert len([row for row in db.rows if isinstance(row, ApplicationCollectionTask)]) == 1


@pytest.mark.asyncio
async def test_ambiguous_placeholder_does_not_fulfill_two_new_loans(workflow):
    db, profile, family, version = setup_schedule(loan=False)
    placeholder = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version))[0]
    placeholder.status, placeholder.evidence_file_id = "verified", uuid4()
    body = deepcopy(version.body)
    body["mortgages"] = [{"id": str(uuid4()), "property_row_id": body["properties"][0]["id"], "status": "active"} for _ in range(2)]
    identified = save_version(family, body, 2)
    states = await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=identified)
    assert len(states) == 2 and all(state.status == "requested" for state in states)
    assert all(state.evidence_file_id is None for state in states)
    assert placeholder.status == "not_applicable" and placeholder.evidence_file_id is not None


@pytest.mark.parametrize(("source", "status", "current"), [
    ("upload", "original", True), ("form", "draft", True), ("form", "published", False),
    ("import", "published", True),
])
@pytest.mark.asyncio
async def test_only_current_explicit_save_can_create_requests(source, status, current, workflow):
    db, profile, family, version = setup_schedule()
    version.source, version.status = source, status
    if not current:
        family.current_version_id = uuid4()
    assert await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version) == []
    assert db.rows == []


@pytest.mark.parametrize(("property_status", "loan_status", "balance", "expected"), [
    ("unknown", "active", None, 1), ("unknown", "unknown", "100", 1),
    ("unknown", "unknown", "0", 0), ("unknown", "unknown", None, 0),
    ("active", "unknown", None, 1), ("free_clear", "paid_off", "0", 0),
])
def test_shared_active_predicate_drives_collection_without_zero_paid_off_inference(property_status, loan_status, balance, expected):
    _, _, family, version = setup_schedule(property_status=property_status, loan_status=loan_status)
    version.body["mortgages"][0]["balance"] = balance
    assert len(requests.statement_requests(version.body, family.id)) == expected


@pytest.mark.parametrize("property_status", ["active", "unknown"])
def test_blank_unlinked_worksheet_mortgage_padding_never_crashes_collection(property_status):
    _, _, family, version = setup_schedule(property_status=property_status, loan=False)
    version.body["mortgages"] = [{"id": str(uuid4()), "lender": "", "status": "unknown"}, {}]
    items = requests.statement_requests(version.body, family.id)
    assert len(items) == (1 if property_status == "active" else 0)
    assert all(context["mortgage_row_id"] is None for context, _item in items)


@pytest.mark.asyncio
async def test_paid_off_then_restored_same_loan_preserves_accepted_completion(workflow):
    db, profile, family, version = setup_schedule()
    state = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version))[0]
    state.status, state.evidence_file_id = "verified", uuid4()
    retired_body = deepcopy(version.body)
    retired_body["properties"][0]["mortgage_status"] = "free_clear"
    retired_body["mortgages"][0]["status"] = "paid_off"
    retired = save_version(family, retired_body, 2)
    assert await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=retired) == []
    assert state.status == "not_applicable"
    restored = save_version(family, version.body, 3)
    active = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=restored))[0]
    assert active is state and active.status == "verified" and active.evidence_file_id is not None


@pytest.mark.asyncio
async def test_staff_cancellation_is_not_reopened_by_later_saved_schedule(workflow):
    db, profile, family, version = setup_schedule()
    state = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version))[0]
    await file_requests.cancel_batch(db, profile=profile,
        request_id=next(iter(file_requests.batch_memberships(state))), user=SimpleNamespace(id=uuid4()))
    later = save_version(family, version.body, 2)
    result = await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=later)
    assert result == [state] and state.status == "not_applicable"
    assert len([row for row in db.rows if isinstance(row, BucketRequestedDocument)]) == 1


@pytest.mark.asyncio
async def test_arf_permission_is_required_before_creating_a_borrower_mortgage_task(workflow):
    db, profile, family, version = setup_schedule()
    workflow.uses_arf.return_value = True
    workflow.permissions.side_effect = HTTPException(403, "Document collection is not authorized")
    with pytest.raises(HTTPException) as error:
        await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version)
    assert error.value.status_code == 403
    workflow.permissions.assert_awaited_once_with(db, requested_documents=["mortgage_statement"])
    assert db.rows == []


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.asyncio
async def test_arf_task_uses_document_type_permission_without_broadening_scope(allowed, workflow):
    db, profile, family, version = setup_schedule()
    profile.funding_category = "arf_external"
    config = SimpleNamespace(intake_mode="partner_hosted_authorized", permitted_documents=["mortgage_statement"] if allowed else ["bank_statement"],
                             permitted_fields=[])
    with patch("app.services.arf_channel.get_configuration", AsyncMock(return_value=config)), patch(
        "app.services.arf_channel.approval_valid", return_value=True
    ):
        await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version)
    task = next(row for row in db.rows if isinstance(row, ApplicationCollectionTask))
    assert task.automation_approved is allowed
    assert task.assignee == ("borrower" if allowed else "internal")


@pytest.mark.asyncio
async def test_unrelated_or_revised_files_cannot_double_fulfill_a_mortgage_task(workflow):
    db, profile, family, version = setup_schedule()
    states = await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version)
    state = states[0]
    requirement = SimpleNamespace(requirement_key=state.requirement_key, canonical_key=state.provenance["canonical_key"],
                                  collection_config=state.provenance["collection_config"])
    file = SimpleNamespace(id=uuid4(), content_hash="same-document", file_name="Mortgage.pdf", created_at=datetime.now(UTC),
                           statement_period=None, requested_document_id=state.requested_document_id)
    revision = SimpleNamespace(**{**vars(file), "id": uuid4()})
    other = SimpleNamespace(**{**vars(file), "id": uuid4(), "content_hash": "other-mortgage", "requested_document_id": uuid4()})
    analyses = {item.id: SimpleNamespace(classification="payoff_or_mortgage_statement", status="completed", analysis={}) for item in (file, revision, other)}
    matches, complete, coverage = application_programs._matching_evidence_files(
        requirement, SimpleNamespace(id=state.requested_document_id), [file, revision, other], analyses)
    assert {row.id for row in matches} == {file.id, revision.id}
    assert complete and coverage["current"] == 1 and coverage["required"] == 1


@pytest.mark.asyncio
async def test_wrong_profile_cannot_create_tasks_in_another_room(workflow):
    db, profile, family, version = setup_schedule()
    family.profile_id = uuid4()
    with pytest.raises(HTTPException) as error:
        await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version)
    assert error.value.status_code == 409 and not db.rows


@pytest.mark.asyncio
async def test_readiness_refresh_retains_mortgage_definition_and_account_upload_todo(workflow):
    db, profile, family, version = setup_schedule()
    state = (await requests.sync_published_mortgage_requests(db, profile=profile, family=family, version=version))[0]
    slot = await db.get(BucketRequestedDocument, state.requested_document_id)
    with patch.object(application_programs, "_evidence_inventory", AsyncMock(return_value=([], {}, set()))), patch.object(
        application_programs, "_accepted_entity_aliases", AsyncMock(return_value=set())
    ), patch.object(application_programs, "_requested_document", AsyncMock(return_value=slot)), patch.object(
        application_programs, "_sync_requirement_evidence", AsyncMock(return_value=[])
    ), patch.object(application_programs, "_reconcile_evidence_decisions", AsyncMock(return_value={})):
        for _ in range(2):
            states, _ = await application_programs._materialize_requirements(db, profile, [], {}, [], {})
            assert states == [state]
            assert state.provenance["source_kind"] == "financial_form_request"
            assert state.provenance["source_context"]["mortgage_row_id"] == version.body["mortgages"][0]["id"]
            assert state.provenance["collection_config"]["document_type"] == "mortgage_statement"
            assert not state.provenance["inactive_definition"]
    projection = SimpleNamespace(requirement_key=state.requirement_key, label=state.label, state_reason=state.state_reason,
        status=state.status, requested_document_id=slot.id, client_visible=True, provenance=state.provenance)
    with patch.object(application_programs, "get_program_readiness", AsyncMock(return_value=SimpleNamespace(requirements=[projection]))), patch(
        "app.dealer_os.services.client_room.active_link", AsyncMock(return_value=SimpleNamespace(token="scoped-token"))
    ):
        todos = await file_requests.loan_todo_items(db, profile, want_pending=True, want_completed=False)
    assert len(todos) == 1
    assert todos[0]["deeplink"] == f"/buckets/request/scoped-token?tab=todo&request={slot.id}"
    assert todos[0]["title"] == state.label


@pytest.mark.parametrize(("quantity", "unit", "action", "kind"), [
    (1, "files", "complete_form", "form"), (2, "files", "upload_documents", "document"),
    (1, "years", "upload_documents", "document"),
])
@pytest.mark.asyncio
async def test_real_estate_file_request_online_form_action_only_for_one_form(quantity, unit, action, kind, workflow):
    db, profile, _, _ = setup_schedule()
    state = (await file_requests.create_batch(db, profile=profile, request_id=uuid4(), user=None,
        items=[FileRequestItem(document_type="real_estate_schedule", name="Real estate schedule", quantity=quantity, unit=unit)]))[0]
    task = next(row for row in db.rows if isinstance(row, ApplicationCollectionTask))
    assert state.provenance["requirement_kind"] == kind
    assert state.provenance["external_definition"]["requirement_kind"] == kind
    assert task.action_type == action
    assert task.config.get("form_kind") == ("real_estate_schedule" if kind == "form" else None)
    assert task.config["evidence_classes"] == ["real_estate_schedule"]


@pytest.mark.asyncio
async def test_stronger_real_estate_file_request_switches_to_upload_without_single_form_shortcut(workflow):
    db, profile, _, _ = setup_schedule()
    item = FileRequestItem(document_type="real_estate_schedule", name="Real estate schedule")
    state = (await file_requests.create_batch(db, profile=profile, request_id=uuid4(), items=[item]))[0]
    await file_requests.create_batch(db, profile=profile, request_id=uuid4(), items=[item.model_copy(update={"quantity": 2})])
    assert state.provenance["requirement_kind"] == "document"
    assert "form_kind" not in state.provenance["collection_config"]
    tasks = [row for row in db.rows if isinstance(row, ApplicationCollectionTask) and row.status != "cancelled"]
    assert len(tasks) == 1 and tasks[0].action_type == "upload_documents"
    assert tasks[0].config["minimum_count"] == 2
