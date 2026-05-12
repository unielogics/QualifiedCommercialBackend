"""Deal create/update/read schemas (Phase 3)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


DealTypeLiteral = Literal["buyer", "seller", "investor", "borrower"]
DealSideLiteral = Literal["buyer", "seller"]
DealStatusLiteral = Literal["open", "active", "paused", "won", "lost", "promoted"]
DealHandoffLiteral = Literal["none", "requested", "packet_built", "promoted"]
DealAILiteral = Literal["idle", "active", "paused"]


class DealCreate(BaseModel):
    deal_type: DealTypeLiteral
    title: str = Field(min_length=1, max_length=160)
    side: DealSideLiteral | None = None  # auto-derived from deal_type when omitted
    property_id: UUID | None = None
    assigned_agent_id: UUID | None = None
    summary: str | None = None


class DealUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    status: Literal["open", "active", "paused", "won", "lost"] | None = None  # 'promoted' is set only by handoff
    handoff_status: DealHandoffLiteral | None = None
    ai_status: DealAILiteral | None = None
    assigned_agent_id: UUID | None = None
    property_id: UUID | None = None


class DealOut(ORMModel):
    id: UUID
    client_id: UUID
    deal_type: DealTypeLiteral
    side: DealSideLiteral
    status: DealStatusLiteral
    handoff_status: DealHandoffLiteral
    ai_status: DealAILiteral
    title: str
    summary: str | None
    property_id: UUID | None
    assigned_agent_id: UUID | None
    promoted_loan_id: UUID | None
    living_profile: dict[str, Any] | None = None
    created_at: Any
    updated_at: Any


class MarkReadyRequest(BaseModel):
    """Phase 4 — body for POST .../mark-ready-for-lending."""
    override_loan_type: str | None = None
    override_purpose: str | None = None
    notes: str | None = None


class MarkReadyResponse(BaseModel):
    loan_id: UUID
    deal_id: UUID
    handoff_packet_id: UUID | None = None
    prequal_request_id: UUID | None = None
    lending_thread_id: UUID | None = None
    handoff_summary: str | None = None
    missing_lending_items: list[str] = []


__all__ = [
    "DealCreate",
    "DealUpdate",
    "DealOut",
    "MarkReadyRequest",
    "MarkReadyResponse",
]
