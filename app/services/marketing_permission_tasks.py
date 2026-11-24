"""Internal staff calendar work for contacts who have no marketing permission."""
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import String, case, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db import SessionLocal
from app.enums import (
    AITaskPriority,
    CalendarEventKind,
    CalendarEventSource,
    CalendarEventStatus,
    Role,
)
from app.models.email_marketing import (
    MarketingEnrollment,
    MarketingPermissionRequest,
    MarketingSubscriber,
)
from app.models.event import CalendarEvent
from app.models.notification import Notification
from app.models.user import User
from app.schemas.email_marketing_audience import RecipientSelection
from app.services import email_marketing as marketing
from app.services.email_marketing_directory import selected_contacts
from app.services.marketing_permission_requests import OPEN_INVITES, REVIEW_PATH

AGGREGATE_ID = uuid5(NAMESPACE_URL, "qualifiedcommercial:marketing-permission-review")
INDIVIDUAL = "marketing_permission"
TEAM_REVIEW = "marketing_permission_review"


async def _create_event(db, *, identifier, owner, kind, external_id, title, description, due):
    await db.execute(insert(CalendarEvent).values(
        id=identifier, owner_user_id=owner, external_ref_kind=kind, external_ref_id=external_id,
        kind=CalendarEventKind.MILESTONE, status=CalendarEventStatus.PENDING, source=CalendarEventSource.AUTO,
        priority=AITaskPriority.MEDIUM, title=title, description=description, starts_at=due,
    ).on_conflict_do_nothing(index_elements=["id"]))


async def ensure_task(db, row, now, *, reopen=False):
    identifier = row.task_event_id or uuid5(row.id, "permission-follow-up")
    await _create_event(
        db, identifier=identifier, owner=row.created_by, kind=INDIVIDUAL, external_id=str(row.id),
        title="Review marketing permission", due=now + timedelta(days=2),
        description=("Review the contact in the permission follow-up history in Email Marketing. Record existing "
                     "permission only with supporting evidence. /admin/email-marketing"),
    )
    if reopen:
        await db.execute(update(CalendarEvent).where(
            CalendarEvent.id == identifier, CalendarEvent.status != CalendarEventStatus.PENDING,
        ).values(status=CalendarEventStatus.PENDING, starts_at=now + timedelta(days=2)))
    row.task_event_id = identifier


async def resolve_task(db, row, now):
    if row.task_event_id is None:
        return
    await db.execute(update(CalendarEvent).where(
        CalendarEvent.id == row.task_event_id, CalendarEvent.status == CalendarEventStatus.PENDING,
    ).values(status=CalendarEventStatus.DONE))
    await _mark_reminder_read(db, row.task_event_id, now)


async def _mark_reminder_read(db, event_id, now):
    await db.execute(update(Notification).where(
        Notification.target_type == "marketing_permission_task", Notification.target_id == str(event_id),
        Notification.read_at.is_(None),
    ).values(read_at=now))


async def _review_backlog(db, now):
    contacts = selected_contacts(marketing.AudienceFilters(), RecipientSelection(mode="all")).subquery()
    count = await db.scalar(select(func.count()).select_from(contacts).where(
        contacts.c.status == "requestable", contacts.c.permission_status.is_(None)))
    event = await db.get(CalendarEvent, AGGREGATE_ID, with_for_update=True)
    if not count:
        if event and event.status == CalendarEventStatus.PENDING:
            event.status = CalendarEventStatus.DONE
            await _mark_reminder_read(db, event.id, now)
        return
    owners = select(User.id).where(User.role == Role.SUPER_ADMIN, User.account_status == "active",
                                   User.deleted_at.is_(None)).order_by(User.created_at, User.id)
    owner = await db.scalar(owners.where(User.id == event.owner_user_id)) if event else None
    if owner is None:
        owner = await db.scalar(owners.limit(1))
    if not owner:
        return
    title = f"Review {count} contact{'s' if count != 1 else ''} missing marketing permission"
    description = f"Review these contacts and assign internal follow-ups in Email Marketing: {REVIEW_PATH}"
    if event is None:
        await _create_event(db, identifier=AGGREGATE_ID, owner=owner, kind=TEAM_REVIEW, external_id="all",
                            title=title, description=description, due=now)
    else:
        event.title, event.owner_user_id = title, owner
        if event.status != CalendarEventStatus.PENDING:
            event.status, event.starts_at = CalendarEventStatus.PENDING, now


async def _resolve_finished(db, now):
    from app.services.marketing_permission_requests import close_request

    permission = MarketingPermissionRequest
    sub = MarketingSubscriber
    consented = select(MarketingEnrollment.id).where(MarketingEnrollment.subscriber_id == sub.id).exists()
    outcome = case(
        (sub.unsubscribed_at.is_not(None) | marketing.contact_hold_reason(permission.email).is_not(None), "blocked"),
        (consented, "confirmed"), (permission.expires_at <= now, "expired"),
    )
    query = select(permission, outcome).outerjoin(sub, sub.email == permission.email).where(
        permission.status.in_(("task_only", *OPEN_INVITES)), outcome.is_not(None),
    ).order_by(permission.id).limit(500).with_for_update(of=permission, skip_locked=True)
    for row, status in (await db.execute(query)).all():
        await close_request(db, row, status, now)


async def _remind(db, now):
    # Keep recurring sweeps set-based for already-notified, still-open tasks.
    # UUID casts have hyphens on PostgreSQL; SQLite stores their 32 hex digits.
    event_key = func.replace(cast(CalendarEvent.id, String), "-", "")
    already_notified = select(Notification.id).where(
        Notification.batch_key == event_key, Notification.recipient_user_id == CalendarEvent.owner_user_id,
        Notification.created_at >= CalendarEvent.starts_at,
    ).exists()
    events = (await db.execute(select(CalendarEvent, MarketingPermissionRequest.email).outerjoin(
        MarketingPermissionRequest, MarketingPermissionRequest.task_event_id == CalendarEvent.id,
    ).join(User, User.id == CalendarEvent.owner_user_id).where(
        CalendarEvent.external_ref_kind.in_((INDIVIDUAL, TEAM_REVIEW)),
        CalendarEvent.status == CalendarEventStatus.PENDING, CalendarEvent.starts_at <= now,
        User.role == Role.SUPER_ADMIN, User.account_status == "active", User.deleted_at.is_(None),
        ~already_notified,
    ))).all()
    for event, address in events:
        # One in-app reminder per due date, including after reopening a cleared aggregate.
        due = marketing._utc_instant(event.starts_at).isoformat()
        await db.execute(insert(Notification).values(id=uuid5(event.id, f"{event.owner_user_id}:{due}"),
            recipient_user_id=event.owner_user_id, event_type="marketing.permission_review_due", category="system",
            priority="medium", title=event.title, body="Review this follow-up in Email Marketing.",
            target_type="marketing_permission_task", target_id=str(event.id),
            deep_link=("/admin/email-marketing?" + urlencode({"search": address})) if address else REVIEW_PATH,
            channels=["in_app"], meta={}, batch_key=event.id.hex, created_at=now,
        ).on_conflict_do_nothing(index_elements=["id"]))


async def reconcile(db, now):
    await _resolve_finished(db, now)
    await _review_backlog(db, now)
    await db.flush()
    await _remind(db, now)
    await db.commit()


async def reconcile_tasks():
    async with SessionLocal() as db:
        await reconcile(db, datetime.now(UTC))
