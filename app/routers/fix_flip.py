"""Fix & Flip Deal Analyzer scenario persistence.

Standalone analyzer runs. Scoping: SUPER_ADMIN / LOAN_EXEC see all;
BROKER sees scenarios they created or attached to their own clients;
CLIENT sees only scenarios they created. Attachment guard: a caller
can only attach client_id / deal_id / loan_id they themselves can
access — IDs in the request body are never trusted.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.client import Client
from app.models.deal import Deal
from app.models.fix_flip_scenario import FixFlipScenario
from app.models.loan import Loan
from app.models.user import User
from app.schemas.fix_flip import (
    FixFlipScenarioCreate,
    FixFlipScenarioRead,
    FixFlipScenarioUpdate,
)

router = APIRouter(prefix="/fix-flip", tags=["fix-flip"])


def _is_operator(user: User) -> bool:
    return user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC)


async def _assert_can_attach(
    db: AsyncSession,
    user: User,
    *,
    client_id: UUID | None,
    deal_id: UUID | None,
    loan_id: UUID | None,
) -> None:
    """403 unless the caller can access every attached record. Never
    trust the IDs from the request body."""
    if _is_operator(user):
        return
    if client_id is not None:
        stmt = select(Client.id).where(Client.id == client_id)
        if user.role == Role.CLIENT and user.client:
            stmt = stmt.where(Client.id == user.client.id)
        elif user.role == Role.BROKER and user.broker:
            stmt = stmt.where(Client.broker_id == user.broker.id)
        else:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot attach this client")
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot attach this client")
    if deal_id is not None:
        stmt = select(Deal.id).where(Deal.id == deal_id)
        if user.role == Role.CLIENT and user.client:
            stmt = stmt.where(Deal.client_id == user.client.id)
        elif user.role == Role.BROKER:
            stmt = stmt.where(Deal.assigned_agent_id == user.id)
        else:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot attach this deal")
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot attach this deal")
    if loan_id is not None:
        stmt = select(Loan.id).where(Loan.id == loan_id)
        if user.role == Role.CLIENT and user.client:
            stmt = stmt.where(Loan.client_id == user.client.id)
        elif user.role == Role.BROKER and user.broker:
            stmt = stmt.where(Loan.broker_id == user.broker.id)
        else:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot attach this loan")
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot attach this loan")


def _scope(user: User, stmt):
    """Read/list scope. Operators see all; everyone else sees only
    rows they created (plus, for brokers, their own clients')."""
    if _is_operator(user):
        return stmt
    if user.role == Role.BROKER and user.broker:
        # created_by self OR attached to one of the broker's clients.
        client_subq = select(Client.id).where(Client.broker_id == user.broker.id)
        return stmt.where(
            or_(
                FixFlipScenario.created_by == user.id,
                FixFlipScenario.client_id.in_(client_subq),
            )
        )
    return stmt.where(FixFlipScenario.created_by == user.id)


async def _load_scoped(db: AsyncSession, user: User, sid: UUID) -> FixFlipScenario:
    row = (
        await db.execute(_scope(user, select(FixFlipScenario).where(FixFlipScenario.id == sid)))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scenario not found")
    return row


@router.post("/scenarios", response_model=FixFlipScenarioRead, status_code=status.HTTP_201_CREATED)
async def create_scenario(
    payload: FixFlipScenarioCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FixFlipScenarioRead:
    await _assert_can_attach(
        db, user,
        client_id=payload.client_id, deal_id=payload.deal_id, loan_id=payload.loan_id,
    )
    row = FixFlipScenario(
        created_by=user.id,
        client_id=payload.client_id,
        deal_id=payload.deal_id,
        loan_id=payload.loan_id,
        status=payload.status or "saved",
        payload=payload.payload,
        deal_score=payload.deal_score,
        deal_grade=payload.deal_grade,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return FixFlipScenarioRead.model_validate(row)


@router.get("/scenarios", response_model=list[FixFlipScenarioRead])
async def list_scenarios(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[FixFlipScenarioRead]:
    stmt = _scope(user, select(FixFlipScenario)).order_by(
        FixFlipScenario.updated_at.desc()
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [FixFlipScenarioRead.model_validate(r) for r in rows]


@router.get("/scenarios/{scenario_id}", response_model=FixFlipScenarioRead)
async def get_scenario(
    scenario_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FixFlipScenarioRead:
    row = await _load_scoped(db, user, scenario_id)
    return FixFlipScenarioRead.model_validate(row)


@router.patch("/scenarios/{scenario_id}", response_model=FixFlipScenarioRead)
async def update_scenario(
    scenario_id: UUID,
    payload: FixFlipScenarioUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FixFlipScenarioRead:
    row = await _load_scoped(db, user, scenario_id)
    changes = payload.model_dump(exclude_unset=True)
    # Re-verify access if attachment links are being changed.
    if any(k in changes for k in ("client_id", "deal_id", "loan_id")):
        await _assert_can_attach(
            db, user,
            client_id=changes.get("client_id", row.client_id),
            deal_id=changes.get("deal_id", row.deal_id),
            loan_id=changes.get("loan_id", row.loan_id),
        )
    for k, v in changes.items():
        setattr(row, k, v)
    await db.flush()
    await db.refresh(row)
    return FixFlipScenarioRead.model_validate(row)
