"""One current source per explicitly identified document, never a filename guess.

The newest received material version blocks older files even when it needs
review, failed, was rejected, or was removed. Uploading reservations are not
received evidence; byte-identical retries do not replace their material source.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased

from app.models.bucket import BucketFile


def _latest_material_clause(file_id):
    candidate, newer = aliased(BucketFile), aliased(BucketFile)
    def received(file):
        return and_(file.status != "uploading", file.document_version_status != "duplicate")
    return exists(select(candidate.id).where(
        candidate.id == file_id,
        candidate.deleted_at.is_(None), received(candidate),
        ~exists(select(newer.id).where(
            newer.bucket_id == candidate.bucket_id,
            newer.document_family_id == candidate.document_family_id,
            newer.document_version_number > candidate.document_version_number,
            received(newer),
        )),
    ))


def latest_document_file_clause(file_id):
    candidate = aliased(BucketFile)
    return and_(_latest_material_clause(file_id), exists(select(candidate.id).where(
        candidate.id == file_id,
        or_(candidate.parent_zip_file_id.is_(None), _latest_material_clause(candidate.parent_zip_file_id)),
    )))


def current_document_file_clause(file_id):
    candidate = aliased(BucketFile)
    return and_(latest_document_file_clause(file_id), exists(select(candidate.id).where(
        candidate.id == file_id, candidate.status == "uploaded",
        candidate.is_current_document_version.is_(True), candidate.document_version_status == "current",
    )))


async def document_file_ids(db: Any, file_ids, *, usable: bool) -> set[UUID]:
    ids = set(file_ids)
    if not ids or not isinstance(getattr(db, "info", None), dict):
        return ids
    predicate = current_document_file_clause if usable else latest_document_file_clause
    return set((await db.execute(select(BucketFile.id).where(BucketFile.id.in_(ids), predicate(BucketFile.id)))).scalars().all())


async def filter_document_files(db: Any, files: list, *, usable: bool = True) -> list:
    selected = await document_file_ids(db, (file.id for file in files), usable=usable)
    return [file for file in files if file.id in selected]


async def lock_document_roots(db: Any, files: list) -> None:
    """Always acquire a family root before its material file or profile rows."""
    if not files or not isinstance(getattr(db, "info", None), dict):
        return
    families = {getattr(file, "document_family_id", None) for file in files}
    families.discard(None)
    with getattr(db, "no_autoflush", nullcontext()):
        parents = {getattr(file, "parent_zip_file_id", None) for file in files}
        parents.discard(None)
        if parents:
            families.update((await db.execute(select(BucketFile.document_family_id).where(
                BucketFile.id.in_(parents),
            ))).scalars().all())
        if families:
            await db.execute(select(BucketFile.id).where(
                BucketFile.document_family_id.in_(families), BucketFile.document_version_number == 1,
            ).order_by(BucketFile.id).with_for_update())


async def lock_latest_sources(db: Any, files: list) -> bool:
    """Serialize review publication with replacement reservation/completion."""
    if not files or not isinstance(getattr(db, "info", None), dict):
        return True
    await lock_document_roots(db, files)
    with getattr(db, "no_autoflush", nullcontext()):
        selected = await document_file_ids(db, (file.id for file in files), usable=False)
    return selected == {file.id for file in files}


def document_version_fields(file: Any) -> dict:
    return {
        "bucket_id": str(file.bucket_id) if getattr(file, "bucket_id", None) else None,
        "source_kind": getattr(file, "source_kind", None),
        "source_detail": getattr(file, "source_detail", None),
        "managed_financial_form_history": str(getattr(file, "source_detail", "") or "").startswith("financial_version:")
        or getattr(file, "source_entity_type", None) in {"financial_form_version", "financial_version"},
        "document_family_id": str(file.document_family_id) if getattr(file, "document_family_id", None) else None,
        "document_version_number": getattr(file, "document_version_number", None) or 1,
        "document_version_status": getattr(file, "document_version_status", None) or "current",
        "is_current_document_version": getattr(file, "is_current_document_version", True),
    }


async def linked_document_versions(db: Any, files: list) -> dict[str, dict]:
    """Read-only version metadata for linked evidence; no saved draft changes."""
    result = {str(file.id): {"file_id": str(file.id), **document_version_fields(file)} for file in files}
    if not files or not isinstance(getattr(db, "info", None), dict):
        return result
    families = {getattr(file, "document_family_id", None) for file in files}
    families.discard(None)
    if not families:
        return result
    latest = list((await db.execute(select(BucketFile).where(
        BucketFile.document_family_id.in_(families), latest_document_file_clause(BucketFile.id),
    ))).scalars().all())
    # Only versions already inside this caller's evidence scope can be opened.
    visible_ids = {file.id for file in files}
    by_family = {file.document_family_id: file for file in latest if file.id in visible_ids}
    for file in files:
        current = by_family.get(getattr(file, "document_family_id", None))
        result[str(file.id)]["is_latest_document_version"] = current is not None and current.id == file.id
        result[str(file.id)]["is_current_document_version"] = bool(
            current is not None and current.id == file.id and current.is_current_document_version
            and current.document_version_status == "current"
        )
        if current is not None:
            result[str(file.id)].update(
                latest_file_id=str(current.id), latest_version_number=current.document_version_number,
                latest_version_status=current.document_version_status,
                latest_document_version_status=current.document_version_status,
                is_latest_document_version=current.id == file.id,
            )
        else:
            result[str(file.id)]["latest_document_version_status"] = "unavailable"
    return result


class PendingDocumentAnalysis:
    """Collection may show a received update without promoting its facts."""

    def __init__(self, analysis: Any, file: Any):
        self.original = analysis
        self.status = getattr(analysis, "status", "pending")
        self.classification = None
        self.confidence = "low"
        self.usability_status = "processing" if self.status in {"pending", "running"} else "needs_review"
        self.summary = "The latest document version is received and needs review before its figures can be used."
        self.analysis = {"key_facts": {}, "read_warnings": [self.summary]}
        self.evidence_claims = []
        self.document_type_candidates = []
        self.document_version_status = getattr(file, "document_version_status", "pending")

    def __getattr__(self, name):
        return getattr(self.original, name)
