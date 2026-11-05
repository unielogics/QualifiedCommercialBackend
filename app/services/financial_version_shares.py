"""Read-only delivery of one immutable financial PDF through existing shares."""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app.models.bucket import BucketFile, BucketPublicShare
from app.services import financial_workspace as workspace


async def create_share(db, profile, version_id, *, ttl_days=7) -> dict:
    family, version = await workspace.version_for_profile(db, profile, version_id)
    # Verify both the immutable hash and the storage object before returning a
    # usable link. This also rejects a missing/deleted source PDF.
    await workspace.version_pdf(db, profile, version.id)
    file = await db.get(BucketFile, version.pdf_file_id or version.source_file_id)
    if file is None or file.bucket_id != profile.primary_bucket_id or file.status != "uploaded" or file.deleted_at is not None:
        raise HTTPException(404, "The saved financial PDF is not available for sharing")
    expires = datetime.now(UTC) + timedelta(days=ttl_days)
    share = BucketPublicShare(
        bucket_id=file.bucket_id, token=secrets.token_urlsafe(32),
        recipient_name=f"{family.label} v{version.version_number}"[:180],
        can_preview=True, can_download=True, expires_at=expires, status="active",
    )
    share.files = [file]
    db.add(share)
    await db.flush()
    from app.routers.buckets import _public_url

    return {"id": str(share.id), "url": _public_url(f"/buckets/public-share/{share.token}"),
            "expires_at": expires.isoformat(), "version_id": str(version.id), "file_id": str(file.id)}
