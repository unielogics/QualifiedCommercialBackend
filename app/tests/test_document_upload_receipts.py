from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import DocStatus, Role
from app.models.document_file_version import DocumentFileVersion
from app.routers import documents
from app.schemas.document import DocumentUploadInit
from app.services import document_upload_receipts as receipts


def upload_payload(**values):
    return DocumentUploadInit(**{
        "loan_id": uuid4(), "name": "Payoff.pdf", "is_other": True,
        "idempotency_key": uuid4(), **values,
    })


def stored_version(payload, actor, **values):
    row = DocumentFileVersion(**{
        "id": uuid4(), "document_id": uuid4(), "version_number": 1,
        "file_name": payload.name, "content_type": payload.content_type,
        "s3_key": "staging/upload", "status": "uploading", "analysis_snapshot": {},
        "idempotency_key": payload.idempotency_key, "uploaded_by_user_id": actor.id, **values,
    })
    receipts.remember(row, payload)
    return row


@pytest.mark.asyncio
async def test_initial_lost_response_and_completed_retries_reuse_one_document_and_version():
    payload = upload_payload()
    actor = SimpleNamespace(id=uuid4(), role=Role.CLIENT)
    row = stored_version(payload, actor)
    saved = []
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=payload.loan_id)),
                         add=Mock(side_effect=saved.append), refresh=AsyncMock(), execute=AsyncMock())

    async def flush():
        for item in saved:
            if item.id is None:
                item.id = uuid4()

    async def reserve(_db, doc, _payload, _actor, **_kwargs):
        row.document_id = doc.id
        row.analysis_snapshot = {}
        return row

    db.flush = AsyncMock(side_effect=flush)
    db.execute.side_effect = [None, SimpleNamespace(all=lambda: [])]
    storage = Mock(generate_presigned_url=Mock(return_value="https://storage.example/staging"))
    with patch.object(documents, "_can_access_loan", AsyncMock(return_value=True)), patch.object(documents, "get_settings", return_value=SimpleNamespace(s3_bucket="private")), patch.object(documents.file_versions, "reserve", AsyncMock(side_effect=reserve)) as reservation, patch.object(receipts.upload_validation, "_s3_client", return_value=storage):
        first = await documents.upload_init(payload, actor, db)
        assert row.analysis_snapshot["upload_request_fingerprint"] == receipts.fingerprint(payload)
        db.execute.side_effect = [None, SimpleNamespace(all=lambda: [(saved[0], row)])]
        retried = await documents.upload_init(payload, actor, db)
        assert retried == first
        row.status, row.s3_key = "uploaded", "immutable/accepted"
        saved[0].status = DocStatus.VERIFIED
        db.execute.side_effect = [None, SimpleNamespace(all=lambda: [(saved[0], row)])]
        completed = await documents.upload_init(payload, actor, db)
    assert completed.document_id == first.document_id and completed.version_id == first.version_id
    assert completed.upload_url is None and completed.s3_key == "immutable/accepted"
    assert len(saved) == 1
    reservation.assert_awaited_once()
    assert storage.generate_presigned_url.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uploaded", "duplicate", "rejected"])
async def test_fulfillment_receipt_replays_before_completed_document_status_gate_without_put(status):
    payload = upload_payload(fulfill_document_id=uuid4(), is_other=False)
    actor = SimpleNamespace(id=uuid4(), role=Role.CLIENT)
    row = stored_version(payload, actor, document_id=payload.fulfill_document_id, status=status)
    doc = SimpleNamespace(id=row.document_id, loan_id=payload.loan_id, status=DocStatus.VERIFIED)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=payload.loan_id)), add=Mock(),
                         execute=AsyncMock(side_effect=[None, SimpleNamespace(all=lambda: [(doc, row)])]))
    with patch.object(documents, "_can_access_loan", AsyncMock(return_value=True)), patch.object(documents, "get_settings", return_value=SimpleNamespace(s3_bucket="private")), patch.object(documents.file_versions, "reserve", AsyncMock()) as reserve, patch.object(receipts.upload_validation, "_s3_client") as storage:
        result = await documents.upload_init(payload, actor, db)
    assert result.document_id == doc.id and result.version_id == row.id and result.upload_url is None
    reserve.assert_not_awaited()
    db.add.assert_not_called()
    storage.assert_not_called()
    assert len(db.execute.await_args_list) == 2


@pytest.mark.asyncio
async def test_receipt_lock_and_lookup_are_scoped_by_loan_actor_and_key():
    payload, actor = upload_payload(), SimpleNamespace(id=uuid4())
    row = stored_version(payload, actor)
    doc = SimpleNamespace(id=row.document_id)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[None, SimpleNamespace(all=lambda: [(doc, row)])]))
    assert await receipts.find(db, payload, actor) == (doc, row)
    first, lookup = [call.args[0] for call in db.execute.await_args_list]
    assert "pg_advisory_xact_lock" in str(first) and "documents" not in str(first)
    params = lookup.compile().params
    assert payload.loan_id in params.values() and actor.id in params.values()
    assert payload.idempotency_key in params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"name": "Other.pdf"}, {"is_other": False},
                                          {"checklist_key": "bank_statements"},
                                          {"fulfill_document_id": uuid4()},
                                          {"content_type": "image/png"}, {"category": "payoff"}])
async def test_same_retry_key_with_different_semantic_request_conflicts(changed):
    payload, actor = upload_payload(), SimpleNamespace(id=uuid4())
    row = stored_version(payload, actor)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[None, SimpleNamespace(all=lambda: [(SimpleNamespace(), row)])]))
    with pytest.raises(HTTPException) as error:
        await receipts.find(db, payload.model_copy(update=changed), actor)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_no_key_uses_existing_flow_and_denied_loan_never_probes_receipts():
    payload, actor = upload_payload(), SimpleNamespace(id=uuid4(), role=Role.CLIENT)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=payload.loan_id)), execute=AsyncMock())
    assert await receipts.find(db, payload.model_copy(update={"idempotency_key": None}), actor) is None
    db.execute.assert_not_awaited()
    with patch.object(documents, "_can_access_loan", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as error:
            await documents.upload_init(payload, actor, db)
    assert error.value.status_code == 404
    db.execute.assert_not_awaited()
