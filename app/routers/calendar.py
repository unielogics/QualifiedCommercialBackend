from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.event import CalendarEvent
from app.models.loan import Loan
from app.schemas.event import CalendarEventCreate, CalendarEventRead

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get("", response_model=list[CalendarEventRead])
async def list_events(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = 30,
) -> list[CalendarEventRead]:
    horizon = datetime.now(timezone.utc) + timedelta(days=days)
    stmt = select(CalendarEvent).where(CalendarEvent.starts_at <= horizon).order_by(CalendarEvent.starts_at)
    if user.role == Role.CLIENT and user.client:
        loans_subq = select(Loan.id).where(Loan.client_id == user.client.id)
        stmt = stmt.where(CalendarEvent.loan_id.in_(loans_subq))
    rows = (await db.execute(stmt)).scalars().all()
    return [CalendarEventRead.model_validate(r) for r in rows]


@router.post("", response_model=CalendarEventRead)
async def create_event(
    payload: CalendarEventCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> CalendarEventRead:
    ev = CalendarEvent(**payload.model_dump())
    db.add(ev)
    await db.flush()
    await db.refresh(ev)
    return CalendarEventRead.model_validate(ev)
