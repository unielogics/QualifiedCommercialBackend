from copy import deepcopy
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from app.dealer_os.models import DealerDocument
from app.dealer_os.services import document_versions as versions
from app.models.bucket import BucketFile


def doc(**values):
    identity = uuid4()
    return DealerDocument(**{
        "id": identity, "dealer_id": uuid4(), "filename": "statement.pdf", "content_type": "application/pdf",
        "size_bytes": 100, "kind": "statement", "status": "extracted", "s3_key": f"archive/{identity}",
        "document_family_id": identity, "document_version_number": 1, "is_current_document_version": True,
        "document_version_status": "current", "content_hash": "a" * 64,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC), "updated_at": datetime(2026, 1, 1, tzinfo=UTC), **values,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("client_upload", [False, True])
async def test_replacement_storage_failure_never_extracts_or_publishes(client_upload):
    from app.dealer_os import router
    from app.enums import Role

    original = doc(extracted={"payoff_amount": 70000})
    user = SimpleNamespace(id=uuid4(), name="Uploader", email="uploader@example.com",
                           role=Role.SUPER_ADMIN)
    db = SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
    upload = UploadFile(BytesIO(b"replacement bytes"), filename="replacement.pdf",
                        headers=Headers({"content-type": "application/pdf"}))
    with patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=SimpleNamespace(id=original.dealer_id))), patch.object(router, "is_audit_client", return_value=client_upload), patch.object(router, "_validate_user_pdf_bytes"), patch.object(router.storage, "put_bytes", return_value=False), patch.object(versions, "prepare", AsyncMock(return_value=None)), patch.object(versions, "extract_latest", AsyncMock()) as extract, patch.object(router.buckets_link, "push_document", AsyncMock()) as mirror:
        with pytest.raises(HTTPException) as error:
            await router.upload_document(original.dealer_id, user, upload, kind="payoff_letter",
                                         account_id="", replaces_document_id=original.id,
                                         expected_document_version=1, idempotency_key=uuid4(), db=db)
    assert error.value.status_code == 503 and "Retry" in error.value.detail
    extract.assert_not_awaited()
    mirror.assert_not_awaited()
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert original.extracted == {"payoff_amount": 70000} and original.is_current_document_version


@pytest.mark.asyncio
async def test_dealer_new_source_is_own_pending_v1_not_inferred_from_filename():
    new = doc()
    db = SimpleNamespace(execute=AsyncMock())
    assert await versions.prepare(db, new, replaces_document_id=None, expected_document_version=None, pinned_account_id=None) is None
    assert new.document_family_id == new.id and new.document_version_number == 1
    assert not new.is_current_document_version and new.document_version_status == "pending"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_dealer_replacement_uses_latest_ordinal_and_original_accepted_account_identity():
    account = uuid4()
    original = doc(account_id=account)
    failed = doc(dealer_id=original.dealer_id, document_family_id=original.document_family_id,
                 document_version_number=2, status="failed", document_version_status="failed",
                 is_current_document_version=False, account_id=uuid4())
    new = doc(dealer_id=original.dealer_id)
    db = SimpleNamespace(get=AsyncMock(return_value=failed))
    with patch.object(versions, "rows", AsyncMock(return_value=[failed, original])):
        await versions.prepare(db, new, replaces_document_id=failed.id, expected_document_version=2, pinned_account_id=account)
        assert new.document_version_number == 3 and new.document_family_id == original.document_family_id
        assert new.supersedes_document_id == failed.id
        with pytest.raises(HTTPException) as error:
            await versions.prepare(db, doc(dealer_id=original.dealer_id), replaces_document_id=failed.id, expected_document_version=2, pinned_account_id=uuid4())
        assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_dealer_cross_client_stale_and_archive_replacements_are_refused():
    original = doc()
    new = doc()
    db = SimpleNamespace(get=AsyncMock(return_value=original))
    with pytest.raises(HTTPException) as error:
        await versions.prepare(db, new, replaces_document_id=original.id, expected_document_version=1, pinned_account_id=None)
    assert error.value.status_code == 404
    new.dealer_id = original.dealer_id
    with patch.object(versions, "rows", AsyncMock(return_value=[original])):
        with pytest.raises(HTTPException) as error:
            await versions.prepare(db, new, replaces_document_id=original.id, expected_document_version=2, pinned_account_id=None)
        assert error.value.status_code == 409
    original.kind = "archive"
    with pytest.raises(HTTPException) as error:
        await versions.prepare(db, new, replaces_document_id=original.id, expected_document_version=1, pinned_account_id=None)
    assert error.value.status_code == 422 and "individual" in error.value.detail


@pytest.mark.asyncio
async def test_dealer_lost_response_retry_reuses_receipt_and_changed_bytes_conflict():
    original = doc(uploaded_by_user_id=uuid4())
    await versions.prepare(SimpleNamespace(), original, replaces_document_id=None, expected_document_version=None, pinned_account_id=None)
    request = doc(dealer_id=original.dealer_id, uploaded_by_user_id=original.uploaded_by_user_id)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[None, SimpleNamespace(scalar_one_or_none=lambda: original)]))
    assert await versions.prepare(db, request, replaces_document_id=None, expected_document_version=None, pinned_account_id=None, idempotency_key=uuid4()) is original
    assert "pg_advisory_xact_lock" in str(db.execute.await_args_list[0].args[0])
    request.content_hash = "b" * 64
    db.execute.side_effect = [None, SimpleNamespace(scalar_one_or_none=lambda: original)]
    with pytest.raises(HTTPException) as error:
        await versions.prepare(db, request, replaces_document_id=None, expected_document_version=None, pinned_account_id=None, idempotency_key=uuid4())
    assert error.value.status_code == 409


class Savepoint:
    def __init__(self, db):
        self.db = db
        self.before = deepcopy(db.ledger)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def rollback(self):
        self.db.ledger[:] = self.before


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "identity_conflict", "failed"])
async def test_dealer_projection_replacement_is_atomic_and_preserves_original_summary(outcome):
    from app.dealer_os.services import document_version_projection as projection
    from app.dealer_os.services import engines

    original = doc(extracted={"months": ["2026-09"], "transactions_count": 5})
    new = doc(dealer_id=original.dealer_id, document_family_id=original.document_family_id, document_version_number=2,
              supersedes_document_id=original.id, is_current_document_version=False, document_version_status="pending", content_hash="b" * 64)
    db = SimpleNamespace(info={}, ledger=[{"source": str(original.id), "amount": 70000}], refresh=AsyncMock())
    db.begin_nested = lambda: Savepoint(db)

    async def retire(_db, _doc):
        db.ledger.clear()

    async def extract(_db, target, _raw, account_id=None):
        db.ledger.append({"source": str(new.id), "amount": 60000})
        target.status = "failed" if outcome == "failed" else "extracted"
        target.error = "Unreadable replacement" if outcome == "failed" else None
        target.extracted = {"months": ["2026-09"], "transactions_count": 4}

    conflict = "The account differs; upload separately" if outcome == "identity_conflict" else None
    with patch.object(versions, "assert_latest", AsyncMock(return_value=[new, original])), patch.object(versions, "rows", AsyncMock(return_value=[new, original])), patch.object(projection, "retire_previous_projection", retire), patch.object(projection, "identity_conflict", return_value=conflict), patch.object(engines, "recompute_snapshot", AsyncMock()) as recompute:
        accepted = await versions.extract_latest(db, new, extract, b"new bytes")
    assert original.extracted == {"months": ["2026-09"], "transactions_count": 5}
    assert accepted == (outcome == "accepted")
    if accepted:
        assert db.ledger == [{"source": str(new.id), "amount": 60000}]
        assert new.is_current_document_version and not original.is_current_document_version
        recompute.assert_awaited_once()
    else:
        assert db.ledger == [{"source": str(original.id), "amount": 70000}]
        assert not new.is_current_document_version and original.is_current_document_version
        assert new.status == ("pending_review" if conflict else "failed")
        recompute.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_dealer_bytes_do_not_extract_or_count_another_source():
    original = doc()
    new = doc(dealer_id=original.dealer_id, document_family_id=original.document_family_id, document_version_number=2,
              supersedes_document_id=original.id, is_current_document_version=False, document_version_status="pending")
    extractor = AsyncMock()
    with patch.object(versions, "assert_latest", AsyncMock(return_value=[new, original])), patch.object(versions, "rows", AsyncMock(return_value=[new, original])):
        assert not await versions.extract_latest(SimpleNamespace(), new, extractor, b"same bytes")
    extractor.assert_not_called()
    assert new.document_version_status == "duplicate" and original.is_current_document_version


@pytest.mark.asyncio
async def test_dealer_bucket_bridge_uses_actual_bucket_family_not_dealer_family_uuid():
    from app.dealer_os.services import buckets_link
    from app.services import document_versions as bucket_versions

    original = doc(bucket_file_id=uuid4())
    old_mirror = BucketFile(id=original.bucket_file_id, bucket_id=uuid4(), document_family_id=uuid4(), document_version_number=1)
    new = doc(dealer_id=original.dealer_id, document_family_id=original.document_family_id,
              document_version_number=2, supersedes_document_id=original.id, is_current_document_version=False,
              document_version_status="pending", status="pending_review", uploaded_by_name="Client")
    db = SimpleNamespace(get=AsyncMock(side_effect=lambda model, _id: original if model is DealerDocument else old_mirror), add=Mock(), flush=AsyncMock())

    async def prepare(_db, mirror, **kwargs):
        assert kwargs["replaces_file_id"] == old_mirror.id
        mirror.document_family_id = old_mirror.document_family_id
        mirror.document_version_number = 2

    with patch.object(buckets_link, "ensure_bucket", AsyncMock(return_value=SimpleNamespace(id=old_mirror.bucket_id))), patch.object(bucket_versions, "prepare_upload", prepare), patch.object(bucket_versions, "mark_received", AsyncMock()):
        mirror = await buckets_link.push_document(db, SimpleNamespace(id=new.dealer_id), new, 100)
    assert mirror.document_family_id == old_mirror.document_family_id != original.document_family_id
    assert mirror.status == "pending_review" and not mirror.is_current_document_version
    assert new.bucket_file_id == mirror.id


@pytest.mark.asyncio
async def test_dealer_history_keeps_original_dates_and_exposes_only_latest_pending_source():
    original = doc()
    new = doc(dealer_id=original.dealer_id, document_family_id=original.document_family_id, document_version_number=2,
              supersedes_document_id=original.id, is_current_document_version=False, document_version_status="pending", status="pending_review")
    with patch.object(versions, "rows", AsyncMock(return_value=[new, original])):
        history = await versions.history(SimpleNamespace(), original, can_upload=True)
    assert history["current_file_id"] is None and history["latest_file_id"] == new.id
    assert history["versions"][1]["created_at"] == original.created_at
    assert history["versions"][1]["document_version_status"] == "superseded"
    assert not history["versions"][1]["is_current_document_version"]
