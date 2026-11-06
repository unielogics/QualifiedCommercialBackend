from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from app.schemas.common import ORMModel
from app.services.notification_groups import NotificationGroup, group_for


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

    @computed_field
    @property
    def group(self) -> NotificationGroup:
        return group_for(self.event_type, self.category, self.meta)


class NotificationListRead(BaseModel):
    unread_count: int
    items: list[NotificationRead]
    unread_counts: dict[NotificationGroup, int] = Field(default_factory=lambda: {"sms": 0, "email": 0, "general": 0})
    total: int = 0
    limit: int = 30
    offset: int = 0
