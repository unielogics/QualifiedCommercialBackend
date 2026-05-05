"""Email draft inbox — broker reviews and approves orchestrator-drafted mail."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import EmailDraftStatus, Role
from app.models.activity import Activity
from app.models.email_draft import EmailDraft
from app.schemas.email_draft import EmailDraftDecision, EmailDraftRead
from app.services.email.orchestrator import send_approved_draft

router = APIRouter(prefix="/email-drafts", tags=["email_drafts"])


@router.get("", response_model=list[EmailDraftRead])
async def list_drafts(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    loan_id: UUID | None = None,
) -> list[EmailDraftRead]:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients cannot view drafts")
    stmt = select(EmailDraft).order_by(EmailDraft.created_at.desc())
    if loan_id is not None:
        stmt = stmt.where(EmailDraft.loan_id == loan_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [EmailDraftRead.model_validate(r) for r in rows]


@router.post("/{draft_id}/decision", response_model=EmailDraftRead)
async def decide_draft(
    draft_id: UUID,
    payload: EmailDraftDecision,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EmailDraftRead:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients cannot decide drafts")

    draft = await db.get(EmailDraft, draft_id)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    if draft.status not in (EmailDraftStatus.PENDING, EmailDraftStatus.APPROVED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot decide a draft already {draft.status.value}")

    # Apply optional broker tweaks before send.
    if payload.body_override is not None:
        draft.body = payload.body_override
    if payload.subject_override is not None:
        draft.subject = payload.subject_override

    if payload.decision == EmailDraftStatus.DISMISSED:
        draft.status = EmailDraftStatus.DISMISSED
        draft.actioned_by = user.email
        db.add(
            Activity(
                loan_id=draft.loan_id,
                actor_id=user.id,
                actor_label=user.role.value if hasattr(user.role, "value") else str(user.role),
                kind="email.draft_dismissed",
                summary=f"Dismissed draft to {draft.to_email}",
                payload={"draft_id": str(draft.id)},
            )
        )
        await db.flush()
        return EmailDraftRead.model_validate(draft)

    if payload.decision == EmailDraftStatus.APPROVED:
        await send_approved_draft(db, draft, actor_label=user.email)
        return EmailDraftRead.model_validate(draft)

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "decision must be approved|dismissed")
