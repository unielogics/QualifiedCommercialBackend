"""Attach generated PDFs only during an explicit marketplace package update."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .. import enums as E
from ..models import CpListing, CpListingDocument
from . import storage

GENERATED_KINDS = frozenset(
    {
        E.DocumentKind.EXECUTIVE_SUMMARY,
        E.DocumentKind.UNDERWRITING_PACKAGE,
        E.DocumentKind.INVESTOR_PRESENTATION,
    }
)


def document_source(kind: str) -> str:
    # Upload-init accepts only the seven manual kinds; these three are reserved
    # for the generated-package copier, and none can satisfy the PPM gate.
    return "generated_package" if kind in GENERATED_KINDS else "uploaded"


def _size(head: dict[str, Any] | None) -> int:
    try:
        return max(0, int((head or {}).get("size_bytes") or 0))
    except (TypeError, ValueError):
        return 0


async def attach_generated_documents(
    db: AsyncSession,
    listing: CpListing,
    artifacts: list[tuple[Any, str, str]],
    *,
    actor_id: UUID,
    now: datetime,
) -> list[dict[str, Any]]:
    """Reuse the ready attachment for the same artifact; replace changed ones.

    The caller locks/reloads the listing before this operation. All source keys
    and copies are verified before any document is superseded or inserted.
    """
    snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
    published = snapshot.get("published") if isinstance(snapshot.get("published"), dict) else {}
    package = published.get("package") if isinstance(published.get("package"), dict) else {}
    receipts = package.get("documents") if isinstance(package.get("documents"), list) else []
    prepared: list[tuple[Any, str, str, CpListingDocument | None, str, int, int]] = []
    for artifact, kind, name in artifacts:
        if kind not in GENERATED_KINDS:
            raise ValueError("Only generated package document kinds can be attached.")
        if not artifact.s3_key:
            raise HTTPException(
                409, f"The {name.lower()} has no stored PDF; refresh Package Readiness first."
            )
    for artifact, kind, name in artifacts:
        attached_id = next(
            (
                str(row.get("document_id"))
                for row in receipts
                if isinstance(row, dict)
                and row.get("kind") == kind
                and str(row.get("artifact_id")) == str(artifact.id)
            ),
            None,
        )
        attached = next(
            (
                row
                for row in listing.documents
                if str(row.id) == attached_id
                and row.kind == kind
                and row.status == E.DocumentStatus.READY
            ),
            None,
        )
        if attached is not None:
            size = _size(storage.head_object(attached.s3_key))
            if size:
                prepared.append(
                    (artifact, kind, name, attached, attached.s3_key, attached.version, size)
                )
                continue
        version = max((row.version for row in listing.documents if row.kind == kind), default=0) + 1
        key = storage.build_key(listing.id, f"{kind}-v{version}.pdf")
        if not storage.copy_object(artifact.s3_key, key, content_type="application/pdf"):
            raise HTTPException(503, "The package PDF could not be copied onto the listing.")
        size = _size(storage.head_object(key))
        if not size:
            raise HTTPException(
                503,
                "The copied package PDF could not be verified. Retry updating the listing package.",
            )
        prepared.append((artifact, kind, name, None, key, version, size))
    pushed: list[dict[str, Any]] = []
    for artifact, kind, name, attached, key, version, size in prepared:
        if attached is None:
            for previous in listing.documents:
                if previous.kind == kind and previous.status != E.DocumentStatus.SUPERSEDED:
                    previous.status = E.DocumentStatus.SUPERSEDED
                    previous.superseded_at = now
            attached = CpListingDocument(
                listing_id=listing.id,
                kind=kind,
                name=f"{name} v{version}.pdf",
                s3_key=key,
                content_type="application/pdf",
                size_bytes=size,
                version=version,
                status=E.DocumentStatus.READY,
                uploaded_by_id=actor_id,
                uploaded_at=now,
            )
            listing.documents.append(attached)
            db.add(attached)
        else:
            attached.size_bytes = size
        await db.flush()
        pushed.append(
            {
                "kind": str(kind),
                "document_id": str(attached.id),
                "artifact_id": str(artifact.id),
                "version": version,
            }
        )
    return pushed
