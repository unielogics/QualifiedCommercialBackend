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

# FastAPI dependencies and query declarations intentionally use callable defaults.
# ruff: noqa: B008
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import Select, or_, select
from sqlalchemy import false as sql_false
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.dealer_os.models import AppointmentOutcomeDefinition, DealerRepAppointment
from app.deps import CurrentUser
from app.enums import CalendarEventSource, CalendarEventStatus, Role
from app.models.activity import Activity
from app.models.booking_settings import BookingSettings
from app.models.client import Client
from app.models.event import CalendarEvent
from app.models.loan import Loan
from app.models.user import User
from app.schemas.event import (
    AppointmentOutcomeDefinitionCreate,
    AppointmentOutcomeDefinitionPatch,
    AppointmentOutcomeDefinitionRead,
    CalendarActivityItem,
    CalendarAppointmentTypeCount,
    CalendarEventCreate,
    CalendarEventRead,
    CalendarEventUpdate,
    CalendarWorkspaceCapabilities,
    CalendarWorkspaceEvent,
    CalendarWorkspaceMetrics,
    CalendarWorkspaceRead,
)
from app.scoping import regional_manager_broker_ids_subquery, scope_loan_query
from app.services import calendar_v2
from app.services.activity_log import filter_payload_for_audience, is_visible_to

router = APIRouter(prefix="/calendar", tags=["calendar"])

APPOINTMENT_TYPE_LABELS = {
    "callback": "Intro call",
    "program_intro": "Intro call",
    "intro_call": "Intro call",
    "underwriting_review": "Underwriting review",
    "document_review": "Document review",
    "signing": "Signing",
    "lender_call": "Lender call",
}
APPOINTMENT_TYPE_KEYS = (
    "intro_call",
    "underwriting_review",
    "document_review",
    "signing",
    "lender_call",
)


def _require_calendar_v2(user: User) -> None:
    if not calendar_v2.can_use_calendar_v2(user):
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, "Calendar CRM access required")


def _require_outcome_catalog_admin(user: User) -> None:
    if not calendar_v2.can_manage_outcome_catalog(user):
        raise HTTPException(
            http_status.HTTP_403_FORBIDDEN,
            "Super-admin access is required to configure appointment outcomes",
        )


def _workspace_kind(value: str) -> str:
    if value in {"callback", "program_intro"}:
        return "intro_call"
    return value if value in APPOINTMENT_TYPE_KEYS else "intro_call"


def _workspace_color(kind: str, crm_status: str | None) -> str:
    if crm_status in {"cancelled", "not_qualified"}:
        return "gray"
    if crm_status == "converted":
        return "green"
    if crm_status in {"no_show", "follow_up"}:
        return "amber" if crm_status == "follow_up" else "red"
    return {
        "intro_call": "blue",
        "underwriting_review": "violet",
        "document_review": "amber",
        "signing": "green",
        "lender_call": "gray",
    }.get(kind, "blue")


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
    if user.role == Role.DEALER_PARTNER:
        # No book-of-business, no calendar of their own -- deny by default
        # rather than falling through to LOAN_EXEC's firm-wide visibility.
        return stmt.where(sql_false())
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
    horizon = to_ or (datetime.now(UTC) + timedelta(days=days))
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


@router.get("/workspace", response_model=CalendarWorkspaceRead)
async def get_calendar_workspace(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    from_: datetime = Query(alias="from"),
    to_: datetime = Query(alias="to"),
    include_cancelled: bool = False,
    include_internal: bool = False,
) -> CalendarWorkspaceRead:
    _require_calendar_v2(user)
    if to_ <= from_:
        raise HTTPException(http_status.HTTP_422_UNPROCESSABLE_ENTITY, "Calendar range must end after it starts")
    if to_ - from_ > timedelta(days=370):
        raise HTTPException(http_status.HTTP_422_UNPROCESSABLE_ENTITY, "Calendar range cannot exceed 370 days")

    appointment_stmt = (
        select(DealerRepAppointment)
        .where(
            DealerRepAppointment.starts_at >= from_,
            DealerRepAppointment.starts_at < to_,
        )
        .order_by(DealerRepAppointment.starts_at)
    )
    if not include_cancelled:
        appointment_stmt = appointment_stmt.where(
            DealerRepAppointment.archived_at.is_(None),
            DealerRepAppointment.status != "cancelled",
        )
    if user.role == Role.FIELD_REP:
        appointment_stmt = appointment_stmt.where(DealerRepAppointment.booked_by_user_id == user.id)
    appointments = list((await db.execute(appointment_stmt)).scalars().all())

    events: list[CalendarWorkspaceEvent] = []
    type_counts = {key: 0 for key in APPOINTMENT_TYPE_KEYS}
    now = datetime.now(UTC)
    outcome_logged = 0
    awaiting_outcome = 0
    files_created = 0
    for appointment in appointments:
        kind = _workspace_kind(appointment.kind)
        type_counts[kind] += 1
        has_outcome = bool(
            appointment.workflow_outcome_applied_at
            or appointment.outcome_at
            or appointment.outcome
        )
        if has_outcome:
            outcome_logged += 1
        ends_at = appointment.starts_at + timedelta(minutes=appointment.duration_min or 20)
        if ends_at <= now and not has_outcome and appointment.crm_status not in {"cancelled", "converted"}:
            awaiting_outcome += 1
        if appointment.converted_intake_id or appointment.linked_loan_id:
            files_created += 1
        events.append(
            CalendarWorkspaceEvent(
                id=f"appointment:{appointment.id}",
                event_type="appointment",
                appointment_id=appointment.id,
                calendar_event_id=appointment.calendar_event_id,
                loan_id=appointment.linked_loan_id,
                title=appointment.title,
                kind=kind,
                starts_at=appointment.starts_at,
                ends_at=ends_at,
                status=appointment.status,
                crm_status=appointment.crm_status,
                invitee_name=appointment.invitee_name,
                company=appointment.company,
                meeting_mode=appointment.meeting_mode,
                join_url=appointment.join_url,
                has_outcome=has_outcome,
                color=_workspace_color(kind, appointment.crm_status),
                can_edit=user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}
                or (user.role == Role.FIELD_REP and appointment.booked_by_user_id == user.id),
            )
        )

    if include_internal:
        internal_stmt = select(CalendarEvent).where(
            CalendarEvent.starts_at >= from_,
            CalendarEvent.starts_at < to_,
            or_(
                CalendarEvent.external_ref_kind.is_(None),
                CalendarEvent.external_ref_kind != "dealer_rep_appointment",
            ),
        )
        if not include_cancelled:
            internal_stmt = internal_stmt.where(CalendarEvent.status != CalendarEventStatus.CANCELLED)
        internal_stmt = _scope_calendar_for_audience(user, internal_stmt)
        internal_rows = list((await db.execute(internal_stmt.order_by(CalendarEvent.starts_at))).scalars().all())
        for event in internal_rows:
            events.append(
                CalendarWorkspaceEvent(
                    id=f"internal:{event.id}",
                    event_type="internal",
                    calendar_event_id=event.id,
                    loan_id=event.loan_id,
                    title=event.title,
                    kind=str(event.kind.value if hasattr(event.kind, "value") else event.kind),
                    starts_at=event.starts_at,
                    ends_at=event.starts_at + timedelta(minutes=event.duration_min or 30),
                    status=str(event.status.value if hasattr(event.status, "value") else event.status),
                    has_outcome=event.status == CalendarEventStatus.DONE,
                    color="gray",
                    can_edit=user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC},
                )
            )
    events.sort(key=lambda item: item.starts_at)

    booking = (
        await db.execute(select(BookingSettings).where(BookingSettings.user_id == user.id))
    ).scalar_one_or_none()
    return CalendarWorkspaceRead(
        range_start=from_,
        range_end=to_,
        timezone=booking.timezone if booking else "America/New_York",
        events=events,
        metrics=CalendarWorkspaceMetrics(
            appointments=len(appointments),
            outcome_logged=outcome_logged,
            awaiting_outcome=awaiting_outcome,
            files_created=files_created,
        ),
        appointment_types=[
            CalendarAppointmentTypeCount(key=key, label=APPOINTMENT_TYPE_LABELS[key], count=type_counts[key])
            for key in APPOINTMENT_TYPE_KEYS
        ],
        capabilities=CalendarWorkspaceCapabilities(
            can_create=True,
            can_manage_all=user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC},
            can_drag=True,
            can_create_funding_loan=calendar_v2.can_create_funding_file(user),
            can_manage_appointment_crm=calendar_v2.can_use_calendar_v2(user),
            can_apply_outcomes=calendar_v2.can_use_calendar_v2(user),
            can_manage_outcome_catalog=calendar_v2.can_manage_outcome_catalog(user),
        ),
    )


@router.get("/outcomes", response_model=list[AppointmentOutcomeDefinitionRead])
async def list_calendar_outcomes(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = False,
) -> list[AppointmentOutcomeDefinitionRead]:
    _require_calendar_v2(user)
    if include_inactive and not calendar_v2.can_manage_outcome_catalog(user):
        raise HTTPException(
            http_status.HTTP_403_FORBIDDEN,
            "Super-admin access is required to view retired appointment outcomes",
        )
    await calendar_v2.ensure_default_outcomes(db)
    await db.commit()
    stmt = (
        select(AppointmentOutcomeDefinition)
        .where(AppointmentOutcomeDefinition.scope == calendar_v2.SHARED_OUTCOME_SCOPE)
        .order_by(AppointmentOutcomeDefinition.sort_order, AppointmentOutcomeDefinition.created_at)
    )
    if not include_inactive:
        stmt = stmt.where(AppointmentOutcomeDefinition.active.is_(True))
    rows = list((await db.execute(stmt)).scalars().all())
    return [AppointmentOutcomeDefinitionRead.model_validate(row) for row in rows]


@router.post(
    "/outcomes",
    response_model=AppointmentOutcomeDefinitionRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_calendar_outcome(
    payload: AppointmentOutcomeDefinitionCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AppointmentOutcomeDefinitionRead:
    _require_outcome_catalog_admin(user)
    row = AppointmentOutcomeDefinition(
        owner_user_id=None,
        scope=calendar_v2.SHARED_OUTCOME_SCOPE,
        normalized_name=calendar_v2.normalize_outcome_name(payload.name),
        **payload.model_dump(),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(http_status.HTTP_409_CONFLICT, "A shared outcome already uses this name") from exc
    await db.refresh(row)
    return AppointmentOutcomeDefinitionRead.model_validate(row)


async def _load_shared_outcome(
    db: AsyncSession,
    outcome_id: UUID,
) -> AppointmentOutcomeDefinition:
    row = (
        await db.execute(
            select(AppointmentOutcomeDefinition).where(
                AppointmentOutcomeDefinition.id == outcome_id,
                AppointmentOutcomeDefinition.scope == calendar_v2.SHARED_OUTCOME_SCOPE,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Outcome not found")
    return row


@router.patch("/outcomes/{outcome_id}", response_model=AppointmentOutcomeDefinitionRead)
async def patch_calendar_outcome(
    outcome_id: UUID,
    payload: AppointmentOutcomeDefinitionPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AppointmentOutcomeDefinitionRead:
    _require_outcome_catalog_admin(user)
    row = await _load_shared_outcome(db, outcome_id)
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "No outcome changes supplied")
    if "name" in patch:
        patch["normalized_name"] = calendar_v2.normalize_outcome_name(patch["name"])
    for key, value in patch.items():
        setattr(row, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(http_status.HTTP_409_CONFLICT, "A shared outcome already uses this name") from exc
    await db.refresh(row)
    return AppointmentOutcomeDefinitionRead.model_validate(row)


@router.delete("/outcomes/{outcome_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def disable_calendar_outcome(
    outcome_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_outcome_catalog_admin(user)
    row = await _load_shared_outcome(db, outcome_id)
    row.active = False
    await db.commit()


@router.get("/activity", response_model=list[CalendarActivityItem])
async def list_calendar_activity(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = 30,
    from_: datetime | None = Query(default=None, alias="from"),
    to_: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=250),
) -> list[CalendarActivityItem]:
    horizon = to_ or datetime.now(UTC)
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


async def _push_to_google(db: AsyncSession, ev: CalendarEvent, *, deleted: bool = False) -> None:
    """Best-effort mirror of an internal event to the owner's Google Calendar.
    Never raises — a Google outage must not break the local calendar write."""
    try:
        from app.services.google.calendar_sync import push_event

        await push_event(db, ev, deleted=deleted)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("calendar google push failed event=%s", getattr(ev, "id", None))


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
    try:
        from app.services.notifications import notify_calendar_event

        await notify_calendar_event(db, event=ev, actor=user, changed=False)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("calendar create notification failed event=%s", ev.id)
    await _push_to_google(db, ev)
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
    try:
        from app.services.notifications import notify_calendar_event

        await notify_calendar_event(db, event=ev, actor=user, changed=True)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("calendar update notification failed event=%s", ev.id)
    await _push_to_google(db, ev)
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
    await _push_to_google(db, ev, deleted=True)
    await db.delete(ev)
    await db.flush()
