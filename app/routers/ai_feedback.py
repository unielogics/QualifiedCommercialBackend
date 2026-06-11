"""AI feedback router — thumbs/comments on AI outputs.

This pass exercises only the `ai_task` output type from Elara Inbox UI,
but the table + endpoints are polymorphic so chat replies, email drafts,
and Living Loan File summaries can light up later without a schema change.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import FeedbackOutputType
from app.models.ai_feedback import AIFeedback
from app.models.ai_task import AITask
from app.schemas.loan_workspace import FeedbackRead, FeedbackUpsert

router = APIRouter(prefix="/ai-feedback", tags=["ai-feedback"])


@router.get("", response_model=list[FeedbackRead])
async def list_feedback(
    output_type: FeedbackOutputType,
    output_id: UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[FeedbackRead]:
    rows = (
        await db.execute(
            select(AIFeedback)
            .where(AIFeedback.output_type == output_type, AIFeedback.output_id == output_id)
            .order_by(AIFeedback.created_at.desc())
        )
    ).scalars().all()
    return [FeedbackRead.model_validate(r) for r in rows]


@router.post("", response_model=FeedbackRead, status_code=status.HTTP_201_CREATED)
async def upsert_feedback(
    body: FeedbackUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FeedbackRead:
    """Upsert by (output_type, output_id, created_by). Second vote from the
    same actor replaces the prior row's rating + comment."""
    # If loan_id wasn't supplied but the output is an ai_task, denormalize
    # from the task so the prompt-assembly hook can find this feedback later
    # without an extra join.
    loan_id = body.loan_id
    if loan_id is None and body.output_type == FeedbackOutputType.AI_TASK:
        task = await db.get(AITask, body.output_id)
        if task is not None:
            loan_id = task.loan_id

    existing = (
        await db.execute(
            select(AIFeedback).where(
                AIFeedback.output_type == body.output_type,
                AIFeedback.output_id == body.output_id,
                AIFeedback.created_by == user.id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.rating = body.rating
        existing.comment = body.comment
        existing.loan_id = loan_id
        await db.flush()
        await db.refresh(existing)
        return FeedbackRead.model_validate(existing)

    fb = AIFeedback(
        output_type=body.output_type,
        output_id=body.output_id,
        loan_id=loan_id,
        rating=body.rating,
        comment=body.comment,
        created_by=user.id,
    )
    db.add(fb)
    await db.flush()
    await db.refresh(fb)
    return FeedbackRead.model_validate(fb)


@router.delete("/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    feedback_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    fb = await db.get(AIFeedback, feedback_id)
    if fb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if fb.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Can only delete your own feedback")
    await db.delete(fb)
    await db.flush()
