"""Unified client workspace aggregate response (Phase 2).

A single round-trip GET /clients/{id}/workspace returns everything the
unified workspace UI needs to render: the client profile, summary of
active deals, summary of active funding files (Loans), a documents
roll-up, the AI plan snapshot, the most recent activity, agent notes,
and a server-computed permissions block so the frontend never
re-derives gating.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.client import ClientRead
from app.schemas.common import ORMModel


# ── Sub-shapes ────────────────────────────────────────────────────────


class WorkspacePermissions(BaseModel):
    """Server-computed role permissions for this client workspace.

    Frontend reads these directly and never re-derives. This keeps
    role logic single-sourced on the backend, where the auth context
    actually lives.
    """

    can_mark_ready_for_lending: bool
    can_edit_underwriting: bool
    can_create_deals: bool
    can_create_funding_files: bool
    can_assign_ai: bool
    can_edit_client_fields: bool
    can_view_funding_tab: bool


AiState = Literal["deployed", "paused", "draft_first", "human_only", "idle"]


class WorkspaceAISummary(BaseModel):
    state: AiState
    outstanding_followups: int
    current_blocker: str | None = None
    next_follow_up_at: datetime | None = None
    next_best_question: str | None = None
    readiness_score: int | None = None


class WorkspaceDocumentsSummary(BaseModel):
    total: int
    missing: int
    pending_review: int


class WorkspaceActivityRow(BaseModel):
    at: datetime
    kind: str
    summary: str
    actor: str | None = None


class WorkspaceNoteRow(BaseModel):
    id: str
    author: str
    at: datetime
    body: str


class WorkspaceSelectedContext(BaseModel):
    """Context the request asked for, plus a server-recommended tab.

    The frontend honors `?tab=` first, then `recommended_tab`, then
    falls back to role-derived defaults. Server populates
    `recommended_tab` from referer/role hints.
    """

    tab: str | None = None
    deal_id: UUID | None = None
    funding_file_id: UUID | None = None
    loan_id: UUID | None = None
    recommended_tab: str | None = None


class FundingFileSummary(ORMModel):
    """A Loan rendered as a FundingFile card. Subset of LoanRead +
    handoff-context fields that the new workspace UI displays."""

    id: UUID
    deal_id: str
    client_id: UUID
    side: str | None = None
    stage: str
    address: str | None = None
    amount: float | None = None
    funding_file_kind: str | None = None  # populated once 0048 migration lands
    source_deal_id: UUID | None = None  # populated once 0048 migration lands
    handoff_summary: str | None = None  # populated once 0048 migration lands
    created_at: datetime
    updated_at: datetime


class DealSummary(BaseModel):
    """Lightweight Deal row for the workspace aggregate. Populated in
    Phase 3 once the Deal model lands; until then the workspace
    endpoint returns an empty list."""

    id: UUID
    client_id: UUID
    deal_type: Literal["buyer", "seller", "investor", "borrower"]
    side: Literal["buyer", "seller"]
    status: str
    handoff_status: str
    ai_status: str
    title: str
    promoted_loan_id: UUID | None = None
    assigned_agent_id: UUID | None = None
    property_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class WorkspaceTabCounts(BaseModel):
    """Numeric pills for the tab strip. None when count is not
    applicable (e.g. before AgentTask model lands in Phase 7)."""

    deals: int | None = None
    funding: int | None = None
    tasks: int | None = None
    ai_follow_up: int | None = None
    documents: int | None = None


# ── Aggregate response ────────────────────────────────────────────────


class WorkspaceOut(BaseModel):
    client: ClientRead
    deals: list[DealSummary]
    funding_files: list[FundingFileSummary]
    documents_summary: WorkspaceDocumentsSummary
    ai_summary: WorkspaceAISummary
    activity_tail: list[WorkspaceActivityRow]
    notes: list[WorkspaceNoteRow]
    role_permissions: WorkspacePermissions
    selected_context: WorkspaceSelectedContext
    tab_counts: WorkspaceTabCounts


__all__ = [
    "WorkspaceOut",
    "WorkspacePermissions",
    "WorkspaceAISummary",
    "WorkspaceDocumentsSummary",
    "WorkspaceActivityRow",
    "WorkspaceNoteRow",
    "WorkspaceSelectedContext",
    "FundingFileSummary",
    "DealSummary",
    "WorkspaceTabCounts",
]
