from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.enums import AITaskPriority, CalendarEventKind
from app.schemas.common import ORMModel


class CalendarEventCreate(BaseModel):
    loan_id: UUID | None = None
    kind: CalendarEventKind
    title: str
    who: str | None = None
    starts_at: datetime
    duration_min: int | None = None
    priority: AITaskPriority | None = None


class CalendarEventRead(ORMModel):
    id: UUID
    loan_id: UUID | None
    kind: CalendarEventKind
    title: str
    who: str | None
    starts_at: datetime
    duration_min: int | None
    priority: AITaskPriority | None
