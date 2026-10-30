"""The desk's work queue, drawn from what actually stalls.

The home page used to surface loan health only, while the places files really
stop — a client who never uploaded, a checklist that aged out, AI suggestions
nobody answered, a room the client was never given a way into — were visible
nowhere. Each row here names one of those and carries the one link that opens
the file, so the queue is something to work rather than something to read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.bucket import (
    Bucket,
    BucketAIActionItem,
    BucketFile,
    BucketRequestedDocument,
    BucketShare,
    BucketUploadLink,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.operator_file import AttentionItem, AttentionQueue

INTAKE_NO_FILES_DAYS = 7
REQUEST_OVERDUE_DAYS = 30
ACTION_ITEM_STALE_DAYS = 7

# Client-waiting rows first: those are the ones where the firm is the blocker.
KIND_ORDER = {"intake_no_files": 0, "no_client_entry": 1, "request_overdue": 2, "action_items_stale": 3}


def lead_href(intake_id: UUID) -> str:
    return f"/admin/ai-underwriter-leads?lead={intake_id}"


def bucket_href(bucket_id: UUID) -> str:
    return f"/admin/buckets?bucket={bucket_id}"


def _age_days(since: datetime | None, now: datetime) -> int:
    if since is None:
        return 0
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return max(0, (now - since).days)


def _severity(age_days: int, threshold_days: int) -> str:
    return "bad" if age_days >= threshold_days * 2 else "warn"


def _resume_sent_at(intake_state: Any) -> datetime | None:
    """When the client was last sent their link, from the intake's own audit."""

    if not isinstance(intake_state, dict):
        return None
    candidates: list[str] = []
    for key in ("resume_email", "dealer_login_email"):
        entry = intake_state.get(key)
        if isinstance(entry, dict) and entry.get("ok") and entry.get("sent_at"):
            candidates.append(str(entry["sent_at"]))
    for key in ("resume_email_deliveries", "dealer_login_email_deliveries"):
        for entry in intake_state.get(key) or []:
            if isinstance(entry, dict) and entry.get("ok") and entry.get("sent_at"):
                candidates.append(str(entry["sent_at"]))
    parsed: list[datetime] = []
    for raw in candidates:
        try:
            parsed.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            continue
    return max(parsed) if parsed else None


def _who(full_name: str | None, business_name: str | None, fallback: str) -> str:
    return business_name or full_name or fallback


def sort_items(items: list[AttentionItem]) -> list[AttentionItem]:
    return sorted(items, key=lambda item: (KIND_ORDER.get(item.kind, 9), -item.age_days, item.title))


async def _intakes_without_files(db: AsyncSession, now: datetime) -> list[AttentionItem]:
    cutoff = now - timedelta(days=INTAKE_NO_FILES_DAYS)
    has_file = exists(select(BucketFile.id).where(BucketFile.bucket_id == PublicUnderwritingIntake.bucket_id))
    rows = (
        await db.execute(
            select(PublicUnderwritingIntake, ApplicationProfile.id)
            .outerjoin(
                ApplicationProfile,
                ApplicationProfile.primary_bucket_id == PublicUnderwritingIntake.bucket_id,
            )
            .where(
                PublicUnderwritingIntake.status == "collecting",
                PublicUnderwritingIntake.created_at < cutoff,
                ~has_file,
            )
        )
    ).all()
    items: list[AttentionItem] = []
    for intake, profile_id in rows:
        age = _age_days(intake.created_at, now)
        sent_at = _resume_sent_at(getattr(intake, "intake_state", None))
        sent_text = f"link sent {_age_days(sent_at, now)}d ago" if sent_at else "no link delivery recorded"
        items.append(
            AttentionItem(
                kind="intake_no_files",
                severity=_severity(age, INTAKE_NO_FILES_DAYS),
                title=_who(intake.full_name, intake.business_name, "Unnamed intake"),
                detail=f"Collecting for {age}d with no documents · {sent_text}",
                age_days=age,
                href=lead_href(intake.id),
                bucket_id=intake.bucket_id,
                intake_id=intake.id,
                profile_id=profile_id,
                link_last_sent_at=sent_at,
                actions=["resend_link"] if profile_id is not None else [],
            )
        )
    return items


async def _intake_by_bucket(db: AsyncSession, bucket_ids: set[UUID]) -> dict[UUID, UUID]:
    if not bucket_ids:
        return {}
    rows = (
        await db.execute(
            select(PublicUnderwritingIntake.bucket_id, PublicUnderwritingIntake.id).where(
                PublicUnderwritingIntake.bucket_id.in_(bucket_ids)
            )
        )
    ).all()
    return {bucket_id: intake_id for bucket_id, intake_id in rows}


def _file_href(bucket_id: UUID, intake_id: UUID | None) -> str:
    return lead_href(intake_id) if intake_id is not None else bucket_href(bucket_id)


async def _overdue_requests(db: AsyncSession, now: datetime) -> list[AttentionItem]:
    cutoff = now - timedelta(days=REQUEST_OVERDUE_DAYS)
    rows = (
        await db.execute(
            select(
                Bucket.id,
                Bucket.name,
                Bucket.client_name,
                func.count(BucketRequestedDocument.id),
                func.min(BucketRequestedDocument.created_at),
            )
            .join(BucketRequestedDocument, BucketRequestedDocument.bucket_id == Bucket.id)
            .where(
                Bucket.archived_at.is_(None),
                BucketRequestedDocument.status == "requested",
                BucketRequestedDocument.created_at < cutoff,
            )
            .group_by(Bucket.id, Bucket.name, Bucket.client_name)
        )
    ).all()
    intakes = await _intake_by_bucket(db, {row[0] for row in rows})
    items: list[AttentionItem] = []
    for bucket_id, name, client_name, count, oldest in rows:
        age = _age_days(oldest, now)
        items.append(
            AttentionItem(
                kind="request_overdue",
                severity=_severity(age, REQUEST_OVERDUE_DAYS),
                title=client_name or name,
                detail=f"{count} requested document{'s' if count != 1 else ''} open for {age}d",
                age_days=age,
                count=int(count),
                href=_file_href(bucket_id, intakes.get(bucket_id)),
                bucket_id=bucket_id,
                intake_id=intakes.get(bucket_id),
            )
        )
    return items


async def _stale_action_items(db: AsyncSession, now: datetime) -> list[AttentionItem]:
    cutoff = now - timedelta(days=ACTION_ITEM_STALE_DAYS)
    rows = (
        await db.execute(
            select(
                Bucket.id,
                Bucket.name,
                Bucket.client_name,
                func.count(BucketAIActionItem.id),
                func.min(BucketAIActionItem.created_at),
            )
            .join(BucketAIActionItem, BucketAIActionItem.bucket_id == Bucket.id)
            .where(
                Bucket.archived_at.is_(None),
                BucketAIActionItem.status == "proposed",
                BucketAIActionItem.created_at < cutoff,
            )
            .group_by(Bucket.id, Bucket.name, Bucket.client_name)
        )
    ).all()
    intakes = await _intake_by_bucket(db, {row[0] for row in rows})
    items: list[AttentionItem] = []
    for bucket_id, name, client_name, count, oldest in rows:
        age = _age_days(oldest, now)
        items.append(
            AttentionItem(
                kind="action_items_stale",
                severity=_severity(age, ACTION_ITEM_STALE_DAYS),
                title=client_name or name,
                detail=f"{count} AI suggestion{'s' if count != 1 else ''} waiting {age}d for a decision",
                age_days=age,
                count=int(count),
                href=_file_href(bucket_id, intakes.get(bucket_id)),
                bucket_id=bucket_id,
                intake_id=intakes.get(bucket_id),
            )
        )
    return items


async def _buckets_without_client_entry(db: AsyncSession, now: datetime) -> list[AttentionItem]:
    live_link = exists(
        select(BucketUploadLink.id).where(
            BucketUploadLink.bucket_id == Bucket.id,
            BucketUploadLink.status == "active",
            (BucketUploadLink.expires_at.is_(None)) | (BucketUploadLink.expires_at >= now),
        )
    )
    any_share = exists(select(BucketShare.id).where(BucketShare.bucket_id == Bucket.id))
    rows = (
        await db.execute(
            select(Bucket).where(Bucket.archived_at.is_(None), ~live_link, ~any_share)
        )
    ).scalars().all()
    intakes = await _intake_by_bucket(db, {bucket.id for bucket in rows})
    items: list[AttentionItem] = []
    for bucket in rows:
        age = _age_days(bucket.created_at, now)
        items.append(
            AttentionItem(
                kind="no_client_entry",
                severity="warn",
                title=bucket.client_name or bucket.name,
                detail="No live upload link or share — the client has no way in",
                age_days=age,
                href=_file_href(bucket.id, intakes.get(bucket.id)),
                bucket_id=bucket.id,
                intake_id=intakes.get(bucket.id),
            )
        )
    return items


async def load_attention_queue(
    db: AsyncSession, *, now: datetime | None = None, limit: int = 60
) -> AttentionQueue:
    now = now or datetime.now(UTC)
    items: list[AttentionItem] = []
    for loader in (
        _intakes_without_files,
        _buckets_without_client_entry,
        _overdue_requests,
        _stale_action_items,
    ):
        items.extend(await loader(db, now))
    items = sort_items(items)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return AttentionQueue(generated_at=now, items=items[:limit], counts=counts)
