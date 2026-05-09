"""AI Preview / Test Mode endpoints (Phase 2/3).

Powers the "Preview AI Plan" / "Test Next Best Question" / "Preview
Handoff Packet" / "Preview Cadence Actions" buttons in both portals.

Each endpoint runs the same logic the live AI runs but does NOT
persist anything. Admins and agents see exactly what the AI will do
before they save changes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.client import Client
from app.services.ai.handoff_builder import build_handoff_packet
from app.services.ai.plan_builder import preview as plan_preview

router = APIRouter(prefix="/ai-preview", tags=["ai-preview"])


# ── Plan preview ───────────────────────────────────────────────────


class PreviewPlanIn(BaseModel):
    client_id: UUID
    loan_id: UUID | None = None
    waive_keys: list[str] | None = None
    custom_instructions: str | None = None


class PreviewPlanOut(BaseModel):
    client_id: UUID
    loan_id: UUID | None
    current_phase: str
    required_items: list[dict]
    waived_items: list[dict]
    next_best_question: str | None
    next_best_action: dict | None
    readiness_score: int | None
    custom_instructions: str | None


@router.post("/plan", response_model=PreviewPlanOut)
async def preview_plan(
    payload: PreviewPlanIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PreviewPlanOut:
    """Run the plan builder in preview mode against the supplied
    overrides. Persists nothing. Used by both portals' preview panels."""
    if user.role not in (Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.role == Role.BROKER and user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    overrides: dict[str, Any] = {}
    if payload.waive_keys:
        overrides["waived_keys"] = payload.waive_keys
    if payload.custom_instructions is not None:
        overrides["custom_instructions"] = payload.custom_instructions

    snap = await plan_preview(
        db,
        client_id=payload.client_id,
        loan_id=payload.loan_id,
        overrides=overrides or None,
    )
    return PreviewPlanOut(
        client_id=snap.client_id,
        loan_id=snap.loan_id,
        current_phase=snap.current_phase,
        required_items=snap.required_items,
        waived_items=snap.waived_items,
        next_best_question=snap.next_best_question,
        next_best_action=snap.next_best_action,
        readiness_score=snap.readiness_score,
        custom_instructions=snap.custom_instructions,
    )


# ── Handoff packet preview ─────────────────────────────────────────


class PreviewHandoffIn(BaseModel):
    client_id: UUID


@router.post("/handoff")
async def preview_handoff(
    payload: PreviewHandoffIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Build the handoff packet as if the agent fired Ready-for-Lending
    right now, without persisting it. Lets the agent see the missing-
    items list + first question + recommended path before committing."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only preview")
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    packet = await build_handoff_packet(
        db,
        client=client,
        agent_id=user.id,
        realtor_thread_id=None,
    )
    # Strip non-JSONable fields the live persist path normally handles.
    packet.pop("client_id", None)
    packet.pop("agent_id", None)
    packet.pop("realtor_thread_id", None)
    return packet


# ── Cadence preview ────────────────────────────────────────────────


class PreviewCadenceIn(BaseModel):
    """Optional client filter — when supplied, dry-runs only that
    client's cadence rules. When NULL, dry-runs everything in the
    caller's pipeline (broker)/all (super_admin)."""
    client_id: UUID | None = None


class CadencePreviewItem(BaseModel):
    rule_id: UUID
    trigger_event: str
    action_type: str
    approval_required: bool
    visibility: str
    client_id: UUID
    client_name: str
    requirement_key: str | None
    message_preview: str | None
    fires_now: bool


@router.post("/cadence", response_model=list[CadencePreviewItem])
async def preview_cadence(
    payload: PreviewCadenceIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[CadencePreviewItem]:
    """Show what the cadence engine would fire today against the
    caller's pipeline. Persists nothing."""
    if user.role not in (Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")

    from app.services.ai.cadence_engine import preview_cadence_pass
    items = await preview_cadence_pass(
        db,
        agent_id=user.id if user.role == Role.BROKER else None,
        client_id=payload.client_id,
    )
    return [CadencePreviewItem(**i) for i in items]
