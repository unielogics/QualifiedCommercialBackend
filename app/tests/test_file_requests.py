from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.application_profile import ApplicationRequirementState, ApplicationRoomDelivery
from app.models.bucket import BucketRequestedDocument
from app.schemas.file_request import FileRequestBatchCreate, FileRequestItem
from app.services import application_programs, collection_tasks, file_requests


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar


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

    async def execute(self, statement):
        model = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if isinstance(row, model)]
        params = statement.compile().params
        # Exercise profile/id scoping and idempotency matching in actual queries.
        for attr in ("profile_id", "idempotency_key"):
            value = next((v for k, v in params.items() if k.startswith(f"{attr}_")), None)
            if value is not None:
                rows = [row for row in rows if getattr(row, attr) == value]
        return Result(rows, rows[0] if rows else None)

    async def get(self, model, row_id):
        return next((row for row in self.rows if isinstance(row, model) and row.id == row_id), None)


def profile():
    return SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None, intake_id=None,
                           missing_item_email_last_sent_at=None, missing_item_email_next_send_at=None)


@pytest.mark.parametrize(("unit", "quantity"), [("files", 101), ("months", 61), ("years", 11), ("files", 0)])
def test_request_bounds_enforce_real_collection_limits(unit, quantity):
    with pytest.raises(ValidationError):
        FileRequestItem(name="Statements", quantity=quantity, unit=unit)


def test_empty_batch_rejected():
    with pytest.raises(ValidationError):
        FileRequestBatchCreate(request_id=uuid4(), items=[])


@pytest.mark.asyncio
async def test_batch_replay_creates_one_set_of_canonical_requests():
    db, owner, request_id = MemoryDB(), profile(), uuid4()
    user = SimpleNamespace(id=uuid4())
    items = [FileRequestItem(document_type="bank_statement", name="Business statements", quantity=6, unit="months"),
             FileRequestItem(document_type="tax_return", name="Business tax returns", quantity=2, unit="years")]
    with patch("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False)), patch.object(
        collection_tasks, "sync_collection_tasks", AsyncMock(return_value=[])
    ), patch.object(file_requests.profiles, "log_profile_action", AsyncMock()) as log:
        first = await file_requests.create_batch(db, profile=owner, request_id=request_id, items=items, user=user)
        second = await file_requests.create_batch(db, profile=owner, request_id=request_id, items=items, user=user)
        assert [row.id for row in first] == [row.id for row in second]
        assert len([row for row in db.rows if isinstance(row, BucketRequestedDocument)]) == 2
        assert len([row for row in db.rows if isinstance(row, ApplicationRequirementState)]) == 2
        log.assert_awaited_once()
        assert first[0].provenance["collection_config"]["required_months"] == 6
        with pytest.raises(HTTPException) as changed:
            await file_requests.create_batch(db, profile=owner, request_id=request_id,
                items=[FileRequestItem(name="Changed request")], user=user)
        assert changed.value.status_code == 409


@pytest.mark.asyncio
async def test_same_client_request_id_is_namespaced_to_profile():
    request_id, user = uuid4(), SimpleNamespace(id=uuid4())
    ids = []
    with patch("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False)), patch.object(
        collection_tasks, "sync_collection_tasks", AsyncMock(return_value=[])
    ), patch.object(file_requests.profiles, "log_profile_action", AsyncMock()):
        for _ in range(2):
            rows = await file_requests.create_batch(MemoryDB(), profile=profile(), request_id=request_id,
                items=[FileRequestItem(name="Statements")], user=user)
            ids.append(rows[0].id)
    assert ids[0] != ids[1]


@pytest.mark.asyncio
async def test_equivalent_batches_share_one_destination_and_strictest_requirement():
    db, owner, user = MemoryDB(), profile(), SimpleNamespace(id=uuid4())
    first_id, second_id = uuid4(), uuid4()
    item = FileRequestItem(document_type="bank_statement", name="Business statements", quantity=2, unit="months")
    with (patch("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False)),
          patch.object(collection_tasks, "sync_collection_tasks", AsyncMock(return_value=[])),
          patch.object(file_requests.profiles, "log_profile_action", AsyncMock())):
        first = await file_requests.create_batch(db, profile=owner, request_id=first_id, items=[item], user=user)
        second = await file_requests.create_batch(db, profile=owner, request_id=second_id, items=[item.model_copy(update={"quantity": 3})], user=user)
        assert first[0].id == second[0].id
        assert len([row for row in db.rows if isinstance(row, BucketRequestedDocument)]) == 1
        assert len([row for row in db.rows if isinstance(row, ApplicationRequirementState)]) == 1
        assert first[0].provenance["collection_config"]["required_months"] == 3
        replay = await file_requests.create_batch(db, profile=owner, request_id=first_id, items=[item], user=user)
        assert replay[0].id == first[0].id
        await file_requests.cancel_batch(db, profile=owner, request_id=second_id, user=user)
        assert first[0].status != "not_applicable"
        assert first[0].provenance["collection_config"]["required_months"] == 2
        await file_requests.cancel_batch(db, profile=owner, request_id=first_id, user=user)
        assert first[0].status == "not_applicable"


@pytest.mark.asyncio
async def test_specific_free_text_reporting_window_requires_review_before_completion():
    db, owner, user = MemoryDB(), profile(), SimpleNamespace(id=uuid4())
    item = FileRequestItem(document_type="bank_statement", name="Business statements", quantity=2,
                           unit="months", period="May and June 2026")
    with (patch("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False)),
          patch.object(collection_tasks, "sync_collection_tasks", AsyncMock(return_value=[])),
          patch.object(file_requests.profiles, "log_profile_action", AsyncMock())):
        states = await file_requests.create_batch(db, profile=owner, request_id=uuid4(), items=[item], user=user)
    assert states[0].verification_required
    assert states[0].provenance["external_definition"]["completion_mode"] == "requires_human_verify"


@pytest.mark.asyncio
async def test_mail_failure_keeps_request_and_replay_does_not_send_again():
    db, owner, request_id = MemoryDB(), profile(), uuid4()
    state = ApplicationRequirementState(id=uuid4(), profile_id=owner.id, requirement_key="staff_request_test",
        requested_document_id=uuid4(), status="requested",
        provenance={"source_kind": "staff_request", "file_request_id": str(request_id)})
    db.add(state)
    send = AsyncMock(side_effect=HTTPException(409, "No verified client email is available"))
    with patch.object(file_requests.missing_item_automation, "send_requirements_email", send), patch.object(
        collection_tasks, "record_outreach", AsyncMock()
    ):
        delivery = await file_requests.deliver_batch(db, profile=owner, request_id=request_id, user=SimpleNamespace(id=uuid4()))
        replay = await file_requests.deliver_batch(db, profile=owner, request_id=request_id, user=SimpleNamespace(id=uuid4()))
    assert delivery.status == "failed"
    assert replay is delivery
    assert state in db.rows and state.status == "requested"
    assert len([row for row in db.rows if isinstance(row, ApplicationRoomDelivery)]) == 1
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_readiness_keeps_staff_definition_and_quantity_after_refresh():
    owner, request_id = profile(), uuid4()
    item = FileRequestItem(document_type="bank_statement", name="Statements", quantity=6, unit="months")
    key = "staff_request_test"
    state = ApplicationRequirementState(id=uuid4(), profile_id=owner.id, requirement_key=key,
        label="Statements", category="Other", required_level="required", status="requested",
        requested_document_id=uuid4(), verification_required=False,
        source_program_keys=[], source_policy_keys=[],
        provenance={"source_kind": "staff_request", "file_request_id": str(request_id),
            "request_item": item.model_dump(), "request_fingerprint": "preserved", "catalog_approved": True,
            "canonical_key": key, "collection_config": file_requests.item_config(item, request_id),
            "external_definition": {"visibility": ["borrower"], "completion_mode": "ai_can_complete"}})
    db = SimpleNamespace(execute=AsyncMock(side_effect=lambda stmt: Result(
        [state] if stmt.column_descriptions[0]["entity"] is ApplicationRequirementState else []
    )),
                         get=AsyncMock(return_value=None), flush=AsyncMock())
    requested = SimpleNamespace(id=state.requested_document_id, requires_signature=False)
    with patch.object(application_programs, "_evidence_inventory", AsyncMock(return_value=([], {}, set()))), patch.object(
        application_programs, "_accepted_entity_aliases", AsyncMock(return_value=set())
    ), patch.object(collection_tasks, "latest_responses", AsyncMock(return_value={})), patch.object(
        application_programs, "_requested_document", AsyncMock(return_value=requested)
    ), patch.object(application_programs, "_sync_requirement_evidence", AsyncMock(return_value=[])), patch.object(
        application_programs, "_reconcile_evidence_decisions", AsyncMock(return_value={})
    ), patch.object(collection_tasks, "sync_collection_tasks", AsyncMock()):
        for _ in range(2):
            states, _ = await application_programs._materialize_requirements(db, owner, [], {}, [], {})
            assert states == [state]
            assert state.provenance["source_kind"] == "staff_request"
            assert state.provenance["file_request_id"] == str(request_id)
            assert state.provenance["request_fingerprint"] == "preserved"
            assert state.provenance["coverage"]["required"] == 6
            assert not state.provenance["inactive_definition"]


def test_month_coverage_counts_periods_and_does_not_reuse_another_subjects_upload():
    request_id, target_id = uuid4(), uuid4()
    item = FileRequestItem(document_type="bank_statement", name="Statements", quantity=2, unit="months", subject="Owner A")
    requirement = SimpleNamespace(requirement_key="staff_request_test", label="Statements", category="Other",
                                  collection_config=file_requests.item_config(item, request_id))
    files = [SimpleNamespace(id=uuid4(), content_hash=str(index), file_name="Statement.pdf", created_at=datetime.now(UTC),
        statement_period=period, requested_document_id=destination)
        for index, (period, destination) in enumerate([("2026-01", target_id), ("2026-01", target_id), ("2026-02", uuid4())])]
    analyses = {file.id: SimpleNamespace(id=uuid4(), analysis_version=1, content_hash=file.content_hash,
        status="completed", classification="bank_statement", confidence="low", analysis={}) for file in files}
    matches, complete, coverage = application_programs._matching_evidence_files(requirement, SimpleNamespace(id=target_id), files, analyses)
    assert {file.id for file in matches} == {file.id for file in files[:2]}
    assert not complete and coverage["current"] == 1 and coverage["required"] == 2


@pytest.mark.asyncio
async def test_cancelled_request_cannot_return_as_a_collection_task():
    row = ApplicationRequirementState(id=uuid4(), profile_id=uuid4(), requirement_key="staff_request_test",
        status="not_applicable", provenance={"source_kind": "staff_request", "cancelled_at": datetime.now(UTC).isoformat()})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[Result(), Result()]), flush=AsyncMock(), add=Mock())
    assert await collection_tasks.sync_collection_tasks(db, SimpleNamespace(id=row.profile_id), [row]) == []


@pytest.mark.asyncio
async def test_stronger_equivalent_request_reopens_coverage_and_preserves_both_batches():
    memory, owner, user = MemoryDB(), profile(), SimpleNamespace(id=uuid4())
    first_id, second_id = uuid4(), uuid4()
    item = FileRequestItem(document_type="bank_statement", name="Statements", quantity=2, unit="months")
    with patch("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False)), patch.object(
        collection_tasks, "sync_collection_tasks", AsyncMock(return_value=[])
    ), patch.object(file_requests.profiles, "log_profile_action", AsyncMock()):
        state = (await file_requests.create_batch(memory, profile=owner, request_id=first_id, items=[item], user=user))[0]
        state.status = "verified"
        await file_requests.create_batch(memory, profile=owner, request_id=second_id,
            items=[item.model_copy(update={"quantity": 3})], user=user)
    files = [SimpleNamespace(id=uuid4(), file_name="Statement.pdf", content_hash=str(index),
        created_at=datetime.now(UTC), statement_period=f"2026-0{index + 1}", requested_document_id=state.requested_document_id)
        for index in range(2)]
    links = [SimpleNamespace(id=uuid4(), file_id=file.id, source="operator", verified_at=datetime.now(UTC),
                             verified_by_user_id=user.id, provenance={}) for file in files]
    analyses = {file.id: SimpleNamespace(id=uuid4(), analysis_version=1, content_hash=file.content_hash,
        status="completed", classification="bank_statement", confidence="low", analysis={}) for file in files}
    db = SimpleNamespace(execute=AsyncMock(side_effect=lambda stmt: Result(
        [state] if stmt.column_descriptions[0]["entity"] is ApplicationRequirementState else []
    )), get=AsyncMock(return_value=None), flush=AsyncMock())
    with patch.object(application_programs, "_evidence_inventory", AsyncMock(return_value=(files, analyses, {"bank_statement"}))), patch.object(
        application_programs, "_accepted_entity_aliases", AsyncMock(return_value=set())
    ), patch.object(collection_tasks, "latest_responses", AsyncMock(return_value={})), patch.object(
        application_programs, "_requested_document", AsyncMock(return_value=SimpleNamespace(id=state.requested_document_id, requires_signature=False))
    ), patch.object(application_programs, "_sync_requirement_evidence", AsyncMock(return_value=links)), patch.object(
        application_programs, "_reconcile_evidence_decisions", AsyncMock(return_value={link.id: SimpleNamespace(decision="accepted") for link in links})
    ), patch.object(collection_tasks, "sync_collection_tasks", AsyncMock()):
        for _ in range(2):
            await application_programs._materialize_requirements(db, owner, [], {}, [], {})
            assert state.status == "received_unverified"
            assert state.provenance["verified_coverage"]["current"] == 2
            assert state.provenance["verified_coverage"]["required"] == 3
            assert not state.provenance["verified_coverage"]["complete"]
            assert set(file_requests.batch_memberships(state)) == {str(first_id), str(second_id)}
