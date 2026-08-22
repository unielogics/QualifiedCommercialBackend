from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bucket import Bucket, BucketAIReview, BucketFile
from app.models.operator_file import BucketIntakeLink, BucketIntakeLinkFile
from app.models.public_underwriting_intake import PublicUnderwritingIntake


async def active_links_for_sources(
    db: AsyncSession,
    *,
    bucket_ids: set[UUID],
    intake_ids: set[UUID],
) -> list[BucketIntakeLink]:
    if not bucket_ids and not intake_ids:
        return []
    predicates = []
    if bucket_ids:
        predicates.append(BucketIntakeLink.bucket_id.in_(bucket_ids))
    if intake_ids:
        predicates.append(BucketIntakeLink.intake_id.in_(intake_ids))
    return list(
        (
            await db.execute(
                select(BucketIntakeLink).where(
                    BucketIntakeLink.unlinked_at.is_(None), or_(*predicates)
                )
            )
        )
        .scalars()
        .all()
    )


async def selected_files_for_intake(db: AsyncSession, intake_id: UUID) -> list[BucketFile]:
    """Return live external file references selected for an intake."""

    return list(
        (
            await db.execute(
                select(BucketFile)
                .join(
                    BucketIntakeLinkFile,
                    BucketIntakeLinkFile.bucket_file_id == BucketFile.id,
                )
                .join(
                    BucketIntakeLink,
                    BucketIntakeLink.id == BucketIntakeLinkFile.link_id,
                )
                .where(
                    BucketIntakeLink.intake_id == intake_id,
                    BucketIntakeLink.unlinked_at.is_(None),
                    BucketIntakeLinkFile.removed_at.is_(None),
                    BucketFile.deleted_at.is_(None),
                    BucketFile.status == "uploaded",
                )
                .order_by(BucketFile.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def review_files_for_intake(
    db: AsyncSession, intake: PublicUnderwritingIntake
) -> list[BucketFile]:
    primary = list(
        (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.bucket_id == intake.bucket_id,
                    BucketFile.status == "uploaded",
                    BucketFile.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    external = await selected_files_for_intake(db, intake.id)
    by_id = {file.id: file for file in [*primary, *external]}
    return list(by_id.values())


async def queue_link_change_review(
    db: AsyncSession,
    *,
    intake: PublicUnderwritingIntake,
    requested_by_user_id: UUID,
) -> BucketAIReview:
    bucket = await db.get(Bucket, intake.bucket_id)
    files = await review_files_for_intake(db, intake)
    review = BucketAIReview(
        bucket_id=intake.bucket_id,
        requested_by_user_id=requested_by_user_id,
        status="queued",
        context_snapshot=(bucket.ai_context or {}) if bucket else {},
        file_ids=[str(file.id) for file in files],
        provider="bedrock",
    )
    db.add(review)
    await db.flush()
    return review
