"""Pydantic shapes for the Deal Workspace endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.enums import DealChatMode, DealChatRole, FeedbackOutputType, FeedbackRating
from app.schemas.common import ORMModel


# ── Instructions ────────────────────────────────────────────────────────


class InstructionRead(ORMModel):
    id: UUID
    loan_id: UUID
    body: str
    created_by: UUID | None
    is_active: bool
    created_at: datetime
    deactivated_at: datetime | None


class InstructionCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


# ── Chat ───────────────────────────────────────────────────────────────


class ChatMessageRead(ORMModel):
    id: UUID
    loan_id: UUID
    from_role: DealChatRole
    from_user_id: UUID | None
    body: str
    client_visible: bool
    created_at: datetime


class ChatSendRequest(BaseModel):
    body: str = Field(min_length=1, max_length=8000)
    mode: DealChatMode


class ChatSendResponse(BaseModel):
    """Polymorphic response — the routing rules in the chat handler can
    produce one of three shapes:
      - kind='message'      → a real chat turn was persisted (`message`),
                              and optionally an AI auto-reply (`ai_reply`).
      - kind='instruction'  → an instruction was created instead.
      - kind='ai_task'      → a broker_suggestion was filed in the AI Inbox.
    """
    kind: str
    message: ChatMessageRead | None = None
    ai_reply: ChatMessageRead | None = None
    instruction: InstructionRead | None = None
    ai_task_id: UUID | None = None
    paused_until: datetime | None = None


class CorrectionCreate(BaseModel):
    correction: str = Field(min_length=1, max_length=4000)


class CorrectionRead(ORMModel):
    id: UUID
    loan_id: UUID
    target_message_id: UUID
    correction: str
    created_by: UUID | None
    created_at: datetime


# ── Scenarios ──────────────────────────────────────────────────────────


class ScenarioBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    discount_points: float = Field(default=0, ge=0)
    loan_amount: float | None = None
    base_rate: float | None = None
    annual_taxes: float | None = None
    annual_insurance: float | None = None
    monthly_hoa: float | None = None
    ltv: float | None = None


class ScenarioRead(ORMModel):
    id: UUID
    loan_id: UUID
    name: str
    discount_points: float
    loan_amount: float | None
    base_rate: float | None
    annual_taxes: float | None
    annual_insurance: float | None
    monthly_hoa: float | None
    ltv: float | None
    recalc_snapshot: dict[str, Any] | None
    created_by: UUID | None
    created_at: datetime


# ── HUD ────────────────────────────────────────────────────────────────


class HudLineRead(ORMModel):
    id: UUID
    loan_id: UUID
    code: str
    label: str
    amount: float
    category: str
    editable: bool


class HudLinePatch(BaseModel):
    label: str | None = None
    amount: float | None = None
    category: str | None = None


# ── Bundled state ──────────────────────────────────────────────────────


class WorkspaceState(BaseModel):
    instructions: list[InstructionRead]
    chat_messages: list[ChatMessageRead]
    scenarios: list[ScenarioRead]
    hud_lines: list[HudLineRead]
    ai_paused_until: datetime | None
    feedback_summary: dict[str, int]  # {"up": 3, "down": 1} across this loan


# ── Feedback ───────────────────────────────────────────────────────────


class FeedbackUpsert(BaseModel):
    output_type: FeedbackOutputType
    output_id: UUID
    loan_id: UUID | None = None
    rating: FeedbackRating
    comment: str | None = Field(default=None, max_length=4000)


class FeedbackRead(ORMModel):
    id: UUID
    output_type: FeedbackOutputType
    output_id: UUID
    loan_id: UUID | None
    rating: FeedbackRating
    comment: str | None
    created_by: UUID
    created_at: datetime
