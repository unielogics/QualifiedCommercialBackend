"""Dealer document versions keep the existing scoped upload/approval workflow."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from app.dealer_os.models import DealerDocument


def family(doc: Any):
    return getattr(doc, "document_family_id", None) or doc.id


def number(doc: Any) -> int:
    return getattr(doc, "document_version_number", None) or 1


async def rows(db: Any, doc: DealerDocument, *, lock: bool = False) -> list[DealerDocument]:
    if lock:
        from app.models.bucket import BucketFile
        from app.services.document_version_selection import lock_document_roots
        with getattr(db, "no_autoflush", nullcontext()):
            mirror_id = getattr(doc, "bucket_file_id", None)
            if mirror_id is None and getattr(doc, "supersedes_document_id", None):
                previous = await db.get(DealerDocument, doc.supersedes_document_id)
                mirror_id = previous.bucket_file_id if previous else None
            if mirror_id is not None:
                mirror = await db.get(BucketFile, mirror_id)
                if mirror is not None:
                    await lock_document_roots(db, [mirror])
                    newest = (await db.execute(select(BucketFile.id).where(
                        BucketFile.bucket_id == mirror.bucket_id,
                        BucketFile.document_family_id == mirror.document_family_id,
                    ).order_by(BucketFile.document_version_number.desc()).limit(1))).scalar_one_or_none()
                    if newest is not None and newest != mirror.id:
                        raise HTTPException(409, "A newer version exists in the financial workspace; review that document history before updating it here")
        with getattr(db, "no_autoflush", nullcontext()):
            await db.execute(select(DealerDocument.id).where(
                DealerDocument.dealer_id == doc.dealer_id,
                DealerDocument.document_family_id == family(doc),
                DealerDocument.document_version_number == 1,
            ).with_for_update())
    return list((await db.execute(select(DealerDocument).where(
        DealerDocument.dealer_id == doc.dealer_id,
        DealerDocument.document_family_id == family(doc),
    ).order_by(DealerDocument.document_version_number.desc()))).scalars().all())


async def prepare(
    db: Any, doc: DealerDocument, *, replaces_document_id: UUID | None,
    expected_document_version: int | None, pinned_account_id: UUID | None,
    idempotency_key: UUID | None = None,
) -> DealerDocument | None:
    doc.id = doc.id or uuid4()
    fingerprint = hashlib.sha256(json.dumps({
        "filename": doc.filename, "kind": doc.kind, "content_hash": doc.content_hash,
        "account_id": str(pinned_account_id) if pinned_account_id else None,
        "uploaded_by_user_id": str(doc.uploaded_by_user_id) if doc.uploaded_by_user_id else None,
        "replaces_document_id": str(replaces_document_id) if replaces_document_id else None,
    }, sort_keys=True).encode()).hexdigest()
    if idempotency_key is not None:
        retry_lock = int.from_bytes(hashlib.sha256(f"dealer-upload:{doc.dealer_id}:{idempotency_key}".encode()).digest()[:8], "big", signed=True)
        with getattr(db, "no_autoflush", nullcontext()):
            await db.execute(select(func.pg_advisory_xact_lock(retry_lock)))
        existing = (await db.execute(select(DealerDocument).where(
            DealerDocument.dealer_id == doc.dealer_id, DealerDocument.upload_idempotency_key == idempotency_key,
        ))).scalar_one_or_none()
        if existing is not None:
            if existing.upload_request_fingerprint != fingerprint:
                raise HTTPException(409, "This retry key was already used for a different document")
            return existing
    doc.upload_idempotency_key = idempotency_key
    doc.upload_request_fingerprint = fingerprint
    doc.document_family_id = doc.id
    doc.document_version_number = 1
    doc.document_version_status = "pending"
    doc.is_current_document_version = False
    if replaces_document_id is None:
        if expected_document_version is not None:
            raise HTTPException(422, "Choose the document being replaced")
        return
    previous = await db.get(DealerDocument, replaces_document_id)
    if previous is None or previous.dealer_id != doc.dealer_id or previous.status == "deleted":
        raise HTTPException(404, "Document to replace was not found for this client")
    if previous.kind == "archive" or (previous.detected_kind or "") == "archive":
        raise HTTPException(422, "ZIP is a collection; upload new versions of its individual documents")
    members = await rows(db, previous, lock=True)
    latest = max(members or [previous], key=number)
    if latest.id != previous.id or number(latest) != expected_document_version:
        raise HTTPException(409, "A newer document version exists; reload its history before uploading")
    accepted = [member for member in members if member.is_current_document_version and member.status == "extracted"]
    baseline = max(accepted, key=number) if accepted else previous
    if pinned_account_id is not None and baseline.account_id is not None and pinned_account_id != baseline.account_id:
        raise HTTPException(422, "This statement belongs to a different account; upload it as a separate document")
    doc.document_family_id = family(previous)
    doc.document_version_number = number(previous) + 1
    doc.supersedes_document_id = previous.id
    return None


async def assert_latest(db: Any, doc: DealerDocument) -> list[DealerDocument]:
    if not isinstance(getattr(db, "info", None), dict):
        return [doc]
    members = await rows(db, doc, lock=True)
    material = [member for member in members if member.document_version_status != "duplicate"]
    latest = max(material or [doc], key=number)
    if latest.id != doc.id:
        raise HTTPException(409, "Only the latest document version can update financial figures")
    return members


async def prelock_related(db: Any, dealer_id: UUID, doc_id: UUID) -> None:
    """Recovery/deletion lock order: all Bucket roots, then Dealer roots."""
    if not isinstance(getattr(db, "info", None), dict):
        return
    from sqlalchemy import or_

    from app.models.bucket import BucketFile
    from app.services.document_version_selection import lock_document_roots

    with getattr(db, "no_autoflush", nullcontext()):
        selected = await db.get(DealerDocument, doc_id)
        if selected is None or selected.dealer_id != dealer_id:
            return
        parent_id = selected.parent_document_id or selected.id
        related = list((await db.execute(select(DealerDocument).where(
            DealerDocument.dealer_id == dealer_id,
            or_(DealerDocument.id.in_({doc_id, parent_id}), DealerDocument.parent_document_id == parent_id),
        ))).scalars().all())
        mirror_ids = {member.bucket_file_id for member in related if member.bucket_file_id is not None}
        if mirror_ids:
            mirrors = list((await db.execute(select(BucketFile).where(BucketFile.id.in_(mirror_ids)))).scalars().all())
            await lock_document_roots(db, mirrors)
        families = {family(member) for member in related}
        if families:
            await db.execute(select(DealerDocument.id).where(
                DealerDocument.dealer_id == dealer_id,
                DealerDocument.document_family_id.in_(families), DealerDocument.document_version_number == 1,
            ).order_by(DealerDocument.id).with_for_update())


async def extract_latest(db: Any, doc: DealerDocument, extractor, raw: bytes | None = None, account_id: UUID | None = None) -> bool:
    """Stage replacement projections atomically; failure keeps prior figures."""
    members = await assert_latest(db, doc)
    if getattr(doc, "supersedes_document_id", None) is None:
        await extractor(db, doc, raw, account_id=account_id)
        if getattr(doc, "status", None) == "extracted":
            doc.document_version_status = "current"
            doc.is_current_document_version = True
            if isinstance(getattr(db, "info", None), dict):
                from app.dealer_os.services.engines import recompute_snapshot
                await recompute_snapshot(db, doc.dealer_id)
            return True
        doc.document_version_status = "failed"
        doc.is_current_document_version = False
        return False
    members = await rows(db, doc, lock=True) if getattr(doc, "supersedes_document_id", None) else members
    latest = max(members or [doc], key=number)
    if latest.id != doc.id:
        raise HTTPException(409, "Only the latest document version can update financial figures")
    accepted = [row for row in members if row.id != doc.id and row.is_current_document_version and row.status == "extracted"]
    previous = max(accepted, key=number) if accepted else next((row for row in members if row.id == doc.supersedes_document_id), None)
    if previous is None:
        raise HTTPException(409, "The previous version is unavailable; review this document before extraction")
    if doc.content_hash and previous.content_hash == doc.content_hash and previous.is_current_document_version:
        doc.status = "extracted"
        doc.document_version_status = "duplicate"
        doc.is_current_document_version = False
        doc.detected_kind = previous.detected_kind
        doc.extracted = {"duplicate_of_document_id": str(previous.id), "notes": ["Identical bytes; the previous accepted source remains current."]}
        return False
    from app.dealer_os.services.document_version_projection import (
        identity_conflict,
        retire_previous_projection,
    )

    cached = {}
    conflict = None
    completed = False
    async with db.begin_nested() as staged:
        await retire_previous_projection(db, doc)
        await extractor(db, doc, raw, account_id=account_id)
        cached = {key: deepcopy(getattr(doc, key, None)) for key in ("extracted", "doc_meta", "detected_kind", "account_id", "error", "status")}
        conflict = identity_conflict(doc, previous) if doc.status == "extracted" else None
        completed = doc.status == "extracted" and conflict is None
        if not completed:
            await staged.rollback()
    if not completed:
        await db.refresh(doc)
        for key in ("extracted", "doc_meta", "detected_kind", "account_id"):
            setattr(doc, key, cached.get(key))
        doc.status = "pending_review" if conflict else "failed"
        doc.error = conflict or cached.get("error") or "The replacement could not be analyzed; prior financial figures were preserved."
        doc.document_version_status = "pending" if conflict else "failed"
        doc.is_current_document_version = False
        return False
    for older in members:
        if older.id != doc.id and older.is_current_document_version:
            older.is_current_document_version = False
            older.document_version_status = "superseded"
    doc.document_version_status = "current"
    doc.is_current_document_version = True
    if isinstance(getattr(db, "info", None), dict):
        from app.dealer_os.services.engines import recompute_snapshot
        await recompute_snapshot(db, doc.dealer_id)
    return True


async def history(db: Any, doc: DealerDocument, *, can_upload: bool) -> dict:
    members = await rows(db, doc)
    members = members or [doc]
    latest = max(members, key=number)
    # A pending new source is explicit. Previous published figures may remain
    # until approval, but an older binary is never presented as latest evidence.
    material = [member for member in members if member.document_version_status != "duplicate"]
    usable_latest = max(material or [latest], key=number)
    current = usable_latest if usable_latest.is_current_document_version and usable_latest.status != "deleted" else None
    return {
        "document_family_id": family(doc), "current_file_id": current.id if current else None,
        "latest_file_id": latest.id,
        "can_upload_new_version": can_upload and latest.status != "deleted" and latest.kind != "archive",
        "versions": [{
            "id": member.id, "file_name": member.filename, "content_type": member.content_type,
            "size_bytes": member.size_bytes, "created_at": member.created_at, "updated_at": member.updated_at,
            "uploaded_by_name": member.uploaded_by_name, "status": member.status,
            "document_family_id": family(member), "document_version_number": number(member),
            "document_version_status": member.document_version_status if member.id == latest.id or (current is not None and member.id == current.id) else "superseded",
            "is_current_document_version": current is not None and member.id == current.id,
            "supersedes_file_id": member.supersedes_document_id,
            "can_preview": bool(member.s3_key or member.bucket_file_id) and member.status != "deleted",
            "can_download": bool(member.s3_key or member.bucket_file_id) and member.status != "deleted",
            "analysis_status": "completed" if member.status == "extracted" else member.status,
        } for member in members],
    }
