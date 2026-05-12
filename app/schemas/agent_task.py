"""AgentTask schemas (Phase 7)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


CategoryLit = Literal[
    "buyer_workflow",
    "seller_workflow",
    "funding_prep",
    "showing",
    "open_house",
    "listing_prep",
    "cma",
    "photography",
    "document_collection",
    "other",
]
VisibilityLit = Literal["agent_private", "team_visible", "funding_visible", "client_visible"]
StatusLit = Literal["open", "in_progress", "waiting", "done", "cancelled"]
OwnerLit = Literal["human", "ai", "shared", "funding_locked"]
PriorityLit = Literal["low", "medium", "high"]


class AgentTaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    category: CategoryLit = "other"
    visibility: VisibilityLit = "team_visible"
    owner_type: OwnerLit = "human"
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    assigned_user_id: UUID | None = None
    due_at: datetime | None = None
    reminder_at: datetime | None = None
    priority: PriorityLit = "medium"
    notes: str | None = None


class AgentTaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    category: CategoryLit | None = None
    visibility: VisibilityLit | None = None
    owner_type: OwnerLit | None = None
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    assigned_user_id: UUID | None = None
    due_at: datetime | None = None
    reminder_at: datetime | None = None
    priority: PriorityLit | None = None
    status: StatusLit | None = None
    notes: str | None = None


class AgentTaskOut(ORMModel):
    id: UUID
    client_id: UUID
    deal_id: UUID | None
    loan_id: UUID | None
    title: str
    description: str | None
    category: CategoryLit
    visibility: VisibilityLit
    owner_type: OwnerLit
    assigned_user_id: UUID | None
    ai_assignment_id: UUID | None
    due_at: datetime | None
    reminder_at: datetime | None
    status: StatusLit
    priority: PriorityLit
    notes: str | None
    created_by: UUID | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PromoteToAiResponse(BaseModel):
    task: AgentTaskOut
    assignment_id: UUID
    requirement_key: str


__all__ = [
    "AgentTaskCreate",
    "AgentTaskUpdate",
    "AgentTaskOut",
    "PromoteToAiResponse",
]
