"""AI suggestions follow their file: when the file leaves active work, the
ones still waiting close, and they reopen if the file comes back. A suggestion
nobody answers in 90 days closes too, for good.

A suggestion (``bucket_ai_action_items`` with status ``proposed``) waits for
the desk to approve or dismiss it. A file marked lost or denied keeps its room
open on purpose so its evidence stays readable (``intake_lifecycle``), which
meant nothing ever answered the suggestions on it and they stayed on the
desk's count for good. On 2026-11-19, 116 of the 283 the dashboard counted sat
on denied or lost files, and 37 more waited on archived rooms.

Closing is a status with a reason and a time, the contract an archived room's
open asks already keep (``bucket_requests``). The reason says the system
closed it, so a file that comes back reopens exactly these and never undoes
something a person decided.

"Left active work" is the Files list's own rule, applied to the room:

* the room is archived (``ROOM_ARCHIVED``); or
* the room belongs to a file and every file on it is lifecycle-closed (lost,
  denied, withdrawn, declined or disqualified) with none funded
  (``FILE_CLOSED``).

A funded file is still active work, and a room no file uses (a standalone
Documents room) has no lifecycle to follow, so neither is touched by that rule.

Expiry is the second rule, for every room: a suggestion left waiting and
untouched for 90 days (``EXPIRE_AFTER``, read off ``updated_at``) closes as
``EXPIRED`` and is never reopened by the system. On 2026-11-19, after the
first rule ran, 87 of the 167 still waiting were older than that: 66 in one
standalone room nobody had used in 140 days. A file that comes back after 90
days gets its old suggestions back as expired rather than waiting: the AI's
next review files fresh ones if they still apply.

The rule is applied in three places:

* readers that count or list waiting work filter with ``room_is_live()``, so
  a file closed a moment ago drops off the desk at once;
* ``reconcile`` closes, expires and reopens the rows; ``run_scheduled_sweep``
  runs it every few minutes, which catches a file closed by any path, and its
  first run after a deploy is the backfill;
* ``closing_reason`` lets the AI review file new recommendations on a closed
  file already closed.

``room_file_closed`` correlates on ``Bucket``. Use it only in a statement
whose own FROM holds the room and neither ``PublicUnderwritingIntake`` nor an
unaliased ``ApplicationProfile``: the intake rule inside it reads both, and
would correlate to the statement's rows instead of its own.

``reconcile`` writes a room activity row, so it must not run inside a request
that has already written a profile: touching the room after the profile
reverses the bucket-before-profile lock order the rest of the app keeps.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket, BucketActivityLog, BucketAIActionItem
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import intake_lifecycle

log = logging.getLogger(__name__)

WAITING = "proposed"
CLOSED = "closed"

FILE_CLOSED = "file_closed"
ROOM_ARCHIVED = "room_archived"
EXPIRED = "expired"
#: Closed because of where the file stands: these come back with the file.
REOPENABLE_REASONS = (FILE_CLOSED, ROOM_ARCHIVED)

#: How long a suggestion may wait untouched before it closes as expired.
EXPIRE_AFTER_DAYS = 90
EXPIRE_AFTER = timedelta(days=EXPIRE_AFTER_DAYS)

REASON_TEXT = {
    FILE_CLOSED: "the file was closed (lost, denied or withdrawn)",
    ROOM_ARCHIVED: "the room was archived",
    EXPIRED: f"unanswered for {EXPIRE_AFTER_DAYS} days",
}

CLOSED_ACTION = "ai_actions_closed"
EXPIRED_ACTION = "ai_actions_expired"
REOPENED_ACTION = "ai_actions_reopened"


def _intake_on_room(*where: Any):
    return exists(
        select(PublicUnderwritingIntake.id).where(
            PublicUnderwritingIntake.bucket_id == Bucket.id, *where
        )
    )


def room_file_closed():
    """SQL: the room belongs to a file and every file on it has closed.

    A file reaches its room two ways: an intake (``bucket_id``), or a profile
    that never came through one (``primary_bucket_id``). A profile with an
    intake is judged through that intake, whose rule already reads it.
    """

    from app.routers.dealer_ai_intake import _closed_intake_lifecycle_clause

    def bare_profile_on_room(closed: bool):
        # Its own alias each time: the caller's statement may hold profiles too.
        bare = aliased(ApplicationProfile)
        terminal = bare.underwriting_status.in_(intake_lifecycle.TERMINAL_UNDERWRITING_STATUSES)
        return exists(
            select(bare.id).where(
                bare.primary_bucket_id == Bucket.id,
                bare.intake_id.is_(None),
                terminal if closed else ~terminal,
            )
        )

    # Built once per use: each subquery gets its own copy of the intake rule.
    return and_(
        or_(_intake_on_room(_closed_intake_lifecycle_clause()), bare_profile_on_room(True)),
        ~_intake_on_room(~_closed_intake_lifecycle_clause()),
        ~bare_profile_on_room(False),
    )


def room_is_live():
    """SQL: the room's waiting work belongs on the desk."""

    return and_(Bucket.archived_at.is_(None), ~room_file_closed())


def _reason(archived: Any, file_closed: Any) -> str | None:
    if archived:
        return ROOM_ARCHIVED
    if file_closed:
        return FILE_CLOSED
    return None


def _reopenable_closed():
    return and_(
        BucketAIActionItem.status == CLOSED,
        BucketAIActionItem.closed_reason.in_(REOPENABLE_REASONS),
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def closing_reason(db: AsyncSession, bucket_id: UUID) -> str | None:
    """Why a suggestion filed on this room now would close at once, or None."""

    row = (
        await db.execute(
            select(
                Bucket.archived_at.is_not(None).label("archived"),
                room_file_closed().label("file_closed"),
            ).where(Bucket.id == bucket_id)
        )
    ).first()
    return _reason(row.archived, row.file_closed) if row is not None else None


def _has(*where: Any):
    return exists(
        select(BucketAIActionItem.id).where(BucketAIActionItem.bucket_id == Bucket.id, *where)
    )


async def rooms_out_of_step(
    db: AsyncSession,
    *,
    bucket_ids: Iterable[UUID] | None = None,
    now: datetime | None = None,
) -> dict[UUID, str | None]:
    """The rooms whose suggestions disagree with their file or have waited too
    long, each with the reason its suggestions should be closed for now (None:
    the file is live). A room in step is left out, so a quiet pass writes
    nothing."""

    cutoff = (now or datetime.now(UTC)) - EXPIRE_AFTER
    stmt = select(
        Bucket.id,
        Bucket.archived_at.is_not(None).label("archived"),
        room_file_closed().label("file_closed"),
        _has(BucketAIActionItem.status == WAITING).label("waiting"),
        _has(
            BucketAIActionItem.status == WAITING, BucketAIActionItem.updated_at < cutoff
        ).label("stale"),
        _has(
            BucketAIActionItem.status == CLOSED, BucketAIActionItem.closed_reason == FILE_CLOSED
        ).label("closed_for_file"),
        _has(
            BucketAIActionItem.status == CLOSED, BucketAIActionItem.closed_reason == ROOM_ARCHIVED
        ).label("closed_for_room"),
    ).where(_has(or_(BucketAIActionItem.status == WAITING, _reopenable_closed())))
    if bucket_ids is not None:
        wanted = sorted(set(bucket_ids))
        if not wanted:
            return {}
        stmt = stmt.where(Bucket.id.in_(wanted))
    out: dict[UUID, str | None] = {}
    for row in (await db.execute(stmt)).all():
        reason = _reason(row.archived, row.file_closed)
        out_of_step = (
            (reason is not None and bool(row.waiting))
            or (reason is None and bool(row.stale))
            or (reason is None and bool(row.closed_for_file or row.closed_for_room))
            or (reason == FILE_CLOSED and bool(row.closed_for_room))
            or (reason == ROOM_ARCHIVED and bool(row.closed_for_file))
        )
        if out_of_step:
            out[row.id] = reason
    return out


async def reconcile(
    db: AsyncSession,
    *,
    bucket_ids: Iterable[UUID] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Close what waits on a closed file, expire what has waited untouched for
    ``EXPIRE_AFTER``, and reopen what this closed on a file that is active
    again. Flushes; the caller commits.

    Rows another transaction holds are skipped, not waited on: a person
    answering a suggestion at this moment wins, and the next pass looks again.
    Changing a row also moves its room's retention date (the 0239 history
    trigger counts ``updated_at``), so the room row is written too.
    """

    now = now or datetime.now(UTC)
    cutoff = now - EXPIRE_AFTER
    report = {"rooms": 0, "closed": 0, "expired": 0, "reopened": 0, "relabelled": 0}
    reason_by_room = await rooms_out_of_step(db, bucket_ids=bucket_ids, now=now)
    report["rooms"] = len(reason_by_room)
    if not reason_by_room:
        return report

    items = (
        await db.execute(
            select(BucketAIActionItem)
            .where(
                BucketAIActionItem.bucket_id.in_(sorted(reason_by_room)),
                or_(BucketAIActionItem.status == WAITING, _reopenable_closed()),
            )
            .order_by(BucketAIActionItem.bucket_id, BucketAIActionItem.created_at)
            .with_for_update(skip_locked=True, of=BucketAIActionItem)
        )
    ).scalars().all()
    closed: Counter[tuple[UUID, str]] = Counter()
    reopened: Counter[UUID] = Counter()

    def close(item: BucketAIActionItem, reason: str) -> None:
        item.status = CLOSED
        item.closed_at = now
        item.closed_reason = reason
        closed[(item.bucket_id, reason)] += 1

    for item in items:
        reason = reason_by_room[item.bucket_id]
        if item.status == WAITING:
            if reason is not None:
                close(item, reason)
            elif _as_utc(item.updated_at) < cutoff:
                close(item, EXPIRED)
        elif reason is None:
            if _as_utc(item.created_at) < cutoff:
                # The file came back, but this was asked for too long ago to
                # wait again; the next review files it afresh if it applies.
                item.closed_at = now
                item.closed_reason = EXPIRED
                closed[(item.bucket_id, EXPIRED)] += 1
            else:
                item.status = WAITING
                item.closed_at = None
                item.closed_reason = None
                reopened[item.bucket_id] += 1
        elif item.closed_reason != reason:
            # Still closed, for another reason now: a room restored while its
            # file stays lost, or a lost file's room archived.
            item.closed_reason = reason
            report["relabelled"] += 1

    for (room_id, reason), count in sorted(closed.items(), key=lambda pair: (str(pair[0][0]), pair[0][1])):
        db.add(
            BucketActivityLog(
                bucket_id=room_id,
                actor_name="QC system",
                actor_role="system",
                action=EXPIRED_ACTION if reason == EXPIRED else CLOSED_ACTION,
                target_type="bucket",
                target_id=str(room_id),
                detail=(
                    f"{count} AI suggestion{'s' if count != 1 else ''} closed: "
                    f"{REASON_TEXT[reason]}"
                ),
                created_at=now,
            )
        )
    for room_id, count in sorted(reopened.items(), key=lambda pair: str(pair[0])):
        db.add(
            BucketActivityLog(
                bucket_id=room_id,
                actor_name="QC system",
                actor_role="system",
                action=REOPENED_ACTION,
                target_type="bucket",
                target_id=str(room_id),
                detail=(
                    f"{count} AI suggestion{'s' if count != 1 else ''} waiting again: "
                    "the file is active again"
                ),
                created_at=now,
            )
        )
    report["expired"] = sum(count for (_room, reason), count in closed.items() if reason == EXPIRED)
    report["closed"] = sum(closed.values()) - report["expired"]
    report["reopened"] = sum(reopened.values())
    await db.flush()
    return report


async def run_scheduled_sweep() -> dict[str, int] | None:
    """The scheduler's tick. Opens its own session like every other job, and
    commits one room at a time: each room's change also writes that room's
    row, so a room another transaction holds delays only itself, and a room
    that fails does not undo the others."""

    from app.db import SessionLocal

    keys = ("rooms", "closed", "expired", "reopened", "relabelled")
    total = dict.fromkeys((*keys, "failed"), 0)
    async with SessionLocal() as db:
        try:
            rooms = sorted(await rooms_out_of_step(db))
        except Exception:
            await db.rollback()
            log.exception("ai_suggestion_closure: could not read the rooms; rolled back")
            return None
        await db.rollback()  # end the read; each room is its own transaction
        for room_id in rooms:
            try:
                report = await reconcile(db, bucket_ids=[room_id])
                await db.commit()
            except Exception:
                await db.rollback()
                total["failed"] += 1
                log.exception("ai_suggestion_closure: room %s failed; rolled back", room_id)
                continue
            for key in keys:
                total[key] += report[key]
    if any(total[key] for key in ("closed", "expired", "reopened", "relabelled", "failed")):
        log.info("ai_suggestion_closure: %s", total)
    return total
