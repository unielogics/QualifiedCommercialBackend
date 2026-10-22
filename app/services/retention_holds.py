"""Atomic legal-hold controls shared by retained business records."""

from __future__ import annotations

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.retention import retention_state


async def set_legal_hold(db: AsyncSession, record, model, *, enabled: bool) -> None:
    """Change only this record's explicit hold; ancestor holds remain effective.

    A reason and actor are required by each API and written to that resource's
    existing audit ledger. Keeping scope local prevents releasing a child hold
    when an independently applied parent hold still governs it.
    """

    value = (
        await db.execute(
            update(model)
            .where(model.id == record.id)
            .values(legal_hold=enabled)
            .returning(model.legal_hold)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one()
    set_committed_value(record, "legal_hold", value)


def status_read(record, *, resource_type: str) -> dict:
    return {
        "id": record.id,
        "resource_type": resource_type,
        "retention_class": record.retention_class,
        "protected_until": record.protected_until,
        "legal_hold": record.legal_hold,
        "effective_legal_hold": record.legal_hold,
        "hold_sources": (
            [f"{resource_type}:{record.id}"] if record.legal_hold else []
        ),
        "retention_state": record.retention_state,
    }


async def effective_status_read(
    db: AsyncSession,
    record,
    *,
    resource_type: str,
) -> dict:
    """Return local and linked retention state without hiding independent holds.

    Buckets, intakes, and application profiles form a graph rather than a
    strict parent/child tree.  In particular, a profile may point at both an
    intake and a primary Bucket, and those can be different Buckets.  Walk the
    complete connected component before evaluating deadlines and holds so the
    answer is identical regardless of which record the UI reloads.
    """

    from app.models.application_profile import ApplicationProfile
    from app.models.bucket import Bucket, BucketFile
    from app.models.operator_file import BucketIntakeLink
    from app.models.public_underwriting_intake import PublicUnderwritingIntake

    records: dict[tuple[str, object], tuple[object, bool]] = {}

    def add(kind: str, row_id, deadline, held: bool) -> None:
        if row_id is not None:
            records[(kind, row_id)] = (deadline, bool(held))

    add(
        resource_type,
        record.id,
        record.protected_until,
        record.legal_hold,
    )

    bucket_ids: set = set()
    intake_ids: set = set()
    profile_ids: set = set()
    if resource_type == "bucket":
        bucket_ids.add(record.id)
    elif resource_type == "bucket_file":
        if record.bucket_id is not None:
            bucket_ids.add(record.bucket_id)
    elif resource_type == "ai_intake":
        intake_ids.add(record.id)
        if record.bucket_id is not None:
            bucket_ids.add(record.bucket_id)
    elif resource_type == "application_profile":
        profile_ids.add(record.id)
        if record.intake_id is not None:
            intake_ids.add(record.intake_id)
        if record.primary_bucket_id is not None:
            bucket_ids.add(record.primary_bucket_id)

    # Discover the full connected graph. A single query pass is insufficient:
    # intake A can reveal profile P, which can reveal Bucket B, whose files (or
    # another linked intake) may carry the effective legal hold.
    while True:
        before = (len(bucket_ids), len(intake_ids), len(profile_ids))

        link_filter = []
        if intake_ids:
            link_filter.append(BucketIntakeLink.intake_id.in_(intake_ids))
        if bucket_ids:
            link_filter.append(BucketIntakeLink.bucket_id.in_(bucket_ids))
        if link_filter:
            # Links remain part of the retention graph after unlinked_at is
            # set. Unlinking access is reversible workflow state; it does not
            # erase the historical evidence relationship.
            link_rows = (
                await db.execute(
                    select(
                        BucketIntakeLink.intake_id,
                        BucketIntakeLink.bucket_id,
                    ).where(or_(*link_filter))
                )
            ).all()
            for intake_id, bucket_id in link_rows:
                intake_ids.add(intake_id)
                bucket_ids.add(bucket_id)

        intake_filter = []
        if intake_ids:
            intake_filter.append(PublicUnderwritingIntake.id.in_(intake_ids))
        if bucket_ids:
            intake_filter.append(
                PublicUnderwritingIntake.bucket_id.in_(bucket_ids)
            )
        if intake_filter:
            intake_rows = (
                await db.execute(
                    select(
                        PublicUnderwritingIntake.id,
                        PublicUnderwritingIntake.bucket_id,
                        PublicUnderwritingIntake.protected_until,
                        PublicUnderwritingIntake.legal_hold,
                    ).where(or_(*intake_filter))
                )
            ).all()
            for row_id, bucket_id, deadline, held in intake_rows:
                intake_ids.add(row_id)
                add("ai_intake", row_id, deadline, held)
                if bucket_id is not None:
                    bucket_ids.add(bucket_id)

        profile_filter = []
        if profile_ids:
            profile_filter.append(ApplicationProfile.id.in_(profile_ids))
        if intake_ids:
            profile_filter.append(ApplicationProfile.intake_id.in_(intake_ids))
        if bucket_ids:
            profile_filter.append(
                ApplicationProfile.primary_bucket_id.in_(bucket_ids)
            )
        if profile_filter:
            profile_rows = (
                await db.execute(
                    select(
                        ApplicationProfile.id,
                        ApplicationProfile.intake_id,
                        ApplicationProfile.primary_bucket_id,
                        ApplicationProfile.protected_until,
                        ApplicationProfile.legal_hold,
                    ).where(or_(*profile_filter))
                )
            ).all()
            for (
                row_id,
                intake_id,
                primary_bucket_id,
                deadline,
                held,
            ) in profile_rows:
                profile_ids.add(row_id)
                add("application_profile", row_id, deadline, held)
                if intake_id is not None:
                    intake_ids.add(intake_id)
                if primary_bucket_id is not None:
                    bucket_ids.add(primary_bucket_id)

        after = (len(bucket_ids), len(intake_ids), len(profile_ids))
        if after == before:
            break

    if bucket_ids:
        bucket_rows = (
            await db.execute(
                select(
                    Bucket.id,
                    Bucket.protected_until,
                    Bucket.legal_hold,
                ).where(Bucket.id.in_(bucket_ids))
            )
        ).all()
        for row_id, deadline, held in bucket_rows:
            add("bucket", row_id, deadline, held)

        file_rows = (
            await db.execute(
                select(
                    BucketFile.id,
                    BucketFile.protected_until,
                    BucketFile.legal_hold,
                ).where(BucketFile.bucket_id.in_(bucket_ids))
            )
        ).all()
        for row_id, deadline, held in file_rows:
            add("bucket_file", row_id, deadline, held)

    deadlines = [deadline for deadline, _held in records.values() if deadline]
    effective_deadline = max(deadlines, default=record.protected_until)
    hold_sources = [
        f"{kind}:{row_id}"
        for (kind, row_id), (_deadline, held) in records.items()
        if held
    ]
    effective_hold = bool(hold_sources)
    return {
        "id": record.id,
        "resource_type": resource_type,
        "retention_class": record.retention_class,
        "protected_until": effective_deadline,
        "legal_hold": record.legal_hold,
        "effective_legal_hold": effective_hold,
        "hold_sources": sorted(hold_sources),
        "retention_state": retention_state(
            protected_until=effective_deadline,
            legal_hold=effective_hold,
        ),
    }


async def is_effective_file_soft_deletion_prohibited(
    db: AsyncSession,
    bucket_file,
) -> bool:
    """Honor graph-wide holds when moving retained evidence to Deleted files."""

    from app.models.bucket import PROTECTED_RETENTION_CLASSES

    local_guard = getattr(bucket_file, "is_soft_deletion_prohibited", None)
    if callable(local_guard) and local_guard():
        return True
    if any(
        not hasattr(bucket_file, attribute)
        for attribute in (
            "id",
            "bucket_id",
            "protected_until",
            "legal_hold",
            "retention_class",
        )
    ):
        # Compatibility for pre-retention in-memory objects. Persisted
        # BucketFile rows always have the complete retained-record contract.
        return False
    effective = await effective_status_read(
        db,
        bucket_file,
        resource_type="bucket_file",
    )
    if effective["effective_legal_hold"]:
        return True
    return (
        bucket_file.retention_class in PROTECTED_RETENTION_CLASSES
        and effective["retention_state"] == "protected"
    )

