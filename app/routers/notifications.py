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
from app.services.communication_events import publish_communication_event
from app.services.notification_groups import NotificationGroup, group_expression
from app.services.notification_navigation import notification_read

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListRead)
async def list_notifications(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    status_filter: str = Query(default="all", alias="status", pattern="^(all|unread)$"),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    group: NotificationGroup | None = Query(default=None),
) -> NotificationListRead:
    base = select(Notification).where(Notification.recipient_user_id == user.id)
    if group:
        base = base.where(group_expression() == group)
    if status_filter == "unread":
        base = base.where(Notification.read_at.is_(None))
    rows = (
        await db.execute(base.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit).offset(offset))
    ).scalars().all()
    unread_groups = (await db.execute(
            select(group_expression().label("notification_group"), func.count(Notification.id)).where(
                Notification.recipient_user_id == user.id,
                Notification.read_at.is_(None),
            ).group_by("notification_group")
        )).all()
    unread_counts = {"sms": 0, "email": 0, "general": 0}
    for key, count in unread_groups:
        unread_counts[key] = int(count or 0)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    return NotificationListRead(
        unread_count=sum(unread_counts.values()),
        items=[notification_read(row, user=user) for row in rows],
        unread_counts=unread_counts, total=int(total or 0), limit=limit, offset=offset,
    )


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_notifications_read(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    group: NotificationGroup | None = Query(default=None),
) -> None:
    statement = update(Notification).where(Notification.recipient_user_id == user.id, Notification.read_at.is_(None))
    if group:
        statement = statement.where(group_expression() == group)
    await db.execute(statement.values(read_at=datetime.now(UTC)))
    await db.flush()
    await publish_communication_event(db, recipient_user_ids={user.id}, event_type="notification.read")


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
        await publish_communication_event(db, recipient_user_ids={user.id}, event_type="notification.read", notification_id=row.id)
    return notification_read(row, user=user)
