"""AgentTask CRUD + AI promotion (Phase 7).

The AI promotion endpoint is the critical glue — it atomically
upserts a synthetic ClientRequirementStatus row with
`requirement_key = f"agent_task:{task_id}"` plus an AITaskAssignment
bound to it, so the existing DealSecretaryPicker renders the task
in its AI column with no UI changes. Reverse direction (drag from
AI → Human, or PATCH owner_type back to 'human') clears the
synthetic CRS row.

Visibility tiers gate what flows through funding handoff (see
services/handoff.promote_deal_to_loan):
- agent_private  : never transferred
- team_visible   : transferred only when broker's firm policy allows
- funding_visible: always transferred
- client_visible : never transferred (client-facing, not UW context)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role, TaskOwnerType
from app.models.agent_task import AgentTask
from app.models.ai_task_assignment import AITaskAssignment
from app.models.client import Client
from app.models.client_requirement_status import ClientRequirementStatus
from app.scoping import scope_client_query
from app.schemas.agent_task import (
    AgentTaskCreate,
    AgentTaskOut,
    AgentTaskUpdate,
    PromoteToAiResponse,
)

router = APIRouter(tags=["agent_tasks"])


# Map AgentTaskCategory → AI-Workbench category bucket (RequirementCategory).
_CATEGORY_MAP: dict[str, str] = {
    "buyer_workflow": "communication",
    "seller_workflow": "communication",
    "funding_prep": "financials",
    "showing": "scheduling",
    "open_house": "scheduling",
    "listing_prep": "agreements",
    "cma": "agreements",
    "photography": "scheduling",
    "document_collection": "financials",
    "other": "communication",
}


async def _load_client_or_404(client_id: UUID, user, db: AsyncSession) -> Client:
    stmt = scope_client_query(user, select(Client).where(Client.id == client_id))
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    return client


def _visibility_clause_for(user):
    """Return a list of allowed visibility values for the calling user.

    - CLIENT: only client_visible
    - BROKER (own client): all except agent_private from other agents
    - ADMIN (super_admin / loan_exec): all four
    """
    if user.role == Role.CLIENT:
        return ["client_visible"]
    if user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        return None  # No filter — see everything.
    # BROKER — return None and the route handler filters out agent_private
    # rows that don't belong to this broker.
    return None


@router.get("/clients/{client_id}/tasks", response_model=list[AgentTaskOut])
async def list_tasks(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    deal_id: UUID | None = None,
    loan_id: UUID | None = None,
    status: str | None = None,
    visibility: str | None = None,
) -> list[AgentTaskOut]:
    await _load_client_or_404(client_id, user, db)
    stmt = select(AgentTask).where(AgentTask.client_id == client_id)
    if deal_id is not None:
        stmt = stmt.where(AgentTask.deal_id == deal_id)
    if loan_id is not None:
        stmt = stmt.where(AgentTask.loan_id == loan_id)
    if status is not None:
        stmt = stmt.where(AgentTask.status == status)
    if visibility is not None:
        stmt = stmt.where(AgentTask.visibility == visibility)

    # Default visibility filter — CLIENT sees only client_visible;
    # BROKER hides agent_private tasks they didn't create.
    if user.role == Role.CLIENT:
        stmt = stmt.where(AgentTask.visibility == "client_visible")
    elif user.role == Role.BROKER:
        stmt = stmt.where(
            (AgentTask.visibility != "agent_private")
            | (AgentTask.created_by == user.id)
            | (AgentTask.assigned_user_id == user.id)
        )

    rows = (
        await db.execute(stmt.order_by(AgentTask.due_at.asc().nulls_last(), AgentTask.created_at.desc()))
    ).scalars().all()
    return [AgentTaskOut.model_validate(r) for r in rows]


@router.post(
    "/clients/{client_id}/tasks",
    response_model=AgentTaskOut,
    status_code=201,
)
async def create_task(
    client_id: UUID,
    payload: AgentTaskCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentTaskOut:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    await _load_client_or_404(client_id, user, db)
    task = AgentTask(
        client_id=client_id,
        created_by=user.id,
        **payload.model_dump(),
    )
    db.add(task)
    await db.flush()
    await db.refresh(task)
    return AgentTaskOut.model_validate(task)


@router.patch("/clients/{client_id}/tasks/{task_id}", response_model=AgentTaskOut)
async def update_task(
    client_id: UUID,
    task_id: UUID,
    payload: AgentTaskUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentTaskOut:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    await _load_client_or_404(client_id, user, db)
    task = (
        await db.execute(
            select(AgentTask).where(AgentTask.id == task_id, AgentTask.client_id == client_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("status") == "done" and task.completed_at is None:
        task.completed_at = datetime.now(timezone.utc)
    for k, v in data.items():
        setattr(task, k, v)
    await db.flush()
    await db.refresh(task)
    return AgentTaskOut.model_validate(task)


@router.delete("/clients/{client_id}/tasks/{task_id}", status_code=204)
async def delete_task(
    client_id: UUID,
    task_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    await _load_client_or_404(client_id, user, db)
    task = (
        await db.execute(
            select(AgentTask).where(AgentTask.id == task_id, AgentTask.client_id == client_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    if task.created_by != user.id and user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only creator or admin can delete")
    await db.delete(task)
    await db.flush()


# ── AI promotion (the critical contract) ────────────────────────────


@router.post(
    "/clients/{client_id}/tasks/{task_id}/promote-to-ai",
    response_model=PromoteToAiResponse,
)
async def promote_to_ai(
    client_id: UUID,
    task_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PromoteToAiResponse:
    """Promote an AI-owned AgentTask into the DealSecretaryPicker.

    Creates a synthetic ClientRequirementStatus row with
    requirement_key='agent_task:{task_id}', source='agent_task', and
    binds an AITaskAssignment to it. The platform requirement resolver
    skips agent_task: prefixed keys so plan rebuilds don't try to
    satisfy synthetic tasks against catalog rules.

    409 when:
      - task.owner_type != 'ai' (caller must set ownership first)
      - task.ai_assignment_id already set (idempotent — return existing)
    """
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    await _load_client_or_404(client_id, user, db)

    task = (
        await db.execute(
            select(AgentTask).where(AgentTask.id == task_id, AgentTask.client_id == client_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    if task.owner_type != TaskOwnerType.AI.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Task must have owner_type='ai' before AI promotion",
        )

    requirement_key = f"agent_task:{task.id}"

    # Idempotency — if already promoted, return the existing assignment.
    if task.ai_assignment_id is not None:
        existing = await db.get(AITaskAssignment, task.ai_assignment_id)
        if existing is not None and existing.deleted_at is None:
            return PromoteToAiResponse(
                task=AgentTaskOut.model_validate(task),
                assignment_id=existing.id,
                requirement_key=requirement_key,
            )

    # Upsert the synthetic CRS row in the right scope.
    crs_stmt = (
        select(ClientRequirementStatus)
        .where(
            ClientRequirementStatus.client_id == client_id,
            ClientRequirementStatus.requirement_key == requirement_key,
        )
    )
    if task.loan_id is not None:
        crs_stmt = crs_stmt.where(ClientRequirementStatus.loan_id == task.loan_id)
    elif task.deal_id is not None:
        crs_stmt = crs_stmt.where(ClientRequirementStatus.deal_id == task.deal_id)
    crs = (await db.execute(crs_stmt)).scalar_one_or_none()
    if crs is None:
        crs = ClientRequirementStatus(
            id=uuid.uuid4(),
            client_id=client_id,
            loan_id=task.loan_id,
            deal_id=task.deal_id if task.loan_id is None else None,
            requirement_key=requirement_key,
            status="asked",
            source="agent_task",
            owner_type=TaskOwnerType.AI.value,
            notes=f"Promoted from AgentTask {task.id} ({task.title})",
        )
        db.add(crs)
        await db.flush()
    else:
        crs.owner_type = TaskOwnerType.AI.value
        crs.status = "asked"

    # Create the AITaskAssignment bound to the CRS row.
    assignment = AITaskAssignment(
        id=uuid.uuid4(),
        client_requirement_status_id=crs.id,
        owner_type=TaskOwnerType.AI.value,
        instructions=task.description or task.title,
        instructions_visibility="agent",
        approval_mode="draft_first",
        created_by=user.id,
    )
    db.add(assignment)
    await db.flush()
    crs.ai_assignment_id = assignment.id
    task.ai_assignment_id = assignment.id
    await db.flush()
    await db.refresh(task)

    return PromoteToAiResponse(
        task=AgentTaskOut.model_validate(task),
        assignment_id=assignment.id,
        requirement_key=requirement_key,
    )
