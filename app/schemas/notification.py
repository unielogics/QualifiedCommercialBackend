from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.schemas.common import ORMModel


class NotificationRead(ORMModel):
    id: UUID
    recipient_user_id: UUID
    event_type: str
    category: str
    priority: str
    title: str
    body: str
    target_type: str | None
    target_id: str | None
    deep_link: str | None
    channels: list[str]
    meta: dict
    batch_key: str | None
    read_at: datetime | None
    pushed_at: datetime | None
    emailed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NotificationListRead(BaseModel):
    unread_count: int
    items: list[NotificationRead]
