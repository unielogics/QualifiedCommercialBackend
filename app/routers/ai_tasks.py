from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import AITaskStatus, Role
from app.models.activity import Activity
from app.models.ai_task import AITask
from app.models.loan import Loan
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
    if user.role == Role.BROKER and user.broker is not None:
        # Brokers see tasks tied to their own loans + firm-wide
        # null-loan tasks. The `loan_id IS NULL` widening is
        # intentional — today's null-loan AITasks are firm-wide
        # alerts (credit-pull expiry, prequal review queues) that
        # ALL operators including brokers should see. If we ever
        # introduce broker-confidential null-loan tasks, add a
        # `visible_to_role` field on AITask rather than tightening
        # this filter; otherwise we'd hide legitimate work the
        # broker needs.
        stmt = stmt.where(
            or_(
                AITask.loan_id.is_(None),
                AITask.loan_id.in_(
                    select(Loan.id).where(Loan.broker_id == user.broker.id)
                ),
            )
        )
    # SUPER_ADMIN / LOAN_EXEC keep firm-wide visibility (no extra
    # filter beyond the PENDING gate).
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
