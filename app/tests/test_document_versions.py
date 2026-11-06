from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.bucket import BucketFile
from app.services import document_versions as versions


def file(**values):
    identity = uuid4()
    return BucketFile(**{
        "id": identity, "bucket_id": uuid4(), "document_family_id": identity,
        "document_version_number": 1, "document_version_status": "current",
        "is_current_document_version": True, "status": "uploaded", "file_name": "payoff.pdf",
        "s3_key": f"immutable/{identity}", "content_type": "application/pdf", "size_bytes": 10,
        "source_kind": "client_room", "retention_class": "bucket_evidence",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC), "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "uploaded_by_name": "Client", **values,
    })


def result(rows):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def db_for(target, rows):
    return SimpleNamespace(get=AsyncMock(return_value=target), execute=AsyncMock(side_effect=[None, result(rows)]))


@pytest.mark.asyncio
async def test_new_upload_is_own_pending_v1_without_guessing_from_name_or_type():
    original = file(status="uploading")
    db = SimpleNamespace(execute=AsyncMock())
    assert await versions.prepare_upload(db, original, replaces_file_id=None, expected_document_version=None, idempotency_key=None) is None
    assert original.document_family_id == original.id and original.document_version_number == 1
    assert not original.is_current_document_version and original.document_version_status == "pending"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_replacement_locks_original_reserves_next_and_preserves_original_binary_and_amount():
    original = file(content_hash="a" * 64, requested_document_id=uuid4())
    next_file = file(bucket_id=original.bucket_id, status="uploading", requested_document_id=None)
    db = db_for(original, [original])
    await versions.prepare_upload(db, next_file, replaces_file_id=original.id, expected_document_version=1, idempotency_key=None)
    assert next_file.document_family_id == original.document_family_id
    assert next_file.document_version_number == 2 and next_file.supersedes_file_id == original.id
    assert next_file.requested_document_id == original.requested_document_id
    assert next_file.document_version_status == "pending" and not next_file.is_current_document_version
    assert original.content_hash == "a" * 64 and original.s3_key.startswith("immutable/")
    assert original.is_current_document_version
    assert db.execute.await_args_list[0].args[0]._for_update_arg is not None


@pytest.mark.asyncio
async def test_stale_replacement_cannot_create_parallel_next_versions():
    original = file()
    newer = file(bucket_id=original.bucket_id, document_family_id=original.document_family_id, document_version_number=2)
    with pytest.raises(HTTPException) as error:
        await versions.prepare_upload(db_for(original, [newer, original]), file(bucket_id=original.bucket_id), replaces_file_id=original.id, expected_document_version=1, idempotency_key=None)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_zip_collection_replacement_is_refused_without_mixing_child_accounts_or_periods():
    original = file(file_name="financials.zip", content_type="application/zip")
    assert not versions.replacement_allowed(original)
    with pytest.raises(HTTPException) as error:
        await versions.prepare_upload(db_for(original, [original]), file(bucket_id=original.bucket_id),
            replaces_file_id=original.id, expected_document_version=1, idempotency_key=None)
    assert error.value.status_code == 422 and "individual" in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"bucket_id": uuid4()}, {"source_kind": "generated"}, {"retention_class": "ach_authorization_proof"}])
async def test_cross_file_and_immutable_workflow_replacements_are_refused(changes):
    original = file()
    next_file = file(bucket_id=original.bucket_id)
    for key, value in changes.items():
        setattr(original, key, value)
    with pytest.raises(HTTPException) as error:
        await versions.prepare_upload(db_for(original, [original]), next_file, replaces_file_id=original.id, expected_document_version=1, idempotency_key=None)
    assert error.value.status_code in {404, 409}


@pytest.mark.asyncio
async def test_newest_usable_analysis_advances_current_without_rewriting_originals():
    original = file(content_hash="a" * 64)
    new = file(bucket_id=original.bucket_id, document_family_id=original.document_family_id, document_version_number=2,
               document_version_status="pending", is_current_document_version=False, content_hash="b" * 64)
    original_key = original.s3_key
    assert await versions.complete_analysis(db_for(original, [new, original]), new, usable=True)
    assert new.is_current_document_version and new.document_version_status == "current"
    assert not original.is_current_document_version and original.document_version_status == "superseded"
    assert original.s3_key == original_key and original.content_hash == "a" * 64


@pytest.mark.asyncio
async def test_received_pending_replacement_makes_old_binary_historical_without_deleting_it():
    original = file(content_hash="a" * 64)
    new = file(bucket_id=original.bucket_id, document_family_id=original.document_family_id, document_version_number=2,
               supersedes_file_id=original.id, document_version_status="pending", is_current_document_version=False)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[None, result([new, original]), None, None]), identity_map={})
    await versions.mark_received(db, new)
    assert original.document_version_status == "superseded" and not original.is_current_document_version
    assert original.s3_key.startswith("immutable/") and original.content_hash == "a" * 64
    assert new.document_version_status == "pending" and not new.is_current_document_version


@pytest.mark.asyncio
async def test_late_old_completion_cannot_override_newer_received_version():
    original = file()
    new = file(bucket_id=original.bucket_id, document_family_id=original.document_family_id, document_version_number=2,
               document_version_status="pending", is_current_document_version=False)
    assert not await versions.complete_analysis(db_for(original, [new, original]), original, usable=True)
    assert not new.is_current_document_version


@pytest.mark.asyncio
async def test_unfinished_upload_reservation_does_not_stop_received_version_promotion():
    original = file()
    reservation = file(bucket_id=original.bucket_id, document_family_id=original.document_family_id,
                       document_version_number=2, status="uploading", document_version_status="pending", is_current_document_version=False)
    assert await versions.complete_analysis(db_for(original, [reservation, original]), original, usable=True)


@pytest.mark.asyncio
async def test_duplicate_bytes_are_recorded_without_promoting_or_counting_another_source():
    original = file(content_hash="a" * 64)
    duplicate = file(bucket_id=original.bucket_id, document_family_id=original.document_family_id, document_version_number=2,
                     document_version_status="pending", is_current_document_version=False, content_hash="a" * 64)
    assert not await versions.complete_analysis(db_for(original, [duplicate, original]), duplicate, usable=True)
    assert duplicate.document_version_status == "duplicate" and duplicate.duplicate_of_file_id == original.id
    assert original.is_current_document_version and not duplicate.is_current_document_version


@pytest.mark.asyncio
async def test_manual_acceptance_survives_cached_medium_analysis_but_explicit_rejection_does_not():
    accepted = file()
    assert await versions.complete_analysis(db_for(accepted, [accepted]), accepted, usable=None)
    accepted.document_version_status = "rejected"
    assert not await versions.complete_analysis(db_for(accepted, [accepted]), accepted, usable=True)
    assert not accepted.is_current_document_version and accepted.document_version_status == "rejected"


@pytest.mark.asyncio
async def test_retry_receipt_matches_scope_and_never_returns_different_document():
    original = file(uploaded_by_user_id=uuid4(), upload_link_id=None, requested_document_id=uuid4())
    request = file(bucket_id=original.bucket_id, file_name=original.file_name, uploaded_by_user_id=original.uploaded_by_user_id,
                   upload_link_id=None, requested_document_id=None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[None, SimpleNamespace(scalar_one_or_none=lambda: original)]))
    assert await versions.prepare_upload(db, request, replaces_file_id=None, expected_document_version=None, idempotency_key=uuid4()) is original
    retry_query = str(db.execute.await_args_list[0].args[0])
    assert "pg_advisory_xact_lock" in retry_query and "FROM buckets" not in retry_query
    request.file_name = "different.pdf"
    db.execute.side_effect = [None, SimpleNamespace(scalar_one_or_none=lambda: original)]
    with pytest.raises(HTTPException) as error:
        await versions.prepare_upload(db, request, replaces_file_id=None, expected_document_version=None, idempotency_key=uuid4())
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_public_history_keeps_dates_without_exposing_storage_or_private_actor_fields():
    original = file()
    new = file(bucket_id=original.bucket_id, document_family_id=original.document_family_id, document_version_number=2,
               document_version_status="pending", is_current_document_version=False)
    db = SimpleNamespace(get=AsyncMock(return_value=original), execute=AsyncMock(side_effect=[result([new, original]), result([])]))
    history = await versions.history(db, original.bucket_id, original.id, can_upload=True, can_preview=False)
    assert history.current_file_id == original.id and history.latest_file_id == new.id
    assert history.versions[1].created_at == original.created_at
    assert history.can_upload_new_version and not any(row.can_preview for row in history.versions)
    assert "s3_key" not in history.model_dump_json() and "uploaded_by_email" not in history.model_dump_json()


@pytest.mark.asyncio
async def test_completed_upload_retry_cannot_mint_a_put_url_to_existing_version():
    from app.routers import dealer_ai_intake as core

    original = file(upload_link_id=uuid4())
    intake = SimpleNamespace(bucket_id=original.bucket_id, bucket_upload_link_id=original.upload_link_id)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))))
    with patch.object(versions, "prepare_upload", AsyncMock(return_value=original)), patch.object(core, "_upload_url", Mock()) as put, patch.object(core, "_bucket_storage_config", return_value=("evidence", "prefix", "kms")):
        reply = await core._start_upload(db, intake, core.DealerFileUploadInit(file_name="payoff.pdf"), SimpleNamespace(), actor_name="Staff", actor_email="staff@example.com")
    assert reply.already_uploaded and reply.upload_url == "" and reply.file_id == original.id
    put.assert_not_called()
