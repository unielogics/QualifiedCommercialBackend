"""Per-loan participant management — frontend-driven.

Operators add / edit / remove the people on a deal's email thread from the
Loan Detail UI. This is the single source of truth used by the Fintech
Orchestrator for PII scrubbing and BCC routing — never read from .env.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.loan import Loan
from app.models.loan_participant import LoanParticipant
from app.schemas.loan_participant import (
    LoanParticipantCreate,
    LoanParticipantRead,
    LoanParticipantUpdate,
    role_defaults,
)

router = APIRouter(prefix="/loans/{loan_id}/participants", tags=["participants"])


async def _ensure_loan_visible(db: AsyncSession, loan_id: UUID, user) -> Loan:
    """Apply the standard scope rules so a client can't peek at another deal."""
    from app.routers.loans import _scope_query  # local import: avoid cycle

    stmt = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(stmt)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    return loan


@router.get("", response_model=list[LoanParticipantRead])
async def list_participants(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[LoanParticipantRead]:
    await _ensure_loan_visible(db, loan_id, user)
    rows = (
        await db.execute(
            select(LoanParticipant).where(LoanParticipant.loan_id == loan_id).order_by(LoanParticipant.role, LoanParticipant.email)
        )
    ).scalars().all()
    # Clients should never see Lender rows — enforce here too.
    if user.role == Role.CLIENT:
        rows = [r for r in rows if r.role != "lender"]
    return [LoanParticipantRead.model_validate(r) for r in rows]


@router.post("", response_model=LoanParticipantRead, status_code=status.HTTP_201_CREATED)
async def create_participant(
    loan_id: UUID,
    payload: LoanParticipantCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LoanParticipantRead:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients cannot manage participants")
    await _ensure_loan_visible(db, loan_id, user)

    defaults = role_defaults(payload.role)
    row = LoanParticipant(
        loan_id=loan_id,
        email=str(payload.email).strip().lower(),
        display_name=payload.display_name,
        role=payload.role,
        company=payload.company,
        cc_outbound=(payload.cc_outbound if payload.cc_outbound is not None else defaults["cc_outbound"]),
        bcc_outbound=(payload.bcc_outbound if payload.bcc_outbound is not None else defaults["bcc_outbound"]),
        hide_identity=(payload.hide_identity if payload.hide_identity is not None else defaults["hide_identity"]),
    )
    db.add(row)
    try:
        await db.flush()
    except Exception as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Could not add participant: {exc}") from exc
    await db.refresh(row)
    return LoanParticipantRead.model_validate(row)


@router.patch("/{participant_id}", response_model=LoanParticipantRead)
async def update_participant(
    loan_id: UUID,
    participant_id: UUID,
    payload: LoanParticipantUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LoanParticipantRead:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients cannot manage participants")
    await _ensure_loan_visible(db, loan_id, user)
    row = await db.get(LoanParticipant, participant_id)
    if row is None or row.loan_id != loan_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Participant not found on this loan")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    await db.flush()
    await db.refresh(row)
    return LoanParticipantRead.model_validate(row)


@router.delete("/{participant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_participant(
    loan_id: UUID,
    participant_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients cannot manage participants")
    await _ensure_loan_visible(db, loan_id, user)
    row = await db.get(LoanParticipant, participant_id)
    if row is None or row.loan_id != loan_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Participant not found on this loan")
    await db.delete(row)
    await db.flush()
    return None
