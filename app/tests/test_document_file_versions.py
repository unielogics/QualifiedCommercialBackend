from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import DocStatus, Role
from app.models.document_file_version import DocumentFileVersion
from app.schemas.document import DocumentUploadInit
from app.services import document_file_versions as service


def doc(**values):
    return SimpleNamespace(**{
        "id": uuid4(), "loan_id": uuid4(), "s3_key": "original.pdf", "name": "Original payoff.pdf",
        "file_version_number": 1, "latest_file_version_number": 1, "latest_file_version_status": "current",
        "current_file_version_id": uuid4(), "status": DocStatus.VERIFIED, "ai_scan_status": "scanned",
        "scan_dirty": False, "ai_notes": "Prior payoff 10000", "verified_at": datetime.now(UTC),
        "verified_by": "ai", "ai_scan_confidence": "0.95", "updated_at": datetime.now(UTC),
        "created_at": datetime.now(UTC), **values,
    })


def version(saved, actor, number=2, **values):
    return DocumentFileVersion(**{
        "id": uuid4(), "document_id": saved.id, "version_number": number,
        "s3_key": f"incoming-{number}", "file_name": f"Payoff {number}.pdf", "content_type": "application/pdf",
        "uploaded_by_user_id": actor.id, "status": "uploading", "analysis_status": "unscanned",
        "analysis_snapshot": {}, "source": "upload", "created_at": datetime.now(UTC), **values,
    })


def fake_db():
    return SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock(), execute=AsyncMock(), get=AsyncMock(return_value=None), identity_map={})


@pytest.mark.asyncio
async def test_reservation_preserves_current_binary_and_verification_and_retries_same_version():
    saved, actor, db = doc(), SimpleNamespace(id=uuid4()), fake_db()
    request = DocumentUploadInit(loan_id=saved.loan_id, name="New payoff.pdf", replace_document_id=saved.id,
        expected_document_version=1, idempotency_key=uuid4())
    baseline = version(saved, actor, 1, id=saved.current_file_version_id, s3_key=saved.s3_key, status="uploaded", analysis_status="scanned")
    with patch.object(service, "versions", AsyncMock(return_value=[baseline])):
        pending = await service.reserve(db, saved, request, actor)
    assert pending.version_number == 2 and pending.s3_key != saved.s3_key
    assert saved.s3_key == "original.pdf" and saved.status == DocStatus.VERIFIED
    assert saved.ai_notes == "Prior payoff 10000" and saved.latest_file_version_number == 2
    with patch.object(service, "versions", AsyncMock(return_value=[pending, baseline])):
        assert await service.reserve(db, saved, request, actor) is pending
        with pytest.raises(HTTPException) as error:
            await service.reserve(db, saved, request.model_copy(update={"idempotency_key": uuid4()}), actor)
    assert error.value.status_code == 409
    assert db.add.call_count == 1


@pytest.mark.asyncio
async def test_new_version_seals_bytes_clears_old_facts_and_is_idempotent():
    saved, actor, db = doc(latest_file_version_number=2), SimpleNamespace(id=uuid4()), fake_db()
    row = version(saved, actor)
    with patch.object(service, "versions", AsyncMock(return_value=[row])), patch.object(service.upload_validation, "seal_s3_upload", AsyncMock(return_value=("sealed-final", "s3-v2"))) as seal, patch.object(service.upload_validation, "validate_s3_pdf_upload", AsyncMock()), patch.object(service, "_content_hash", return_value="newhash"):
        assert await service.complete(db, saved, row, actor)
        assert not await service.complete(db, saved, row, actor)
    seal.assert_awaited_once_with("incoming-2", file_id=row.id)
    assert saved.s3_key == row.s3_key == "sealed-final" and row.s3_version_id == "s3-v2"
    assert saved.file_version_number == 2 and saved.current_file_version_id == row.id
    assert saved.verified_at is None and saved.ai_notes is None
    assert saved.scan_dirty and saved.ai_scan_status == "queued"
    assert db.execute.call_count == 1
    assert db.execute.call_args.args[0].compile().params["is_current"] is False


@pytest.mark.asyncio
async def test_duplicate_upload_preserves_current_analysis_and_does_not_fulfill_again():
    saved, actor, db = doc(latest_file_version_number=2), SimpleNamespace(id=uuid4()), fake_db()
    row = version(saved, actor)
    prior = version(saved, actor, 1, id=saved.current_file_version_id, status="uploaded", analysis_status="scanned", content_hash="samehash")
    with patch.object(service, "versions", AsyncMock(return_value=[row, prior])), patch.object(service.upload_validation, "seal_s3_upload", AsyncMock(return_value=("newsealed", None))), patch.object(service.upload_validation, "validate_s3_pdf_upload", AsyncMock()), patch.object(service, "_content_hash", return_value="samehash"):
        assert not await service.complete(db, saved, row, actor)
    assert row.status == "duplicate" and saved.s3_key == "original.pdf"
    assert saved.ai_notes == "Prior payoff 10000" and saved.latest_file_version_status == "duplicate"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_late_old_upload_is_retained_without_overwriting_newer_source():
    saved, actor, db = doc(file_version_number=3, latest_file_version_number=3), SimpleNamespace(id=uuid4()), fake_db()
    row = version(saved, actor, 2)
    with patch.object(service, "versions", AsyncMock(return_value=[row])), patch.object(service.upload_validation, "seal_s3_upload", AsyncMock(return_value=("oldsealed", None))), patch.object(service.upload_validation, "validate_s3_pdf_upload", AsyncMock()), patch.object(service, "_content_hash", return_value="oldhash"):
        assert not await service.complete(db, saved, row, actor)
    assert row.status == "uploaded" and saved.file_version_number == 3
    assert saved.s3_key == "original.pdf" and saved.ai_notes == "Prior payoff 10000"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_latest_version_stops_old_facts_without_destroying_prior_binary():
    saved, actor, db = doc(latest_file_version_number=2), SimpleNamespace(id=uuid4()), fake_db()
    row = version(saved, actor)
    with patch.object(service, "versions", AsyncMock(return_value=[row])), patch.object(service.upload_validation, "seal_s3_upload", AsyncMock(return_value=("badsealed", None))), patch.object(service.upload_validation, "validate_s3_pdf_upload", AsyncMock(side_effect=service.upload_validation.PasswordProtectedPDF("locked"))), patch.object(service.upload_validation, "discard_s3_upload", AsyncMock()), patch("app.services.activity_log.mark_loan_dirty", AsyncMock()):
        with pytest.raises(HTTPException) as error:
            await service.complete(db, saved, row, actor)
    assert error.value.status_code == 422 and row.status == "rejected"
    assert saved.s3_key is None and saved.latest_file_version_status == "rejected"
    assert saved.verified_at is None and saved.current_file_version_id is None
    db.commit.assert_awaited_once()


def test_history_retains_dated_original_and_explicit_pending_rejected_states_without_storage_keys():
    saved, actor = doc(), SimpleNamespace(id=uuid4())
    old = version(saved, actor, 1, id=saved.current_file_version_id, status="uploaded", analysis_status="scanned")
    new = version(saved, actor, 2, status="rejected", analysis_status="failed")
    state = service.history(saved, [new, old], can_upload=True)
    assert state["latest_file_id"] == new.id and state["current_file_id"] is None
    assert state["versions"][0]["document_version_status"] == "rejected"
    assert not state["versions"][0]["can_download"] and state["versions"][1]["can_download"]
    assert "s3_key" not in str(state)
    assert not any(row["is_current_document_version"] for row in state["versions"])


@pytest.mark.asyncio
async def test_older_completion_cannot_revive_source_after_a_newer_rejection():
    saved, actor, db = doc(latest_file_version_number=3, latest_file_version_status="rejected", s3_key=None), SimpleNamespace(id=uuid4()), fake_db()
    row = version(saved, actor, 2)
    rejected = version(saved, actor, 3, status="rejected", analysis_status="failed")
    with patch.object(service, "versions", AsyncMock(return_value=[rejected, row])), patch.object(service.upload_validation, "seal_s3_upload", AsyncMock(return_value=("oldsealed", None))), patch.object(service.upload_validation, "validate_s3_pdf_upload", AsyncMock()), patch.object(service, "_content_hash", return_value="oldhash"):
        assert not await service.complete(db, saved, row, actor)
    assert row.analysis_status == "superseded" and saved.s3_key is None
    assert saved.latest_file_version_status == "rejected" and not saved.scan_dirty
    db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_failed", [False, True])
async def test_reverting_to_historical_bytes_or_retrying_failed_source_is_a_new_version(previous_failed):
    saved, actor, db = doc(file_version_number=2, latest_file_version_number=3), SimpleNamespace(id=uuid4()), fake_db()
    first = version(saved, actor, 1, status="uploaded", analysis_status="scanned", content_hash="original-A")
    second = version(saved, actor, 2, id=saved.current_file_version_id, status="uploaded", analysis_status="failed" if previous_failed else "scanned", content_hash="original-A" if previous_failed else "different-B")
    latest = version(saved, actor, 3, analysis_snapshot={"upload_request_fingerprint": "receipt"})
    with patch.object(service, "versions", AsyncMock(return_value=[latest, second, first])), patch.object(service.upload_validation, "seal_s3_upload", AsyncMock(return_value=("newsealed", None))), patch.object(service.upload_validation, "validate_s3_pdf_upload", AsyncMock()), patch.object(service, "_content_hash", return_value="original-A"):
        assert await service.complete(db, saved, latest, actor)
    assert saved.file_version_number == 3 and saved.current_file_version_id == latest.id
    assert saved.ai_scan_status == "queued" and saved.ai_notes is None
    assert latest.status == "uploaded" and latest.analysis_snapshot["upload_request_fingerprint"] == "receipt"


@pytest.mark.asyncio
async def test_legacy_history_is_scoped_and_payment_proof_cannot_be_replaced():
    from app.routers import documents

    saved, db = doc(), fake_db()
    db.get.return_value = saved
    user = SimpleNamespace(id=uuid4(), role=Role.CLIENT)
    with patch.object(documents, "_can_access_loan", AsyncMock(return_value=False)), patch.object(service, "versions", AsyncMock()) as read:
        with pytest.raises(HTTPException) as error:
            await documents.document_version_history(saved.id, user, db)
    assert error.value.status_code == 404
    read.assert_not_awaited()


def test_payoff_extraction_keeps_named_amounts_distinct_and_never_guesses_purchase_price():
    import json

    from app.services.document_scanner import _parse_response

    scan = _parse_response(json.dumps({"document_type": "Payoff letter", "key_amounts": [250000, 110000],
        "extracted_facts": {"payoff_amount": 110000, "original_amount": 250000, "payoff_good_through": "2026-11-30"}}))
    assert scan.extracted_facts["payoff_amount"] == 110000
    assert scan.extracted_facts["original_amount"] == 250000
    assert "purchase_price" not in scan.extracted_facts


def test_legacy_scan_rejects_nonfinite_boolean_or_unnamed_facts():
    from app.services.document_scanner import _parse_response

    scan = _parse_response('{"confidence":"NaN","key_amounts":[true,"Infinity",100],"extracted_facts":{"payoff_amount":true,"loan_amount":"Infinity","invented":500}}')
    assert scan.confidence == 0 and scan.key_amounts == [100]
    assert scan.extracted_facts == {}
    assert _parse_response('[]').confidence == 0


def test_zip_collection_preserves_history_but_versions_its_individual_documents():
    saved, actor = doc(name="Statements.zip"), SimpleNamespace(id=uuid4())
    archive = version(saved, actor, 1, file_name="Statements.zip", content_type="application/zip", status="uploaded")
    assert not service.history(saved, [archive], can_upload=True)["can_upload_new_version"]
