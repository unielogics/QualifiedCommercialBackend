"""Owner-scoped Workspace inbox read/reply API (Phase 5).

Serves the isolated per-mailbox inbox backed by `EmailMessage` (Phase 4). HARD
ISOLATION CONTRACT — every query in this module filters
`EmailMessage.owner_user_id == user.id`; there is no cross-owner read path, and a
thread/message id belonging to another owner resolves to 404, never another
mailbox's content. Bodies are stored encrypted at rest and are decrypted ONLY here,
ONLY for the owner. The shared loan/client feeds never see a body — they get the
body-less `email.tracked` Activity breadcrumb written by the sync engine.

Endpoints (all under /api/v1/inbox, all owner-scoped):
  GET  /inbox/threads                     list threads (newest-first, paginated)
  GET  /inbox/threads/{thread_id}         one thread, messages body-decrypted
  POST /inbox/threads/{thread_id}/reply   reply via send_as_user (owner's Gmail)
  POST /inbox/threads/{thread_id}/mark-read   mark every message in a thread
  POST /inbox/messages/{message_id}/mark-read mark one message
  POST /inbox/messages/{message_id}/star      star/unstar one message
  GET  /inbox/search?q=                    subject/sender search (owner-scoped)
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Text, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.models.email_message import EmailMessage
from app.schemas.inbox import (
    InboxMessageRead,
    InboxReplyRequest,
    InboxReplyResponse,
    InboxThreadDetail,
    InboxThreadListResponse,
    InboxThreadSummary,
    MarkReadRequest,
    StarRequest,
)
from app.services.email.user_inbox_sync import decrypt_body
from app.services.email.user_mailer import send_as_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox", tags=["inbox"])

# Cap the message rows scanned when composing the thread list. Threads are grouped
# in-process from this window; when the cap is hit we flag the response `truncated`
# so the UI can hint "narrow with search" rather than silently omitting mail.
_LIST_MESSAGE_CAP = 500
_PREVIEW_LEN = 160
_RE_PREFIX = re.compile(r"^\s*(re|fwd?)\s*:\s*", re.IGNORECASE)
_WS = re.compile(r"\s+")


def _thread_key(row: EmailMessage) -> str:
    """A stable grouping key: the Gmail thread id, or the row id for un-threaded
    singletons (gmail_thread_id is nullable)."""
    return row.gmail_thread_id or str(row.id)


def _sort_dt(row: EmailMessage) -> datetime:
    """received_at, falling back to created_at, then epoch — stable ordering that
    tolerates NULL received_at (nullable on the model)."""
    dt = row.received_at or row.created_at
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _preview(body: str | None) -> str | None:
    if not body:
        return None
    collapsed = _WS.sub(" ", body).strip()
    if not collapsed:
        return None
    return collapsed[:_PREVIEW_LEN]


def _decrypt(row: EmailMessage) -> str | None:
    """Decrypt a row's body for the owner. Best-effort — a decrypt failure (e.g. a
    rotated key) must not 500 the whole thread; the message renders body-less."""
    try:
        return decrypt_body(row.body_text_enc, row.encryption_provider)
    except Exception:  # noqa: BLE001
        log.warning("inbox: body decrypt failed message=%s", row.id)
        return None


def _thread_id_clause(thread_id: str):
    """Match rows in a thread: the Gmail thread id, OR (for un-threaded singletons)
    the row id when `thread_id` is a UUID string. Always combine with the owner
    scope at the call site."""
    clauses = [EmailMessage.gmail_thread_id == thread_id]
    try:
        as_uuid = uuid.UUID(thread_id)
        clauses.append(EmailMessage.id == as_uuid)
    except (ValueError, AttributeError):
        pass
    return or_(*clauses)


async def _load_thread_rows(db: AsyncSession, *, owner_id: uuid.UUID, thread_id: str) -> list[EmailMessage]:
    rows = (
        await db.execute(
            select(EmailMessage)
            .where(EmailMessage.owner_user_id == owner_id)  # ISOLATION: owner-only
            .where(_thread_id_clause(thread_id))
        )
    ).scalars().all()
    return sorted(rows, key=_sort_dt)  # oldest-first within a thread


@router.get("/threads", response_model=InboxThreadListResponse)
async def list_threads(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(40, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
):
    """List the owner's inbox threads, newest activity first. Threads are grouped
    from the most recent `_LIST_MESSAGE_CAP` messages of THIS owner only."""
    stmt = (
        select(EmailMessage)
        .where(EmailMessage.owner_user_id == user.id)  # ISOLATION: owner-only
        .order_by(EmailMessage.received_at.desc().nullslast(), EmailMessage.created_at.desc())
        .limit(_LIST_MESSAGE_CAP)
    )
    rows = (await db.execute(stmt)).scalars().all()
    truncated = len(rows) >= _LIST_MESSAGE_CAP

    # Group into threads in-process (grouping key is nullable so a GROUP BY can't
    # cleanly express the singleton fallback).
    groups: dict[str, list[EmailMessage]] = {}
    for r in rows:
        groups.setdefault(_thread_key(r), []).append(r)

    summaries: list[InboxThreadSummary] = []
    for key, members in groups.items():
        members.sort(key=_sort_dt)
        latest = members[-1]
        unread = sum(1 for m in members if not m.is_read)
        participants: list[str] = []
        for m in members:
            if m.from_email and m.from_email not in participants:
                participants.append(m.from_email)
        # linkage: take the first non-null loan/client across the thread
        loan_id = next((m.loan_id for m in members if m.loan_id is not None), None)
        client_id = next((m.client_id for m in members if m.client_id is not None), None)
        role = next((m.matched_party_role for m in members if m.matched_party_role), None)
        summaries.append(
            InboxThreadSummary(
                thread_id=key,
                subject=latest.subject,
                last_from=latest.from_email,
                preview=_preview(_decrypt(latest)),
                last_received_at=latest.received_at or latest.created_at,
                message_count=len(members),
                unread_count=unread,
                is_starred=any(m.is_starred for m in members),
                has_attachments=any(m.has_attachments for m in members),
                participants=participants[:5],
                loan_id=loan_id,
                client_id=client_id,
                matched_party_role=role,
            )
        )

    if unread_only:
        summaries = [s for s in summaries if s.unread_count > 0]
    if starred_only:
        summaries = [s for s in summaries if s.is_starred]

    summaries.sort(key=lambda s: (s.last_received_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    total = len(summaries)
    page = summaries[offset : offset + limit]
    return InboxThreadListResponse(threads=page, total=total, truncated=truncated)


@router.get("/threads/{thread_id}", response_model=InboxThreadDetail)
async def get_thread(thread_id: str, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    """One thread with every message's body DECRYPTED — owner-only. A thread id
    that belongs to another owner (or doesn't exist) is a 404, never a leak."""
    rows = await _load_thread_rows(db, owner_id=user.id, thread_id=thread_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")

    messages = [
        InboxMessageRead(
            id=r.id,
            gmail_thread_id=r.gmail_thread_id,
            gmail_message_id=r.gmail_message_id,
            direction=r.direction,
            from_email=r.from_email,
            to_emails=r.to_emails,
            cc_emails=r.cc_emails,
            subject=r.subject,
            body_text=_decrypt(r),
            received_at=r.received_at or r.created_at,
            is_read=r.is_read,
            is_starred=r.is_starred,
            has_attachments=r.has_attachments,
            loan_id=r.loan_id,
            client_id=r.client_id,
            matched_party_role=r.matched_party_role,
        )
        for r in rows
    ]
    latest = rows[-1]
    return InboxThreadDetail(
        thread_id=thread_id,
        subject=next((r.subject for r in reversed(rows) if r.subject), None),
        loan_id=next((r.loan_id for r in rows if r.loan_id is not None), None),
        client_id=next((r.client_id for r in rows if r.client_id is not None), None),
        matched_party_role=next((r.matched_party_role for r in rows if r.matched_party_role), None),
        messages=messages,
    )


@router.post("/threads/{thread_id}/reply", response_model=InboxReplyResponse)
async def reply_to_thread(
    thread_id: str,
    payload: InboxReplyRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    """Reply to a thread from the owner's connected Gmail (SES fallback). Records an
    outbound EmailMessage row in the same thread so the sent reply shows in-thread.

    NOTE: recipient-side RFC5322 In-Reply-To/References threading is not set (the
    shared send path doesn't carry those headers and isn't modified here) — the
    reply is a normal "Re:" email; our own thread view stitches it via
    gmail_thread_id. This keeps the change isolated to the inbox surface.
    """
    if not payload.body or not payload.body.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply body is required")

    rows = await _load_thread_rows(db, owner_id=user.id, thread_id=thread_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")

    latest_inbound = next((r for r in reversed(rows) if r.direction == "inbound"), rows[-1])

    # Recipients default to the latest inbound sender.
    to_emails = payload.to_emails or ([latest_inbound.from_email] if latest_inbound.from_email else [])
    to_emails = [e for e in to_emails if e and "@" in e]
    if not to_emails:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid recipient for this thread")
    cc_emails = [e for e in (payload.cc_emails or []) if e and "@" in e] or None

    base_subject = payload.subject or latest_inbound.subject or "(no subject)"
    subject = base_subject if _RE_PREFIX.match(base_subject) else f"Re: {base_subject}"

    result = await send_as_user(
        db,
        user.id,
        to_emails=to_emails,
        subject=subject,
        body_text=payload.body,
        cc_emails=cc_emails,
    )
    if not result.ok:
        # Do NOT persist an outbound row for a failed send.
        return InboxReplyResponse(ok=False, detail=result.detail)

    # Record the sent reply as an outbound EmailMessage in the same thread. gmail
    # message ids must be unique per (mailbox, gmail_message_id); the SES path
    # returns no gmail id, so fall back to a synthetic local id.
    body_enc, provider = _encrypt_reply_body(payload.body)
    # Stitch to the same thread. For a singleton inbound (gmail_thread_id NULL) its
    # thread key is str(id); reuse that so the outbound row groups WITH it rather
    # than forming a disconnected new thread.
    real_thread = latest_inbound.gmail_thread_id or str(latest_inbound.id)
    out_gmail_id = result.message_id or f"local-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    # from_email is the mailbox owner's identity for THEIR OWN thread view (the row is
    # owner-scoped). On the SES fallback the wire From is the firm address; we
    # intentionally attribute the reply to the owner here rather than the transport.
    db.add(
        EmailMessage(
            owner_user_id=user.id,
            mailbox=latest_inbound.mailbox,
            gmail_message_id=out_gmail_id,
            gmail_thread_id=real_thread,
            direction="outbound",
            from_email=(user.email or latest_inbound.mailbox),
            to_emails=to_emails,
            cc_emails=cc_emails,
            subject=subject,
            snippet=None,
            body_text_enc=body_enc,
            body_html_enc=None,
            encryption_provider=provider,
            received_at=now,
            loan_id=latest_inbound.loan_id,
            client_id=latest_inbound.client_id,
            matched_party_role=latest_inbound.matched_party_role,
            is_read=True,
            has_attachments=False,
        )
    )
    # Sending a reply implicitly acknowledges the inbound messages in the thread.
    for r in rows:
        if not r.is_read:
            r.is_read = True

    # The email has ALREADY been sent. Commit the outbound row + read-state now (rather
    # than relying on get_db's post-response teardown commit) so a persistence failure
    # is surfaced to the caller as a degraded result instead of silently losing the
    # record of a message that physically left.
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        log.exception("inbox reply: send succeeded but persistence failed thread=%s", thread_id)
        return InboxReplyResponse(
            ok=True,
            detail="sent_not_recorded",
            message_id=result.message_id,
        )

    return InboxReplyResponse(ok=True, detail=result.detail, message_id=result.message_id)


@router.post("/threads/{thread_id}/mark-read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_thread_read(
    thread_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: MarkReadRequest | None = None,
):
    """Mark every message in the owner's thread read/unread."""
    is_read = payload.is_read if payload is not None else True
    result = await db.execute(
        update(EmailMessage)
        .where(EmailMessage.owner_user_id == user.id)  # ISOLATION: owner-only
        .where(_thread_id_clause(thread_id))
        .values(is_read=is_read)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    return None


@router.post("/messages/{message_id}/mark-read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_message_read(
    message_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: MarkReadRequest | None = None,
):
    is_read = payload.is_read if payload is not None else True
    row = (
        await db.execute(
            select(EmailMessage)
            .where(EmailMessage.owner_user_id == user.id)  # ISOLATION: owner-only
            .where(EmailMessage.id == message_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    row.is_read = is_read
    return None


@router.post("/messages/{message_id}/star", status_code=status.HTTP_204_NO_CONTENT)
async def star_message(
    message_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: StarRequest | None = None,
):
    is_starred = payload.is_starred if payload is not None else True
    row = (
        await db.execute(
            select(EmailMessage)
            .where(EmailMessage.owner_user_id == user.id)  # ISOLATION: owner-only
            .where(EmailMessage.id == message_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    row.is_starred = is_starred
    return None


@router.get("/search", response_model=InboxThreadListResponse)
async def search_inbox(
    user: CurrentUser,
    q: str = Query(..., min_length=2, max_length=200),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(40, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Search the owner's mail by subject / sender / recipient. Bodies are encrypted
    at rest so they are NOT server-searchable — matches plaintext subject + from.
    Uses icontains(autoescape=True) so a literal % or _ in the query is treated as
    text, not a LIKE wildcard."""
    term = q.strip()
    stmt = (
        select(EmailMessage)
        .where(EmailMessage.owner_user_id == user.id)  # ISOLATION: owner-only
        .where(
            or_(
                EmailMessage.subject.icontains(term, autoescape=True),
                EmailMessage.from_email.icontains(term, autoescape=True),
                EmailMessage.to_emails.cast(Text).icontains(term, autoescape=True),
            )
        )
        .order_by(EmailMessage.received_at.desc().nullslast(), EmailMessage.created_at.desc())
        .limit(_LIST_MESSAGE_CAP)
    )
    rows = (await db.execute(stmt)).scalars().all()
    truncated = len(rows) >= _LIST_MESSAGE_CAP

    groups: dict[str, list[EmailMessage]] = {}
    for r in rows:
        groups.setdefault(_thread_key(r), []).append(r)

    summaries: list[InboxThreadSummary] = []
    for key, members in groups.items():
        members.sort(key=_sort_dt)
        latest = members[-1]
        summaries.append(
            InboxThreadSummary(
                thread_id=key,
                subject=latest.subject,
                last_from=latest.from_email,
                preview=_preview(_decrypt(latest)),
                last_received_at=latest.received_at or latest.created_at,
                message_count=len(members),
                unread_count=sum(1 for m in members if not m.is_read),
                is_starred=any(m.is_starred for m in members),
                has_attachments=any(m.has_attachments for m in members),
                participants=[m.from_email for m in members if m.from_email][:5],
                loan_id=next((m.loan_id for m in members if m.loan_id is not None), None),
                client_id=next((m.client_id for m in members if m.client_id is not None), None),
                matched_party_role=next((m.matched_party_role for m in members if m.matched_party_role), None),
            )
        )

    summaries.sort(key=lambda s: (s.last_received_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    total = len(summaries)
    return InboxThreadListResponse(threads=summaries[offset : offset + limit], total=total, truncated=truncated)


def _encrypt_reply_body(body: str) -> tuple[str | None, str]:
    """Encrypt the outbound reply body at rest, mirroring the sync engine's
    _encrypt_body so outbound rows use the same envelope as inbound ones."""
    from app.services.email.user_inbox_sync import _encrypt_body

    return _encrypt_body(body)
