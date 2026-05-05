from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.enums import AITaskPriority, AITaskSource, AITaskStatus
from app.schemas.common import ORMModel


class AITaskRead(ORMModel):
    id: UUID
    loan_id: UUID | None
    source: AITaskSource
    priority: AITaskPriority
    status: AITaskStatus
    action: str
    title: str
    summary: str
    confidence: float
    agent: str
    draft_payload: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class AITaskDecision(BaseModel):
    decision: AITaskStatus  # APPROVED / DISMISSED
    edited_payload: dict[str, Any] | None = None
