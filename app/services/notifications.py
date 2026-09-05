from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.agent_task import AgentTask
from app.models.bucket import Bucket, BucketFile
from app.models.client import Client
from app.models.event import CalendarEvent
from app.models.loan import Loan
from app.models.notification import Notification
from app.models.user import User
from app.services import provenance
from app.services.email.ses_client import SesSendResult, send_email
from app.services.push import fire_and_forget_push

log = logging.getLogger(__name__)

BATCH_WINDOW = timedelta(minutes=2)


def _role_value(role: Any) -> str:
    return role.value if hasattr(role, "value") else str(role)


async def users_with_roles(db: AsyncSession, *roles: Role) -> list[User]:
    return (
        await db.execute(
            select(User).where(
                User.role.in_([r.value for r in roles]),
                User.deleted_at.is_(None),
            )
        )
    ).scalars().all()


async def loan_agent_user_ids(db: AsyncSession, loan: Loan) -> set[UUID]:
    ids: set[UUID] = set()
    if loan.assigned_owner_id:
        ids.add(loan.assigned_owner_id)
    if loan.broker_id:
        from app.models.broker import Broker

        broker = (
            await db.execute(select(Broker).where(Broker.id == loan.broker_id))
        ).scalar_one_or_none()
        if broker and broker.user_id:
            ids.add(broker.user_id)
    return ids


async def client_agent_user_ids(db: AsyncSession, client: Client) -> set[UUID]:
    ids: set[UUID] = set()
    if client.current_agent_id:
        ids.add(client.current_agent_id)
    if client.broker_id:
        from app.models.broker import Broker

        broker = (
            await db.execute(select(Broker).where(Broker.id == client.broker_id))
        ).scalar_one_or_none()
        if broker and broker.user_id:
            ids.add(broker.user_id)
    return ids


async def _load_recipients(db: AsyncSession, recipient_ids: set[UUID]) -> list[User]:
    if not recipient_ids:
        return []
    return (
        await db.execute(
            select(User).where(User.id.in_(recipient_ids), User.deleted_at.is_(None))
        )
    ).scalars().all()


async def notify_users(
    db: AsyncSession,
    *,
    recipient_ids: set[UUID],
    event_type: str,
    title: str,
    body: str,
    category: str = "system",
    priority: str = "medium",
    target_type: str | None = None,
    target_id: str | None = None,
    deep_link: str | None = None,
    meta: dict[str, Any] | None = None,
    batch_key: str | None = None,
    email: bool = False,
    push: bool = True,
    actor_user_id: UUID | None = None,
) -> list[Notification]:
    recipient_ids = {rid for rid in recipient_ids if rid and rid != actor_user_id}
    recipients = await _load_recipients(db, recipient_ids)
    if not recipients:
        return []

    now = datetime.now(UTC)
    channels = ["in_app"]
    if push:
        channels.append("push")
    if email:
        channels.append("email")

    rows: list[Notification] = []
    for recipient in recipients:
        row: Notification | None = None
        incoming_count = int((meta or {}).get("count") or 1)
        display_body = body.replace("{count}", str(incoming_count))
        if batch_key:
            row = (
                await db.execute(
                    select(Notification)
                    .where(
                        Notification.recipient_user_id == recipient.id,
                        Notification.batch_key == batch_key,
                        Notification.read_at.is_(None),
                        Notification.created_at >= now - BATCH_WINDOW,
                    )
                    .order_by(Notification.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is None:
            row = Notification(
                recipient_user_id=recipient.id,
                event_type=event_type,
                category=category,
                priority=priority,
                title=title,
                body=display_body,
                target_type=target_type,
                target_id=target_id,
                deep_link=deep_link,
                channels=channels,
                meta=meta or {},
                batch_key=batch_key,
            )
            db.add(row)
        else:
            existing_meta = dict(row.meta or {})
            count = int(existing_meta.get("count") or 1) + incoming_count
            row.title = title
            row.body = body.replace("{count}", str(count)) if "{count}" in body else body
            row.meta = {**existing_meta, **(meta or {}), "count": count}
            row.channels = list(dict.fromkeys([*(row.channels or []), *channels]))
        rows.append(row)
        if push:
            fire_and_forget_push(
                recipient.id,
                title=row.title,
                body=row.body,
                data={
                    "kind": event_type,
                    "target_type": target_type or "",
                    "target_id": target_id or "",
                    "deep_link": deep_link or "",
                },
            )
            row.pushed_at = now
        if email and recipient.email:
            # emailed_at used to be stamped here, next to a fire-and-forget task
            # whose result was discarded — so a bounced or refused send was
            # recorded as a delivered one, on every caller. Stamp it only when
            # the send actually succeeded, and keep the reason when it did not.
            sent = await _send_notification_email(
                db,
                recipient.email,
                subject=row.title,
                body=f"{row.body}\n\nOpen Qualified Commercial: {deep_link or '/'}",
                event_type=event_type,
                owner_user_id=recipient.id,
            )
            if sent.ok:
                row.emailed_at = now
            else:
                row.meta = {**(row.meta or {}), "email_error": sent.detail[:200]}
    await db.flush()
    from app.services.communication_events import publish_communication_event

    for row in rows:
        await publish_communication_event(
            db,
            recipient_user_ids={row.recipient_user_id},
            event_type="notification.created",
            notification_id=row.id,
        )
    return rows


async def notify_inbound_communication(
    db: AsyncSession,
    *,
    recipient_ids: set[UUID],
    channel: str,
    sender_label: str | None,
    thread_id: str,
    message_id: str | None = None,
    subject: str | None = None,
) -> list[Notification]:
    """Create one durable in-app notification for one inbound communication.

    These are intentionally not batched. Each received email or text remains
    independently reviewable until the recipient opens its thread or clears
    notifications. Message bodies stay out of this general-purpose table.
    """
    normalized_channel = "email" if channel == "email" else "SMS"
    sender = (sender_label or "Unknown sender").strip() or "Unknown sender"
    clean_subject = " ".join((subject or "").split())[:180]
    body = clean_subject if clean_subject else "Open Inbox to review this message."
    return await notify_users(
        db,
        recipient_ids=recipient_ids,
        event_type=f"{channel}_received",
        category="messages",
        priority="high",
        title=f"New {normalized_channel} from {sender}",
        body=body,
        target_type="communication_thread",
        target_id=thread_id,
        deep_link=f"/inbox?thread={quote(thread_id, safe='')}",
        meta={
            "channel": channel,
            "thread_id": thread_id,
            "message_id": message_id,
        },
        email=False,
        push=True,
    )


async def _send_notification_email(
    db: AsyncSession, to_email: str, *, subject: str, body: str,
    event_type: str = "", owner_user_id=None,
) -> SesSendResult:
    """Send, record, and report.

    boto3 is blocking so it runs on a thread, but the caller waits for the
    answer: a notification that claims to have been emailed and was not is
    worse than a slow one. The ledger row is what the audit page reads, and it
    is written whether the send worked or not.
    """
    try:
        result = await asyncio.to_thread(
            send_email,
            to_email=to_email,
            subject=subject[:200],
            body_text=body,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("notification email failed to=%s", to_email)
        result = SesSendResult(False, None, f"send_failed: {exc}")

    from app.services.messaging import outbox

    await outbox.record(
        db,
        channel="email",
        status="sent" if result.ok else "failed",
        draft=outbox.Draft(to=to_email, subject=subject[:200], body_text=body),
        context=(event_type or "notification")[:48],
        provider="ses",
        provider_message_id=result.message_id,
        detail="" if result.ok else result.detail,
        # The recipient is a colleague, so the notification is theirs to see.
        subject=outbox.Subject(owner_user_id=owner_user_id),
    )
    return result


async def notify_document_uploaded(
    db: AsyncSession,
    *,
    loan: Loan,
    document_name: str,
    actor: User,
) -> None:
    recipients = await loan_agent_user_ids(db, loan)
    if loan.source_deal_id is not None:
        recipients.update(user.id for user in await users_with_roles(db, Role.LOAN_EXEC, Role.SUPER_ADMIN))
    client = await db.get(Client, loan.client_id)
    client_name = client.name if client else loan.deal_id
    await notify_users(
        db,
        recipient_ids=recipients,
        actor_user_id=actor.id,
        event_type="document_uploaded",
        category="documents",
        priority="high",
        title=f"New document uploaded for {client_name}",
        body=f"{{count}} file(s) uploaded to {loan.deal_id}. Latest: {document_name}.",
        target_type="loan",
        target_id=str(loan.id),
        deep_link=f"/loans/{loan.id}?tab=documents",
        meta={"loan_id": str(loan.id), "deal_id": loan.deal_id, "document_name": document_name, "count": 1},
        batch_key=f"loan_upload:{loan.id}",
        email=True,
        push=True,
    )


async def notify_bucket_file_uploaded(
    db: AsyncSession,
    *,
    bucket: Bucket,
    file: BucketFile,
) -> None:
    recipients = {bucket.created_by_id} if bucket.created_by_id else set()
    recipients.update(user.id for user in await users_with_roles(db, Role.SUPER_ADMIN))
    await notify_users(
        db,
        recipient_ids={rid for rid in recipients if rid},
        event_type="bucket_file_uploaded",
        category="buckets",
        priority="high",
        title=f"New bucket upload: {bucket.name}",
        # The team could not tell a client's own upload from one the desk made on
        # their behalf: this email was identical either way.
        body=(
            f"{{count}} file(s) uploaded to {bucket.name}. Latest: {file.file_name}."
            f"\nSource: {provenance.describe_document(file)}."
        ),
        target_type="bucket",
        target_id=str(bucket.id),
        deep_link=f"/admin/buckets?bucket={bucket.id}",
        meta={
            "bucket_id": str(bucket.id),
            "file_id": str(file.id),
            "file_name": file.file_name,
            "count": 1,
            "source_kind": file.source_kind,
            "source_label": provenance.describe_document(file),
        },
        batch_key=f"bucket_upload:{bucket.id}",
        email=True,
        push=True,
    )


async def notify_funding_handoff(
    db: AsyncSession,
    *,
    loan: Loan,
    client: Client,
    actor: User,
) -> None:
    recipients = await loan_agent_user_ids(db, loan)
    recipients.update(user.id for user in await users_with_roles(db, Role.LOAN_EXEC, Role.SUPER_ADMIN))
    await notify_users(
        db,
        recipient_ids=recipients,
        actor_user_id=actor.id,
        event_type="funding_handoff",
        category="pipeline",
        priority="high",
        title=f"{client.name} moved to funding",
        body=f"{client.name} was promoted into underwriting as {loan.deal_id}.",
        target_type="loan",
        target_id=str(loan.id),
        deep_link=f"/loans/{loan.id}",
        meta={"loan_id": str(loan.id), "client_id": str(client.id), "deal_id": loan.deal_id},
        batch_key=f"funding_handoff:{loan.id}",
        email=True,
        push=True,
    )


async def notify_message_sent(
    db: AsyncSession,
    *,
    loan: Loan,
    from_role: str,
    actor: User,
) -> None:
    recipients: set[UUID] = set()
    client = await db.get(Client, loan.client_id)
    if from_role == "client":
        recipients.update(await loan_agent_user_ids(db, loan))
        recipients.update(user.id for user in await users_with_roles(db, Role.LOAN_EXEC, Role.SUPER_ADMIN))
    elif client and client.user_id:
        recipients.add(client.user_id)
    await notify_users(
        db,
        recipient_ids=recipients,
        actor_user_id=actor.id,
        event_type="message_received",
        category="messages",
        priority="medium",
        title=f"New message on {loan.deal_id}",
        body=f"A new message was posted for {client.name if client else loan.deal_id}.",
        target_type="loan",
        target_id=str(loan.id),
        deep_link=f"/loans/{loan.id}?tab=messages",
        meta={"loan_id": str(loan.id), "deal_id": loan.deal_id, "from_role": from_role},
        batch_key=f"message:{loan.id}:{from_role}",
        email=True,
        push=True,
    )


async def notify_calendar_event(
    db: AsyncSession,
    *,
    event: CalendarEvent,
    actor: User,
    changed: bool = False,
) -> None:
    recipients: set[UUID] = {event.owner_user_id} if event.owner_user_id else set()
    if event.loan_id:
        loan = await db.get(Loan, event.loan_id)
        if loan:
            recipients.update(await loan_agent_user_ids(db, loan))
            client = await db.get(Client, loan.client_id)
            if client and client.user_id:
                recipients.add(client.user_id)
    await notify_users(
        db,
        recipient_ids={rid for rid in recipients if rid},
        actor_user_id=actor.id,
        event_type="appointment_updated" if changed else "appointment_created",
        category="calendar",
        priority="medium",
        title=("Appointment updated" if changed else "New appointment"),
        body=f"{event.title} is scheduled for {event.starts_at.strftime('%b %d at %I:%M %p') if hasattr(event.starts_at, 'strftime') else 'the calendar'}.",
        target_type="calendar_event",
        target_id=str(event.id),
        deep_link="/calendar",
        meta={"event_id": str(event.id), "loan_id": str(event.loan_id) if event.loan_id else None},
        batch_key=f"calendar:{event.id}",
        email=True,
        push=True,
    )


async def notify_agent_task_assigned(
    db: AsyncSession,
    *,
    task: AgentTask,
    actor: User,
    changed: bool = False,
) -> None:
    if not task.assigned_user_id:
        return
    await notify_users(
        db,
        recipient_ids={task.assigned_user_id},
        actor_user_id=actor.id,
        event_type="task_updated" if changed else "task_assigned",
        category="tasks",
        priority=task.priority or "medium",
        title=("Task updated" if changed else "New task assigned"),
        body=task.title,
        target_type="agent_task",
        target_id=str(task.id),
        deep_link=f"/clients/{task.client_id}?tab=tasks",
        meta={"task_id": str(task.id), "client_id": str(task.client_id)},
        batch_key=f"agent_task:{task.id}",
        email=True,
        push=True,
    )
