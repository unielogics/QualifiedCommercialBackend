"""Client sign-ins that happen without a login account.

Most clients never get a Clerk login. They enter their secure room with the
PIN on an upload link, or a dealer lead enters with a one-time code emailed
to them. Both are recorded already (``bucket_activity_logs`` and
``dealer_intake_login_challenges``); this module reads them back as one
"sign-in" vocabulary so the client-access directory can show them next to
the account-based login states.

A room can carry PIN links for more than one person (a partner, a CPA, a
second upload party), and every link's entry lands on the same bucket. The
access endpoint stamps each entry with the link's recipient email, so a
sign-in is credited to a subject only when that email is the subject's own.
Entries from a link that was issued without an email cannot be told apart
and stay with the room's subject.

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
    room_count: int = 0
    code_count: int = 0

    def merged_with(self, other: SigninSummary | None) -> SigninSummary:
        if other is None:
            return self
        return SigninSummary(
            count=self.count + other.count,
            last_at=latest(self.last_at, other.last_at),
            room_count=self.room_count + other.room_count,
            code_count=self.code_count + other.code_count,
        )


EMPTY = SigninSummary()

# One room may hold several links; entries are grouped by the recipient email
# the access endpoint stamped on them (lower-cased, None when the link had none).
RoomGroups = dict[UUID, list[tuple[str | None, SigninSummary]]]


def merge(summaries: Iterable[SigninSummary | None]) -> SigninSummary:
    total = EMPTY
    for summary in summaries:
        total = total.merged_with(summary)
    return total


def latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def normalized_emails(values: Iterable[str | None]) -> set[str]:
    return {value.strip().lower() for value in values if value and value.strip()}


def _belongs_to(actor_email: str | None, own: set[str]) -> bool:
    """An entry is the subject's own when its link carried their email, or no email at all."""

    return actor_email is None or actor_email in own


async def room_signins_by_bucket(
    db: AsyncSession, bucket_ids: Iterable[UUID | None]
) -> RoomGroups:
    ids = {value for value in bucket_ids if value is not None}
    if not ids:
        return {}
    actor_email = func.lower(BucketActivityLog.actor_email)
    rows = (
        await db.execute(
            select(
                BucketActivityLog.bucket_id,
                actor_email,
                func.count(BucketActivityLog.id),
                func.max(BucketActivityLog.created_at),
            )
            .where(
                BucketActivityLog.bucket_id.in_(ids),
                BucketActivityLog.action == ROOM_SIGNIN_ACTION,
            )
            .group_by(BucketActivityLog.bucket_id, actor_email)
        )
    ).all()
    groups: RoomGroups = {}
    for bucket_id, email, count, last_at in rows:
        groups.setdefault(bucket_id, []).append(
            (email or None, SigninSummary(count=int(count or 0), last_at=last_at, room_count=int(count or 0)))
        )
    return groups


async def code_signins_by_intake(
    db: AsyncSession, intake_ids: Iterable[UUID | None]
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
        intake_id: SigninSummary(count=int(count or 0), last_at=last_at, code_count=int(count or 0))
        for intake_id, count, last_at in rows
    }


def summary_for(
    *,
    bucket_ids: Iterable[UUID | None],
    intake_ids: Iterable[UUID | None],
    emails: Iterable[str | None],
    room: RoomGroups,
    code: dict[UUID, SigninSummary],
) -> SigninSummary:
    """Fold the room and code sign-ins that are one directory subject's own."""

    own = normalized_emails(emails)
    parts: list[SigninSummary | None] = []
    for bucket_id in {value for value in bucket_ids if value is not None}:
        parts.extend(
            summary
            for actor_email, summary in room.get(bucket_id, [])
            if _belongs_to(actor_email, own)
        )
    parts.extend(code.get(intake_id) for intake_id in {i for i in intake_ids if i is not None})
    return merge(parts)


@dataclass(frozen=True)
class SigninEvent:
    id: UUID
    action: str
    reason: str
    created_at: datetime
    request_metadata: dict
    # False when another person's link on the same room was used. Listed so
    # the admin sees who else has been in the room, never counted.
    own: bool = True


def _room_reason(row: BucketActivityLog, *, own: bool) -> str:
    succeeded = row.action == ROOM_SIGNIN_ACTION
    who = row.actor_name or row.actor_email or "another link holder"
    if not own:
        return (
            f"{who} entered the secure room with their own PIN link"
            if succeeded
            else f"Wrong PIN was entered on {who}'s link"
        )
    if succeeded:
        return (
            f"Entered the secure room with the room PIN as {row.actor_name}"
            if row.actor_name
            else "Entered the secure room with the room PIN"
        )
    return "Wrong room PIN was entered"


async def signin_events(
    db: AsyncSession,
    *,
    bucket_ids: Iterable[UUID | None],
    intake_ids: Iterable[UUID | None],
    emails: Iterable[str | None],
    limit: int = 100,
) -> list[SigninEvent]:
    """The individual sign-ins (and failed PIN attempts) for one subject, newest first."""

    buckets = {value for value in bucket_ids if value is not None}
    intakes = {value for value in intake_ids if value is not None}
    own = normalized_emails(emails)
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
            actor_email = (row.actor_email or "").strip().lower() or None
            is_own = _belongs_to(actor_email, own)
            events.append(
                SigninEvent(
                    id=row.id,
                    action=(
                        HISTORY_ROOM_SIGNIN
                        if row.action == ROOM_SIGNIN_ACTION
                        else HISTORY_ROOM_SIGNIN_FAILED
                    ),
                    reason=_room_reason(row, own=is_own),
                    created_at=row.created_at,
                    request_metadata={
                        "bucket_id": str(row.bucket_id),
                        "upload_link_id": row.target_id,
                        "actor_name": row.actor_name,
                        "actor_email": row.actor_email,
                        "ip_address": row.ip_address,
                        "user_agent": row.user_agent,
                    },
                    own=is_own,
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
