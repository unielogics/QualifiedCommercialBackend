"""Durable upload receipts across the legacy loan's document identities."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.document_file_version import DocumentFileVersion
from app.schemas.document import DocumentUploadInit, DocumentUploadInitResponse
from app.services import upload_validation


def fingerprint(payload: DocumentUploadInit) -> str:
    semantic = payload.model_dump(mode="json", exclude={"idempotency_key"})
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def find(
    db: AsyncSession, payload: DocumentUploadInit, actor: Any,
) -> tuple[Document, DocumentFileVersion] | None:
    """Serialize a scoped retry key before creating or locking any document."""
    if payload.idempotency_key is None:
        return None
    lock_name = f"loan-document-upload:{payload.loan_id}:{actor.id}:{payload.idempotency_key}"
    lock_id = int.from_bytes(hashlib.sha256(lock_name.encode()).digest()[:8], "big", signed=True)
    await db.execute(select(func.pg_advisory_xact_lock(lock_id)))
    rows = (await db.execute(select(Document, DocumentFileVersion).join(
        DocumentFileVersion, DocumentFileVersion.document_id == Document.id,
    ).where(
        Document.loan_id == payload.loan_id,
        DocumentFileVersion.uploaded_by_user_id == actor.id,
        DocumentFileVersion.idempotency_key == payload.idempotency_key,
    ))).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise HTTPException(409, "This upload retry key has conflicting receipts; use a new upload key")
    doc, version = rows[0]
    if (version.analysis_snapshot or {}).get("upload_request_fingerprint") != fingerprint(payload):
        raise HTTPException(409, "This upload retry key belongs to a different upload request")
    return doc, version


def remember(version: DocumentFileVersion, payload: DocumentUploadInit) -> None:
    if payload.idempotency_key is not None:
        version.analysis_snapshot = {
            **(version.analysis_snapshot or {}), "upload_request_fingerprint": fingerprint(payload),
        }


def response(doc: Document, version: DocumentFileVersion, settings: Any) -> DocumentUploadInitResponse:
    # Completed and rejected versions never receive another browser PUT URL.
    upload_url = None
    if settings.s3_bucket and version.status == "uploading":
        upload_url = upload_validation._s3_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.s3_bucket, "Key": version.s3_key,
                    "ContentType": version.content_type, "ServerSideEncryption": "AES256"},
            ExpiresIn=900,
        )
    return DocumentUploadInitResponse(
        document_id=doc.id, version_id=version.id, s3_key=version.s3_key, upload_url=upload_url,
    )
