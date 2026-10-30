"""Client sign-ins that happen without a login account.

Most clients never get a Clerk login. They enter their secure room with the
PIN on an upload link, or a dealer lead enters with a one-time code emailed
to them. Both are recorded already (``bucket_activity_logs`` and
``dealer_intake_login_challenges``); this module reads them back as one
"sign-in" vocabulary so the client-access directory can show them next to
the account-based login states.

Nothing here writes. The room entry endpoint writes the activity row, the
code verification endpoint stamps the challenge.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bucket import BucketActivityLog
from app.models.dealer_intake_login import DealerIntakeLoginChallenge

# The successful room-PIN entry. Written once per PIN submission by the
# public room's access endpoint; silent refreshes do not write it.
ROOM_SIGNIN_ACTION = "upload_link_accessed"
ROOM_SIGNIN_FAILED_ACTION = "upload_passcode_failed"

# Vocabulary the access history uses for these events.
HISTORY_ROOM_SIGNIN = "room_pin_signin"
HISTORY_ROOM_SIGNIN_FAILED = "room_pin_failed"
HISTORY_CODE_SIGNIN = "email_code_signin"


@dataclass(frozen=True)
class SigninSummary:
    count: int = 0
    last_at: datetime | None = None

    def merged_with(self, other: SigninSummary | None) -> SigninSummary:
        if other is None:
            return self
        candidates = [value for value in (self.last_at, other.last_at) if value is not None]
        return SigninSummary(
            count=self.count + other.count,
            last_at=max(candidates) if candidates else None,
        )


EMPTY = SigninSummary()


def merge(summaries: Iterable[SigninSummary | None]) -> SigninSummary:
    total = EMPTY
    for summary in summaries:
        total = total.merged_with(summary)
    return total


def latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


async def room_signins_by_bucket(
    db: AsyncSession, bucket_ids: Iterable[UUID]
) -> dict[UUID, SigninSummary]:
    ids = {value for value in bucket_ids if value is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                BucketActivityLog.bucket_id,
                func.count(BucketActivityLog.id),
                func.max(BucketActivityLog.created_at),
            )
            .where(
                BucketActivityLog.bucket_id.in_(ids),
                BucketActivityLog.action == ROOM_SIGNIN_ACTION,
            )
            .group_by(BucketActivityLog.bucket_id)
        )
    ).all()
    return {
        bucket_id: SigninSummary(count=int(count or 0), last_at=last_at)
        for bucket_id, count, last_at in rows
    }


async def code_signins_by_intake(
    db: AsyncSession, intake_ids: Iterable[UUID]
) -> dict[UUID, SigninSummary]:
    ids = {value for value in intake_ids if value is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                DealerIntakeLoginChallenge.intake_id,
                func.count(DealerIntakeLoginChallenge.id),
                func.max(DealerIntakeLoginChallenge.used_at),
            )
            .where(
                DealerIntakeLoginChallenge.intake_id.in_(ids),
                DealerIntakeLoginChallenge.used_at.is_not(None),
            )
            .group_by(DealerIntakeLoginChallenge.intake_id)
        )
    ).all()
    return {
        intake_id: SigninSummary(count=int(count or 0), last_at=last_at)
        for intake_id, count, last_at in rows
    }


def summary_for(
    *,
    bucket_ids: Iterable[UUID | None],
    intake_ids: Iterable[UUID | None],
    room: dict[UUID, SigninSummary],
    code: dict[UUID, SigninSummary],
) -> SigninSummary:
    """Fold every room and code sign-in that belongs to one directory subject."""

    parts: list[SigninSummary | None] = [
        room.get(bucket_id) for bucket_id in {b for b in bucket_ids if b is not None}
    ]
    parts.extend(code.get(intake_id) for intake_id in {i for i in intake_ids if i is not None})
    return merge(parts)


@dataclass(frozen=True)
class SigninEvent:
    id: UUID
    action: str
    reason: str
    created_at: datetime
    request_metadata: dict


async def signin_events(
    db: AsyncSession,
    *,
    bucket_ids: Iterable[UUID | None],
    intake_ids: Iterable[UUID | None],
    limit: int = 100,
) -> list[SigninEvent]:
    """The individual sign-ins (and failed PIN attempts) for one subject, newest first."""

    buckets = {value for value in bucket_ids if value is not None}
    intakes = {value for value in intake_ids if value is not None}
    events: list[SigninEvent] = []
    if buckets:
        rows = (
            await db.execute(
                select(BucketActivityLog)
                .where(
                    BucketActivityLog.bucket_id.in_(buckets),
                    BucketActivityLog.action.in_((ROOM_SIGNIN_ACTION, ROOM_SIGNIN_FAILED_ACTION)),
                )
                .order_by(BucketActivityLog.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        for row in rows:
            succeeded = row.action == ROOM_SIGNIN_ACTION
            events.append(
                SigninEvent(
                    id=row.id,
                    action=HISTORY_ROOM_SIGNIN if succeeded else HISTORY_ROOM_SIGNIN_FAILED,
                    reason=(
                        "Entered the secure room with the room PIN"
                        if succeeded
                        else "Wrong room PIN was entered"
                    ),
                    created_at=row.created_at,
                    request_metadata={
                        "bucket_id": str(row.bucket_id),
                        "ip_address": row.ip_address,
                        "user_agent": row.user_agent,
                    },
                )
            )
    if intakes:
        rows = (
            await db.execute(
                select(DealerIntakeLoginChallenge)
                .where(
                    DealerIntakeLoginChallenge.intake_id.in_(intakes),
                    DealerIntakeLoginChallenge.used_at.is_not(None),
                )
                .order_by(DealerIntakeLoginChallenge.used_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        for row in rows:
            events.append(
                SigninEvent(
                    id=row.id,
                    action=HISTORY_CODE_SIGNIN,
                    reason="Signed in with the one-time code emailed to them",
                    created_at=row.used_at,
                    request_metadata={
                        "intake_id": str(row.intake_id),
                        "ip_address": row.ip_address,
                        "user_agent": row.user_agent,
                    },
                )
            )
    events.sort(key=lambda event: event.created_at, reverse=True)
    return events[:limit]
