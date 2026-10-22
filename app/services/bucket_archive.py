"""Shared security operations for reversible Bucket archival."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bucket import (
    BucketPublicShare,
    BucketShare,
    BucketUploadLink,
    BucketVendorAccess,
)


async def revoke_external_access(db: AsyncSession, bucket_id: UUID) -> int:
    """Permanently revoke active links when a Bucket leaves active work.

    Restoring the Bucket intentionally does not reactivate old client, public,
    vendor, or upload credentials. Staff must issue fresh access explicitly.
    """

    revoked = 0
    for model in (
        BucketUploadLink,
        BucketShare,
        BucketPublicShare,
        BucketVendorAccess,
    ):
        result = await db.execute(
            update(model)
            .where(model.bucket_id == bucket_id, model.status == "active")
            .values(status="revoked")
            .execution_options(synchronize_session=False)
        )
        revoked += result.rowcount or 0
    return revoked

