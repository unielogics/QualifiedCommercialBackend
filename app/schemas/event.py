from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.enums import (
    AITaskPriority,
    CalendarEventKind,
    CalendarEventSource,
    CalendarEventStatus,
)
from app.schemas.common import ORMModel


class CalendarEventCreate(BaseModel):
    loan_id: UUID | None = None
    kind: CalendarEventKind
    title: str
    description: str | None = None
    who: str | None = None
    starts_at: datetime
    duration_min: int | None = None
    priority: AITaskPriority | None = None
    owner_user_id: UUID | None = None


class CalendarEventUpdate(BaseModel):
    """All fields optional — partial update semantics. The router
    only persists keys present in the payload (model_dump
    exclude_unset=True)."""
    kind: CalendarEventKind | None = None
    title: str | None = None
    description: str | None = None
    who: str | None = None
    starts_at: datetime | None = None
    duration_min: int | None = None
    priority: AITaskPriority | None = None
    owner_user_id: UUID | None = None
    status: CalendarEventStatus | None = None


class CalendarEventRead(ORMModel):
    id: UUID
    loan_id: UUID | None
    kind: CalendarEventKind
    title: str
    description: str | None = None
    who: str | None
    starts_at: datetime
    duration_min: int | None
    priority: AITaskPriority | None
    status: CalendarEventStatus
    source: CalendarEventSource
    owner_user_id: UUID | None = None
    external_ref_kind: str | None = None
    external_ref_id: str | None = None


class CalendarActivityItem(BaseModel):
    id: UUID
    loan_id: UUID | None
    client_id: UUID | None
    kind: str
    summary: str
    actor_label: str | None = None
    occurred_at: datetime
    payload: dict | None = None
