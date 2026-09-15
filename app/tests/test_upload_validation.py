from __future__ import annotations

import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pypdf import PdfWriter

from app.enums import DocStatus
from app.routers import buckets as buckets_router
from app.routers import documents as documents_router
from app.schemas.bucket import BucketUploadComplete
from app.schemas.document import DocumentUploadComplete
from app.services import upload_validation


def _pdf(*, password: str | None = None) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner-secret")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _zip_with(name: str, raw: bytes) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, raw)
    return output.getvalue()


def test_unencrypted_pdf_is_accepted() -> None:
    upload_validation.validate_pdf_bytes(
        _pdf(),
        file_name="statement.pdf",
        content_type="application/pdf",
    )


def test_permissions_only_pdf_with_empty_user_password_is_accepted() -> None:
    upload_validation.validate_pdf_bytes(
        _pdf(password=""),
        file_name="bank.pdf",
        content_type="application/pdf",
    )


def test_true_open_password_pdf_is_rejected() -> None:
    with pytest.raises(upload_validation.PasswordProtectedPDF):
        upload_validation.validate_pdf_bytes(
            _pdf(password="client-secret"),
            file_name="bank.pdf",
            content_type="application/pdf",
        )


def test_pdf_magic_cannot_be_disguised_by_filename_or_content_type() -> None:
    with pytest.raises(upload_validation.PasswordProtectedPDF):
        upload_validation.validate_pdf_bytes(
            _pdf(password="client-secret"),
            file_name="upload.bin",
            content_type="application/octet-stream",
        )


def test_malformed_encryption_text_is_not_labeled_password_protected() -> None:
    upload_validation.validate_pdf_bytes(
        b"%PDF-1.7\n/Encrypt definitely-not-a-valid-pdf",
        file_name="damaged.pdf",
        content_type="application/pdf",
    )


def test_zip_containing_locked_pdf_is_rejected_before_children_are_stored() -> None:
    raw = _zip_with("statements/August.pdf", _pdf(password="client-secret"))

    with pytest.raises(upload_validation.PasswordProtectedPDF):
        upload_validation.validate_upload_bytes(
            raw,
            file_name="statements.zip",
            content_type="application/zip",
        )


@pytest.mark.asyncio
async def test_oversized_s3_object_is_rejected_from_head_before_body_read() -> None:
    client = SimpleNamespace(
        head_object=Mock(
            return_value={"ContentLength": upload_validation.DEFAULT_MAX_UPLOAD_BYTES + 1}
        ),
        get_object=Mock(),
    )
    settings = SimpleNamespace(s3_bucket="evidence")

    with (
        patch.object(upload_validation, "get_settings", return_value=settings),
        patch.object(upload_validation, "_s3_client", return_value=client),
        pytest.raises(upload_validation.UploadTooLarge),
    ):
        await upload_validation.validate_s3_pdf_upload(
            s3_key="locked.pdf",
            file_name="locked.pdf",
            content_type="application/pdf",
        )

    client.head_object.assert_called_once()
    client.get_object.assert_not_called()


@pytest.mark.asyncio
async def test_s3_size_mismatch_is_rejected_from_head_before_body_read() -> None:
    client = SimpleNamespace(
        head_object=Mock(return_value={"ContentLength": 11}),
        get_object=Mock(),
    )
    settings = SimpleNamespace(s3_bucket="evidence")

    with (
        patch.object(upload_validation, "get_settings", return_value=settings),
        patch.object(upload_validation, "_s3_client", return_value=client),
        pytest.raises(upload_validation.UploadSizeMismatch),
    ):
        await upload_validation.validate_s3_pdf_upload(
            s3_key="statement.pdf",
            file_name="statement.pdf",
            content_type="application/pdf",
            expected_size_bytes=10,
        )

    client.head_object.assert_called_once()
    client.get_object.assert_not_called()


@pytest.mark.asyncio
async def test_bucket_completion_rejects_quarantines_and_deletes_locked_pdf() -> None:
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        file_name="locked.pdf",
        content_type="application/pdf",
        s3_key="buckets/uploads/locked.pdf",
        status="uploading",
        extraction_status=None,
        extraction_reason=None,
        delete_storage_status=None,
    )
    db = SimpleNamespace(commit=AsyncMock())

    with (
        patch.object(
            upload_validation,
            "validate_s3_pdf_upload",
            side_effect=upload_validation.PasswordProtectedPDF,
        ),
        patch.object(upload_validation, "discard_s3_upload", return_value=True) as discard,
        patch.object(buckets_router, "_log", AsyncMock()) as audit,
        pytest.raises(HTTPException) as error,
    ):
        await buckets_router._validate_completed_bucket_file(
            db,
            file,
            None,
            action="file_upload_rejected",
            actor_name="Client",
        )

    assert error.value.status_code == 422
    assert error.value.detail == {
        "code": "password_protected_pdf",
        "message": "This PDF is password-protected. Remove the password and upload an unlocked copy.",
    }
    assert file.status == "rejected"
    assert file.extraction_status == "rejected"
    assert upload_validation.PASSWORD_PROTECTED_PDF_CODE in file.extraction_reason
    assert file.delete_storage_status == "deleted"
    discard.assert_awaited_once_with(file.s3_key)
    audit.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejected_bucket_completion_is_idempotent_without_another_s3_read() -> None:
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        status="rejected",
        extraction_reason='[{"reason":"password_protected_pdf"}]',
    )
    db = SimpleNamespace(commit=AsyncMock())

    with (
        patch.object(upload_validation, "validate_s3_pdf_upload") as validate,
        pytest.raises(HTTPException) as error,
    ):
        await buckets_router._validate_completed_bucket_file(
            db,
            file,
            None,
            action="file_upload_rejected",
        )

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "password_protected_pdf"
    validate.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_bucket_completion_keeps_requirement_open_when_pdf_is_locked() -> None:
    bucket_id = uuid4()
    link = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        recipient_name="Client",
        recipient_email="client@example.com",
        completed_at=None,
    )
    requested = SimpleNamespace(id=uuid4(), bucket_id=bucket_id, status="requested")
    file = SimpleNamespace(
        id=uuid4(),
        bucket_id=bucket_id,
        upload_link_id=link.id,
        requested_document_id=requested.id,
        deleted_at=None,
        status="uploading",
        file_name="locked.pdf",
        content_type="application/pdf",
        size_bytes=100,
        s3_key="buckets/uploads/locked.pdf",
        uploaded_by_name="Client",
        uploaded_by_email="client@example.com",
        extraction_status=None,
        extraction_reason=None,
        delete_storage_status=None,
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[file, requested]),
        add=lambda _row: None,
        commit=AsyncMock(),
    )
    background = SimpleNamespace(add_task=Mock())

    with (
        patch.object(buckets_router, "_load_upload_link_or_404", AsyncMock(return_value=link)),
        patch.object(
            buckets_router.locked_file_requests,
            "require_current_unlocked_copy_upload_target",
            AsyncMock(),
        ),
        patch.object(
            upload_validation,
            "validate_s3_pdf_upload",
            AsyncMock(side_effect=upload_validation.PasswordProtectedPDF),
        ),
        patch.object(upload_validation, "discard_s3_upload", AsyncMock(return_value=True)),
        patch.object(buckets_router, "_log", AsyncMock()),
        pytest.raises(HTTPException) as error,
    ):
        await buckets_router.request_upload_complete(
            "token",
            BucketUploadComplete(file_id=file.id),
            background,
            SimpleNamespace(),
            db,
        )

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "password_protected_pdf"
    assert file.status == "rejected"
    assert requested.status == "requested"
    assert link.completed_at is None
    background.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_loan_document_completion_rejects_before_received_side_effects() -> None:
    loan = SimpleNamespace(id=uuid4())
    document = SimpleNamespace(
        id=uuid4(),
        loan_id=loan.id,
        name="locked.pdf",
        s3_key="loans/example/locked.pdf",
        status=DocStatus.REQUESTED,
        received_on=None,
        scan_dirty=False,
        ai_scan_status="unscanned",
        ai_notes=None,
    )
    user = SimpleNamespace(id=uuid4(), role="client")
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[document, loan]),
        add=lambda _row: None,
        commit=AsyncMock(),
    )

    with (
        patch.object(documents_router, "_can_access_loan", AsyncMock(return_value=True)),
        patch.object(
            upload_validation,
            "validate_s3_pdf_upload",
            side_effect=upload_validation.PasswordProtectedPDF,
        ),
        patch.object(upload_validation, "discard_s3_upload", return_value=True) as discard,
        patch.object(documents_router.file_events, "emit", AsyncMock()) as emit,
        pytest.raises(HTTPException) as error,
    ):
        await documents_router.upload_complete(
            DocumentUploadComplete(document_id=document.id),
            user,
            db,
        )

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "password_protected_pdf"
    assert document.status == DocStatus.REQUESTED
    assert document.received_on is None
    assert document.s3_key is None
    assert document.scan_dirty is False
    assert document.ai_scan_status == "failed"
    assert document.ai_notes == "password_protected_pdf"
    discard.assert_awaited_once_with("loans/example/locked.pdf")
    db.commit.assert_awaited_once()
    emit.assert_not_awaited()
