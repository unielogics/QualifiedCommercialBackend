"""Explicit immutable upload lineage, independent of document type or filename.

Financial form publication remains a separate reviewed workflow. A replacement
retains its original binary, analysis, citations and published figures.
"""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bucket import (
    PROTECTED_RETENTION_CLASSES,
    BucketAIReview,
    BucketFile,
    BucketFileAnalysis,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.bucket import BucketDocumentHistoryRead, BucketDocumentVersionRead


def family_id(file: Any) -> UUID:
    return getattr(file, "document_family_id", None) or file.id


def version_number(file: Any) -> int:
    return getattr(file, "document_version_number", None) or 1


def replacement_allowed(file: Any) -> bool:
    return (
        getattr(file, "deleted_at", None) is None
        and getattr(file, "status", None) in {"uploaded", "rejected"}
        and getattr(file, "retention_class", None) not in PROTECTED_RETENTION_CLASSES
        and not getattr(file, "source_immutable_ref", None)
        and getattr(file, "source_kind", None) != "generated"
        and not str(getattr(file, "file_name", "")).lower().endswith(".zip")
        and "zip" not in str(getattr(file, "content_type", "")).lower()
    )


async def _family_rows(db: AsyncSession, file: BucketFile, *, lock: bool = False) -> list[BucketFile]:
    identity = family_id(file)
    if lock:
        # All replacements and analysis completions acquire the same v1 row
        # before touching any later member; ordinal reservations are serialized.
        with getattr(db, "no_autoflush", nullcontext()):
            await db.execute(select(BucketFile.id).where(
                BucketFile.bucket_id == file.bucket_id,
                BucketFile.document_family_id == identity,
                BucketFile.document_version_number == 1,
            ).with_for_update())
    return list((await db.execute(select(BucketFile).where(
        BucketFile.bucket_id == file.bucket_id,
        BucketFile.document_family_id == identity,
    ).order_by(BucketFile.document_version_number.desc()))).scalars().all())


async def lock_family(db: AsyncSession, file: BucketFile) -> None:
    if isinstance(file, BucketFile) and getattr(file, "document_family_id", None) is not None:
        from app.services.document_version_selection import lock_document_roots
        await lock_document_roots(db, [file])
        await _family_rows(db, file, lock=True)


async def prepare_upload(
    db: AsyncSession, file: BucketFile, *, replaces_file_id: UUID | None,
    expected_document_version: int | None, idempotency_key: UUID | None,
) -> BucketFile | None:
    """Reserve a version, returning an existing matching retry when present."""
    if idempotency_key is not None:
        retry_lock = int.from_bytes(hashlib.sha256(f"bucket-upload:{file.bucket_id}:{idempotency_key}".encode()).digest()[:8], "big", signed=True)
        with getattr(db, "no_autoflush", nullcontext()):
            await db.execute(select(func.pg_advisory_xact_lock(retry_lock)))
        existing = (await db.execute(select(BucketFile).where(
            BucketFile.bucket_id == file.bucket_id,
            BucketFile.upload_idempotency_key == idempotency_key,
        ))).scalar_one_or_none()
        if existing is not None:
            if any(getattr(existing, key, None) != getattr(file, key, None) for key in (
                "file_name", "size_bytes", "content_type", "upload_link_id", "uploaded_by_user_id",
            )) or (file.requested_document_id is not None and existing.requested_document_id != file.requested_document_id) or getattr(existing, "supersedes_file_id", None) != replaces_file_id:
                raise HTTPException(409, "The upload retry key was already used for a different document")
            return existing
    file.upload_idempotency_key = idempotency_key
    file.document_family_id = file.id
    file.document_version_number = 1
    file.document_version_status = "pending"
    file.is_current_document_version = False
    if replaces_file_id is None:
        if expected_document_version is not None:
            raise HTTPException(422, "Choose the document being replaced")
        return None
    if expected_document_version is None:
        raise HTTPException(422, "The latest document version is required for a replacement")
    if str(file.file_name).lower().endswith(".zip") or "zip" in str(file.content_type).lower():
        raise HTTPException(422, "ZIP is a collection; upload new versions of its individual documents")
    target = await db.get(BucketFile, replaces_file_id)
    if target is None or target.bucket_id != file.bucket_id:
        raise HTTPException(404, "Document to replace was not found in this file")
    if not replacement_allowed(target):
        if str(getattr(target, "file_name", "")).lower().endswith(".zip") or "zip" in str(getattr(target, "content_type", "")).lower():
            raise HTTPException(422, "ZIP is a collection; upload new versions of its individual documents")
        raise HTTPException(409, "This document is retained by its original workflow; create the next version there")
    from app.services.document_version_selection import lock_document_roots
    await lock_document_roots(db, [target])
    rows = await _family_rows(db, target, lock=True)
    latest = max(rows or [target], key=version_number)
    if latest.id != target.id or version_number(latest) != expected_document_version:
        raise HTTPException(409, "A newer document version exists; reload its history before uploading")
    if file.requested_document_id is None:
        file.requested_document_id = target.requested_document_id
    if file.requested_document_id != target.requested_document_id:
        raise HTTPException(409, "A replacement must keep the same document requirement")
    file.document_family_id = family_id(target)
    file.document_version_number = version_number(latest) + 1
    file.supersedes_file_id = target.id
    return None


async def mark_received(db: AsyncSession, file: BucketFile) -> None:
    """Invalidate decisions after bytes arrive, without erasing their history."""
    if not isinstance(file, BucketFile) or getattr(file, "document_family_id", None) is None:
        return
    if file.supersedes_file_id is not None and file.document_version_status == "pending":
        for previous in await _family_rows(db, file, lock=True):
            if previous.id != file.id and previous.is_current_document_version:
                previous.is_current_document_version = False
                previous.document_version_status = "superseded"
    marker = {"document_versions_stale": True}
    await db.execute(update(BucketAIReview).where(BucketAIReview.bucket_id == file.bucket_id).values(
        context_snapshot=func.coalesce(BucketAIReview.context_snapshot, literal({}, type_=JSONB)).op("||")(literal(marker, type_=JSONB)),
    ).execution_options(synchronize_session=False))
    await db.execute(update(PublicUnderwritingIntake).where(PublicUnderwritingIntake.bucket_id == file.bucket_id).values(
        latest_review_id=None,
        result_snapshot=PublicUnderwritingIntake.result_snapshot.op("||")(literal({"_document_versions_stale": True}, type_=JSONB)),
    ).execution_options(synchronize_session=False))
    for row in getattr(db, "identity_map", {}).values():
        if isinstance(row, BucketAIReview) and row.bucket_id == file.bucket_id:
            row.context_snapshot = {**(row.context_snapshot or {}), **marker}
        elif isinstance(row, PublicUnderwritingIntake) and row.bucket_id == file.bucket_id:
            row.latest_review_id = None
            row.latest_review = None
            row.intake_state = {key: value for key, value in (row.intake_state or {}).items() if key != "loan_program_fit"}
            if row.result_snapshot:
                row.result_snapshot = {**row.result_snapshot, "_document_versions_stale": True}


async def complete_analysis(
    db: AsyncSession, file: BucketFile, *, usable: bool | None,
    content_hash: str | None = None,
) -> bool:
    """Promote only the newest usable bytes, ignoring late older completions."""
    if not isinstance(file, BucketFile) or getattr(file, "document_family_id", None) is None:
        return usable is True
    rows = await _family_rows(db, file, lock=True)
    if file.status == "rejected" or file.document_version_status == "rejected":
        file.document_version_status = "rejected"
        file.is_current_document_version = False
        return False
    material = [row for row in rows if row.document_version_status != "duplicate" and row.status != "uploading"]
    latest = max(material or [file], key=version_number)
    if latest.id != file.id or file.deleted_at is not None:
        return False
    digest = content_hash or file.content_hash
    previous = [row for row in material if version_number(row) < version_number(file)]
    preceding = max(previous, key=version_number) if previous else None
    duplicate = preceding if preceding is not None and digest and preceding.content_hash == digest and preceding.document_version_status in {"current", "superseded"} else None
    if duplicate is not None:
        file.document_version_status = "duplicate"
        file.duplicate_of_file_id = duplicate.id
        file.is_current_document_version = False
        duplicate.is_current_document_version = True
        duplicate.document_version_status = "current"
        return False
    if usable is not True:
        if usable is None and file.is_current_document_version and file.document_version_status == "current":
            return True
        file.document_version_status = "pending" if usable is None else "failed"
        file.is_current_document_version = False
        return False
    for row in rows:
        if row.id != file.id and row.is_current_document_version:
            row.is_current_document_version = False
            row.document_version_status = "superseded"
    file.is_current_document_version = True
    file.document_version_status = "current"
    return True


async def history(
    db: AsyncSession, bucket_id: UUID, file_id: UUID, *, can_upload: bool, can_preview: bool,
) -> BucketDocumentHistoryRead:
    file = await db.get(BucketFile, file_id)
    if file is None or file.bucket_id != bucket_id:
        raise HTTPException(404, "Document history was not found in this file")
    rows = await _family_rows(db, file)
    rows = rows or [file]
    analyses = list((await db.execute(select(BucketFileAnalysis).where(
        BucketFileAnalysis.bucket_file_id.in_([row.id for row in rows]),
    ).order_by(BucketFileAnalysis.analysis_version.desc(), BucketFileAnalysis.created_at.desc()))).scalars().all())
    latest_analysis = {}
    for analysis in analyses:
        latest_analysis.setdefault(analysis.bucket_file_id, analysis)
    from app.services.locked_file_requests import public_analysis_state
    latest = max(rows, key=version_number)
    current = next((row for row in rows if row.is_current_document_version and row.deleted_at is None), None)
    return BucketDocumentHistoryRead(
        document_family_id=family_id(file), current_file_id=current.id if current else None,
        latest_file_id=latest.id, can_upload_new_version=can_upload and replacement_allowed(latest),
        versions=[BucketDocumentVersionRead(
            id=row.id, bucket_id=row.bucket_id, file_name=row.file_name, content_type=row.content_type,
            size_bytes=row.size_bytes, created_at=row.created_at, updated_at=row.updated_at,
            uploaded_by_name=row.uploaded_by_name, status=row.status,
            document_family_id=family_id(row), document_version_number=version_number(row),
            is_current_document_version=row.is_current_document_version,
            document_version_status=row.document_version_status,
            supersedes_file_id=row.supersedes_file_id, duplicate_of_file_id=row.duplicate_of_file_id,
            analysis_status=public_analysis_state(row, latest_analysis.get(row.id)).analysis_status,
            analysis_reason_code=public_analysis_state(row, latest_analysis.get(row.id)).analysis_reason_code,
            analysis_review_state=public_analysis_state(row, latest_analysis.get(row.id)).analysis_review_state,
            can_preview=can_preview and row.status == "uploaded" and row.deleted_at is None,
            can_download=can_preview and row.status == "uploaded" and row.deleted_at is None,
        ) for row in rows],
    )
