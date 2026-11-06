"""Versioned uploads for the legacy vault, without changing checklist identity."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DocStatus, Role
from app.models.document import Document
from app.models.document_analysis_result import DocumentAnalysisResult
from app.models.document_file_version import DocumentFileVersion
from app.services import upload_validation


async def versions(db: AsyncSession, doc: Document) -> list[DocumentFileVersion]:
    return list((await db.execute(select(DocumentFileVersion).where(
        DocumentFileVersion.document_id == doc.id,
    ).order_by(DocumentFileVersion.version_number.desc()))).scalars().all())


def scan_snapshot(doc: Any) -> dict:
    return {key: str(value) if key in {"verified_at", "ai_scan_confidence", "verified_by"} and value is not None else value
            for key in ("status", "ai_notes", "ai_scan_status", "ai_scan_confidence", "verified_at", "verified_by")
            for value in [getattr(doc, key, None)]}


def archive_collection(doc: Any, rows: list[DocumentFileVersion]) -> bool:
    latest = rows[0] if rows else None
    name = latest.file_name if latest is not None else doc.name
    content_type = latest.content_type if latest is not None else ""
    return name.casefold().endswith(".zip") or content_type.casefold() in {"application/zip", "application/x-zip-compressed"}


async def reserve(db: AsyncSession, doc: Document, payload: Any, actor: Any, *, initial: bool = False) -> DocumentFileVersion:
    rows = await versions(db, doc)
    if getattr(payload, "replace_document_id", None) and archive_collection(doc, rows):
        raise HTTPException(409, "ZIP archives are collections. Upload a new version of the individual document instead.")
    if not rows and doc.s3_key and not initial:
        baseline = DocumentFileVersion(
            id=uuid4(), document_id=doc.id, version_number=1, file_name=doc.name,
            s3_key=doc.s3_key, content_type="application/octet-stream", source="legacy_baseline",
            status="uploaded", analysis_status=doc.ai_scan_status or "unscanned",
            analysis_snapshot=scan_snapshot(doc), created_at=doc.updated_at or doc.created_at,
        )
        db.add(baseline)
        rows = [baseline]
        doc.current_file_version_id = baseline.id
        doc.file_version_number = 1
        doc.latest_file_version_number = 1
        await db.flush()
    retry_key = getattr(payload, "idempotency_key", None)
    if retry_key:
        receipt = next((row for row in rows if row.idempotency_key == retry_key), None)
        if receipt is not None:
            if receipt.file_name != payload.name or receipt.content_type != payload.content_type or receipt.uploaded_by_user_id != actor.id:
                raise HTTPException(409, "This upload retry key belongs to another document version")
            return receipt
    latest = max((row.version_number for row in rows), default=0)
    if getattr(payload, "replace_document_id", None) is not None and payload.expected_document_version != latest:
        raise HTTPException(409, "The document changed; reload its history before uploading a new version")
    version_id = uuid4()
    row = DocumentFileVersion(
        id=version_id, document_id=doc.id, version_number=latest + 1,
        file_name=payload.name, content_type=payload.content_type,
        s3_key=f"loans/document-versions/{doc.loan_id}/{doc.id}/{version_id}",
        idempotency_key=retry_key, uploaded_by_user_id=actor.id, status="uploading",
        analysis_status="unscanned", source="upload", analysis_snapshot={},
    )
    db.add(row)
    doc.latest_file_version_number = row.version_number
    doc.latest_file_version_status = "uploading"
    await db.flush()
    return row


def _content_hash(key: str, version_id: str | None = None) -> str:
    from app.config import get_settings

    parameters = {"Bucket": get_settings().s3_bucket, "Key": key}
    if version_id:
        parameters["VersionId"] = version_id
    response = upload_validation._s3_client().get_object(**parameters)
    raw = upload_validation._read_body(response, max_bytes=upload_validation.DEFAULT_MAX_UPLOAD_BYTES)
    return hashlib.sha256(raw).hexdigest()


async def complete(db: AsyncSession, doc: Document, row: DocumentFileVersion, actor: Any) -> bool:
    """Promote only this validated upload; return False for a durable replay."""
    legacy_attempt = row.source == "legacy_baseline" and row.uploaded_by_user_id is None
    staff_completion = getattr(actor, "role", None) in {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER}
    if row.document_id != doc.id or (row.uploaded_by_user_id != actor.id and not legacy_attempt and not staff_completion):
        raise HTTPException(404, "Document upload not found")
    if row.status in {"uploaded", "duplicate"}:
        return False
    if row.status != "uploading":
        raise HTTPException(409, "This upload was rejected; upload a corrected version")
    prior = await versions(db, doc)
    material = [item for item in prior if item.id != row.id and item.status not in {"uploading", "duplicate"}]
    latest_received_number = max([row.version_number, doc.file_version_number or 0, *[item.version_number for item in material]])
    superseded_attempt = row.version_number < latest_received_number
    sealed_key = None
    try:
        sealed_key, storage_version = await upload_validation.seal_s3_upload(row.s3_key, file_id=row.id)
        validated_version = await upload_validation.validate_s3_pdf_upload(s3_key=sealed_key, file_name=row.file_name, content_type=row.content_type)
        storage_version = validated_version if isinstance(validated_version, str) else storage_version
        content_hash = await asyncio.to_thread(_content_hash, sealed_key, storage_version)
        row.s3_key = sealed_key
        row.s3_version_id = storage_version
    except upload_validation.UploadInspectionUnavailable as exc:
        if sealed_key:
            await upload_validation.discard_s3_upload(sealed_key)
        raise HTTPException(503, "The uploaded file could not be inspected; retry completion") from exc
    except (upload_validation.PasswordProtectedPDF, upload_validation.UploadTooLarge, upload_validation.UploadSizeMismatch) as exc:
        row.status = "rejected"
        row.analysis_status = "failed"
        row.analysis_snapshot = {**(row.analysis_snapshot or {}), "reason": type(exc).__name__}
        row.uploaded_at = datetime.now(UTC)
        if sealed_key:
            await upload_validation.discard_s3_upload(sealed_key)
        if not superseded_attempt:
            doc.s3_key = None
            doc.current_file_version_id = None
            doc.file_version_number = row.version_number
            doc.latest_file_version_status = "rejected"
            doc.status = DocStatus.FLAGGED
            doc.ai_scan_status = "failed"
            doc.scan_dirty = False
            doc.ai_notes = "Latest replacement was rejected; prior versions remain in History."
            doc.verified_at = None
            doc.verified_by = None
            await invalidate_analyses(db, doc)
            from app.services.activity_log import mark_loan_dirty
            await mark_loan_dirty(db, doc.loan_id)
        await db.commit()
        raise HTTPException(422, "The replacement could not be accepted. The prior document is preserved.") from exc
    except Exception as exc:
        if sealed_key:
            await upload_validation.discard_s3_upload(sealed_key)
        raise HTTPException(503, "The uploaded file could not be read; retry completion") from exc
    if legacy_attempt:
        row.uploaded_by_user_id = actor.id
    row.content_hash = content_hash
    row.uploaded_at = datetime.now(UTC)
    row.status = "uploaded"
    if superseded_attempt:
        row.analysis_status = "superseded"
        return False
    previous_material = max((item for item in material if item.version_number < row.version_number), key=lambda item: item.version_number, default=None)
    duplicate = previous_material if previous_material is not None and (
        previous_material.id == doc.current_file_version_id
        and previous_material.status == "uploaded" and previous_material.analysis_status in {"scanned", "reviewed"}
        and previous_material.content_hash == content_hash and doc.s3_key is not None
        and doc.latest_file_version_status not in {"failed", "rejected"}
    ) else None
    if duplicate is not None:
        row.status = "duplicate"
        row.analysis_status = duplicate.analysis_status
        row.analysis_snapshot = {**(row.analysis_snapshot or {}), "duplicate_of_version_id": str(duplicate.id)}
        if row.version_number == doc.latest_file_version_number:
            doc.latest_file_version_status = "duplicate"
        return False
    if doc.current_file_version_id:
        previous = await db.get(DocumentFileVersion, doc.current_file_version_id)
        if previous is not None:
            previous.analysis_snapshot = {**(previous.analysis_snapshot or {}), **scan_snapshot(doc)}
            previous.analysis_status = doc.ai_scan_status
    doc.current_file_version_id = row.id
    doc.file_version_number = row.version_number
    doc.s3_key = row.s3_key
    doc.name = row.file_name
    doc.status = DocStatus.RECEIVED
    doc.received_on = row.uploaded_at.date()
    doc.verified_at = None
    doc.verified_by = None
    doc.ai_notes = None
    doc.ai_scan_confidence = None
    doc.ai_scan_status = "queued"
    doc.scan_dirty = True
    row.analysis_status = "queued"
    if row.version_number == doc.latest_file_version_number:
        doc.latest_file_version_status = "pending"
    await invalidate_analyses(db, doc)
    return True


async def invalidate_analyses(db: AsyncSession, doc: Document) -> None:
    await db.execute(update(DocumentAnalysisResult).where(
        DocumentAnalysisResult.document_id == doc.id,
    ).values(is_current=False).execution_options(synchronize_session=False))
    for cached in getattr(db, "identity_map", {}).values():
        if isinstance(cached, DocumentAnalysisResult) and cached.document_id == doc.id:
            cached.is_current = False


def history(doc: Document, rows: list[DocumentFileVersion], *, can_upload: bool) -> dict:
    material = [row for row in rows if row.status not in {"uploading", "duplicate"}]
    latest_received = max(material, key=lambda row: row.version_number, default=None)
    usable = latest_received if latest_received is not None and (
        latest_received.id == doc.current_file_version_id and doc.s3_key is not None
        and latest_received.status == "uploaded" and latest_received.analysis_status in {"scanned", "reviewed"}
        and doc.latest_file_version_status not in {"failed", "rejected"}
    ) else None
    entries = []
    for row in rows:
        current = usable is not None and row.id == usable.id
        state = (row.status if row.status in {"duplicate", "rejected"} else "failed" if row.analysis_status == "failed"
                 else "superseded" if latest_received is not None and row.version_number < latest_received.version_number
                 else "pending" if row.status == "uploading" or row.analysis_status != "scanned"
                 else "current" if current else "superseded")
        if current:
            state = "current"
        entries.append({"id": row.id, "file_name": row.file_name, "content_type": row.content_type,
                        "created_at": row.created_at, "updated_at": row.uploaded_at or row.created_at,
                        "document_family_id": doc.id, "document_version_number": row.version_number,
                        "document_version_status": state, "is_current_document_version": current,
                        "analysis_status": row.analysis_status,
                        "source": row.source, "uploaded_at": row.uploaded_at,
                        "can_preview": row.status in {"uploaded", "duplicate"}, "can_download": row.status in {"uploaded", "duplicate"}})
    return {"document_family_id": doc.id, "current_file_id": usable.id if usable else None,
            "latest_file_id": rows[0].id if rows else None, "versions": entries, "can_upload_new_version": can_upload and not archive_collection(doc, rows)}
