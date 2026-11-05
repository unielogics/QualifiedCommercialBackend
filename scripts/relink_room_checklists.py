"""Relink what the client already gave to what the application needs.

Three faults left rooms looking unfinished after the client had done the work:

1. A statement whose cycle crossed a month boundary ("2026-05-30 to
   2026-06-30") was filed under its first month, so a six-month checklist
   stalled at five. Every single-statement period is re-derived with the
   closing-month rule.
2. The readiness playbook minted its own asks beside the room's checklist
   ("Last 6 months business bank statements" beside "Last 6 months bank
   statements"), so the client's uploads sat on one row while the application
   waited on the other. The room's row adopts the requirement, anything
   attached to the duplicate moves over, and the duplicate is closed.
3. The intake page recorded uploads at 0 bytes. The real size is read back
   from S3.

Then readiness is recomputed for every profile touched, so the requirement
states and the room's checklist agree again.

    python -m scripts.relink_room_checklists [--apply] [--bucket <id>]

Without --apply it prints what it would do and changes nothing. Run it only
on a build that carries the adoption rule, or the next readiness pass on an
older build would reword the adopted rows.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models.application_profile import ApplicationProfile, ApplicationRequirementState
from app.models.bucket import BucketFile, BucketFileAnalysis, BucketRequestedDocument
from app.services.application_programs import (
    EXPECTED_CLASSIFICATIONS,
    SIGNATURE_REQUIREMENTS,
    adoptable_room_request,
    get_program_readiness,
)
from app.services.bucket_evidence import statement_months_from_analysis
from app.services.bucket_requests import CLOSED_STATUS

MERGED_REASON = "merged_into_room_checklist"
OPEN_STATUSES_EXCLUDED = [CLOSED_STATUS, "not_applicable"]


async def retag_statement_periods(db, *, bucket_id: UUID | None, apply: bool) -> tuple[int, set[UUID]]:
    query = (
        select(BucketFile, BucketFileAnalysis)
        .join(BucketFileAnalysis, BucketFileAnalysis.bucket_file_id == BucketFile.id)
        .where(
            BucketFile.deleted_at.is_(None),
            BucketFile.status == "uploaded",
            BucketFileAnalysis.status == "completed",
            BucketFileAnalysis.classification == "bank_statement",
        )
        .order_by(
            BucketFile.id,
            BucketFileAnalysis.analysis_version.desc(),
            BucketFileAnalysis.created_at.desc(),
        )
    )
    if bucket_id:
        query = query.where(BucketFile.bucket_id == bucket_id)
    changed, buckets, seen = 0, set(), set()
    for file, analysis in (await db.execute(query)).all():
        if file.id in seen:
            continue
        if file.content_hash and analysis.content_hash != file.content_hash:
            continue  # an analysis of an earlier version of the bytes
        seen.add(file.id)
        months = statement_months_from_analysis(analysis.analysis)
        if len(months) != 1:
            continue  # a packet of several statements keeps what it has
        month = next(iter(months))
        if file.statement_period == month:
            continue
        print(f"  period   {file.file_name[:50]:<50} {file.statement_period or '—':>8} -> {month}")
        if apply:
            file.statement_period = month
        changed += 1
        buckets.add(file.bucket_id)
    return changed, buckets


async def merge_duplicate_asks(db, *, bucket_id: UUID | None, apply: bool) -> tuple[int, set[UUID]]:
    query = select(BucketRequestedDocument).where(
        BucketRequestedDocument.requirement_key.is_not(None),
        BucketRequestedDocument.status.notin_(OPEN_STATUSES_EXCLUDED),
    )
    if bucket_id:
        query = query.where(BucketRequestedDocument.bucket_id == bucket_id)
    rows = (await db.execute(query.order_by(BucketRequestedDocument.created_at))).scalars().all()
    minted: dict[UUID, list[BucketRequestedDocument]] = defaultdict(list)
    for row in rows:
        source = row.requirement_source if isinstance(row.requirement_source, dict) else {}
        if source.get("kind") == "program_readiness" and not source.get("adopted"):
            minted[row.bucket_id].append(row)
    merged, buckets = 0, set()
    now = datetime.now(UTC)
    for bid, dupes in minted.items():
        candidates = list(
            (
                await db.execute(
                    select(BucketRequestedDocument)
                    .where(
                        BucketRequestedDocument.bucket_id == bid,
                        BucketRequestedDocument.requirement_key.is_(None),
                        BucketRequestedDocument.status.notin_(OPEN_STATUSES_EXCLUDED),
                    )
                    .order_by(BucketRequestedDocument.created_at)
                )
            )
            .scalars()
            .all()
        )
        if not candidates:
            continue
        counts = {
            row[0]: int(row[1])
            for row in (
                await db.execute(
                    select(BucketFile.requested_document_id, func.count())
                    .where(
                        BucketFile.bucket_id == bid,
                        BucketFile.deleted_at.is_(None),
                        BucketFile.status == "uploaded",
                        BucketFile.requested_document_id.is_not(None),
                    )
                    .group_by(BucketFile.requested_document_id)
                )
            ).all()
        }
        for dupe in dupes:
            key = dupe.requirement_key
            expected = EXPECTED_CLASSIFICATIONS.get(key)
            if not expected and key not in SIGNATURE_REQUIREMENTS:
                continue
            chosen = adoptable_room_request(
                candidates, requirement_key=key, expected=set(expected or ()), file_counts=counts
            )
            if chosen is None:
                continue
            candidates.remove(chosen)
            print(
                f"  adopt    {str(bid)[:8]} {key:<36} '{chosen.name}' ({counts.get(chosen.id, 0)} files)"
                f"  <- closes '{dupe.name}' ({counts.get(dupe.id, 0)} files, {dupe.status})"
            )
            if apply:
                chosen.requirement_key = key
                chosen.requirement_source = {**dupe.requirement_source, "adopted": True, "merged_from": str(dupe.id)}
                if chosen.status == "not_applicable":
                    chosen.status = "requested"
                if dupe.status == "uploaded" and not chosen.requires_signature:
                    chosen.status = "uploaded"
                await db.execute(
                    update(BucketFile)
                    .where(BucketFile.requested_document_id == dupe.id)
                    .values(requested_document_id=chosen.id)
                )
                await db.execute(
                    update(ApplicationRequirementState)
                    .where(ApplicationRequirementState.requested_document_id == dupe.id)
                    .values(requested_document_id=chosen.id)
                )
                dupe.requirement_source = {**dupe.requirement_source, "merged_into": str(chosen.id)}
                dupe.requirement_key = None
                dupe.status = CLOSED_STATUS
                dupe.closed_at = now
                dupe.closed_reason = MERGED_REASON
            merged += 1
            buckets.add(bid)
    return merged, buckets


async def restore_sizes(db, *, bucket_id: UUID | None, apply: bool) -> int:
    from app.routers.buckets import _s3_client

    query = select(BucketFile).where(
        BucketFile.size_bytes == 0,
        BucketFile.deleted_at.is_(None),
        BucketFile.status == "uploaded",
        BucketFile.s3_key.is_not(None),
    )
    if bucket_id:
        query = query.where(BucketFile.bucket_id == bucket_id)
    files = (await db.execute(query.order_by(BucketFile.created_at))).scalars().all()
    if not files:
        return 0
    s3 = _s3_client()
    storage_bucket = get_settings().s3_bucket
    restored = 0
    for file in files:
        try:
            head = await asyncio.to_thread(s3.head_object, Bucket=storage_bucket, Key=file.s3_key)
        except Exception as exc:  # noqa: BLE001 - one unreadable object must not stop the rest
            print(f"  size     {file.file_name[:50]:<50} skipped: {type(exc).__name__}")
            continue
        size = int(head.get("ContentLength") or 0)
        if size <= 0:
            continue
        print(f"  size     {file.file_name[:50]:<50} 0 -> {size:,} B")
        if apply:
            file.size_bytes = size
        restored += 1
    return restored


async def recompute_readiness(db, buckets: set[UUID], *, apply: bool) -> tuple[int, int]:
    if not buckets:
        return 0, 0
    profiles = (
        await db.execute(
            select(ApplicationProfile).where(ApplicationProfile.primary_bucket_id.in_(list(buckets)))
        )
    ).scalars().all()
    done, failed = 0, 0
    for profile in profiles:
        if not apply:
            print(f"  recompute profile {profile.id} (room {str(profile.primary_bucket_id)[:8]})")
            done += 1
            continue
        try:
            await get_program_readiness(db, profile)
            await db.commit()
            print(f"  recomputed profile {profile.id} (room {str(profile.primary_bucket_id)[:8]})")
            done += 1
        except Exception as exc:  # noqa: BLE001 - report, keep going
            await db.rollback()
            print(f"  FAILED   profile {profile.id}: {exc!r}")
            failed += 1
    return done, failed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes; default is a dry run")
    parser.add_argument("--bucket", type=UUID, default=None, help="limit to one room")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"== relink room checklists ({mode}) ==")
    async with SessionLocal() as db:
        print("\n-- 1. statement periods (closing-month rule)")
        periods, touched = await retag_statement_periods(db, bucket_id=args.bucket, apply=args.apply)
        print("\n-- 2. duplicate asks")
        merged, touched_by_merge = await merge_duplicate_asks(db, bucket_id=args.bucket, apply=args.apply)
        touched |= touched_by_merge
        print("\n-- 3. upload sizes")
        sizes = await restore_sizes(db, bucket_id=args.bucket, apply=args.apply)
        if args.apply:
            await db.commit()
        print("\n-- 4. readiness")
        done, failed = await recompute_readiness(db, touched, apply=args.apply)
    print(
        f"\n{mode}: {periods} period(s) retagged, {merged} duplicate ask(s) merged, "
        f"{sizes} size(s) restored, {done} profile(s) recomputed, {failed} failed"
    )


if __name__ == "__main__":
    asyncio.run(main())
