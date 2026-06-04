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
from dataclasses import dataclass, field
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
from app.services.ai.usage import tracked_messages_create
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
class ThreadEntryAttachment:
    """Lightweight attachment ref attached to a ThreadEntry. The
    download URL is short-lived (1 hour) and built fresh on each
    /lender-thread call — the frontend should not cache it."""

    id: str
    filename: str
    mime_type: str
    size_bytes: int
    source: str  # 'outbound_upload' | 'system_doc_ref' | 'inbound_lender'
    direction: str  # 'outbound' | 'inbound'
    download_url: str | None = None


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
    # Round-2: surface the actual delivery outcome so the UI can show a
    # green/amber/red pill instead of treating every "outbound" as if it
    # went out.
    send_status: Literal["sent", "saved", "failed", "n/a"] = "n/a"
    send_note: str | None = None
    to_email: str | None = None
    # Round-4: attachments associated with this message (committed
    # outbound uploads + inbound MIME parts pulled by the poller).
    attachments: list[ThreadEntryAttachment] = field(default_factory=list)


@dataclass
class ThreadResponse:
    loan_id: str
    lender_name: str | None
    entries: list[ThreadEntry]
    # Round-3 (added 2026-05-14) — structured AI extract for the right-
    # column to-do panel. Filtered server-side per viewer role:
    # super_admin / loan_exec see the full extract; broker / client
    # see only items tagged sensitivity='external'. None if no LLM
    # has run yet or ANTHROPIC_API_KEY is unset.
    lender_extract: dict[str, Any] | None = None


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
    outbound_drafts: list[EmailDraft],
) -> tuple[str, EmailDraft | None]:
    """Find which EmailDraft row recorded this outbound Message. Returns
    (sender_label, draft). Matching is by closest timestamp within a
    60s window — the reply handler writes both rows in the same
    transaction so they cluster.

    We deliberately accept any non-PENDING outbound draft (status in
    {SENT, APPROVED}) so the saved-only path (Gmail not configured →
    APPROVED) is also matched. The caller derives send_status from the
    returned draft's status + sent_message_id."""
    nearest: EmailDraft | None = None
    best_delta = _SENDER_MATCH_WINDOW
    for d in outbound_drafts:
        # EmailDraft has TimestampMixin → updated_at on SENT, created_at
        # on the original write. created_at is more reliable since
        # APPROVED rows may never be re-updated.
        ts = d.updated_at or d.created_at
        if ts is None:
            continue
        delta = abs(msg.sent_at - ts)
        if delta <= best_delta:
            best_delta = delta
            nearest = d
    if nearest is None:
        return ("Internal team", None)
    label = nearest.actioned_by or "Internal team"
    return (label, nearest)


def _derive_send_status(draft: EmailDraft | None) -> tuple[str, str | None]:
    """Translate an EmailDraft row into (send_status, sent_message_id)
    for the UI pill. send_status is one of:
      - "sent"   : Gmail confirmed delivery with a message_id
      - "saved"  : message saved but NOT sent (Gmail not configured)
      - "failed" : reserved for the (should-never-happen) state where
                   status=SENT but message_id is null
      - "n/a"    : matching draft not found
    """
    if draft is None:
        return ("n/a", None)
    if draft.status == EmailDraftStatus.SENT:
        if draft.sent_message_id:
            return ("sent", draft.sent_message_id)
        return ("failed", None)
    # APPROVED, PENDING, DISMISSED all => saved (not delivered)
    return ("saved", None)


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

    # Pre-load all committed attachments on this loan in one shot,
    # bucketed by message_id. Avoids N+1 lookups when the thread has
    # many messages. The Message.attachments relationship has
    # lazy="selectin" so .attachments on each msg works too — but
    # this is one query for the whole thread, cheaper at scale.
    from app.models.message_attachment import MessageAttachment as _MA
    from app.services.lender_attachments import presign_download

    att_rows = (
        await db.execute(
            select(_MA)
            .where(_MA.loan_id == loan.id)
            .where(_MA.status == "committed")
            .order_by(_MA.created_at.asc())
        )
    ).scalars().all()
    atts_by_msg: dict[UUID, list[_MA]] = {}
    for a in att_rows:
        if a.message_id is None:
            continue
        atts_by_msg.setdefault(a.message_id, []).append(a)

    async def _build_attachments_for(msg_id: UUID) -> list[ThreadEntryAttachment]:
        out: list[ThreadEntryAttachment] = []
        for a in atts_by_msg.get(msg_id, []):
            out.append(
                ThreadEntryAttachment(
                    id=str(a.id),
                    filename=a.filename,
                    mime_type=a.mime_type,
                    size_bytes=a.size_bytes,
                    source=a.source,
                    direction=a.direction,
                    download_url=await presign_download(a),
                )
            )
        return out

    draft_rows = (
        await db.execute(
            select(EmailDraft)
            .where(EmailDraft.loan_id == loan.id)
            .order_by(EmailDraft.created_at.asc())
        )
    ).scalars().all()

    # Outbound drafts = anything that's NOT pending (already actioned or
    # saved). Used to match outbound Message rows to their corresponding
    # EmailDraft so we can derive send_status.
    outbound_drafts = [d for d in draft_rows if d.status != EmailDraftStatus.PENDING]
    drafts_pending = [d for d in draft_rows if d.status == EmailDraftStatus.PENDING]

    # Pull recent Activity rows so we can attach the verbatim "Gmail
    # not configured / send failed / sent" note to each outbound entry.
    # The note lives in activities.payload.note and was written by
    # post_reply at send time.
    activity_rows = (
        await db.execute(
            select(Activity)
            .where(Activity.loan_id == loan.id)
            .where(Activity.kind.in_(["lender_thread.replied", "lender_thread.ai_replied"]))
            .order_by(Activity.occurred_at.asc())
        )
    ).scalars().all()

    def _note_for_msg(msg: Message) -> str | None:
        nearest: Activity | None = None
        best_delta = _SENDER_MATCH_WINDOW
        for a in activity_rows:
            if a.occurred_at is None:
                continue
            delta = abs(msg.sent_at - a.occurred_at)
            if delta <= best_delta:
                best_delta = delta
                nearest = a
        if nearest is None or not nearest.payload:
            return None
        n = nearest.payload.get("note")
        return str(n) if n else None

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
                    send_status="n/a",
                    attachments=await _build_attachments_for(m.id),
                )
            )
        elif m.from_role == MessageFrom.AI:
            sender_label, draft = _resolve_outbound_sender(m, outbound_drafts)
            status, sent_msg_id = _derive_send_status(draft)
            entries.append(
                ThreadEntry(
                    id=str(m.id),
                    kind="ai_outbound",
                    sender_label=f"AI · {sender_label}" if sender_label != "Internal team" else "AI Assistant",
                    sender_role="ai",
                    sent_at=m.sent_at,
                    body=body,
                    subject=draft.subject if draft else None,
                    is_ai_drafted=True,
                    sent_message_id=sent_msg_id,
                    send_status=status,
                    send_note=_note_for_msg(m),
                    to_email=draft.to_email if draft else None,
                    attachments=await _build_attachments_for(m.id),
                )
            )
        elif m.from_role == MessageFrom.BROKER:
            sender_label, draft = _resolve_outbound_sender(m, outbound_drafts)
            status, sent_msg_id = _derive_send_status(draft)
            entries.append(
                ThreadEntry(
                    id=str(m.id),
                    kind="outbound",
                    sender_label=sender_label,
                    sender_role="broker",
                    sent_at=m.sent_at,
                    body=body,
                    subject=draft.subject if draft else None,
                    sent_message_id=sent_msg_id,
                    send_status=status,
                    send_note=_note_for_msg(m),
                    to_email=draft.to_email if draft else None,
                    attachments=await _build_attachments_for(m.id),
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
                to_email=d.to_email,
                send_status="saved",
                send_note="Draft — will not be sent until approved.",
            )
        )

    entries.sort(key=lambda e: e.sent_at)

    # Pick the right extract view based on viewer role:
    #   super_admin / loan_exec → full lender_extract (internal + external)
    #   broker / client          → externals-only view that drops sensitive items
    profile = loan.living_profile or {}
    if viewer.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        lender_extract = profile.get("lender_extract")
    else:
        lender_extract = profile.get("lender_extract_external")

    return ThreadResponse(
        loan_id=str(loan.id),
        lender_name=loan.lender.name if loan.lender else None,
        entries=entries,
        lender_extract=lender_extract,
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
        result = await tracked_messages_create(
            db,
            feature="lender_thread_subject",
            client=client,
            model=model_light(),
            loan_id=loan_id,
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
    db: AsyncSession,
    loan_id: UUID,
    client_id: UUID | None,
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
        result = await tracked_messages_create(
            db,
            feature="lender_thread_reply",
            client=client,
            model=model_light(),
            loan_id=loan_id,
            client_id=client_id,
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
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str]:
    """Call Gmail if DWD is configured; return (sent_message_id, note).
    Never raises — Gmail being unavailable should not roll back the
    user's reply intent. Falls through with (None, reason) so the
    Message row still gets written for thread continuity in dev.

    `attachments` is the same shape build_message accepts:
    [{filename, mime_type, data: bytes}, ...]. Caller materializes
    these from MessageAttachment rows before invoking.
    """
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
        resp = send_message(
            cfg,
            to=to_email,
            subject=subject,
            body=body,
            attachments=attachments,
        )
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
    attachment_ids: list[UUID] | None = None,
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

    actor_label = (
        actor.name
        or actor.email
        or (actor.role.value if hasattr(actor.role, "value") else str(actor.role))
    )
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
            db=db,
            loan_id=loan.id,
            client_id=loan.client_id,
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

    # Materialize staged attachments (download from S3) so we can hand
    # them to Gmail as MIME parts. We only do this for send_now /
    # instruct_ai; save_draft already returned above without sending.
    materialized_attachments: list[dict[str, Any]] = []
    staged_atts = []
    if attachment_ids:
        from app.models.message_attachment import MessageAttachment as _MA
        from app.services.lender_attachments import (
            materialize_attachments_for_send,
        )

        staged_atts = (
            await db.execute(
                select(_MA).where(
                    _MA.id.in_(attachment_ids),
                    _MA.loan_id == loan.id,
                )
            )
        ).scalars().all()
        materialized_attachments = await materialize_attachments_for_send(staged_atts)

    sent_message_id, note = _gmail_send_or_skip(
        to_email=to_email,
        subject=subject,
        body=body,
        attachments=materialized_attachments or None,
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
            "actor_role": actor.role.value if hasattr(actor.role, "value") else str(actor.role),
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

    # Commit staged attachments to this Message row. We've already
    # materialized their bytes for the Gmail send above — this step
    # is just flipping ownership pointers so the timeline + audit
    # drawer can render them.
    if attachment_ids:
        from app.services.lender_attachments import (
            commit_attachments_to_message,
        )

        await commit_attachments_to_message(
            db,
            loan_id=loan.id,
            message_id=msg.id,
            attachment_ids=attachment_ids,
        )

    # Refresh the structured AI extract (loans.living_profile.lender_extract)
    # so the right-column to-do panel + the general AI chat both see
    # our latest outbound commitments. Non-fatal — Message/EmailDraft
    # already persisted above.
    try:
        from app.services.ai.lender_extractor import extract_and_persist

        await extract_and_persist(
            db,
            loan_id=loan.id,
            trigger=f"post_reply:{mode}",
        )
    except Exception:
        log.exception(
            "post_reply: lender_extractor failed (non-fatal); "
            "outbound rows are already saved"
        )

    derived_status, _ = _derive_send_status(draft)
    entry = ThreadEntry(
        id=str(msg.id),
        kind="ai_outbound" if mode == "instruct_ai" else "outbound",
        sender_label=actor_for_label,
        sender_role="ai" if mode == "instruct_ai" else "broker",
        sent_at=msg.sent_at,
        body=body,
        subject=subject,
        is_ai_drafted=mode == "instruct_ai",
        sent_message_id=sent_message_id,
        send_status=derived_status,
        send_note=note,
        to_email=to_email,
    )
    return ReplyResponse(mode=mode, entry=entry, note=note)


# ---------------------------------------------------------------------------
# Round-2 additions: per-message audit + preview-before-send
# ---------------------------------------------------------------------------

@dataclass
class GmailPayloadView:
    """Friendly + raw view of what was (or would be) handed to Gmail."""

    from_email: str
    to_email: str
    subject: str
    body: str
    raw_base64: str | None
    would_send_via: str  # "service-account-DWD" | "(not configured)"


@dataclass
class EntryAuditResponse:
    entry_id: str
    message: dict[str, Any] | None
    email_draft: dict[str, Any] | None
    activity: dict[str, Any] | None
    gmail_payload: GmailPayloadView | None
    send_status: str
    send_note: str | None


def _build_gmail_payload_view(
    *,
    to_email: str | None,
    subject: str | None,
    body: str,
) -> GmailPayloadView:
    """Construct the would-be-sent payload. Always computes the base64
    raw form so the audit drawer can show exactly what hits the wire."""
    settings = get_settings()
    delegated = settings.gmail_delegated_user or ""
    configured = bool(settings.gmail_service_account_path and settings.gmail_delegated_user)
    # Defensive — surface the user-meaningful subject even when the draft
    # row was never created (e.g., dev-only inbound entry).
    subj = subject or "(no subject)"
    to = to_email or ""
    # Local import keeps this module testable without google libs installed.
    from app.services.email.gmail_client import build_message

    built = build_message(to=to, subject=subj, body=body, from_email=delegated or None)
    return GmailPayloadView(
        from_email=built.from_email,
        to_email=built.to_email,
        subject=built.subject,
        body=built.body,
        raw_base64=built.raw_base64,
        would_send_via="service-account-DWD" if configured else "(not configured)",
    )


async def load_entry_audit(
    db: AsyncSession,
    *,
    loan_id: UUID,
    entry_id: str,
    viewer: User,
) -> EntryAuditResponse:
    """Return everything we know about a single thread entry — Message
    row, matched EmailDraft, matched Activity, and the would-be Gmail
    API payload (computed fresh; not cached).

    `entry_id` is either a Message UUID or `draft:<EmailDraft UUID>` for
    pending drafts. Super-admin / loan-exec only — caller enforces role.
    """
    if viewer.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise LenderThreadError(
            "Only super_admin and loan_exec can view raw send audit."
        )
    loan = await _load_loan(db, loan_id)

    # Case A: pending-draft entry
    if entry_id.startswith("draft:"):
        draft_uuid = UUID(entry_id.split(":", 1)[1])
        draft = (
            await db.execute(select(EmailDraft).where(EmailDraft.id == draft_uuid))
        ).scalar_one_or_none()
        if draft is None or draft.loan_id != loan.id:
            raise LenderThreadError("Draft not found on this loan.")
        gmail_view = _build_gmail_payload_view(
            to_email=draft.to_email, subject=draft.subject, body=draft.body
        )
        return EntryAuditResponse(
            entry_id=entry_id,
            message=None,
            email_draft=_email_draft_to_dict(draft),
            activity=None,
            gmail_payload=gmail_view,
            send_status="saved",
            send_note="Pending draft — not sent.",
        )

    # Case B: normal Message entry
    try:
        msg_uuid = UUID(entry_id)
    except ValueError as exc:
        raise LenderThreadError(f"Invalid entry_id: {entry_id!r}") from exc
    msg = (
        await db.execute(select(Message).where(Message.id == msg_uuid))
    ).scalar_one_or_none()
    if msg is None or msg.loan_id != loan.id:
        raise LenderThreadError("Message not found on this loan.")

    # Find matched draft + activity by 60s window.
    draft_rows = (
        await db.execute(
            select(EmailDraft)
            .where(EmailDraft.loan_id == loan.id)
            .order_by(EmailDraft.created_at.asc())
        )
    ).scalars().all()
    outbound_drafts = [d for d in draft_rows if d.status != EmailDraftStatus.PENDING]
    _, matched_draft = _resolve_outbound_sender(msg, outbound_drafts)

    activity_rows = (
        await db.execute(
            select(Activity)
            .where(Activity.loan_id == loan.id)
            .where(Activity.kind.in_(["lender_thread.replied", "lender_thread.ai_replied", "email.inbound"]))
            .order_by(Activity.occurred_at.asc())
        )
    ).scalars().all()
    matched_activity: Activity | None = None
    best_delta = _SENDER_MATCH_WINDOW
    for a in activity_rows:
        if a.occurred_at is None:
            continue
        delta = abs(msg.sent_at - a.occurred_at)
        if delta <= best_delta:
            best_delta = delta
            matched_activity = a

    status, _ = _derive_send_status(matched_draft)
    note: str | None = None
    if matched_activity and matched_activity.payload:
        n = matched_activity.payload.get("note")
        note = str(n) if n else None

    # Gmail payload — for inbound entries we still render what an OUTBOUND
    # reply to this lender would look like, but flag it as a preview.
    gmail_view: GmailPayloadView | None = None
    if msg.from_role != MessageFrom.LENDER:
        # Outbound — show exactly what we sent (or would send).
        gmail_view = _build_gmail_payload_view(
            to_email=matched_draft.to_email if matched_draft else None,
            subject=matched_draft.subject if matched_draft else None,
            body=msg.body,
        )

    return EntryAuditResponse(
        entry_id=entry_id,
        message=_message_to_dict(msg),
        email_draft=_email_draft_to_dict(matched_draft) if matched_draft else None,
        activity=_activity_to_dict(matched_activity) if matched_activity else None,
        gmail_payload=gmail_view,
        send_status=status,
        send_note=note,
    )


def _message_to_dict(m: Message) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "from_role": str(m.from_role),
        "body": m.body,
        "is_draft": m.is_draft,
        "is_system": m.is_system,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
    }


def _email_draft_to_dict(d: EmailDraft) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "to_email": d.to_email,
        "cc_emails": d.cc_emails,
        "bcc_emails": d.bcc_emails,
        "subject": d.subject,
        "body": d.body,
        "status": str(d.status),
        "actioned_by": d.actioned_by,
        "sent_message_id": d.sent_message_id,
        "triggered_by_kind": d.triggered_by_kind,
        "triggered_by_payload": d.triggered_by_payload,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _activity_to_dict(a: Activity) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "kind": a.kind,
        "summary": a.summary,
        "payload": a.payload,
        "actor_label": a.actor_label,
        "occurred_at": a.occurred_at.isoformat() if a.occurred_at else None,
    }


# --- Preview-before-send -----------------------------------------------------

@dataclass
class PreviewResponse:
    mode: ReplyMode
    to_email: str
    subject: str
    body: str
    gmail_payload: GmailPayloadView
    gmail_ready: bool
    gmail_status_note: str


async def preview_reply(
    db: AsyncSession,
    *,
    loan_id: UUID,
    actor: User,
    mode: ReplyMode,
    text: str,
) -> PreviewResponse:
    """Return exactly what would be transmitted if this reply were sent
    NOW — without writing anything. For `instruct_ai`, this also runs
    the LLM draft step so the operator can review the AI's output.

    Same role gate as post_reply: super_admin + loan_exec only."""
    if actor.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise LenderThreadError(
            "Only super_admin and loan_exec can preview a lender reply."
        )
    if mode not in ("send_now", "instruct_ai", "save_draft"):
        raise LenderThreadError(f"Unknown reply mode: {mode!r}")
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

    subject_base = f"Re: {loan.address}"
    subject = inject_deal_id(subject_base, loan.deal_id)

    if mode == "instruct_ai":
        thread = await load_thread(db, loan_id=loan_id, viewer=actor)
        thread_text = _format_thread_for_llm(thread.entries)
        body = await _ai_draft_from_instruction(
            db=db,
            loan_id=loan.id,
            client_id=loan.client_id,
            instruction=text,
            thread_text=thread_text,
            deal_id=loan.deal_id,
            address=loan.address,
            lender_contact_name=lender.contact_name,
        )
    else:
        body = text.strip()

    gmail_view = _build_gmail_payload_view(
        to_email=to_email, subject=subject, body=body
    )
    settings = get_settings()
    gmail_ready = bool(settings.gmail_service_account_path and settings.gmail_delegated_user)
    if gmail_ready:
        status_note = f"Gmail ready — will send via {settings.gmail_delegated_user}."
    else:
        status_note = (
            "Gmail not configured. The message will be saved locally with status "
            "APPROVED but will NOT be delivered to the lender."
        )

    return PreviewResponse(
        mode=mode,
        to_email=to_email,
        subject=subject,
        body=body,
        gmail_payload=gmail_view,
        gmail_ready=gmail_ready,
        gmail_status_note=status_note,
    )


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
