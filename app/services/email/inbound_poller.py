"""Inbound Gmail poller — pulls lender replies into the lender thread.

Runs every 60s via apscheduler. Each tick:

  1. Gmail users.messages.list q='is:unread subject:"[QC-"' against the
     delegated mailbox (franco@qualifiedcommercial.com).
  2. For each new message, fetch its body, parse the `[QC-{deal_id}]`
     tag, and look up the matching Loan.
  3. Write a `Message(from_role=LENDER, body=parsed)` row — this is
     what makes the message appear in the LenderThread mailbox UI.
  4. Run the existing `orchestrator.process_inbound` so the broker-
     facing PII-scrubbed EmailDraft + AITask path still fires.
  5. Mark the Gmail message as `UNREAD: false` so we don't re-process
     on the next tick.

Single-instance assumption: only one qcbackend container runs, so
exactly one poller is active. If we ever scale out, move this to
EventBridge → /admin/cron/tick?job=inbound_poll, like FRED.

Failure isolation: each message is processed inside its own
try/except so a single bad message doesn't block the rest of the
batch. Gmail label updates happen even on partial failures (we'd
rather drop a message than re-process it 1000 times).
"""

from __future__ import annotations

import asyncio
import base64
import email
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.enums import MessageFrom, ParticipantRole
from app.models.activity import Activity
from app.models.loan import Loan
from app.models.loan_participant import LoanParticipant
from app.models.message import Message
from app.services.activity_log import mark_loan_dirty
from app.services.email.orchestrator import InboundEmail, process_inbound
from app.services.email.parser import extract_deal_id

log = logging.getLogger(__name__)

# Gmail's search query for messages tagged with our deal-id convention.
# We DON'T filter by is:unread here because the operator might open a
# reply manually before the poller runs, and we'd then miss it forever.
# Dedup is enforced DB-side via Activity.payload.gmail_id.
#
# `newer_than:7d` bounds how far back we look so a poll never has to
# walk an unbounded inbox history.
_GMAIL_SEARCH_QUERY = 'subject:"[QC-" newer_than:7d in:inbox'

# Hard cap per tick — keeps any one tick from blowing up Anthropic spend
# or Gmail API quota if an old batch suddenly becomes visible.
_BATCH_LIMIT = 25


# Serializes every inbound poll — the 60s scheduler tick AND the
# webhook-triggered polls (app/routers/webhooks.py) share this lock so
# two polls can never run concurrently and double-ingest a message in
# the window between one poll's messages.list and its Activity commit.
_poll_lock = asyncio.Lock()


async def run_inbound_poll() -> None:
    """Serialized poll entry point. Used by apscheduler and the Gmail
    push webhook. Only one poll runs at a time; a concurrent caller
    waits (pushes are infrequent, so the wait is bounded and cheap)."""
    async with _poll_lock:
        await _run_inbound_poll_impl()


async def _run_inbound_poll_impl() -> None:
    """Single-tick poller body.

    Idempotent — dedups already-processed messages via the Activity
    log. Safe to invoke manually for testing."""
    settings = get_settings()
    if settings.use_fake_inbox:
        log.debug("inbound_poller: USE_FAKE_INBOX=true; skipping real Gmail pull")
        return
    if not settings.gmail_service_account_path or not settings.gmail_delegated_user:
        log.debug("inbound_poller: Gmail not configured; skipping")
        return

    try:
        # Lazy imports — see gmail_client.py module top.
        from app.services.email.gmail_client import gmail_config, get_gmail_service
    except ImportError as exc:
        log.warning("inbound_poller: Gmail client not importable: %s", exc)
        return

    cfg = gmail_config()
    if cfg is None:
        log.warning("inbound_poller: gmail_config() returned None")
        return

    try:
        svc = get_gmail_service(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("inbound_poller: build gmail service failed: %s", exc)
        return

    # 1. List unread messages matching the deal-id subject pattern.
    try:
        resp = svc.users().messages().list(
            userId="me",
            q=_GMAIL_SEARCH_QUERY,
            maxResults=_BATCH_LIMIT,
        ).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("inbound_poller: messages.list failed: %s", exc)
        return

    msg_refs = resp.get("messages", []) or []
    if not msg_refs:
        log.debug("inbound_poller: no new lender messages")
        return

    log.info("inbound_poller: processing %d new message(s)", len(msg_refs))

    # Open ONE DB session for the whole batch — atomic commit at end.
    # Dedup is enforced via the Activity table: every successful ingest
    # logs `Activity(kind='email.inbound', payload={..., gmail_id})`,
    # so on subsequent polls we look that up and skip.
    async with SessionLocal() as db:
        # Bulk-load the set of gmail_ids we've already processed in the
        # last 30 days. JSONB containment query is indexed in Postgres.
        already_seen = await _load_processed_gmail_ids(db)
        log.debug("inbound_poller: %d gmail_ids already in audit log", len(already_seen))

        for ref in msg_refs:
            gmail_id = ref.get("id")
            if not gmail_id:
                continue
            if gmail_id in already_seen:
                continue
            try:
                detail = svc.users().messages().get(
                    userId="me", id=gmail_id, format="full"
                ).execute()
            except Exception as exc:  # noqa: BLE001
                log.warning("inbound_poller: messages.get(%s) failed: %s", gmail_id, exc)
                continue

            parsed = _parse_gmail_message(detail)
            if parsed is None:
                log.info("inbound_poller: skipping gmail_id=%s (no deal_id tag)", gmail_id)
                continue

            try:
                await _ingest_one(
                    db,
                    parsed=parsed,
                    gmail_id=gmail_id,
                    gmail_svc=svc,
                    detail=detail,
                )
            except Exception:
                log.exception("inbound_poller: ingest failed for gmail_id=%s", gmail_id)
                # Continue — next tick will retry this one because no
                # Activity row was logged.

        await db.commit()


async def _load_processed_gmail_ids(db: AsyncSession) -> set[str]:
    """Return the set of Gmail message IDs we've already turned into
    Activity rows. Bounded to the last 30 days so the working set
    stays small."""
    from datetime import timedelta
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows = (
        await db.execute(
            select(Activity.payload)
            .where(Activity.kind == "email.inbound")
            .where(Activity.occurred_at >= cutoff)
        )
    ).scalars().all()
    out: set[str] = set()
    for p in rows:
        if isinstance(p, dict) and isinstance(p.get("gmail_id"), str):
            out.add(p["gmail_id"])
    return out


def _parse_gmail_message(detail: dict[str, Any]) -> InboundEmail | None:
    """Turn a Gmail API message resource into our InboundEmail dataclass.

    Returns None if the subject doesn't carry our [QC-{deal_id}] tag
    (the orchestrator would ignore it anyway; bailing early saves a
    DB lookup).
    """
    headers = {h["name"].lower(): h["value"] for h in detail.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "")
    deal_id = extract_deal_id(subject)
    if deal_id is None:
        return None

    sender = headers.get("from", "")
    # Gmail's "From" header is typically "Name <email@host>" — extract
    # the bare address for participant matching.
    if "<" in sender and ">" in sender:
        sender = sender.split("<", 1)[1].rsplit(">", 1)[0].strip()
    sender = sender.lower().strip()

    body = _extract_body(detail.get("payload", {}))

    return InboundEmail(
        sender=sender,
        subject=subject,
        body=body,
        deal_id=deal_id,
    )


def _extract_body(payload: dict[str, Any]) -> str:
    """Walk the MIME tree and pull the text/plain part (or fall back to
    text/html stripped to text). Gmail's payload is recursive — parts
    may have nested parts."""
    if not payload:
        return ""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        return _decode_part(payload.get("body", {}))
    if mime_type == "text/html":
        # Best-effort: keep the HTML for now; downstream PII filter
        # strips it later. Operators reading raw audit can copy clean.
        return _decode_part(payload.get("body", {}))
    # Multipart — recurse, preferring text/plain.
    parts = payload.get("parts", []) or []
    if not parts:
        return ""
    # First pass — look for text/plain.
    for p in parts:
        if p.get("mimeType") == "text/plain":
            text = _decode_part(p.get("body", {}))
            if text:
                return text
    # Second pass — anything we can read.
    for p in parts:
        text = _extract_body(p)
        if text:
            return text
    return ""


def _decode_part(body: dict[str, Any]) -> str:
    data = body.get("data")
    if not data:
        return ""
    # Gmail uses URL-safe base64 without padding.
    pad = "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(data + pad)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Find every MIME part with a filename. Returns a list of dicts
    with {filename, mime_type, attachment_id, size_bytes}. The
    attachment_id is the Gmail-side reference we need to pass to
    users.messages.attachments.get to fetch the bytes."""
    out: list[dict[str, Any]] = []
    if not payload:
        return out
    filename = payload.get("filename") or ""
    body = payload.get("body") or {}
    attachment_id = body.get("attachmentId")
    if filename and attachment_id:
        out.append(
            {
                "filename": filename,
                "mime_type": payload.get("mimeType") or "application/octet-stream",
                "attachment_id": attachment_id,
                "size_bytes": int(body.get("size") or 0),
            }
        )
    for p in payload.get("parts", []) or []:
        out.extend(_walk_attachments(p))
    return out


async def _ingest_one(
    db: AsyncSession,
    *,
    parsed: InboundEmail,
    gmail_id: str,
    gmail_svc: Any = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Persist a single inbound lender email.

    Writes a Message(from_role=LENDER) row keyed on the deal_id +
    sender match, then runs the existing orchestrator so the broker-
    approval EmailDraft path still fires.

    The two artifacts are complementary:
      - Message row → drives the LenderThread mailbox UI
      - EmailDraft  → drives the existing broker-approval queue
    """
    from sqlalchemy.orm import selectinload as _selectinload

    loan = (
        await db.execute(
            select(Loan)
            # Eager-load broker so the push notification's
            # `loan.broker.user_id` access doesn't trigger a lazy
            # load in async context (raises MissingGreenlet).
            .options(_selectinload(Loan.broker))
            .where(Loan.deal_id == parsed.deal_id)
        )
    ).scalar_one_or_none()
    if loan is None:
        log.info(
            "inbound_poller: deal_id=%s in subject does not match any loan (gmail_id=%s)",
            parsed.deal_id, gmail_id,
        )
        return

    # Match sender to a LoanParticipant; only LENDER-role senders get
    # written to the thread (we never echo broker/client mail back into
    # the lender thread).
    participants = (
        await db.execute(
            select(LoanParticipant).where(LoanParticipant.loan_id == loan.id)
        )
    ).scalars().all()
    sender_role = None
    for p in participants:
        if p.email.lower() == parsed.sender:
            sender_role = p.role
            break

    if sender_role != ParticipantRole.LENDER:
        log.info(
            "inbound_poller: gmail_id=%s sender=%s on loan=%s is not a LENDER participant (role=%s); "
            "skipping Message row, running orchestrator only",
            gmail_id, parsed.sender, loan.id, sender_role,
        )
    else:
        # 1. Append to the lender thread as an inbound entry.
        msg = Message(
            loan_id=loan.id,
            from_role=MessageFrom.LENDER,
            body=parsed.body.strip() or "(empty body)",
            is_draft=False,
            # Don't override sent_at — let server_default fire so it
            # reflects ingestion time. Gmail's internal Date header is
            # subject to client clock skew.
        )
        db.add(msg)

        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=None,
                actor_label="lender",
                kind="email.inbound",
                summary=f"Inbound from {parsed.sender}: {parsed.subject[:120]}",
                payload={
                    "subject": parsed.subject,
                    "from": parsed.sender,
                    "gmail_id": gmail_id,
                    "mode": "poller",
                },
            )
        )

        # Material change — let The Associate re-summarize + the new AI
        # extractor (Phase 2) pick this up.
        await mark_loan_dirty(db, loan.id)
        await db.flush()
        log.info(
            "inbound_poller: ingested gmail_id=%s into loan=%s as Message %s",
            gmail_id, loan.id, msg.id,
        )

        # 1b. Extract attachments. Each attachment requires a separate
        # users.messages.attachments.get call against Gmail to fetch
        # the bytes (Gmail doesn't inline them by default beyond a
        # few KB threshold). Persist via the lender_attachments
        # service so the audit + UI surfaces pick them up.
        if gmail_svc is not None and detail is not None:
            try:
                from app.services.lender_attachments import (
                    decode_b64url,
                    ingest_inbound_attachment,
                )

                refs = _walk_attachments(detail.get("payload", {}) or {})
                for ref in refs:
                    try:
                        att_resp = (
                            gmail_svc.users()
                            .messages()
                            .attachments()
                            .get(
                                userId="me",
                                messageId=gmail_id,
                                id=ref["attachment_id"],
                            )
                            .execute()
                        )
                        data_b64 = att_resp.get("data") or ""
                        if not data_b64:
                            continue
                        await ingest_inbound_attachment(
                            db,
                            loan_id=loan.id,
                            message_id=msg.id,
                            gmail_id=gmail_id,
                            filename=ref["filename"],
                            mime_type=ref["mime_type"],
                            data=decode_b64url(data_b64),
                        )
                    except Exception:
                        log.exception(
                            "inbound_poller: attachment fetch failed "
                            "gmail_id=%s filename=%s (non-fatal)",
                            gmail_id, ref.get("filename"),
                        )
                if refs:
                    log.info(
                        "inbound_poller: pulled %d attachment(s) for "
                        "gmail_id=%s",
                        len(refs), gmail_id,
                    )
            except Exception:
                log.exception(
                    "inbound_poller: attachment walk failed for gmail_id=%s",
                    gmail_id,
                )

    # 2. Always run the orchestrator so the existing broker-approval
    # EmailDraft + AITask flow stays consistent for callers that key
    # off those tables (the EmailDrafts panel, etc.). It logs its own
    # Activity row keyed on email.inbound so audit is preserved either
    # way.
    try:
        await process_inbound(db, parsed)
    except Exception:
        log.exception(
            "inbound_poller: orchestrator failed for gmail_id=%s — Message row stays",
            gmail_id,
        )

    # 3. Refresh the structured AI extract (loans.living_profile.
    # lender_extract). This is what feeds the right-column "What the
    # lender needs" panel and the loan-chat AI grounding. Failure is
    # non-fatal — the Message row + EmailDraft are already persisted.
    if sender_role == ParticipantRole.LENDER:
        try:
            from app.services.ai.lender_extractor import extract_and_persist

            await extract_and_persist(db, loan_id=loan.id, trigger="inbound_poller")
        except Exception:
            log.exception(
                "inbound_poller: lender_extractor failed for gmail_id=%s "
                "(non-fatal; Message + orchestrator already persisted)",
                gmail_id,
            )

        # 4. Push notification to the loan's broker (NOT to the client —
        # one-way mirror; client never sees raw lender mail).
        # super_admin is omitted because they're the delegated Gmail
        # mailbox owner and already get a native Gmail notification.
        try:
            await _notify_broker_of_inbound(loan=loan, parsed=parsed)
        except Exception:
            log.exception(
                "inbound_poller: broker push notify failed for gmail_id=%s "
                "(non-fatal)",
                gmail_id,
            )


async def _notify_broker_of_inbound(*, loan: Loan, parsed: InboundEmail) -> None:
    """Fire-and-forget FCM push to the loan's assigned broker so they
    know a lender reply arrived without having to keep the platform
    open. The broker doesn't get Gmail (the SA-delegated mailbox is
    franco's), so without this they have no signal."""
    if loan.broker is None or loan.broker.user_id is None:
        return
    deal_id = loan.deal_id or "(no deal id)"
    snippet = (parsed.body or "").strip().replace("\n", " ")[:140]
    # Local import — push service is allowed to be missing in dev.
    try:
        from app.services.push import fire_and_forget_push
    except ImportError:
        return
    fire_and_forget_push(
        loan.broker.user_id,
        title=f"Lender reply on {deal_id}",
        body=snippet or "(empty body)",
        data={
            "kind": "lender_inbound",
            "loan_id": str(loan.id),
            "deal_id": deal_id,
        },
    )
