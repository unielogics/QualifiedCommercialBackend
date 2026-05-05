"""Living Loan File — explicit refresh + inbound email webhook for orchestrator.

Two endpoints, both scoped to a single loan:

POST /loans/{id}/summary/refresh
    Re-runs the AI summarizer and updates loan.status_summary + deal_health.

POST /loans/{id}/inbound-email
    Synthetic-or-real inbound email entrypoint for the Fintech Orchestrator.
    The Pub/Sub-driven worker calls this once it has parsed a Gmail
    notification; the dev tool can call it with synthetic JSON to demo the
    PII scrub + draft creation flow without needing DWD.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.services.ai.summarizer import refresh_summary
from app.services.email.orchestrator import InboundEmail, process_inbound
from app.services.email.parser import extract_deal_id

router = APIRouter(prefix="/loans/{loan_id}", tags=["orchestrator"])


class SummaryRefreshResponse(BaseModel):
    summary: str
    deal_health: str
    used_stub: bool


class InboundEmailRequest(BaseModel):
    sender: str
    subject: str
    body: str


class InboundEmailResponse(BaseModel):
    loan_id: str | None
    sender_role: str
    draft_id: str | None
    task_id: str | None
    note: str


@router.post("/summary/refresh", response_model=SummaryRefreshResponse)
async def refresh_loan_summary(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> SummaryRefreshResponse:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    try:
        result = await refresh_summary(db, loan_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return SummaryRefreshResponse(
        summary=result.summary,
        deal_health=result.deal_health.value,
        used_stub=result.used_stub,
    )


@router.post("/inbound-email", response_model=InboundEmailResponse)
async def inbound_email(
    loan_id: UUID,
    payload: InboundEmailRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> InboundEmailResponse:
    """Test-friendly entrypoint. The real Pub/Sub worker will normally call
    `process_inbound` directly without going through HTTP."""
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")

    deal_id = extract_deal_id(payload.subject)
    email = InboundEmail(
        sender=payload.sender, subject=payload.subject, body=payload.body, deal_id=deal_id
    )
    result = await process_inbound(db, email)

    # Cross-check: the URL-bound loan_id should match the one we resolved from
    # the subject. If not, surface a 400 so the caller fixes their integration.
    if result.loan_id and str(loan_id) != result.loan_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Subject deal_id resolved to a different loan ({result.loan_id}); refusing to process.",
        )

    # Refresh the Living Loan File so the dashboard updates immediately.
    if result.loan_id:
        try:
            await refresh_summary(db, UUID(result.loan_id))
        except Exception:  # noqa: BLE001 — refresh failures shouldn't fail the relay
            pass

    return InboundEmailResponse(**result.__dict__)
