"""Lender thread service — read the per-loan lender conversation and
post replies in one of three modes (send_now / instruct_ai / save_draft).

What the thread actually contains:

  * Messages with from_role=LENDER          → inbound from the lender
  * Messages with from_role=BROKER          → outbound from the
                                              brokerage side (sent by
                                              super_admin / loan_exec
                                              via this service)
  * Messages with from_role=AI              → outbound drafted by AI
                                              after an 'instruct_ai'
                                              reply
  * EmailDrafts with status=PENDING         → saved-for-later drafts
                                              not yet sent (composer
                                              'save_draft' mode)

Drafts that have already been sent appear in `messages` (from_role=AI
or BROKER) — we don't duplicate them in the timeline.

Sender label resolution for the thread response:

  * LENDER  → loan.lender.name (or "The Lender" if hide_identity and
              viewer is broker/client)
  * AI      → "AI Assistant" + (the actor who instructed it if known)
  * BROKER  → matched EmailDraft.actioned_by within ±60s, else
              "Internal team"

Reply flow (the three modes):

  * send_now    — body is sent to the lender immediately via Gmail
                  (or skipped with a clear note if Gmail isn't
                  configured). Writes Message(from_role=BROKER) +
                  EmailDraft(status=SENT, actioned_by=user.name) +
                  Activity(kind="email.outbound") + AIOutreachEvent
                  is NOT written (it's reserved for AI-task cadence).
                  Marks loan dirty so The Associate re-summarizes.

  * instruct_ai — user's text is treated as a prompt to the LLM,
                  along with the most recent thread context. Haiku
                  drafts a polite institutional reply, which is then
                  sent immediately via Gmail. Records the user's
                  instruction in Message(from_role=AI).body prefix +
                  Activity for the audit trail. The user's
                  instruction itself is NOT sent to the lender.

  * save_draft  — writes EmailDraft(status=PENDING) only. Nothing
                  hits Gmail. Appears in the timeline as a draft pill
                  + in the existing EmailDraftsCard.

Role gate: send_now and instruct_ai require Role.SUPER_ADMIN or
Role.LOAN_EXEC (the "underwriter user type" — no funding-team
approval gate). save_draft is permitted for the same two roles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.enums import EmailDraftStatus, MessageFrom, ParticipantRole, Role
from app.models.activity import Activity
from app.models.email_draft import EmailDraft
from app.models.lender import Lender
from app.models.loan import Loan
from app.models.loan_participant import LoanParticipant
from app.models.message import Message
from app.models.user import User
from app.services.activity_log import mark_loan_dirty
from app.services.ai.anthropic_client import get_client, model_light
from app.services.email.parser import inject_deal_id
from app.services.email.pii_filter import RedactionContext, redact_text

log = logging.getLogger(__name__)

# Window used to associate a Message(from_role=BROKER|AI) with the
# EmailDraft row that recorded who sent it. Outbound writes happen in
# the same transaction so timestamps cluster tightly — 60s is generous.
_SENDER_MATCH_WINDOW = timedelta(seconds=60)

ReplyMode = Literal["send_now", "instruct_ai", "save_draft"]


class LenderThreadError(ValueError):
    """Caller-fixable problem. Routers map to HTTP 400."""


@dataclass
class ThreadEntry:
    id: str
    kind: Literal["inbound", "outbound", "ai_outbound", "pending_draft"]
    sender_label: str
    sender_role: str  # "lender" | "broker" | "ai" | "system"
    sent_at: datetime
    body: str
    subject: str | None = None
    is_ai_drafted: bool = False
    sent_message_id: str | None = None
    draft_id: str | None = None  # only populated for pending_draft


@dataclass
class ThreadResponse:
    loan_id: str
    lender_name: str | None
    entries: list[ThreadEntry]


@dataclass
class ThreadSummaryResponse:
    loan_id: str
    headline: str
    open_asks: list[str]
    suggested_next_reply: str
    message_count: int


@dataclass
class ReplyResponse:
    mode: ReplyMode
    entry: ThreadEntry | None
    note: str


async def _load_loan(db: AsyncSession, loan_id: UUID) -> Loan:
    loan = (
        await db.execute(
            select(Loan)
            .options(
                selectinload(Loan.participants),
                selectinload(Loan.lender),
            )
            .where(Loan.id == loan_id)
        )
    ).scalar_one_or_none()
    if loan is None:
        raise LenderThreadError("Loan not found")
    return loan


def _redaction_ctx(participants: list[LoanParticipant]) -> RedactionContext:
    return RedactionContext.from_participants(participants)


def _viewer_should_redact(viewer_role: Role) -> bool:
    # Super-admin and loan-exec see the unredacted thread; broker/client see
    # the lender's identity scrubbed per the One-Way Mirror rule.
    return viewer_role in (Role.BROKER, Role.CLIENT)


def _lender_display_name(loan: Loan, viewer_role: Role) -> str:
    """What we call the lender in the thread sender column. Redacted for
    broker/client viewers, full name for the operator side."""
    if _viewer_should_redact(viewer_role):
        return "The Lender"
    if loan.lender is not None and loan.lender.name:
        return loan.lender.name
    # Fall back to the LENDER participant row if the FK isn't set yet.
    for p in loan.participants:
        if p.role == ParticipantRole.LENDER and p.display_name:
            return p.display_name
    return "Lender"


def _resolve_outbound_sender(
    msg: Message,
    drafts_sent: list[EmailDraft],
) -> tuple[str, str | None]:
    """Find which EmailDraft row recorded this outbound Message. Returns
    (sender_label, sent_message_id). Falls back to "Internal team" if no
    draft within the window was found."""
    nearest: EmailDraft | None = None
    best_delta = _SENDER_MATCH_WINDOW
    for d in drafts_sent:
        # EmailDraft has TimestampMixin → updated_at; that's when status
        # flipped to SENT. We use updated_at as the proxy for "sent at".
        if d.updated_at is None:
            continue
        delta = abs(msg.sent_at - d.updated_at)
        if delta <= best_delta:
            best_delta = delta
            nearest = d
    if nearest is None:
        return ("Internal team", None)
    label = nearest.actioned_by or "Internal team"
    return (label, nearest.sent_message_id)


async def load_thread(
    db: AsyncSession,
    *,
    loan_id: UUID,
    viewer: User,
) -> ThreadResponse:
    loan = await _load_loan(db, loan_id)
    ctx = _redaction_ctx(loan.participants)
    redact = _viewer_should_redact(viewer.role)

    msg_rows = (
        await db.execute(
            select(Message)
            .where(Message.loan_id == loan.id)
            .order_by(Message.sent_at.asc())
        )
    ).scalars().all()

    draft_rows = (
        await db.execute(
            select(EmailDraft)
            .where(EmailDraft.loan_id == loan.id)
            .order_by(EmailDraft.created_at.asc())
        )
    ).scalars().all()

    drafts_sent = [d for d in draft_rows if d.status == EmailDraftStatus.SENT]
    drafts_pending = [d for d in draft_rows if d.status == EmailDraftStatus.PENDING]

    lender_label = _lender_display_name(loan, viewer.role)
    entries: list[ThreadEntry] = []

    for m in msg_rows:
        if m.is_draft:
            # Old-flow AI-drafted-awaiting-broker rows — keep them out
            # of the thread; they live in EmailDraftsCard.
            continue
        body = redact_text(m.body, ctx) if redact else m.body
        if m.from_role == MessageFrom.LENDER:
            entries.append(
                ThreadEntry(
                    id=str(m.id),
                    kind="inbound",
                    sender_label=lender_label,
                    sender_role="lender",
                    sent_at=m.sent_at,
                    body=body,
                )
            )
        elif m.from_role == MessageFrom.AI:
            sender_label, sent_msg_id = _resolve_outbound_sender(m, drafts_sent)
            entries.append(
                ThreadEntry(
                    id=str(m.id),
                    kind="ai_outbound",
                    sender_label=f"AI · {sender_label}" if sender_label != "Internal team" else "AI Assistant",
                    sender_role="ai",
                    sent_at=m.sent_at,
                    body=body,
                    is_ai_drafted=True,
                    sent_message_id=sent_msg_id,
                )
            )
        elif m.from_role == MessageFrom.BROKER:
            sender_label, sent_msg_id = _resolve_outbound_sender(m, drafts_sent)
            entries.append(
                ThreadEntry(
                    id=str(m.id),
                    kind="outbound",
                    sender_label=sender_label,
                    sender_role="broker",
                    sent_at=m.sent_at,
                    body=body,
                    sent_message_id=sent_msg_id,
                )
            )
        # MessageFrom.CLIENT is intentionally skipped — the lender thread
        # never relays the borrower's chat.

    for d in drafts_pending:
        # We surface pending drafts inline so the operator sees what's
        # queued without leaving the page. They still appear in
        # EmailDraftsCard for the broker-approval flow.
        body = redact_text(d.body, ctx) if redact else d.body
        entries.append(
            ThreadEntry(
                id=f"draft:{d.id}",
                kind="pending_draft",
                sender_label=d.actioned_by or "Saved draft",
                sender_role="system",
                sent_at=d.created_at,
                body=body,
                subject=d.subject,
                draft_id=str(d.id),
            )
        )

    entries.sort(key=lambda e: e.sent_at)
    return ThreadResponse(
        loan_id=str(loan.id),
        lender_name=loan.lender.name if loan.lender else None,
        entries=entries,
    )


_SUMMARY_SYSTEM_PROMPT = """You summarize a lender ↔ brokerage email thread for an operator.

Output STRICT JSON with exactly three keys:
  - "headline": ONE sentence describing the current state of the
    conversation (where we are, what the lender most recently said
    or asked). 30 words max.
  - "open_asks": a JSON array of short strings (3-8 words each) —
    things the lender is currently waiting on, or things we're
    waiting on them for. Empty array if nothing is open.
  - "suggested_next_reply": ONE short paragraph the operator could
    use as a starting point for their reply. Polite, institutional,
    concrete. 60 words max. If nothing useful can be suggested,
    return an empty string.

Do NOT include any prose outside the JSON. Do NOT use markdown."""


_FALLBACK_SUMMARY = ThreadSummaryResponse(
    loan_id="",
    headline="No lender conversation yet on this deal.",
    open_asks=[],
    suggested_next_reply="",
    message_count=0,
)


def _format_thread_for_llm(entries: list[ThreadEntry]) -> str:
    """Render the timeline in a compact text form for the summarizer."""
    lines: list[str] = []
    for e in entries:
        ts = e.sent_at.strftime("%Y-%m-%d %H:%M")
        prefix = {
            "inbound": "LENDER",
            "outbound": "US",
            "ai_outbound": "US (AI)",
            "pending_draft": "DRAFT",
        }.get(e.kind, "?")
        lines.append(f"[{ts}] {prefix} — {e.sender_label}: {e.body.strip()}")
    return "\n\n".join(lines)


async def summarize_thread(
    db: AsyncSession,
    *,
    loan_id: UUID,
    viewer: User,
) -> ThreadSummaryResponse:
    thread = await load_thread(db, loan_id=loan_id, viewer=viewer)
    if not thread.entries:
        empty = _FALLBACK_SUMMARY
        return ThreadSummaryResponse(
            loan_id=str(loan_id),
            headline=empty.headline,
            open_asks=empty.open_asks,
            suggested_next_reply=empty.suggested_next_reply,
            message_count=0,
        )

    settings = get_settings()
    if not settings.anthropic_api_key:
        # Deterministic fallback — first inbound + last inbound + count.
        first_inbound = next((e for e in thread.entries if e.kind == "inbound"), None)
        last_inbound = next(
            (e for e in reversed(thread.entries) if e.kind == "inbound"), None
        )
        headline = (
            f"{len(thread.entries)} thread message(s); "
            f"last lender update {last_inbound.sent_at.date().isoformat() if last_inbound else 'n/a'}."
        )
        return ThreadSummaryResponse(
            loan_id=str(loan_id),
            headline=headline,
            open_asks=[],
            suggested_next_reply=(
                "Confirm receipt and ask for next-step timing." if first_inbound else ""
            ),
            message_count=len(thread.entries),
        )

    transcript = _format_thread_for_llm(thread.entries)
    try:
        client = get_client()
        result = await client.messages.create(
            model=model_light(),
            max_tokens=400,
            system=_SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": transcript}],
        )
        text = "".join(
            b.text for b in result.content if getattr(b, "type", None) == "text"
        ).strip()
        # Tolerant JSON extraction — Haiku occasionally wraps in ```json fences.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        import json
        parsed: dict[str, Any] = json.loads(text)
        return ThreadSummaryResponse(
            loan_id=str(loan_id),
            headline=str(parsed.get("headline") or "").strip()[:400],
            open_asks=[str(x).strip() for x in (parsed.get("open_asks") or []) if str(x).strip()][:8],
            suggested_next_reply=str(parsed.get("suggested_next_reply") or "").strip()[:800],
            message_count=len(thread.entries),
        )
    except Exception as exc:  # noqa: BLE001 — never block the UI on an LLM hiccup
        log.warning("lender_thread.summarize: LLM call failed: %s", exc)
        return ThreadSummaryResponse(
            loan_id=str(loan_id),
            headline=f"{len(thread.entries)} message(s) in the lender thread.",
            open_asks=[],
            suggested_next_reply="",
            message_count=len(thread.entries),
        )


# ---------------------------------------------------------------------------
# Reply path
# ---------------------------------------------------------------------------

_INSTRUCT_SYSTEM_PROMPT = """You draft short, formal lender-facing emails for a commercial real estate brokerage.

Tone: institutional, polite, concise. Three short paragraphs maximum.
Output ONLY the email body (plain text, no subject line, no markdown).
You are writing TO the lender on behalf of the brokerage."""


async def _ai_draft_from_instruction(
    *,
    instruction: str,
    thread_text: str,
    deal_id: str,
    address: str,
    lender_contact_name: str | None,
) -> str:
    """Convert the operator's instruction into a finished email body."""
    settings = get_settings()
    fallback = (
        f"{'Hi ' + lender_contact_name + ',' if lender_contact_name else 'Hello,'}\n\n"
        f"{instruction.strip()}\n\n"
        f"Re: {deal_id} — {address}.\n"
    )
    if not settings.anthropic_api_key:
        return fallback
    try:
        client = get_client()
        result = await client.messages.create(
            model=model_light(),
            max_tokens=500,
            system=_INSTRUCT_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Deal: {deal_id} — {address}\n"
                        f"Lender contact: {lender_contact_name or '(unknown)'}\n\n"
                        f"Prior thread:\n{thread_text or '(no prior messages)'}\n\n"
                        f"Operator's instruction to you (the AI):\n{instruction.strip()}\n\n"
                        "Write the email body."
                    ),
                }
            ],
        )
        text = "".join(
            b.text for b in result.content if getattr(b, "type", None) == "text"
        ).strip()
        return text or fallback
    except Exception as exc:  # noqa: BLE001
        log.warning("lender_thread: AI draft from instruction failed: %s", exc)
        return fallback


def _gmail_send_or_skip(
    *,
    to_email: str,
    subject: str,
    body: str,
) -> tuple[str | None, str]:
    """Call Gmail if DWD is configured; return (sent_message_id, note).
    Never raises — Gmail being unavailable should not roll back the
    user's reply intent. Falls through with (None, reason) so the
    Message row still gets written for thread continuity in dev."""
    settings = get_settings()
    if not (settings.gmail_service_account_path and settings.gmail_delegated_user):
        return (None, "Gmail not configured (SA path / delegated user) — message stored locally only.")
    # Local imports keep this module testable without google libs installed.
    try:
        from app.services.email.gmail_client import gmail_config, send_message
    except ImportError as exc:
        return (None, f"Gmail client not importable: {exc}")
    cfg = gmail_config()
    if cfg is None:
        return (None, "Gmail config returned None — service account path empty.")
    try:
        resp = send_message(cfg, to=to_email, subject=subject, body=body)
        return (resp.get("id"), f"Sent via Gmail. message_id={resp.get('id')}")
    except Exception as exc:  # noqa: BLE001
        log.warning("lender_thread: Gmail send failed: %s", exc)
        return (None, f"Gmail send failed: {exc}")


async def post_reply(
    db: AsyncSession,
    *,
    loan_id: UUID,
    actor: User,
    mode: ReplyMode,
    text: str,
) -> ReplyResponse:
    if mode not in ("send_now", "instruct_ai", "save_draft"):
        raise LenderThreadError(f"Unknown reply mode: {mode!r}")
    if actor.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise LenderThreadError(
            "Only super_admin and loan_exec can post to the lender thread."
        )
    if not text or not text.strip():
        raise LenderThreadError("Reply text is required.")

    loan = await _load_loan(db, loan_id)
    if loan.lender_id is None or loan.lender is None:
        raise LenderThreadError(
            "No lender is connected to this loan. Connect a lender first."
        )

    lender: Lender = loan.lender
    to_email = lender.submission_email or lender.contact_email
    if not to_email:
        raise LenderThreadError(
            f"Lender '{lender.name}' has no submission_email or contact_email."
        )

    actor_label = actor.name or actor.email or actor.role.value
    subject_base = f"Re: {loan.address}"
    subject = inject_deal_id(subject_base, loan.deal_id)

    # --- save_draft -------------------------------------------------
    if mode == "save_draft":
        draft = EmailDraft(
            loan_id=loan.id,
            to_email=to_email,
            cc_emails=None,
            bcc_emails=None,
            subject=subject,
            body=text.strip(),
            status=EmailDraftStatus.PENDING,
            triggered_by_kind="lender_thread_manual_draft",
            actioned_by=actor_label,
        )
        db.add(draft)
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=actor.id,
                actor_label=actor_label,
                kind="lender_thread.draft_saved",
                summary=f"Saved lender draft ({len(text)} chars)",
                payload={"draft_id_pending": True},
            )
        )
        await db.flush()
        await db.refresh(draft)
        return ReplyResponse(
            mode=mode,
            entry=ThreadEntry(
                id=f"draft:{draft.id}",
                kind="pending_draft",
                sender_label=actor_label,
                sender_role="system",
                sent_at=draft.created_at,
                body=text.strip(),
                subject=subject,
                draft_id=str(draft.id),
            ),
            note="Draft saved. It will not be sent until you approve it.",
        )

    # --- send_now / instruct_ai -------------------------------------
    if mode == "instruct_ai":
        # Render thread context for the LLM, then have it write the email.
        thread = await load_thread(db, loan_id=loan_id, viewer=actor)
        thread_text = _format_thread_for_llm(thread.entries)
        body = await _ai_draft_from_instruction(
            instruction=text,
            thread_text=thread_text,
            deal_id=loan.deal_id,
            address=loan.address,
            lender_contact_name=lender.contact_name,
        )
        from_role = MessageFrom.AI
        actor_for_label = f"AI (instructed by {actor_label})"
        activity_kind = "lender_thread.ai_replied"
    else:
        body = text.strip()
        from_role = MessageFrom.BROKER
        actor_for_label = actor_label
        activity_kind = "lender_thread.replied"

    sent_message_id, note = _gmail_send_or_skip(
        to_email=to_email, subject=subject, body=body
    )
    status = EmailDraftStatus.SENT if sent_message_id else EmailDraftStatus.APPROVED
    sent_at = datetime.now(timezone.utc)

    msg = Message(
        loan_id=loan.id,
        from_role=from_role,
        body=body,
        is_draft=False,
        sent_at=sent_at,
    )
    db.add(msg)

    draft = EmailDraft(
        loan_id=loan.id,
        to_email=to_email,
        cc_emails=None,
        bcc_emails=None,
        subject=subject,
        body=body,
        status=status,
        triggered_by_kind="lender_thread_send_now" if mode == "send_now" else "lender_thread_instruct_ai",
        triggered_by_payload={
            "actor_role": actor.role.value,
            "instruction": text.strip() if mode == "instruct_ai" else None,
        },
        actioned_by=actor_for_label,
        sent_message_id=sent_message_id,
    )
    db.add(draft)

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=actor.id,
            actor_label=actor_label,
            kind=activity_kind,
            summary=f"Replied to {lender.name} via {mode} ({len(body)} chars)",
            payload={
                "lender_id": str(lender.id),
                "to_email": to_email,
                "sent_message_id": sent_message_id,
                "mode": mode,
                "note": note,
            },
        )
    )

    # Material change — let The Associate re-summarize on its next pass.
    await mark_loan_dirty(db, loan.id)
    await db.flush()
    await db.refresh(msg)
    await db.refresh(draft)

    entry = ThreadEntry(
        id=str(msg.id),
        kind="ai_outbound" if mode == "instruct_ai" else "outbound",
        sender_label=actor_for_label,
        sender_role="ai" if mode == "instruct_ai" else "broker",
        sent_at=msg.sent_at,
        body=body,
        is_ai_drafted=mode == "instruct_ai",
        sent_message_id=sent_message_id,
    )
    return ReplyResponse(mode=mode, entry=entry, note=note)


# ---------------------------------------------------------------------------
# Dev-only test injection — writes a Message(from_role=LENDER) row as if
# Gmail had just delivered a reply. Routed through here so the same code
# path future-proofs us when real Pub/Sub ingestion arrives.
# ---------------------------------------------------------------------------

async def inject_inbound_lender_email(
    db: AsyncSession,
    *,
    loan_id: UUID,
    from_email: str,
    subject: str,
    body: str,
) -> Message:
    """Write a synthetic inbound lender message into the loan thread.

    Verifies (a) the loan exists and (b) `from_email` matches the
    connected lender's submission_email or contact_email — so the
    operator can't accidentally inject a message attributed to a
    counterparty that isn't on the deal.
    """
    loan = await _load_loan(db, loan_id)
    if loan.lender is None:
        raise LenderThreadError(
            "Loan has no connected lender — connect one first before injecting."
        )
    lender_emails = {
        (loan.lender.submission_email or "").lower(),
        (loan.lender.contact_email or "").lower(),
    }
    lender_emails.discard("")
    if from_email.lower() not in lender_emails:
        raise LenderThreadError(
            f"from_email {from_email!r} does not match the connected lender's "
            f"submission_email or contact_email."
        )

    msg = Message(
        loan_id=loan.id,
        from_role=MessageFrom.LENDER,
        body=body.strip(),
        is_draft=False,
        sent_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=None,
            actor_label="lender",
            kind="email.inbound",
            summary=f"Inbound from {from_email}: {subject[:120]}",
            payload={"subject": subject, "from": from_email, "mode": "dev_inject"},
        )
    )
    await mark_loan_dirty(db, loan.id)
    await db.flush()
    await db.refresh(msg)
    return msg
