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


class ChatAttachmentRead(BaseModel):
    document_id: UUID
    name: str
    mime: str | None = None
    url: str | None = None


class ChatMessageRead(ORMModel):
    id: UUID
    # Optional so the same schema serializes deal_chat_messages
    # (keyed on deal_id, no loan_id).
    loan_id: UUID | None = None
    from_role: DealChatRole
    from_user_id: UUID | None
    # Resolved display name of the human sender (users.name, falling
    # back to brokers.display_name). None for AI / unresolved — the
    # frontend renders "Elara" for AI and the role word as
    # the suffix.
    from_name: str | None = None
    body: str
    client_visible: bool
    created_at: datetime
    # Optional file attachment (alembic 0056). None for plain text.
    attachment: ChatAttachmentRead | None = None


class ChatSendRequest(BaseModel):
    body: str = Field(min_length=1, max_length=8000)
    mode: DealChatMode
    # Optional document attachment (alembic 0056). The client uploads
    # via /documents/upload-init→complete, then references the
    # resulting document_id here so the note + file land in one turn.
    attachment_document_id: UUID | None = None


class ChatSendResponse(BaseModel):
    """Polymorphic response — the routing rules in the chat handler can
    produce one of three shapes:
      - kind='message'      → a real chat turn was persisted (`message`),
                              and optionally an AI auto-reply (`ai_reply`).
      - kind='instruction'  → an instruction was created instead.
      - kind='ai_task'      → a broker_suggestion was filed in Elara Inbox.
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
    # Alembic 0042 — settlement-statement-style extras.
    payee: str | None = None
    note: str | None = None
    created_by_share_link_id: UUID | None = None


class HudLinePatch(BaseModel):
    label: str | None = None
    amount: float | None = None
    category: str | None = None
    payee: str | None = None
    note: str | None = None


class HudLineCreate(BaseModel):
    """POST body for the operator-side add-row endpoint. `code` defaults
    to "custom" since most operator additions are loan-specific line
    items the playbook didn't seed."""
    label: str
    amount: float = 0
    category: str = "variable"
    code: str = "custom"
    payee: str | None = None
    note: str | None = None


# ── HUD share links (alembic 0042) ─────────────────────────────────────


class HudShareLinkCreate(BaseModel):
    label: str | None = None
    invitee_email: str | None = None
    invitee_role: str | None = None
    expires_at: datetime | None = None


class HudShareLinkRead(ORMModel):
    id: UUID
    loan_id: UUID
    token: str
    label: str | None
    invitee_email: str | None
    invitee_role: str | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class PublicHudView(BaseModel):
    """Token-resolved HUD payload returned to invitees. Only loan
    identifiers + the HUD lines they can edit (those tagged to this
    share link OR the still-editable `category=variable` rows)."""
    loan_label: str
    loan_address: str
    invitee_label: str | None
    invitee_role: str | None
    revoked: bool
    expired: bool
    lines: list[HudLineRead]


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
