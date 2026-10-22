"""Durable minimum retention for AI Intake workspaces and their evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import case, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket, BucketFile
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.retention import (
    AI_INTAKE_RETENTION_CLASS,
    APPLICATION_RETENTION_CLASS,
    BUCKET_EVIDENCE_RETENTION_CLASS,
    BUCKET_RETENTION_CLASS,
    later_deadline,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _atomic_extend_record(
    db: AsyncSession,
    record,
    model,
    *,
    retention_class: str,
    candidate: datetime,
) -> datetime:
    """Monotonically extend one row without a stale ORM read/write race."""

    row = (
        await db.execute(
            update(model)
            .where(model.id == record.id)
            .values(
                retention_class=func.coalesce(
                    model.retention_class,
                    retention_class,
                ),
                protected_until=case(
                    (model.protected_until.is_(None), candidate),
                    (model.protected_until < candidate, candidate),
                    else_=model.protected_until,
                ),
            )
            .returning(model.retention_class, model.protected_until)
            .execution_options(synchronize_session=False)
        )
    ).one()
    set_committed_value(record, "retention_class", row.retention_class)
    set_committed_value(record, "protected_until", row.protected_until)
    return row.protected_until


async def _extend_bucket_files(
    db: AsyncSession,
    *,
    bucket_id,
    deadline: datetime,
) -> None:
    """Protect active and soft-deleted bytes without weakening stronger holds."""

    await db.execute(
        update(BucketFile)
        .where(BucketFile.bucket_id == bucket_id)
        .values(
            retention_class=func.coalesce(
                BucketFile.retention_class,
                BUCKET_EVIDENCE_RETENTION_CLASS,
            ),
            protected_until=case(
                (BucketFile.protected_until.is_(None), deadline),
                (BucketFile.protected_until < deadline, deadline),
                else_=BucketFile.protected_until,
            ),
        )
        .execution_options(synchronize_session=False)
    )


async def extend_intake_retention(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    anchor: datetime | None = None,
) -> datetime:
    """Extend the intake, its Bucket, and every evidence row atomically."""

    anchor = anchor or _now()
    intake_candidate = later_deadline(
        None,
        anchors=(
            getattr(intake, "created_at", None),
            getattr(intake, "completed_at", None),
            anchor,
        ),
    )
    intake_deadline = await _atomic_extend_record(
        db,
        intake,
        PublicUnderwritingIntake,
        retention_class=AI_INTAKE_RETENTION_CLASS,
        candidate=intake_candidate,
    )

    bucket_id = getattr(intake, "bucket_id", None)
    if bucket_id is None:
        return intake_deadline
    bucket = await db.get(Bucket, bucket_id)
    if bucket is not None:
        bucket_candidate = max(
            intake_deadline,
            later_deadline(
                None,
                anchors=(getattr(bucket, "created_at", None), anchor),
            ),
        )
        bucket_deadline = await _atomic_extend_record(
            db,
            bucket,
            Bucket,
            retention_class=BUCKET_RETENTION_CLASS,
            candidate=bucket_candidate,
        )
        deadline = max(intake_deadline, bucket_deadline)
    else:
        deadline = intake_deadline
    await _extend_bucket_files(db, bucket_id=bucket_id, deadline=deadline)
    await db.execute(
        update(ApplicationProfile)
        .where(
            or_(
                ApplicationProfile.intake_id == intake.id,
                ApplicationProfile.primary_bucket_id == bucket_id,
            )
        )
        .values(
            retention_class=func.coalesce(
                ApplicationProfile.retention_class,
                APPLICATION_RETENTION_CLASS,
            ),
            protected_until=case(
                (ApplicationProfile.protected_until.is_(None), deadline),
                (ApplicationProfile.protected_until < deadline, deadline),
                else_=ApplicationProfile.protected_until,
            ),
        )
        .execution_options(synchronize_session=False)
    )
    return deadline


async def extend_bucket_retention(
    db: AsyncSession,
    bucket: Bucket,
    *,
    anchor: datetime | None = None,
) -> datetime:
    """Extend a Bucket archive and all linked Intake/evidence retention."""

    anchor = anchor or _now()
    candidate = later_deadline(
        None,
        anchors=(getattr(bucket, "created_at", None), anchor),
    )
    deadline = await _atomic_extend_record(
        db,
        bucket,
        Bucket,
        retention_class=BUCKET_RETENTION_CLASS,
        candidate=candidate,
    )
    await _extend_bucket_files(db, bucket_id=bucket.id, deadline=deadline)
    await db.execute(
        update(PublicUnderwritingIntake)
        .where(PublicUnderwritingIntake.bucket_id == bucket.id)
        .values(
            retention_class=func.coalesce(
                PublicUnderwritingIntake.retention_class,
                AI_INTAKE_RETENTION_CLASS,
            ),
            protected_until=case(
                (PublicUnderwritingIntake.protected_until.is_(None), deadline),
                (PublicUnderwritingIntake.protected_until < deadline, deadline),
                else_=PublicUnderwritingIntake.protected_until,
            ),
        )
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(ApplicationProfile)
        .where(ApplicationProfile.primary_bucket_id == bucket.id)
        .values(
            retention_class=func.coalesce(
                ApplicationProfile.retention_class,
                APPLICATION_RETENTION_CLASS,
            ),
            protected_until=case(
                (ApplicationProfile.protected_until.is_(None), deadline),
                (ApplicationProfile.protected_until < deadline, deadline),
                else_=ApplicationProfile.protected_until,
            ),
        )
        .execution_options(synchronize_session=False)
    )
    return deadline


async def extend_application_profile_retention(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    anchor: datetime | None = None,
) -> datetime:
    """Extend a case record and every linked Intake/Bucket evidence record."""

    anchor = anchor or _now()
    candidate = later_deadline(
        None,
        anchors=(
            getattr(profile, "created_at", None),
            getattr(profile, "underwriting_updated_at", None),
            anchor,
        ),
    )
    deadline = await _atomic_extend_record(
        db,
        profile,
        ApplicationProfile,
        retention_class=APPLICATION_RETENTION_CLASS,
        candidate=candidate,
    )

    intake_id = getattr(profile, "intake_id", None)
    if intake_id is not None:
        intake = await db.get(PublicUnderwritingIntake, intake_id)
        if intake is not None:
            deadline = max(
                deadline,
                await extend_intake_retention(db, intake, anchor=anchor),
            )

    bucket_id = getattr(profile, "primary_bucket_id", None)
    if bucket_id is not None:
        bucket = await db.get(Bucket, bucket_id)
        if bucket is not None:
            deadline = max(
                deadline,
                await extend_bucket_retention(db, bucket, anchor=anchor),
            )

    # A linked Bucket/Intake could already have a stronger compliance
    # deadline. Re-read through one final atomic max rather than assigning a
    # potentially stale ORM value.
    await _atomic_extend_record(
        db,
        profile,
        ApplicationProfile,
        retention_class=APPLICATION_RETENTION_CLASS,
        candidate=deadline,
    )
    return profile.protected_until

