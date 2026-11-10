"""Batch-history reads must not run unrelated readiness materialization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.application_profile import ApplicationRequirementState, ApplicationRoomDelivery
from app.models.bucket import BucketRequestedDocument
from app.routers import file_requests as routes
from app.schemas.file_request import FileRequestBatchCreate, FileRequestItem
from app.services import application_programs, collection_tasks, file_requests
from app.tests.test_file_requests import MemoryDB, profile


class RequestDB(MemoryDB):
    def __init__(self):
        super().__init__()
        self.commit = AsyncMock()


@pytest.mark.asyncio
async def test_get_lists_existing_batch_without_readiness_writes_or_notifications(monkeypatch):
    db, owner = RequestDB(), profile()
    actor, request_id = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC), uuid4()
    item = FileRequestItem(document_type="operating_plan", name="Operating expenses and earnings")
    state = ApplicationRequirementState(id=uuid4(), profile_id=owner.id,
        requirement_key="operating_expenses_and_earnings_plan", label=item.name, requested_document_id=uuid4(),
        status="requested", provenance={"source_kind": "staff_request", "file_request_id": str(request_id),
            "request_item": item.model_dump()})
    db.add(state)
    await db.flush()
    loader = AsyncMock(return_value=owner)
    readiness = AsyncMock(side_effect=RuntimeError("Unrelated readiness calculation cannot run during batch reads"))
    delivery = AsyncMock(side_effect=AssertionError("A read cannot send a file request"))
    monkeypatch.setattr(routes.profiles, "load_profile", loader)
    monkeypatch.setattr(application_programs, "get_program_readiness", readiness)
    monkeypatch.setattr(file_requests, "deliver_batch", delivery)

    result = await routes.list_requests(owner.id, actor, db)
    assert len(result["requests"]) == 1
    returned = result["requests"][0]["items"][0]
    assert returned["document_type"] == "operating_plan" and returned["status"] == "requested"
    assert returned["requested_document_id"] == str(state.requested_document_id)
    loader.assert_awaited_once_with(db, owner.id, actor)
    readiness.assert_not_awaited()
    delivery.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_history_get_succeeds_without_first_use_readiness_materialization(monkeypatch):
    db, owner, actor = RequestDB(), profile(), SimpleNamespace(role=Role.LOAN_EXEC)
    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(return_value=owner))
    readiness = AsyncMock(side_effect=AssertionError("Readiness is not a batch-history dependency"))
    monkeypatch.setattr(application_programs, "get_program_readiness", readiness)
    assert await routes.list_requests(owner.id, actor, db) == {"requests": []}
    readiness.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert db.rows == []


@pytest.mark.asyncio
async def test_operating_form_creation_materializes_destination_and_returns_batch_even_if_readiness_unavailable(monkeypatch):
    db, owner = RequestDB(), profile()
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    payload = FileRequestBatchCreate(request_id=uuid4(), notify=False, items=[
        FileRequestItem(document_type="operating_plan", name="Operating expenses and earnings",
                        instructions="Enter current expenses and proposed operating improvements")])
    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(return_value=owner))
    monkeypatch.setattr(routes, "_require_training_live_action", AsyncMock())
    monkeypatch.setattr(routes.profiles, "log_profile_action", AsyncMock())
    monkeypatch.setattr("app.services.arf_channel.profile_uses_arf", AsyncMock(return_value=False))
    sync = AsyncMock(return_value=[])
    monkeypatch.setattr(collection_tasks, "sync_collection_tasks", sync)
    readiness = AsyncMock(side_effect=RuntimeError("Readiness would fail after the request committed"))
    monkeypatch.setattr(application_programs, "get_program_readiness", readiness)
    sender = AsyncMock()
    monkeypatch.setattr(file_requests, "deliver_batch", sender)

    result = await routes.create_requests(owner.id, payload, Mock(), actor, db)
    assert result["request_id"] == str(payload.request_id) and result["notification"] is None
    assert len(result["requests"]) == 1
    documents = [row for row in db.rows if isinstance(row, BucketRequestedDocument)]
    states = [row for row in db.rows if isinstance(row, ApplicationRequirementState)]
    assert len(documents) == len(states) == 1
    assert documents[0].id == states[0].requested_document_id
    assert states[0].provenance["collection_config"]["form_kind"] == "operating_plan"
    assert states[0].provenance["external_definition"]["requirement_kind"] == "form"
    assert states[0].verification_required
    assert result["requests"][0]["items"][0]["document_type"] == "operating_plan"
    sync.assert_awaited_once()
    readiness.assert_not_awaited()
    sender.assert_not_awaited()
    db.commit.assert_awaited_once()
    assert not any(isinstance(row, ApplicationRoomDelivery) for row in db.rows)


@pytest.mark.asyncio
async def test_request_history_acl_still_rejects_clients_before_loading_the_file(monkeypatch):
    loader = AsyncMock()
    monkeypatch.setattr(routes.profiles, "load_profile", loader)
    with pytest.raises(HTTPException) as error:
        await routes.list_requests(uuid4(), SimpleNamespace(role=Role.CLIENT), RequestDB())
    assert error.value.status_code == 403
    loader.assert_not_awaited()
