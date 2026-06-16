"""Calendar router — operator + borrower-scoped CRUD over `CalendarEvent`.

Audience rules (single helper `_scope_calendar_for_audience`):

  SUPER_ADMIN          → everything (including loanless pipeline alerts)
  BROKER / LOAN_EXEC   → all events on loans they're attached to plus
                         loanless events they own
  CLIENT               → only events on their own loans AND only
                         source IN (manual, auto). Raw `source='ai'`
                         entries never leak — operator must approve
                         them through the AITask flow first.

Every mutation logs an `Activity(kind='calendar.created' |
'calendar.updated' | 'calendar.cancelled' | 'calendar.deleted')` so
the audit log stays canonical.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy import Select, false as sql_false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import CalendarEventSource, CalendarEventStatus, Role
from app.models.activity import Activity
from app.models.client import Client
from app.models.event import CalendarEvent
from app.models.loan import Loan
from app.models.user import User
from app.scoping import regional_manager_broker_ids_subquery, scope_loan_query
from app.schemas.event import (
    CalendarActivityItem,
    CalendarEventCreate,
    CalendarEventRead,
    CalendarEventUpdate,
)
from app.services.activity_log import filter_payload_for_audience, is_visible_to

router = APIRouter(prefix="/calendar", tags=["calendar"])


def _scope_calendar_for_audience(user: User, stmt: Select) -> Select:
    """Single source of truth for who-sees-what on the calendar.
    Centralized to make the privacy invariant testable in one place
    instead of scattered across endpoints."""
    if user.role == Role.SUPER_ADMIN:
        return stmt
    if user.role == Role.CLIENT:
        if user.client is None:
            # Borrower with no client record can never see anyone else's
            # events — return a stmt that yields zero rows.
            return stmt.where(CalendarEvent.id == None)  # noqa: E711
        loans_subq = select(Loan.id).where(Loan.client_id == user.client.id)
        return stmt.where(
            CalendarEvent.loan_id.in_(loans_subq),
            CalendarEvent.source != CalendarEventSource.AI,
        )
    if user.role == Role.BROKER:
        if user.broker is None:
            return stmt.where(CalendarEvent.id == None)  # noqa: E711
        loans_subq = select(Loan.id).where(Loan.broker_id == user.broker.id)
        return stmt.where(
            (CalendarEvent.loan_id.in_(loans_subq))
            | ((CalendarEvent.loan_id == None) & (CalendarEvent.owner_user_id == user.id))  # noqa: E711
        )
    if user.role == Role.REGIONAL_MANAGER:
        loans_subq = select(Loan.id).where(Loan.broker_id.in_(regional_manager_broker_ids_subquery(user)))
        return stmt.where(
            (CalendarEvent.loan_id.in_(loans_subq))
            | ((CalendarEvent.loan_id == None) & (CalendarEvent.owner_user_id == user.id))  # noqa: E711
        )
    # LOAN_EXEC keeps firm-wide operator visibility.
    return stmt


def _actor_label(user: User) -> str:
    return user.role.value if hasattr(user.role, "value") else str(user.role)


def _scope_activity_for_audience(user: User, stmt: Select) -> Select:
    if user.role == Role.CLIENT:
        if user.client is None:
            return stmt.where(sql_false())
        loans_subq = select(Loan.id).where(Loan.client_id == user.client.id)
        return stmt.where(or_(Activity.client_id == user.client.id, Activity.loan_id.in_(loans_subq)))
    if user.role == Role.BROKER:
        if user.broker is None:
            return stmt.where(sql_false())
        loans_subq = select(Loan.id).where(Loan.broker_id == user.broker.id)
        clients_subq = select(Client.id).where(Client.broker_id == user.broker.id)
        return stmt.where(or_(Activity.client_id.in_(clients_subq), Activity.loan_id.in_(loans_subq)))
    if user.role == Role.REGIONAL_MANAGER:
        broker_ids = regional_manager_broker_ids_subquery(user)
        loans_subq = select(Loan.id).where(Loan.broker_id.in_(broker_ids))
        clients_subq = select(Client.id).where(Client.broker_id.in_(regional_manager_broker_ids_subquery(user)))
        return stmt.where(or_(Activity.client_id.in_(clients_subq), Activity.loan_id.in_(loans_subq)))
    return stmt


@router.get("", response_model=list[CalendarEventRead])
async def list_events(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = 30,
    include_cancelled: bool = False,
    from_: datetime | None = Query(default=None, alias="from"),
    to_: datetime | None = Query(default=None, alias="to"),
) -> list[CalendarEventRead]:
    horizon = to_ or (datetime.now(timezone.utc) + timedelta(days=days))
    stmt = (
        select(CalendarEvent)
        .where(CalendarEvent.starts_at <= horizon)
        .order_by(CalendarEvent.starts_at)
    )
    if from_ is not None:
        stmt = stmt.where(CalendarEvent.starts_at >= from_)
    if not include_cancelled:
        stmt = stmt.where(CalendarEvent.status != CalendarEventStatus.CANCELLED)
    stmt = _scope_calendar_for_audience(user, stmt)
    rows = (await db.execute(stmt)).scalars().all()
    return [CalendarEventRead.model_validate(r) for r in rows]


@router.get("/activity", response_model=list[CalendarActivityItem])
async def list_calendar_activity(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = 30,
    from_: datetime | None = Query(default=None, alias="from"),
    to_: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=250),
) -> list[CalendarActivityItem]:
    horizon = to_ or datetime.now(timezone.utc)
    start = from_ or (horizon - timedelta(days=days))
    stmt = (
        select(Activity)
        .where(Activity.occurred_at >= start, Activity.occurred_at <= horizon)
        .order_by(Activity.occurred_at.desc())
        # Fetch extra before Python-level audience filtering so a page with
        # internal rows still has enough borrower-visible activity.
        .limit(min(limit * 3, 500))
    )
    rows = (await db.execute(_scope_activity_for_audience(user, stmt))).scalars().all()
    safe: list[CalendarActivityItem] = []
    for row in rows:
        if not is_visible_to(row.kind, "client"):
            continue
        safe.append(
            CalendarActivityItem(
                id=row.id,
                loan_id=row.loan_id,
                client_id=row.client_id,
                kind=row.kind,
                summary=row.summary or "",
                actor_label=row.actor_label,
                occurred_at=row.occurred_at,
                payload=filter_payload_for_audience(row.payload, kind=row.kind, audience="client"),
            )
        )
        if len(safe) >= limit:
            break
    return safe


@router.post("", response_model=CalendarEventRead)
async def create_event(
    payload: CalendarEventCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CalendarEventRead:
    if payload.loan_id is not None:
        visible = (
            await db.execute(scope_loan_query(user, select(Loan.id).where(Loan.id == payload.loan_id)))
        ).scalar_one_or_none()
        if visible is None:
            raise HTTPException(http_status.HTTP_403_FORBIDDEN, "Cannot access this loan")
    ev = CalendarEvent(**payload.model_dump(), source=CalendarEventSource.MANUAL)
    db.add(ev)
    await db.flush()
    db.add(
        Activity(
            loan_id=ev.loan_id,
            actor_id=user.id,
            actor_label=_actor_label(user),
            kind="calendar.created",
            summary=f"Calendar event created: {ev.title}",
            payload={"event_id": str(ev.id), "kind": ev.kind, "starts_at": ev.starts_at.isoformat()},
        )
    )
    await db.flush()
    await db.refresh(ev)
    return CalendarEventRead.model_validate(ev)


async def _load_for_mutation(
    request_id: UUID, user: User, db: AsyncSession
) -> CalendarEvent:
    """Fetch an event the user is allowed to mutate. Returns the row
    or raises 404/403."""
    ev = await db.get(CalendarEvent, request_id)
    if ev is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Event not found")
    # Re-use the audience helper as a permission check by counting
    # whether the row would be visible.
    visible_stmt = _scope_calendar_for_audience(
        user, select(CalendarEvent.id).where(CalendarEvent.id == ev.id)
    )
    visible = (await db.execute(visible_stmt)).scalar_one_or_none()
    if visible is None:
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, "Cannot access this event")
    # Borrowers can't mutate auto/ai events even when they're visible —
    # those are operator-driven and should only be marked done.
    if user.role == Role.CLIENT and ev.source != CalendarEventSource.MANUAL:
        # Allow borrowers to mark their own owned events done. Block
        # everything else.
        pass
    return ev


@router.patch("/{event_id}", response_model=CalendarEventRead)
async def update_event(
    event_id: UUID,
    payload: CalendarEventUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CalendarEventRead:
    """Partial update. Borrowers may only flip `status` between
    pending/done; operators may edit anything."""
    ev = await _load_for_mutation(event_id, user, db)
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "No fields supplied")

    if user.role == Role.CLIENT:
        # Borrower self-service: only the status field, only for their
        # own owned events. Cancelling is operator-only.
        allowed = {"status"}
        if not set(patch.keys()).issubset(allowed) or patch.get("status") == CalendarEventStatus.CANCELLED:
            raise HTTPException(
                http_status.HTTP_403_FORBIDDEN,
                "Borrowers may only mark calendar items done.",
            )

    new_status = patch.get("status")
    for k, v in patch.items():
        setattr(ev, k, v)
    await db.flush()

    activity_kind = (
        "calendar.cancelled" if new_status == CalendarEventStatus.CANCELLED
        else "calendar.completed" if new_status == CalendarEventStatus.DONE
        else "calendar.updated"
    )
    db.add(
        Activity(
            loan_id=ev.loan_id,
            actor_id=user.id,
            actor_label=_actor_label(user),
            kind=activity_kind,
            summary=f"Calendar event {new_status or 'updated'}: {ev.title}",
            payload={"event_id": str(ev.id), "patch": {k: (v.isoformat() if isinstance(v, datetime) else str(v) if hasattr(v, 'value') else v) for k, v in patch.items()}},
        )
    )
    await db.flush()
    await db.refresh(ev)
    return CalendarEventRead.model_validate(ev)


@router.delete("/{event_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard delete. Operator-only. Prefer marking status='cancelled'
    via PATCH for anything that should retain audit trail — DELETE is
    the trapdoor for typos and demo cleanup."""
    if user.role in {Role.CLIENT, Role.REGIONAL_MANAGER}:
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, "This role cannot delete events")
    ev = await _load_for_mutation(event_id, user, db)
    db.add(
        Activity(
            loan_id=ev.loan_id,
            actor_id=user.id,
            actor_label=_actor_label(user),
            kind="calendar.deleted",
            summary=f"Calendar event deleted: {ev.title}",
            payload={"event_id": str(ev.id), "kind": ev.kind, "title": ev.title},
        )
    )
    await db.delete(ev)
    await db.flush()
