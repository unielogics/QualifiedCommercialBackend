from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import AITaskStatus, Role
from app.models.activity import Activity
from app.models.ai_task import AITask
from app.schemas.ai_task import AITaskDecision, AITaskRead

router = APIRouter(prefix="/ai-tasks", tags=["ai-tasks"])


@router.get("", response_model=list[AITaskRead])
async def list_tasks(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[AITaskRead]:
    if user.role == Role.CLIENT:
        return []
    stmt = (
        select(AITask)
        .where(AITask.status == AITaskStatus.PENDING)
        .order_by(AITask.priority, AITask.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [AITaskRead.model_validate(r) for r in rows]


@router.post("/{task_id}/decision", response_model=AITaskRead)
async def decide(
    task_id: UUID,
    decision: AITaskDecision,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AITaskRead:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator action only")
    task = await db.get(AITask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    if decision.decision not in {AITaskStatus.APPROVED, AITaskStatus.DISMISSED}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid decision")
    task.status = decision.decision
    if decision.edited_payload is not None:
        task.draft_payload = decision.edited_payload
    db.add(
        Activity(
            loan_id=task.loan_id,
            actor_id=user.id,
            actor_label=user.role,
            kind=f"ai_task.{decision.decision.value}",
            summary=f"AI task {task.title} → {decision.decision.value}",
        )
    )
    await db.flush()
    await db.refresh(task)
    return AITaskRead.model_validate(task)
