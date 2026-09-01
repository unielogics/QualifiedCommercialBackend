from __future__ import annotations

# FastAPI dependency declarations intentionally use Depends in defaults.
# ruff: noqa: B008
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.dealer_os.models import (
    DealerBusiness,
    DealerMessage,
    DealerRepContact,
    DealerRepInboxMessage,
    DealerRepInboxThread,
)
from app.deps import CurrentUser
from app.enums import MessageFrom, Role
from app.models.bucket import Bucket, BucketAIMessage, BucketNote, BucketUploadLink
from app.models.client import Client
from app.models.email_message import EmailMessage
from app.models.loan import Loan
from app.models.message import Message
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.communication import (
    UnifiedCommunicationCompose,
    UnifiedCommunicationMessage,
    UnifiedCommunicationSeen,
    UnifiedCommunicationThread,
    UnifiedCommunicationThreadDetail,
    UnifiedCommunicationThreadPage,
)
from app.scoping import scope_client_query, scope_loan_query
from app.services.user_access import is_audit_client

router = APIRouter(prefix="/communications", tags=["communications"])
SCAN_LIMIT = 750


def _at(value: datetime | None) -> datetime:
    return value or datetime.min.replace(tzinfo=UTC)


def _snippet(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.split())
    return text[:180] or None


async def _visible_intakes(db: AsyncSession, user: User) -> list[PublicUnderwritingIntake]:
    stmt = select(PublicUnderwritingIntake).order_by(PublicUnderwritingIntake.last_message_at.desc().nullslast()).limit(SCAN_LIMIT)
    if user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        pass
    elif user.role == Role.DEALER_PARTNER:
        stmt = stmt.where(PublicUnderwritingIntake.broker_id == user.id)
    elif user.role in (Role.CLIENT, Role.BROKER, Role.REGIONAL_MANAGER):
        client_ids = scope_client_query(user, select(Client.id))
        stmt = stmt.where(PublicUnderwritingIntake.client_id.in_(client_ids))
    else:
        return []
    return list((await db.execute(stmt)).scalars().all())


async def _visible_dealers(db: AsyncSession, user: User) -> list[DealerBusiness]:
    stmt = select(DealerBusiness).limit(SCAN_LIMIT)
    if user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        pass
    elif is_audit_client(user):
        stmt = stmt.where(DealerBusiness.dealer_user_id == user.id)
    elif user.role == Role.FIELD_REP:
        stmt = stmt.where(DealerBusiness.owner_user_id == user.id)
    else:
        return []
    return list((await db.execute(stmt)).scalars().all())


async def _loan_threads(db: AsyncSession, user: User) -> list[UnifiedCommunicationThread]:
    loans = list(
        (
            await db.execute(
                scope_loan_query(user, select(Loan)).order_by(Loan.updated_at.desc()).limit(SCAN_LIMIT)
            )
        ).scalars().all()
    )
    if not loans:
        return []
    clients = {
        row.id: row
        for row in (
            await db.execute(select(Client).where(Client.id.in_({loan.client_id for loan in loans})))
        ).scalars().all()
    }
    messages = list(
        (
            await db.execute(
                select(Message).where(Message.loan_id.in_([loan.id for loan in loans])).order_by(Message.sent_at.asc())
            )
        ).scalars().all()
    )
    grouped: dict[UUID, list[Message]] = {}
    for message in messages:
        grouped.setdefault(message.loan_id, []).append(message)
    threads = []
    for loan in loans:
        rows = grouped.get(loan.id, [])
        if not rows:
            continue
        latest = rows[-1]
        client = clients.get(loan.client_id)
        threads.append(
            UnifiedCommunicationThread(
                id=f"loan:{loan.id}",
                title=loan.address,
                participant_name=client.name if client else None,
                participant_email=client.email if client else None,
                participant_type="client",
                source_kind="loan",
                source_id=str(loan.id),
                source_ref=loan.deal_id,
                source_label=loan.address,
                channel="client",
                transport="portal",
                message_count=len(rows),
                latest_snippet=_snippet(latest.body),
                latest_at=latest.sent_at,
                href=f"/loans/{loan.id}?tab=messages",
            )
        )
    return threads


def _intake_allowed_channels(user: User) -> set[str]:
    if user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        return {"underwriter_ai", "client", "partner", "internal"}
    if user.role == Role.DEALER_PARTNER:
        return {"partner"}
    if user.role == Role.CLIENT:
        return {"client"}
    if user.role in (Role.BROKER, Role.REGIONAL_MANAGER):
        return {"client"}
    return set()


async def _intake_threads(db: AsyncSession, user: User) -> list[UnifiedCommunicationThread]:
    intakes = await _visible_intakes(db, user)
    if not intakes:
        return []
    by_bucket = {intake.bucket_id: intake for intake in intakes}
    ai_rows = list(
        (
            await db.execute(
                select(BucketAIMessage).where(BucketAIMessage.bucket_id.in_(by_bucket)).order_by(BucketAIMessage.created_at.asc())
            )
        ).scalars().all()
    )
    note_rows = list(
        (
            await db.execute(
                select(BucketNote).where(
                    BucketNote.bucket_id.in_(by_bucket), BucketNote.visibility == "admin"
                ).order_by(BucketNote.created_at.asc())
            )
        ).scalars().all()
    )
    grouped: dict[tuple[UUID, str], list[object]] = {}
    for row in ai_rows:
        channel = "client" if row.audience == "uploader" else "underwriter_ai"
        grouped.setdefault((row.bucket_id, channel), []).append(row)
    for row in note_rows:
        channel = "internal" if row.channel == "internal" else "partner"
        grouped.setdefault((row.bucket_id, channel), []).append(row)
    allowed = _intake_allowed_channels(user)
    result = []
    for (bucket_id, channel), rows in grouped.items():
        if channel not in allowed:
            continue
        intake = by_bucket[bucket_id]
        latest = rows[-1]
        content = latest.content
        created_at = latest.created_at
        label = {
            "underwriter_ai": "Underwriter AI",
            "client": "Client conversation",
            "partner": "Partner channel",
            "internal": "Internal notes",
        }[channel]
        result.append(
            UnifiedCommunicationThread(
                id=f"intake:{intake.id}:{channel}",
                title=intake.business_name or intake.full_name,
                participant_name=intake.full_name,
                participant_email=intake.email,
                participant_type="dealer_partner" if channel == "partner" else "internal" if channel == "internal" else "client",
                source_kind="intake",
                source_id=str(intake.id),
                source_ref=f"QC-I-{str(intake.id)[:8].upper()}",
                source_label=label,
                channel=channel,
                transport="portal",
                message_count=len(rows),
                latest_snippet=_snippet(content),
                latest_at=created_at,
                href=f"/admin/ai-underwriter-leads?lead={intake.id}&channel={channel}",
            )
        )
    return result


async def _dealer_threads(db: AsyncSession, user: User) -> list[UnifiedCommunicationThread]:
    dealers = await _visible_dealers(db, user)
    if not dealers:
        return []
    by_id = {dealer.id: dealer for dealer in dealers}
    messages = list(
        (
            await db.execute(
                select(DealerMessage).where(DealerMessage.dealer_id.in_(by_id)).order_by(DealerMessage.created_at.asc())
            )
        ).scalars().all()
    )
    grouped: dict[tuple[UUID, str], list[DealerMessage]] = {}
    for message in messages:
        channel = "client" if message.channel == "client" else "desk"
        if is_audit_client(user) and channel != "client":
            continue
        grouped.setdefault((message.dealer_id, channel), []).append(message)
    return [
        UnifiedCommunicationThread(
            id=f"dealer:{dealer_id}:{channel}",
            title=by_id[dealer_id].legal_name or by_id[dealer_id].name,
            participant_name=by_id[dealer_id].name,
            participant_email=by_id[dealer_id].email,
            participant_type="rep" if channel == "desk" else "client",
            source_kind="dealer",
            source_id=str(dealer_id),
            source_ref=by_id[dealer_id].case_ref,
            source_label="Rep / audit file",
            channel=channel,
            transport="portal",
            message_count=len(rows),
            latest_snippet=_snippet(rows[-1].body),
            latest_at=rows[-1].created_at,
            href=f"/admin/dealer-messages?dealer={dealer_id}&channel={channel}",
        )
        for (dealer_id, channel), rows in grouped.items()
    ]


async def _rep_threads(db: AsyncSession, user: User) -> list[UnifiedCommunicationThread]:
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP):
        return []
    stmt = select(DealerRepInboxThread, DealerRepContact).outerjoin(
        DealerRepContact, DealerRepContact.id == DealerRepInboxThread.contact_id
    )
    if user.role == Role.FIELD_REP:
        stmt = stmt.where(DealerRepInboxThread.owner_user_id == user.id)
    rows = list((await db.execute(stmt.order_by(DealerRepInboxThread.last_message_at.desc().nullslast()).limit(SCAN_LIMIT))).all())
    return [
        UnifiedCommunicationThread(
            id=f"rep:{thread.id}",
            title=thread.subject,
            participant_name=contact.full_name if contact else None,
            participant_email=contact.email if contact else None,
            participant_type="rep_lead",
            source_kind="rep",
            source_id=str(thread.dealer_id or thread.contact_id or thread.id),
            source_ref=None,
            source_label=contact.company if contact else "Rep inbox",
            channel=thread.channel,
            transport=thread.channel,
            unread_count=thread.unread_count,
            message_count=0,
            latest_snippet=None,
            latest_at=thread.last_message_at or thread.created_at,
            assigned_desk=str(thread.owner_user_id) if thread.owner_user_id else None,
            href=f"/admin/dealer-messages?rep_thread={thread.id}",
            can_reply=thread.owner_user_id == user.id,
        )
        for thread, contact in rows
    ]


async def _email_threads(db: AsyncSession, user: User) -> list[UnifiedCommunicationThread]:
    rows = list(
        (
            await db.execute(
                select(EmailMessage)
                .where(EmailMessage.owner_user_id == user.id)
                .order_by(EmailMessage.received_at.desc().nullslast(), EmailMessage.created_at.desc())
                .limit(SCAN_LIMIT)
            )
        ).scalars().all()
    )
    grouped: dict[str, list[EmailMessage]] = {}
    for row in rows:
        grouped.setdefault(row.gmail_thread_id or str(row.id), []).append(row)
    result = []
    for key, members in grouped.items():
        members.sort(key=lambda row: _at(row.received_at or row.created_at))
        latest = members[-1]
        result.append(
            UnifiedCommunicationThread(
                id=f"email:{key}",
                title=latest.subject or "Email conversation",
                participant_name=latest.from_email,
                participant_email=latest.from_email,
                participant_type=latest.matched_party_role or "email_contact",
                source_kind="email",
                source_id=key,
                source_ref=None,
                source_label="Connected mailbox",
                channel="email",
                transport="email",
                unread_count=sum(1 for row in members if not row.is_read),
                message_count=len(members),
                latest_snippet=_snippet(latest.snippet),
                latest_at=latest.received_at or latest.created_at,
                href=f"/inbox?thread={key}",
            )
        )
    return result


async def _all_threads(db: AsyncSession, user: User) -> list[UnifiedCommunicationThread]:
    parts = [
        await _loan_threads(db, user),
        await _intake_threads(db, user),
        await _dealer_threads(db, user),
        await _rep_threads(db, user),
        await _email_threads(db, user),
    ]
    rows = [row for part in parts for row in part]
    rows.sort(key=lambda row: row.latest_at, reverse=True)
    return rows


@router.get("/threads", response_model=UnifiedCommunicationThreadPage)
async def list_unified_communication_threads(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, max_length=160),
    participant_type: str | None = None,
    source_kind: str | None = None,
    channel: str | None = None,
    unread_only: bool = False,
    limit: int = Query(60, ge=1, le=150),
    offset: int = Query(0, ge=0),
) -> UnifiedCommunicationThreadPage:
    rows = await _all_threads(db, user)
    term = (q or "").strip().lower()
    if term:
        rows = [row for row in rows if term in " ".join(filter(None, (row.title, row.participant_name, row.participant_email, row.source_ref, row.latest_snippet))).lower()]
    if participant_type:
        rows = [row for row in rows if row.participant_type == participant_type]
    if source_kind:
        rows = [row for row in rows if row.source_kind == source_kind]
    if channel:
        rows = [row for row in rows if row.channel == channel]
    if unread_only:
        rows = [row for row in rows if row.unread_count > 0]
    total = len(rows)
    return UnifiedCommunicationThreadPage(
        items=rows[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
        totals_by_participant=dict(Counter(row.participant_type for row in rows)),
        totals_by_channel=dict(Counter(row.channel for row in rows)),
        unread_total=sum(row.unread_count for row in rows),
    )


async def _thread_summary(db: AsyncSession, user: User, thread_id: str) -> UnifiedCommunicationThread:
    row = next((thread for thread in await _all_threads(db, user) if thread.id == thread_id), None)
    parts = thread_id.split(":")
    if row is None and len(parts) == 3 and parts[0] == "intake" and parts[2] in _intake_allowed_channels(user):
        try:
            intake_id = UUID(parts[1])
        except ValueError:
            intake_id = None
        intake = next((item for item in await _visible_intakes(db, user) if item.id == intake_id), None)
        if intake is not None:
            channel = parts[2]
            label = {
                "underwriter_ai": "Underwriter AI",
                "client": "Client conversation",
                "partner": "Partner channel",
                "internal": "Internal notes",
            }[channel]
            row = UnifiedCommunicationThread(
                id=thread_id,
                title=intake.business_name or intake.full_name,
                participant_name=intake.full_name,
                participant_email=intake.email,
                participant_type="dealer_partner" if channel == "partner" else "internal" if channel == "internal" else "client",
                source_kind="intake",
                source_id=str(intake.id),
                source_ref=f"QC-I-{str(intake.id)[:8].upper()}",
                source_label=label,
                channel=channel,
                transport="portal",
                latest_at=intake.created_at,
                href=f"/admin/ai-underwriter-leads?lead={intake.id}&channel={channel}",
            )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return row


def _message_direction(user: User, sender_type: str) -> str:
    own = {str(user.role), getattr(user.role, "value", str(user.role)), "broker", "super_admin", "loan_exec"}
    if user.role == Role.CLIENT:
        own.add("client")
    return "outbound" if sender_type in own else "inbound"


@router.get("/threads/{thread_id:path}", response_model=UnifiedCommunicationThreadDetail)
async def get_unified_communication_thread(
    thread_id: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> UnifiedCommunicationThreadDetail:
    thread = await _thread_summary(db, user, thread_id)
    messages: list[UnifiedCommunicationMessage] = []
    parts = thread_id.split(":")
    if parts[0] == "loan":
        rows = list((await db.execute(select(Message).where(Message.loan_id == UUID(parts[1])).order_by(Message.sent_at.asc()))).scalars().all())
        messages = [
            UnifiedCommunicationMessage(
                id=str(row.id), thread_id=thread_id, body=row.body, sender_type=str(row.from_role),
                direction=_message_direction(user, str(row.from_role)), channel="client", transport="portal", created_at=row.sent_at,
            ) for row in rows
        ]
    elif parts[0] == "intake":
        intake = await db.get(PublicUnderwritingIntake, UUID(parts[1]))
        channel = parts[2]
        if channel in {"underwriter_ai", "client"}:
            audience = "admin" if channel == "underwriter_ai" else "uploader"
            stmt = select(BucketAIMessage).where(BucketAIMessage.bucket_id == intake.bucket_id, BucketAIMessage.audience == audience)
            if audience == "uploader":
                stmt = stmt.where(BucketAIMessage.upload_link_id == intake.bucket_upload_link_id)
            rows = list((await db.execute(stmt.order_by(BucketAIMessage.created_at.asc()))).scalars().all())
            messages = [
                UnifiedCommunicationMessage(
                    id=str(row.id), thread_id=thread_id, body=row.content, sender_name=row.author_name,
                    sender_type="ai" if row.role == "assistant" else "client" if channel == "client" and row.user_id is None else "operator",
                    direction="system" if row.role == "assistant" else _message_direction(user, "client" if channel == "client" and row.user_id is None else "super_admin"),
                    channel=channel, transport="portal", created_at=row.created_at,
                ) for row in rows
            ]
        else:
            note_channel = "internal" if channel == "internal" else "partner"
            rows = list((await db.execute(select(BucketNote).where(BucketNote.bucket_id == intake.bucket_id, BucketNote.visibility == "admin", BucketNote.channel == note_channel).order_by(BucketNote.created_at.asc()))).scalars().all())
            messages = [
                UnifiedCommunicationMessage(
                    id=str(row.id), thread_id=thread_id, body=row.content, sender_name=row.author_name,
                    sender_type=row.author_role, direction=_message_direction(user, row.author_role), channel=channel, transport="portal", created_at=row.created_at,
                ) for row in rows
            ]
    elif parts[0] == "dealer":
        dealer_id, channel = UUID(parts[1]), parts[2]
        stmt = select(DealerMessage).where(DealerMessage.dealer_id == dealer_id)
        stmt = stmt.where(DealerMessage.channel == "client") if channel == "client" else stmt.where(DealerMessage.channel != "client")
        rows = list((await db.execute(stmt.order_by(DealerMessage.created_at.asc()))).scalars().all())
        messages = [
            UnifiedCommunicationMessage(
                id=str(row.id), thread_id=thread_id, body=row.body, sender_name=row.author_name,
                sender_type="client" if row.author_user_id is None else "operator", direction=_message_direction(user, "client" if row.author_user_id is None else "super_admin"),
                channel=channel, transport="portal", created_at=row.created_at,
            ) for row in rows
        ]
    elif parts[0] == "rep":
        rows = list((await db.execute(select(DealerRepInboxMessage).where(DealerRepInboxMessage.thread_id == UUID(parts[1])).order_by(DealerRepInboxMessage.created_at.asc()))).scalars().all())
        messages = [
            UnifiedCommunicationMessage(
                id=str(row.id), thread_id=thread_id, body=row.body, sender_name=row.sender,
                sender_type="rep" if row.direction == "outbound" else "rep_lead", direction=row.direction,
                channel=row.channel, transport=row.channel, created_at=row.created_at, seen=row.read_at is not None,
                delivery_status=row.delivery_status,
            ) for row in rows
        ]
    elif parts[0] == "email":
        key = ":".join(parts[1:])
        from app.routers.inbox import _decrypt, _load_thread_rows

        rows = await _load_thread_rows(db, owner_id=user.id, thread_id=key)
        messages = [
            UnifiedCommunicationMessage(
                id=str(row.id), thread_id=thread_id, body=_decrypt(row) or row.snippet or "",
                sender_name=row.from_email, sender_type=row.matched_party_role or "email_contact",
                direction=row.direction, channel="email", transport="email", created_at=row.received_at or row.created_at,
                seen=row.is_read,
            ) for row in rows
        ]
    return UnifiedCommunicationThreadDetail(thread=thread, messages=messages)


@router.post("/threads/{thread_id:path}/messages", response_model=UnifiedCommunicationThreadDetail)
async def reply_unified_communication_thread(
    thread_id: str,
    payload: UnifiedCommunicationCompose,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UnifiedCommunicationThreadDetail:
    thread = await _thread_summary(db, user, thread_id)
    if not thread.can_reply:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This conversation is read-only from your desk")
    body = payload.body.strip()
    parts = thread_id.split(":")
    if parts[0] == "loan":
        role = MessageFrom.CLIENT if user.role == Role.CLIENT else MessageFrom.BROKER
        db.add(Message(loan_id=UUID(parts[1]), from_role=role, body=body))
        await db.commit()
    elif parts[0] == "intake":
        intake = await db.get(PublicUnderwritingIntake, UUID(parts[1]))
        channel = parts[2]
        if channel in {"underwriter_ai", "client"}:
            from app.services.bucket_ai import create_chat_reply

            bucket = await db.get(Bucket, intake.bucket_id)
            upload_link = (
                await db.get(BucketUploadLink, intake.bucket_upload_link_id)
                if channel == "client" and intake.bucket_upload_link_id
                else None
            )
            await create_chat_reply(
                db,
                bucket=bucket,
                audience="admin" if channel == "underwriter_ai" else "uploader",
                message=body,
                actor_name=(f"Underwriter - {user.name}" if channel == "client" else user.name or user.email),
                user=user,
                upload_link=upload_link,
                preferred_language=intake.preferred_language,
                intake_id=intake.id,
            )
        else:
            db.add(BucketNote(bucket_id=intake.bucket_id, author_name=user.name or user.email, author_role=str(user.role), visibility="admin", channel="internal" if channel == "internal" else "partner", content=body))
        intake.last_message_at = datetime.now(UTC)
        await db.commit()
    elif parts[0] == "dealer":
        dealer_id, channel = UUID(parts[1]), parts[2]
        db.add(DealerMessage(dealer_id=dealer_id, author_user_id=user.id, author_name=user.name, body=body, internal=channel != "client", channel="client" if channel == "client" else "desk"))
        await db.commit()
    elif parts[0] == "rep":
        from app.dealer_os.router import create_rep_inbox_message
        from app.dealer_os.schemas import RepInboxMessageCreate

        await create_rep_inbox_message(UUID(parts[1]), RepInboxMessageCreate(body=body), user, db)
    elif parts[0] == "email":
        from app.routers.inbox import reply_to_thread
        from app.schemas.inbox import InboxReplyRequest

        result = await reply_to_thread(":".join(parts[1:]), InboxReplyRequest(body=body), user, db)
        if not result.ok:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, result.detail or "Email could not be sent")
    return await get_unified_communication_thread(thread_id, user, db)


@router.post("/threads/{thread_id:path}/seen", response_model=UnifiedCommunicationSeen)
async def mark_unified_communication_seen(
    thread_id: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> UnifiedCommunicationSeen:
    await _thread_summary(db, user, thread_id)
    parts = thread_id.split(":")
    seen_at = datetime.now(UTC)
    if parts[0] == "rep":
        thread = await db.get(DealerRepInboxThread, UUID(parts[1]))
        if thread and thread.owner_user_id == user.id:
            rows = list((await db.execute(select(DealerRepInboxMessage).where(DealerRepInboxMessage.thread_id == thread.id, DealerRepInboxMessage.direction == "inbound", DealerRepInboxMessage.read_at.is_(None)))).scalars().all())
            for row in rows:
                row.read_at = seen_at
            thread.unread_count = 0
            await db.commit()
    elif parts[0] == "email":
        key = ":".join(parts[1:])
        from app.routers.inbox import _load_thread_rows

        rows = await _load_thread_rows(db, owner_id=user.id, thread_id=key)
        for row in rows:
            row.is_read = True
        await db.commit()
    return UnifiedCommunicationSeen(thread_id=thread_id, seen_at=seen_at)
