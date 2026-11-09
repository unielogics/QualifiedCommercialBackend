from __future__ import annotations

# FastAPI dependency declarations intentionally use Depends in defaults.
# ruff: noqa: B008
import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import false as sql_false
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, get_db
from app.dealer_os.models import (
    DealerBusiness,
    DealerMessage,
    DealerRepContact,
    DealerRepInboxMessage,
    DealerRepInboxThread,
)
from app.deps import CurrentUser, resolve_user_from_headers
from app.enums import MessageFrom, Role
from app.models.bucket import Bucket, BucketAIMessage, BucketNote, BucketUploadLink
from app.models.client import Client
from app.models.email_message import EmailMessage
from app.models.loan import Loan
from app.models.message import Message
from app.models.notification import Notification
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.sms_message import SmsMessage
from app.models.user import User
from app.schemas.communication import (
    CommunicationMessagesSeen,
    ComposeChannelResult,
    ComposeRecipient,
    FileCommunicationSeen,
    FileCommunicationUnread,
    FileCommunicationUnreadBatchRequest,
    FileCommunicationUnreadBatchResponse,
    UnifiedCommunicationCompose,
    UnifiedCommunicationMessage,
    UnifiedCommunicationSeen,
    UnifiedCommunicationThread,
    UnifiedCommunicationThreadDetail,
    UnifiedCommunicationThreadPage,
    UnifiedComposeRequest,
    UnifiedComposeResult,
    UnifiedContactGroup,
    UnifiedContactPage,
)
from app.scoping import scope_client_query, scope_loan_query
from app.services import communication_images, file_events, inline_images
from app.services.communication_events import HEARTBEAT_SECONDS, user_audience
from app.services.communication_events import broker as communication_event_broker
from app.services.dealer_partner_access import (
    DEALER_INTAKE_VARIANT,
    require_dealer_partner_standing,
)
from app.services.user_access import is_audit_client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/communications", tags=["communications"])
SCAN_LIMIT = 750


@router.get("/capabilities")
async def communication_capabilities(user: CurrentUser):
    from app.dealer_os.services import sms_provider

    mode = sms_provider.image_delivery_mode()
    if mode == "secure_link" and not communication_images.sms_share_available():
        mode = "unavailable"
    return {
        "image_upload": {"max_images": communication_images.MAX_IMAGES,
                         "max_image_bytes": communication_images.MAX_IMAGE_BYTES,
                         "max_total_bytes": communication_images.MAX_TOTAL_BYTES,
                         "allowed_mime_types": sorted(communication_images.ALLOWED_MIME)},
        "sms": {"image_delivery": mode, "max_total_bytes": 5 * 1024 * 1024 if mode == "mms" else communication_images.MAX_TOTAL_BYTES,
                "label": "Images are sent as picture messages (MMS)." if mode == "mms" else
                         "Images are sent as secure links that expire in 7 days." if mode == "secure_link" else
                         "Image delivery is unavailable for the selected SMS provider."},
    }


@router.get("/images/{share_id}", include_in_schema=False)
async def get_sms_image_share(share_id: UUID, db: AsyncSession = Depends(get_db)):
    """An unguessable, time-limited link, activated only by a successful send."""
    import urllib.parse

    from app.models.inline_image import InlineImage

    row = await db.get(InlineImage, share_id)
    if row is None or row.subject_kind != "sms_image_share" or row.status != "shared" or row.attached_at is None:
        raise HTTPException(404, "This image link is unavailable or has expired.", headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})
    attached = row.attached_at if row.attached_at.tzinfo else row.attached_at.replace(tzinfo=UTC)
    if attached < datetime.now(UTC) - timedelta(days=7):
        raise HTTPException(404, "This image link is unavailable or has expired.", headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})
    if row.subject_id:
        message = await db.get(SmsMessage, UUID(row.subject_id))
        if message is not None and (message.direction != "outbound" or message.status in {"failed", "blocked", "undelivered"}):
            raise HTTPException(404, "This image link is unavailable or has expired.", headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})
    try:
        data = await asyncio.to_thread(communication_images._read_bytes, row)
    except Exception as exc:
        raise HTTPException(404, "This image link is unavailable or has expired.", headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}) from exc
    return Response(content=data, media_type=row.mime_type, headers={
        "Content-Disposition": "inline; filename*=UTF-8''" + urllib.parse.quote(row.filename),
        "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "X-Robots-Tag": "noindex, nofollow",
    })


async def _outbound_images(db, image_ids, user):
    try:
        return await communication_images.load(db, image_ids, user.id)
    except inline_images.InlineImageError as exc:
        raise HTTPException(400, str(exc)) from exc


def _delivery_warning(detail: str | None) -> str | None:
    if detail == "sent_but_image_links_unavailable":
        return "The text was sent, but its image links are unavailable. Contact support before sending a replacement."
    if detail and detail.startswith("sent_not_recorded"):
        return "The message was sent, but its delivery history could not be saved. Do not send it again automatically."
    return None


@router.get("/events")
async def stream_communication_events(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_dev_user: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Stream scoped invalidation signals; message content stays in read APIs.

    Authentication runs on a session this route opens and closes itself — a
    request-scoped one would stay open for the life of the stream — so it goes
    through `resolve_user_from_headers`, never `CurrentUser`.
    """
    async with SessionLocal() as auth_db:
        try:
            user = await resolve_user_from_headers(
                request,
                authorization=authorization,
                x_dev_user=x_dev_user,
                db=auth_db,
            )
            user_id = user.id
            await auth_db.commit()
        except HTTPException as exc:
            # A refused credential is ordinary (expired token on reconnect):
            # one line, no traceback.
            await auth_db.rollback()
            log.warning("communication event stream refused: status=%s", exc.status_code)
            raise
        except Exception:
            # Anything else is a bug in the auth path. This used to be a bare
            # rollback-and-raise, which turned a TypeError into an opaque 500
            # with no log line on every connection for weeks.
            await auth_db.rollback()
            log.exception("communication event stream auth failed")
            raise

    async def stream():
        yield "retry: 3000\n\n"
        ready = {
            "id": f"ready:{user_id}",
            "type": "sync.required",
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        yield f"event: sync.required\ndata: {json.dumps(ready, separators=(',', ':'))}\n\n"
        async with communication_event_broker.subscribe([user_audience(user_id)]) as queue:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                event_type = str(event.get("type") or "sync.required")
                event_id = str(event.get("id") or "")
                payload = json.dumps(event, separators=(",", ":"))
                yield f"id: {event_id}\nevent: {event_type}\ndata: {payload}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sync-state")
async def communication_sync_state(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Cheap recovery cursor used only while the event stream is unavailable."""
    thread_scope = []
    if user.role == Role.FIELD_REP:
        thread_scope.append(DealerRepInboxThread.owner_user_id == user.id)
    elif user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        thread_scope.append(DealerRepInboxThread.owner_user_id == user.id)

    latest_thread = (
        await db.execute(select(func.max(DealerRepInboxThread.updated_at)).where(*thread_scope))
    ).scalar_one_or_none()
    latest_message = (
        await db.execute(
            select(func.max(DealerRepInboxMessage.updated_at))
            .join(DealerRepInboxThread, DealerRepInboxThread.id == DealerRepInboxMessage.thread_id)
            .where(*thread_scope)
        )
    ).scalar_one_or_none()
    latest_notification = (
        await db.execute(
            select(func.max(Notification.updated_at)).where(Notification.recipient_user_id == user.id)
        )
    ).scalar_one_or_none()
    unread_messages = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(DealerRepInboxThread.unread_count), 0)).where(*thread_scope)
            )
        ).scalar_one()
        or 0
    )
    unread_notifications = int(
        (
            await db.execute(
                select(func.count(Notification.id)).where(
                    Notification.recipient_user_id == user.id,
                    Notification.read_at.is_(None),
                )
            )
        ).scalar_one()
        or 0
    )
    latest = max(
        (value for value in (latest_thread, latest_message, latest_notification) if value is not None),
        default=None,
    )
    return {
        "revision": latest.isoformat() if latest else None,
        "unread_messages": unread_messages,
        "unread_notifications": unread_notifications,
        "stream_connected": communication_event_broker.connected,
        "server_time": datetime.now(UTC).isoformat(),
    }


@router.post("/intakes/unread-batch", response_model=FileCommunicationUnreadBatchResponse)
async def intake_communication_unread_batch(
    payload: FileCommunicationUnreadBatchRequest, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FileCommunicationUnreadBatchResponse:
    from app.services.file_communication_unread import unread_batch

    return FileCommunicationUnreadBatchResponse(items=await unread_batch(db, user=user, intake_ids=payload.intake_ids))


@router.get("/intakes/{intake_id}/unread", response_model=FileCommunicationUnread)
async def intake_communication_unread(intake_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> FileCommunicationUnread:
    from app.services.communication_read_state import file_unread, load_file_scope

    intake, profile = await load_file_scope(db, user, intake_id)
    return await file_unread(db, user=user, intake=intake, profile=profile)


@router.post("/intakes/{intake_id}/seen", response_model=FileCommunicationUnread)
async def intake_communication_seen(intake_id: UUID, payload: FileCommunicationSeen, user: CurrentUser,
    db: AsyncSession = Depends(get_db)) -> FileCommunicationUnread:
    from app.services.communication_read_state import file_seen, load_file_scope

    intake, profile = await load_file_scope(db, user, intake_id)
    result = await file_seen(db, user=user, intake=intake, profile=profile, payload=payload)
    await db.commit()
    return result


def _at(value: datetime | None) -> datetime:
    return value or datetime.min.replace(tzinfo=UTC)


def _phone_key(value: str | None) -> str | None:
    """A phone reduced to what makes two numbers the SAME person.

    "+1 (862) 384-1951", "8623841951" and "18623841951" are one human, and
    keying identity on the raw string split them into separate contacts. Last
    ten digits for NANP; the full digit string otherwise, so international
    numbers are not collapsed by their tail.
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return None
    # NANP only: 11 digits beginning with the country code collapse to 10.
    # Anything else keeps every digit — truncating an international number to
    # its last ten could alias it onto an unrelated US number.
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


async def _client_for_phone_scoped(db: AsyncSession, user: User, phone: str):
    """A unique global identity, then the viewer's book; never newest wins."""
    from app.services.communication_scope import canonical_phone, phone_identity_sql

    number = canonical_phone(phone)
    if not number:
        return None
    matches = (await db.execute(select(Client).where(phone_identity_sql(Client.phone) == number))).scalars().all()
    if len({row.id for row in matches}) != 1:
        return None
    client = matches[0]
    visible = (await db.execute(scope_client_query(user, select(Client.id)).where(Client.id == client.id))).scalar_one_or_none()
    return client if visible is not None else None


async def _rep_contact_for_phone(db: AsyncSession, user: User, phone: str):
    """A rep contact on this number, scoped to the rep who owns it."""
    from app.services.communication_scope import canonical_phone, phone_identity_sql

    number = canonical_phone(phone)
    if not number or user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP):
        return None
    matches = (await db.execute(select(DealerRepContact).where(phone_identity_sql(DealerRepContact.phone_e164) == number))).scalars().all()
    if len({row.id for row in matches}) != 1:
        return None
    contact = matches[0]
    return contact if user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC) or contact.owner_user_id == user.id else None


def _same_number(phone: str):
    """SQL predicate matching every stored spelling of one number.

    Rows are normalized on write, but a thread is addressed by whichever
    spelling the newest message used, and an exact string match would drop
    every message stored in another format.
    """
    from app.services.communication_scope import canonical_phone, phone_identity_sql

    number = canonical_phone(phone)
    return phone_identity_sql(SmsMessage.phone_e164) == number if number else sql_false()


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
        await require_dealer_partner_standing(db, user)
        stmt = stmt.where(
            PublicUnderwritingIntake.broker_id == user.id,
            PublicUnderwritingIntake.variant == DEALER_INTAKE_VARIANT,
        )
    elif user.role in (Role.CLIENT, Role.BROKER, Role.REGIONAL_MANAGER):
        client_ids = scope_client_query(user, select(Client.id))
        stmt = stmt.where(PublicUnderwritingIntake.client_id.in_(client_ids))
    else:
        return []
    return list((await db.execute(stmt)).scalars().all())


async def _visible_dealers(db: AsyncSession, user: User) -> list[DealerBusiness]:
    stmt = select(DealerBusiness).where(DealerBusiness.is_training.is_(False)).limit(SCAN_LIMIT)
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
    if not rows:
        return []

    # Previews and counts. Without these every rep row in the contact accordion
    # renders blank, which is what a rep sees most of — their book is rep leads.
    thread_ids = [thread.id for thread, _ in rows]
    counts: dict[UUID, int] = {}
    latest_body: dict[UUID, str] = {}
    for tid, n in (
        await db.execute(
            select(DealerRepInboxMessage.thread_id, func.count())
            .where(DealerRepInboxMessage.thread_id.in_(thread_ids))
            .group_by(DealerRepInboxMessage.thread_id)
        )
    ).all():
        counts[tid] = n
    # One row per thread: the newest message, via a window rather than a query
    # per thread — a rep with a full book would otherwise fan out badly.
    ranked = (
        select(
            DealerRepInboxMessage.thread_id,
            DealerRepInboxMessage.body,
            func.row_number()
            .over(
                partition_by=DealerRepInboxMessage.thread_id,
                order_by=DealerRepInboxMessage.created_at.desc(),
            )
            .label("rn"),
        )
        .where(DealerRepInboxMessage.thread_id.in_(thread_ids))
        .subquery()
    )
    for tid, body in (
        await db.execute(select(ranked.c.thread_id, ranked.c.body).where(ranked.c.rn == 1))
    ).all():
        latest_body[tid] = body

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
            message_count=counts.get(thread.id, 0),
            latest_snippet=_snippet(latest_body.get(thread.id)),
            participant_phone=contact.phone_e164 if contact else None,
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


async def _sms_threads(db: AsyncSession, user: User) -> list[UnifiedCommunicationThread]:
    """The sms_messages ledger as an inbox source — one thread per person.

    Grouped by the number. Visibility follows the book the role actually has:
    operators see everything, brokers see their clients, and a field rep sees
    the numbers belonging to rep contacts they own — which is the only book a
    rep has, since rep leads are not clients. Unattributed numbers stay
    admin-only either way.
    """
    from app.services.communication_read_state import sms_unread_by_phone
    from app.services.communication_scope import (
        SMS_ROLES,
        canonical_phone,
        phone_identity_sql,
        scoped_sms_query,
    )

    if user.role not in SMS_ROLES:
        return []
    unread_by_phone = await sms_unread_by_phone(db, user.id)
    stmt = (
        (await scoped_sms_query(db, user)).add_columns(Client)
        .outerjoin(Client, Client.id == SmsMessage.client_id)
        .order_by(SmsMessage.created_at.desc(), SmsMessage.id.desc())
        .limit(SCAN_LIMIT)
    )
    rows = list((await db.execute(stmt)).all())
    rows.sort(key=lambda pair: (_at(pair[0].created_at), str(pair[0].id)))
    numbers = {number for message, _ in rows if (number := canonical_phone(message.phone_e164))}
    global_clients = (await db.execute(select(Client).where(phone_identity_sql(Client.phone).in_(numbers)))).scalars().all() if numbers else []
    clients_by_number: dict[str, set[UUID]] = {}
    for candidate in global_clients:
        if number := canonical_phone(candidate.phone):
            clients_by_number.setdefault(number, set()).add(candidate.id)

    # Rep leads are not clients, so a number that belongs to one would render as
    # a bare phone number. Resolve those names too, scoped to what the viewer
    # may see, keyed on normalized digits like everything else.
    contact_stmt = select(DealerRepContact).where(DealerRepContact.phone_e164.is_not(None))
    if user.role == Role.FIELD_REP:
        contact_stmt = contact_stmt.where(DealerRepContact.owner_user_id == user.id)
    elif user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        contact_stmt = contact_stmt.where(sql_false())
    rep_candidates: dict[str, list[DealerRepContact]] = {}
    for contact in (await db.execute(contact_stmt.limit(SCAN_LIMIT))).scalars():
        key = canonical_phone(contact.phone_e164)
        if key:
            rep_candidates.setdefault(key, []).append(contact)
    rep_by_key = {key: values[0] for key, values in rep_candidates.items() if len({row.id for row in values}) == 1}

    # Keyed on the NUMBER, not the client. Texting is a conversation with a
    # phone: an outbound send with no client link and the inbound reply that
    # matched a client are the same thread to everyone except the database.
    grouped: dict[str, list[tuple[SmsMessage, Client | None]]] = {}
    for message, client in rows:
        grouped.setdefault(canonical_phone(message.phone_e164) or message.phone_e164, []).append(
            (message, client)
        )
    result = []
    for _key, members in grouped.items():
        latest, _ = members[-1]
        # Address the thread by a real number, not the identity key.
        key = canonical_phone(latest.phone_e164) or latest.phone_e164
        # Any message on this number that resolved to a client names the thread;
        # failing that, a rep contact on the same number does.
        known_clients = {client.id: client for _, client in members if client is not None}
        client = next(iter(known_clients.values())) if len(known_clients) == 1 and set(known_clients) == clients_by_number.get(key, set()) else None
        rep_contact = None if client is not None else rep_by_key.get(_key)
        # A blocked or failed send is worth surfacing in the preview — "why
        # didn't the text go out" should be visible from the inbox row.
        snippet = latest.body or latest.detail or latest.status
        result.append(
            UnifiedCommunicationThread(
                id=f"sms:phone:{key}",
                title=(
                    client.name
                    if client is not None
                    else rep_contact.full_name
                    if rep_contact is not None
                    else latest.phone_e164
                ),
                participant_name=(
                    client.name
                    if client is not None
                    else rep_contact.full_name
                    if rep_contact is not None
                    else None
                ),
                participant_email=(
                    client.email
                    if client is not None
                    else rep_contact.email
                    if rep_contact is not None
                    else None
                ),
                participant_phone=key,
                participant_type="client" if client is not None else "rep_lead" if rep_contact is not None else "client",
                source_kind="sms",
                source_id=(
                    str(client.id)
                    if client is not None
                    else str(rep_contact.id)
                    if rep_contact is not None
                    else latest.phone_e164
                ),
                source_ref=None,
                source_label=(
                    rep_contact.company if rep_contact is not None and rep_contact.company else "Text messages"
                ),
                channel="sms",
                transport="sms",
                unread_count=unread_by_phone.get(key, 0),
                message_count=len(members),
                latest_snippet=_snippet(snippet),
                latest_at=latest.created_at,
                href=(f"/clients/{client.id}" if client is not None else "/messages"),
            )
        )
    return result


async def _all_threads(
    db: AsyncSession, user: User, *, include_intake: bool = False
) -> list[UnifiedCommunicationThread]:
    """Every human conversation, one list.

    AI intake threads — the client's conversation with the underwriter AI and
    the AI's own channel — are deliberately NOT part of the inbox: they are a
    workflow surface with their own screen (/admin/ai-underwriter-leads), and
    mixing machine dialogue into the message inbox buried the messages that
    need a human. They are still resolvable by direct thread id (deep links
    from the AI-leads screen), which is what `include_intake` is for.
    """
    parts = [
        await _loan_threads(db, user),
        await _dealer_threads(db, user),
        await _rep_threads(db, user),
        await _email_threads(db, user),
        await _sms_threads(db, user),
    ]
    if include_intake:
        parts.append(await _intake_threads(db, user))
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
    row = next(
        (thread for thread in await _all_threads(db, user, include_intake=True) if thread.id == thread_id),
        None,
    )
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
    if row is None and len(parts) == 2 and parts[0] == "loan":
        try:
            loan_id = UUID(parts[1])
        except ValueError:
            loan_id = None
        loan = (await db.execute(scope_loan_query(user, select(Loan).where(Loan.id == loan_id)))).scalar_one_or_none() if loan_id else None
        if loan is not None:
            row = UnifiedCommunicationThread(id=thread_id, title=loan.deal_id or "Loan conversation", participant_type="client",
                source_kind="loan", source_id=str(loan.id), channel="client", transport="portal",
                latest_at=loan.updated_at or loan.created_at, href=f"/messages?thread={thread_id}")
    if row is None and len(parts) == 2 and parts[0] == "rep":
        try:
            thread = await db.get(DealerRepInboxThread, UUID(parts[1]))
        except ValueError:
            thread = None
        if thread is not None and (user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC} or user.role == Role.FIELD_REP and thread.owner_user_id == user.id):
            row = UnifiedCommunicationThread(id=thread_id, title=thread.subject, participant_type="rep_lead",
                source_kind="rep", source_id=str(thread.dealer_id or thread.contact_id or thread.id),
                channel=thread.channel, transport=thread.channel, latest_at=thread.last_message_at or thread.created_at,
                href=f"/messages?thread={thread_id}", can_reply=thread.owner_user_id == user.id)
    if row is None and len(parts) == 2 and parts[0] == "email":
        from app.routers.inbox import _load_thread_rows

        owned = await _load_thread_rows(db, owner_id=user.id, thread_id=parts[1])
        if owned:
            latest = owned[-1]
            row = UnifiedCommunicationThread(id=thread_id, title=latest.subject or "Email conversation",
                participant_type=latest.matched_party_role or "email_contact", participant_email=latest.from_email,
                source_kind="email", source_id=parts[1], channel="email", transport="email",
                latest_at=latest.received_at or latest.created_at, href=f"/messages?thread={thread_id}")
    if row is None and len(parts) == 3 and parts[0] == "sms" and parts[1] == "phone":
        # A conversation with a person exists whether or not it has history —
        # the first text to an appointment's client has to start somewhere, and
        # _all_threads only lists numbers the ledger has already seen.
        from app.services.communication_scope import (
            GLOBAL_SMS_ROLES,
            SMS_ROLES,
            canonical_phone,
            scoped_sms_query,
        )

        phone = canonical_phone(parts[2])
        if phone and user.role in SMS_ROLES:
            name = email = None
            client = await _client_for_phone_scoped(db, user, phone)
            contact = None
            if client is not None:
                name, email = client.name, client.email
            else:
                contact = await _rep_contact_for_phone(db, user, phone)
                if contact is not None:
                    name, email = contact.full_name, contact.email
            existing = (await db.execute((await scoped_sms_query(db, user, phone)).limit(1))).scalars().first()
            if user.role not in GLOBAL_SMS_ROLES and client is None and contact is None and existing is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
            row = UnifiedCommunicationThread(
                id=f"sms:phone:{phone}",
                title=name or phone,
                participant_name=name,
                participant_email=email,
                participant_phone=phone,
                participant_type="client",
                source_kind="sms",
                source_id=phone,
                source_label="Text messages",
                channel="sms",
                transport="sms",
                message_count=0,
                latest_at=datetime.now(UTC),
                href="/inbox",
            )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return row


def _message_direction(user: User, sender_type: str) -> str:
    own = {str(user.role), getattr(user.role, "value", str(user.role)), "broker", "super_admin", "loan_exec"}
    if user.role == Role.CLIENT:
        own.add("client")
    return "outbound" if sender_type in own else "inbound"


@router.get("/contacts", response_model=UnifiedContactPage)
async def list_communication_contacts(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, max_length=160),
    channel: str | None = None,
    unread_only: bool = False,
    limit: int = Query(80, ge=1, le=200),
) -> UnifiedContactPage:
    """The inbox grouped by PERSON rather than by thread.

    One row per contact, newest conversation first; expanding a contact shows
    every thread they appear in — portal chat, email, SMS — so an operator can
    refresh their memory of the whole relationship in one place. Identity is
    email where known, else phone, else the source record, and a client-matched
    SMS thread carries the client's email so the same person's channels merge.
    """
    threads = await _all_threads(db, user)

    # Identity by union rather than a single key. A person's outbound texts may
    # carry only a phone while their inbound reply matched a client record and
    # carries an email too — keyed on one field each, the same human split into
    # two contacts. Threads are merged when they share ANY identifier.
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def keys_of(row: UnifiedCommunicationThread) -> list[str]:
        out = []
        if row.participant_email:
            out.append(f"em:{row.participant_email.strip().lower()}")
        phone_key = _phone_key(row.participant_phone)
        if phone_key:
            out.append(f"ph:{phone_key}")
        if not out:
            out.append(f"src:{row.source_kind}:{row.source_id}")
        return out

    thread_keys: list[tuple[UnifiedCommunicationThread, str]] = []
    for row in threads:
        ks = keys_of(row)
        for k in ks[1:]:
            union(ks[0], k)
        thread_keys.append((row, ks[0]))

    grouped: dict[str, list[UnifiedCommunicationThread]] = {}
    for row, k in thread_keys:
        grouped.setdefault(find(k), []).append(row)

    contacts: list[UnifiedContactGroup] = []
    for key, rows in grouped.items():
        rows.sort(key=lambda row: row.latest_at, reverse=True)
        latest = rows[0]
        name = next((r.participant_name for r in rows if r.participant_name), None) or latest.title
        contacts.append(
            UnifiedContactGroup(
                key=key,
                name=name,
                email=next((r.participant_email for r in rows if r.participant_email), None),
                phone=next((r.participant_phone for r in rows if r.participant_phone), None),
                channels=sorted({r.channel for r in rows}),
                sources=sorted({r.source_kind for r in rows}),
                unread_total=sum(r.unread_count for r in rows),
                message_total=sum(r.message_count for r in rows),
                latest_thread_id=latest.id,
                latest_snippet=latest.latest_snippet,
                latest_channel=latest.channel,
                latest_at=latest.latest_at,
                threads=rows,
            )
        )

    term = (q or "").strip().lower()
    if term:
        contacts = [
            c for c in contacts
            if term in " ".join(filter(None, (c.name, c.email, c.phone, c.latest_snippet, *(r.title for r in c.threads)))).lower()
        ]
    if channel:
        contacts = [c for c in contacts if channel in c.channels]
    if unread_only:
        contacts = [c for c in contacts if c.unread_total > 0]
    contacts.sort(key=lambda c: c.latest_at, reverse=True)
    return UnifiedContactPage(
        items=contacts[:limit],
        total=len(contacts),
        unread_total=sum(c.unread_total for c in contacts),
    )


#: Roles with no one to start a conversation with from this inbox. FIELD_REP is
#: deliberately absent: a rep composes to the contacts they own, resolved below.
_OPERATOR_DENY = (Role.CLIENT, Role.REGIONAL_MANAGER, Role.DEALER, Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER)


async def _own_rep_contacts(db: AsyncSession, user: User) -> list[DealerRepContact]:
    """Rep contacts this user may message. Empty for roles with no rep book."""
    stmt = select(DealerRepContact).order_by(
        DealerRepContact.last_activity_at.desc().nullslast(),
        DealerRepContact.updated_at.desc(),
    ).where(DealerRepContact.archived_at.is_(None))
    if user.role == Role.FIELD_REP:
        stmt = stmt.where(DealerRepContact.owner_user_id == user.id)
    elif user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        return []
    return list((await db.execute(stmt.limit(SCAN_LIMIT))).scalars().all())


@router.get("/recipients", response_model=list[ComposeRecipient])
async def search_compose_recipients(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, max_length=120),
    limit: int = Query(15, ge=1, le=40),
) -> list[ComposeRecipient]:
    """Who a new message can go to: clients, AI-intake leads, audit files.

    One search across all three, each row carrying what the composer needs to
    decide which channels are even possible — no phone, no SMS checkbox.
    """
    if user.role in _OPERATOR_DENY:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator-only action")
    term = (q or "").strip().lower()

    def matches(*fields: str | None) -> bool:
        return not term or term in " ".join(f for f in fields if f).lower()

    out: list[ComposeRecipient] = []

    for contact in await _own_rep_contacts(db, user):
        if matches(contact.full_name, contact.company, contact.email, contact.phone_e164):
            out.append(ComposeRecipient(
                kind="rep_contact", id=str(contact.id), name=contact.full_name,
                label=contact.company or "Field contact",
                email=contact.email, phone=contact.phone_e164,
            ))

    if user.role == Role.FIELD_REP:
        # A rep's book is their contacts. Clients, intake leads and audit files
        # belong to the funding desk and are not theirs to open a thread with.
        return out[:limit]

    client_stmt = scope_client_query(user, select(Client)).order_by(Client.created_at.desc()).limit(SCAN_LIMIT)
    for client in (await db.execute(client_stmt)).scalars():
        if matches(client.name, client.email, client.phone):
            out.append(ComposeRecipient(kind="client", id=str(client.id), name=client.name, label="Client", email=client.email, phone=client.phone))

    for intake in await _visible_intakes(db, user):
        if matches(intake.full_name, intake.business_name, intake.email, intake.phone):
            out.append(ComposeRecipient(kind="intake", id=str(intake.id), name=intake.full_name, label=f"AI intake · {intake.business_name or 'lead'}", email=intake.email, phone=intake.phone))

    for dealer in await _visible_dealers(db, user):
        if matches(dealer.name, dealer.legal_name, dealer.email, dealer.phone):
            out.append(ComposeRecipient(kind="dealer", id=str(dealer.id), name=dealer.legal_name or dealer.name, label="Audit file", email=dealer.email, phone=dealer.phone))

    return out[:limit]


@router.post("/compose", response_model=UnifiedComposeResult)
async def compose_new_message(
    payload: UnifiedComposeRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UnifiedComposeResult:
    """Start a conversation from the inbox — SMS, email, or both.

    Each channel reports its own outcome; "the text failed but the email went"
    is a different situation from "nothing went", and the composer shows both
    honestly. SMS rides the guarded path (suppression list, ledger row either
    way); email goes out as the operator (connected Gmail, SES fallback).
    """
    if user.role in _OPERATOR_DENY:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator-only action")

    name = email = phone = None
    client_id = None
    thread_id: str | None = None
    if payload.recipient_kind == "client":
        client = (
            await db.execute(scope_client_query(user, select(Client)).where(Client.id == UUID(payload.recipient_id)))
        ).scalar_one_or_none()
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
        name, email, phone, client_id = client.name, client.email, client.phone, client.id
    elif payload.recipient_kind == "intake":
        intake = next((i for i in await _visible_intakes(db, user) if str(i.id) == payload.recipient_id), None)
        if intake is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Intake lead not found")
        name, email, phone = intake.full_name, intake.email, intake.phone
    elif payload.recipient_kind == "rep_contact":
        contact = next(
            (c for c in await _own_rep_contacts(db, user) if str(c.id) == payload.recipient_id), None
        )
        if contact is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
        name, email, phone = contact.full_name, contact.email, contact.phone_e164
    else:
        dealer = next((d for d in await _visible_dealers(db, user) if str(d.id) == payload.recipient_id), None)
        if dealer is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit file not found")
        name, email, phone = dealer.legal_name or dealer.name, dealer.email, dealer.phone

    results: list[ComposeChannelResult] = []
    body = payload.body.strip()
    images = await _outbound_images(db, payload.image_ids, user)

    if "sms" in payload.channels:
        if not phone:
            results.append(ComposeChannelResult(channel="sms", ok=False, detail=f"{name} has no phone number on file."))
        else:
            from app.services import sms as sms_service

            sms_result = await sms_service.send_sms_checked(
                db, to_phone=phone, body=body, client_id=client_id, context="manual",
                **({"images": images} if images else {}),
            )
            results.append(ComposeChannelResult(channel="sms", ok=sms_result.ok, detail=sms_result.detail))
            if sms_result.ok:
                from app.dealer_os.services.consent_delivery import normalize_phone

                normalized = normalize_phone(phone)
                if normalized:
                    thread_id = f"sms:phone:{normalized}"

    if "email" in payload.channels:
        if not email:
            results.append(ComposeChannelResult(channel="email", ok=False, detail=f"{name} has no email on file."))
        else:
            from app.services.email.user_mailer import send_as_user

            mail = await send_as_user(
                db, user.id,
                to_emails=[email],
                subject=(payload.subject or "").strip() or f"Message from {user.name or 'Qualified Commercial'}",
                body_text=body,
                **({"attachments": communication_images.attachment_bytes(images)} if images else {}),
            )
            results.append(ComposeChannelResult(channel="email", ok=mail.ok, detail=mail.detail or ""))
            if mail.ok and images:
                from app.routers.inbox import _record_outbound_message

                try:
                    async with db.begin_nested():
                        message = await _record_outbound_message(
                            db, user=user, result=mail, body=body, to_emails=[email],
                            subject=(payload.subject or "").strip() or f"Message from {user.name or 'Qualified Commercial'}",
                            images=images, client_id=client_id,
                        )
                        if thread_id is None:
                            thread_id = f"email:{message.gmail_thread_id or message.id}"
                except Exception:
                    log.exception("Email sent but attachment history could not be stored")
                    results[-1].detail = "sent_not_recorded"

    warning = next((_delivery_warning(r.detail) for r in results if r.ok and _delivery_warning(r.detail)), None)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        if not any(r.ok for r in results):
            raise
        log.exception("A composed message was sent but its delivery history could not be committed")
        warning = _delivery_warning("sent_not_recorded")
        thread_id = None
    return UnifiedComposeResult(ok=any(r.ok for r in results), results=results, thread_id=thread_id, delivery_warning=warning)


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
                    sender_type=_bucket_sender_type(row, channel),
                    direction="system" if row.role == "assistant" else _message_direction(user, "client" if _bucket_sender_type(row, channel) == "client" else "super_admin"),
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
        rep_images = await inline_images.hydrate(db, "rep_inbox_message", [str(row.id) for row in rows])
        messages = [
            UnifiedCommunicationMessage(
                id=str(row.id), thread_id=thread_id, body=row.body, sender_name=row.sender,
                sender_type="rep" if row.direction == "outbound" else "rep_lead", direction=row.direction,
                channel=row.channel, transport=row.channel, created_at=row.created_at, seen=row.read_at is not None,
                delivery_status=row.delivery_status,
                images=rep_images.get(str(row.id), []),
            ) for row in rows
        ]
    elif parts[0] == "sms":
        # Every message on this number, client-linked or not, in whatever
        # format it was stored — see _sms_threads.
        from app.services.communication_scope import scoped_sms_query

        stmt = await scoped_sms_query(db, user, thread.participant_phone or parts[2])
        rows = list((await db.execute(stmt.order_by(SmsMessage.created_at.asc()))).scalars().all())
        # A picture the client texted us, filed against the ledger row. The
        # thread was already authorised above, so these ride the same decision.
        sms_images = await inline_images.hydrate(db, "sms_message", [str(row.id) for row in rows])
        messages = [
            UnifiedCommunicationMessage(
                id=str(row.id), thread_id=thread_id,
                # A send that never left has no body worth faking — show why.
                body=row.body or row.detail or row.status,
                sender_name=None if row.direction == "inbound" else "Qualified Commercial",
                sender_type="client" if row.direction == "inbound" else "operator",
                direction="inbound" if row.direction == "inbound" else "outbound",
                channel="sms", transport=f"sms:{row.provider}" if row.provider else "sms",
                created_at=row.created_at,
                delivery_status=row.status if row.direction == "outbound" else None,
                images=sms_images.get(str(row.id), []),
            ) for row in rows
        ]
    elif parts[0] == "email":
        key = ":".join(parts[1:])
        from app.routers.inbox import _decrypt, _load_thread_rows

        rows = await _load_thread_rows(db, owner_id=user.id, thread_id=key)
        email_images = await inline_images.hydrate(db, "email_message", [str(row.id) for row in rows])
        messages = [
            UnifiedCommunicationMessage(
                id=str(row.id), thread_id=thread_id, body=_decrypt(row) or row.snippet or "",
                sender_name=row.from_email, sender_type=row.matched_party_role or "email_contact",
                direction=row.direction, channel="email", transport="email", created_at=row.received_at or row.created_at,
                seen=row.is_read,
                images=email_images.get(str(row.id), []),
            ) for row in rows
        ]
    return UnifiedCommunicationThreadDetail(thread=thread, messages=messages)


def _bucket_sender_type(row: BucketAIMessage, channel: str) -> str:
    if row.role == "assistant":
        return "ai"
    if row.sender_kind == "operator":
        return "operator"
    if row.sender_kind == "client":
        return "client"
    return "client" if channel == "client" and row.user_id is None else "operator"


def _loan_reply_event_title(user: User) -> str:
    """What the timeline says about a reply on the loan thread — who wrote,
    never what they wrote."""
    if user.role == Role.CLIENT:
        return f"Message from {user.name or 'your client'}"
    if user.role in (Role.BROKER, Role.REGIONAL_MANAGER, Role.FIELD_REP):
        return "Reply from your agent"
    return "Reply from the desk"


@router.post("/threads/{thread_id:path}/messages", response_model=UnifiedCommunicationThreadDetail)
async def reply_unified_communication_thread(
    thread_id: str,
    payload: UnifiedCommunicationCompose,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UnifiedCommunicationThreadDetail:
    thread = await _thread_summary(db, user, thread_id)
    if not thread.can_reply:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This conversation is read-only from your desk")
    body = payload.body.strip()
    parts = thread_id.split(":")
    delivered = False
    warning = None
    images = await _outbound_images(db, payload.image_ids, user) if parts[0] != "email" else []
    if images and parts[0] not in {"sms", "email", "rep"}:
        raise HTTPException(400, "Use the SMS or email conversation to send image attachments.")
    if parts[0] == "loan":
        role = MessageFrom.CLIENT if user.role == Role.CLIENT else MessageFrom.BROKER
        db.add(Message(loan_id=UUID(parts[1]), from_role=role, body=body))
        # File timeline: one line for the reply, never its body.
        await file_events.emit(
            db,
            loan_id=UUID(parts[1]),
            kind="message.sent",
            visibility=file_events.VISIBILITY_CLIENT,
            title=_loan_reply_event_title(user),
            actor=user,
            target_type="communication_thread",
            target_id=thread_id,
        )
        await db.commit()
    elif parts[0] == "intake":
        intake = await db.get(PublicUnderwritingIntake, UUID(parts[1]))
        channel = parts[2]
        if channel in {"underwriter_ai", "client"}:
            from app.services.ai import engagement
            from app.services.bucket_ai import create_chat_reply, create_human_message

            bucket = await db.get(Bucket, intake.bucket_id)
            upload_link = (
                await db.get(BucketUploadLink, intake.bucket_upload_link_id)
                if channel == "client" and intake.bucket_upload_link_id
                else None
            )
            if channel == "client":
                # The borrower is being answered by a person. The AI neither replies
                # to this message nor to the borrower's next one for the takeover
                # window — the same rule as the cockpit's client conversation.
                await create_human_message(
                    db,
                    bucket=bucket,
                    audience="uploader",
                    message=body,
                    actor_name=f"Underwriter — {user.name}" if user.name else "Underwriter",
                    user=user,
                    upload_link=upload_link,
                )
                if upload_link is not None:
                    engagement.pause(upload_link)
            else:
                await create_chat_reply(
                    db,
                    bucket=bucket,
                    audience="admin",
                    message=body,
                    actor_name=user.name or user.email,
                    user=user,
                    upload_link=upload_link,
                    preferred_language=intake.preferred_language,
                    intake_id=intake.id,
                )
        else:
            db.add(BucketNote(bucket_id=intake.bucket_id, author_name=user.name or user.email, author_role=str(user.role), visibility="admin", channel="internal" if channel == "internal" else "partner", content=body))
            # File timeline: a note on the partner channel is the team's; an
            # internal note stays with the desk.
            await file_events.emit(
                db,
                intake_id=intake.id,
                kind="message.sent",
                visibility=file_events.VISIBILITY_DESK if channel == "internal" else file_events.VISIBILITY_TEAM,
                title="Internal note" if channel == "internal" else "Note to the partner channel",
                actor=user,
                target_type="communication_thread",
                target_id=thread_id,
            )
        intake.last_message_at = datetime.now(UTC)
        await db.commit()
    elif parts[0] == "dealer":
        dealer_id, channel = UUID(parts[1]), parts[2]
        db.add(DealerMessage(dealer_id=dealer_id, author_user_id=user.id, author_name=user.name, body=body, internal=channel != "client", channel="client" if channel == "client" else "desk"))
        # File timeline: the client channel at the client tier, the desk
        # channel at the team tier — the same words the file's own message
        # route uses for this thread.
        author = user.name or user.email or "someone"
        await file_events.emit(
            db,
            dealer_id=dealer_id,
            kind="message.sent",
            visibility=file_events.VISIBILITY_CLIENT if channel == "client" else file_events.VISIBILITY_TEAM,
            title=(
                f"Message from {author}"
                if is_audit_client(user)
                else f"Reply to the client from {author}"
                if channel == "client"
                else f"Desk message from {author}"
            ),
            actor=user,
            target_type="communication_thread",
            target_id=thread_id,
        )
        await db.commit()
    elif parts[0] == "rep":
        from app.dealer_os.router import create_rep_inbox_message
        from app.dealer_os.schemas import RepInboxMessageCreate

        # request is threaded through because create_rep_inbox_message uses it
        # for the training-dealer live-send guard, which must not be bypassed.
        sent_message = await create_rep_inbox_message(
            thread_id=UUID(parts[1]),
            payload=RepInboxMessageCreate(body=body, image_ids=payload.image_ids),
            request=request,
            user=user,
            db=db,
        )
        if sent_message.delivery_status == "failed":
            raise HTTPException(502, sent_message.provider_error or "The message could not be sent")
        delivered = True
        warning = _delivery_warning(sent_message.provider_error)
    elif parts[0] == "sms":
        from app.services import sms as sms_service
        from app.services.communication_scope import GLOBAL_SMS_ROLES

        phone = parts[2]
        # Keep the reply attached to whoever this number already resolved to,
        # so the thread does not fork the moment we answer it.
        client = await _client_for_phone_scoped(db, user, phone)
        contact = await _rep_contact_for_phone(db, user, phone) if client is None else None
        if user.role not in GLOBAL_SMS_ROLES and client is None and contact is None:
            raise HTTPException(409, "This phone has no unambiguous contact in your book. Reply from the explicitly linked client file instead")
        linked = client.id if client else None
        result = await sms_service.send_sms_checked(
            db, to_phone=phone, body=body, client_id=linked, context="manual",
            **({"images": images} if images else {}),
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            if not result.ok:
                raise
            warning = _delivery_warning("sent_not_recorded")
        delivered = result.ok
        warning = warning or _delivery_warning(result.detail)
        if not result.ok:
            # The blocked/failed row is already in the ledger; the composer
            # still deserves the honest reason instead of a silent refresh.
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, result.detail or "Text could not be sent")
    elif parts[0] == "email":
        from app.routers.inbox import reply_to_thread
        from app.schemas.inbox import InboxReplyRequest

        result = await reply_to_thread(
            thread_id=":".join(parts[1:]),
            payload=InboxReplyRequest(body=body, image_ids=payload.image_ids),
            user=user,
            db=db,
        )
        if not result.ok:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, result.detail or "Email could not be sent")
        delivered = True
        warning = _delivery_warning(result.detail)
    try:
        detail = await get_unified_communication_thread(thread_id=thread_id, user=user, db=db)
    except Exception:
        if not delivered:
            raise
        return UnifiedCommunicationThreadDetail(thread=thread, messages=[], delivery_warning=warning or _delivery_warning("sent_not_recorded"))
    if warning:
        detail.delivery_warning = warning
    return detail


@router.post("/threads/{thread_id:path}/seen", response_model=UnifiedCommunicationSeen)
async def mark_unified_communication_seen(
    thread_id: str, user: CurrentUser, db: AsyncSession = Depends(get_db), payload: CommunicationMessagesSeen | None = None,
) -> UnifiedCommunicationSeen:
    await _thread_summary(db, user, thread_id)
    parts = thread_id.split(":")
    seen_at = datetime.now(UTC)
    requested = set(payload.message_ids) if payload is not None else None
    from app.services.communication_read_state import (
        mark_rep_messages_seen,
        read_received_notifications,
        sms_source_ids,
    )

    if parts[0] == "rep":
        thread = await db.get(DealerRepInboxThread, UUID(parts[1]))
        statement = select(DealerRepInboxMessage).where(DealerRepInboxMessage.thread_id == thread.id)
        if requested is not None:
            statement = statement.where(DealerRepInboxMessage.id.in_(requested))
        rows = list((await db.execute(statement)).scalars().all())
        if requested is not None and {row.id for row in rows} != requested:
            raise HTTPException(404, "A rendered message does not belong to this thread")
        await mark_rep_messages_seen(db, thread=thread, user_id=user.id, rows=[row for row in rows if row.direction == "inbound"], at=seen_at)
    elif parts[0] == "email":
        key = ":".join(parts[1:])
        from app.routers.inbox import _load_thread_rows

        rows = await _load_thread_rows(db, owner_id=user.id, thread_id=key)
        if requested is not None and not requested.issubset({row.id for row in rows}):
            raise HTTPException(404, "A rendered message does not belong to this mailbox thread")
        selected = [row for row in rows if row.direction == "inbound" and (requested is None or row.id in requested)]
        from app.services.communication_read_state import mark_mailbox_messages_seen

        await mark_mailbox_messages_seen(db, user_id=user.id, rows=selected, at=seen_at)
    elif parts[0] == "sms":
        from app.services.communication_scope import scoped_sms_query

        statement = await scoped_sms_query(db, user, parts[2])
        if requested is not None:
            statement = statement.where(SmsMessage.id.in_(requested))
        rows = list((await db.execute(statement)).scalars().all())
        if requested is not None and {row.id for row in rows} != requested:
            raise HTTPException(404, "A rendered text message does not belong to this authorized conversation")
        await read_received_notifications(db, user_id=user.id,
            message_ids=await sms_source_ids(db, [row for row in rows if row.direction == "inbound"]), at=seen_at)
    else:
        # Legacy portal callers do not supply provider message IDs. Scope the
        # notification update to this already-authorized exact thread.
        statement = update(Notification).where(Notification.recipient_user_id == user.id,
            Notification.target_type == "communication_thread", Notification.target_id == thread_id, Notification.read_at.is_(None))
        if requested is not None:
            statement = statement.where(Notification.meta["message_id"].astext.in_([str(value) for value in requested]))
        await db.execute(statement.values(read_at=seen_at))
    from app.services.communication_events import publish_communication_event

    await publish_communication_event(db, recipient_user_ids={user.id}, event_type="message.seen", thread_id=thread_id)
    await db.commit()
    return UnifiedCommunicationSeen(thread_id=thread_id, seen_at=seen_at)
