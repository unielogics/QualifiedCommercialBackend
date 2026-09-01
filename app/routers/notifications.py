from __future__ import annotations

# FastAPI dependency declarations intentionally use Depends in defaults.
# ruff: noqa: B008
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.models.notification import Notification
from app.schemas.notification import NotificationListRead, NotificationRead

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListRead)
async def list_notifications(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    status_filter: str = Query(default="all", alias="status", pattern="^(all|unread)$"),
    limit: int = Query(default=30, ge=1, le=100),
) -> NotificationListRead:
    base = select(Notification).where(Notification.recipient_user_id == user.id)
    if status_filter == "unread":
        base = base.where(Notification.read_at.is_(None))
    rows = (
        await db.execute(base.order_by(Notification.created_at.desc()).limit(limit))
    ).scalars().all()
    unread_count = (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.recipient_user_id == user.id,
                Notification.read_at.is_(None),
            )
        )
    ).scalar_one()
    return NotificationListRead(
        unread_count=int(unread_count or 0),
        items=[NotificationRead.model_validate(row) for row in rows],
    )


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_notifications_read(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    await db.execute(
        update(Notification)
        .where(Notification.recipient_user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await db.flush()


@router.post("/{notification_id}/read", response_model=NotificationRead)
async def mark_notification_read(
    notification_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> NotificationRead:
    row = await db.get(Notification, notification_id)
    if row is None or row.recipient_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        await db.flush()
        await db.refresh(row)
    return NotificationRead.model_validate(row)
