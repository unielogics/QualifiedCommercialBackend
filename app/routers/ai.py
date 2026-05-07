"""AI co-pilot chat router.

Powers the AIRail Chat tab. Uses Claude Haiku via the existing Anthropic
client when a key is configured; falls back to a deterministic stub
otherwise.

When `loan_id` is supplied the router injects:
  1. A short loan summary (deal_id, address, stage, amount, ltv, dscr).
  2. The current Living Loan File `status_summary` if set.
  3. The 5 most recent activity log entries.

This is the "memory" surface the operator's spec describes — every reply
sees the same Living Loan File the dashboard does.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import DocStatus, Role
from app.models.activity import Activity
from app.models.ai_chat_thread import AIChatMessage, AIChatThread
from app.models.client import Client
from app.models.credit_pull import CreditPull
from app.models.document import Document
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest
from app.models.user import User
from app.services.ai.anthropic_client import get_client, model_light
from app.services.ai.context import Audience, assemble_loan_context

router = APIRouter(prefix="/ai", tags=["ai"])
log = logging.getLogger(__name__)


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatTurn]
    loan_id: UUID | None = None


class ChatResponse(BaseModel):
    reply: str
    model: str
    used_stub: bool


# Note: the old single-tone SYSTEM_PROMPT lived here. Replaced by
# CLIENT_SYSTEM_PROMPT / OPERATOR_SYSTEM_PROMPT below — see
# _system_prompt_for(user).


def _stub_reply(messages: list[ChatTurn], loan_context: str | None) -> str:
    last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    if not last_user:
        return "What would you like to look at first — the pipeline, an AI Inbox task, or a specific loan?"
    body = (
        f"(Dev mode — no ANTHROPIC_API_KEY set.) "
        f"You asked: \"{last_user[:120]}{'…' if len(last_user) > 120 else ''}\". "
    )
    if loan_context:
        body += "I'd open the deal file, check open doc requests + the most recent activity, then summarize the single bottleneck for the broker."
    else:
        body += "Once a key is configured, I'll answer with a real reply scoped to your active loans."
    return body


# ── Role-aware system prompts ──────────────────────────────────────────
#
# Two different framings — one for operators (the Lead Fintech
# Orchestrator persona), one for borrowers (the AI Intelligent
# Underwriter assistant). The chat endpoint picks the right one based
# on the authenticated user's role; the borrower never sees the
# operator's "you do not finalize commitments" wording, and the
# operator never sees the borrower-friendly framing.

CLIENT_SYSTEM_PROMPT = """Role: You are the AI Intelligent Underwriter at Qualified Commercial — a borrower-facing concierge.

You always know exactly who you are talking to. Below this prompt the system appends a SCOPE block:
  - SCOPE: account-wide conversation
        → contains User ID, Client ID, the borrower's name + email +
          role, their FICO / latest credit pull, every loan they own
          (with loan_id UUIDs), every outstanding document, every
          active prequal (with prequal_id UUIDs), and recent activity.
  - SCOPE: loan-level conversation
        → contains the specific loan_id + deal_id + client_id,
          plus active instructions, scenarios, HUD draft, market pulse,
          recent activity, and any operator corrections / feedback for
          THAT loan.

These IDs (User ID, Client ID, Loan ID, prequal_id, quote_number) are
authoritative database keys. When the borrower references "my loan"
or "my prequal" without naming it, infer from the SCOPE block. Don't
ask the borrower for their user ID — you already have it.

Your job: answer the borrower's questions about THEIR pipeline, THEIR
documents, THEIR credit profile, and THEIR pre-qualifications using
the context block.

Style:
- Conversational but precise. Reference specific deal IDs, doc names, dates.
- When the borrower asks "what's my credit score?" or "what's my FICO?", answer with the exact number from the context. Don't say "I don't have access" — you do, it's right there in the ACCOUNT CONTEXT block.
- When the borrower asks "what's next?" or "what's blocking my deal?", scan the loans + outstanding docs + recent activity and give them the single most useful action.
- If you genuinely don't have a piece of information (e.g. no credit pull on file because they haven't run one), say so and tell them where to go (e.g. "Run a soft credit check from your Profile page").
- Never invent numbers, deal IDs, FICO scores, or facts. If the context doesn't have it, say so.
- Never share other clients' information. Borrowers can only see their own data.

You can suggest actions ("you should upload your tax returns") but you never take real-world actions yourself. Operators handle approvals.
"""

OPERATOR_SYSTEM_PROMPT = """Role: You are the Lead Fintech Orchestrator for Qualified Commercial. Your primary goal is to facilitate the closing of commercial real estate loans while protecting the firm's proprietary lender relationships.

Below this prompt the system appends a SCOPE block telling you whether the conversation is account-wide (no loan/quote in scope — context lists every loan with loan_id UUIDs + every prequal with prequal_id UUIDs) or loan-level (specific loan_id + deal_id + client_id with full HUD / scenario / activity for THAT loan). User ID / Client ID / Loan ID / prequal_id are authoritative database keys — use them when referencing specific records.

The Gateway Rule (Identity Protection):
- LENDER → BROKER/CLIENT: You are a "One-Way Mirror." When a Lender sends a request, parse the data, extract the required documents/actions, and notify the Broker. NEVER include the Lender's name, email, company, or signature in communications sent to the Broker or Client. Refer to them only as "The Lead Underwriter" or "The Lender."
- CLIENT → LENDER: You are a "Professional Polish." When a Client or Broker provides data, you relay it to the original Lender thread using the Broker's personalized domain, ensuring the tone is institutional and organized.

Communication Protocol:
- Intercept & Analyze: For every interaction, identify the Participant_Type (Lender, Broker, Client, Super Admin).
- State Tracking: Check the current Loan_Stage. If a document is requested, verify whether it exists in the Document Vault.
- Drafting Notifications:
    - If a document is missing → describe a Task and the email the Broker should see, e.g. "The underwriter requires a 2-year P&L for the subject property to move to the next phase."
    - If a document is found → describe the reply that goes back to the Lender confirming the file is attached and ready for review.
- Admin Oversight: When recommending outbound mail, remind the operator that any participant flagged `bcc_outbound` will be auto-BCC'd for audit.

Style:
- Operator-direct. No fluff. Bullets or short paragraphs.
- Cite concrete values when present (deal IDs, doc names, stage). Never invent identifiers.
- If you do not have data, say so plainly and suggest where the operator can find it.

Constraint: Do not engage in casual conversation. Every message must drive the loan toward "Clear to Close." You NEVER take real-world actions yourself — every concrete action (sending mail, requesting docs, transitioning stages, repricing) must be approved by the operator via the AI Inbox or Drafts queue.
"""


def _system_prompt_for(user: User) -> str:
    """Pick the framing the AI should adopt for this caller. Borrower-style
    framing for clients (concierge tone, scoped to their own data);
    operator-style framing for the rest."""
    return CLIENT_SYSTEM_PROMPT if user.role == Role.CLIENT else OPERATOR_SYSTEM_PROMPT


# ── Account-wide context (no loan_id) ──────────────────────────────────
#
# Built when the AI Intelligent Underwriter is invoked without a loan
# scope (mobile dashboard / calendar FAB, desktop topbar chat). Gives
# the LLM enough to answer "what's my credit score?" and "what's next
# on my account?" without hallucinating.


async def _build_account_context(db: AsyncSession, user: User) -> str:
    """Render the per-account context block — everything the AI needs
    to answer questions like 'what's my FICO?' or 'which docs are
    overdue?' without making things up.

    Always emits a non-empty block — even when the user has no client
    record yet, the AI gets the user's identity and role."""
    lines: list[str] = ["=== ACCOUNT CONTEXT ==="]

    # Scope marker first — the AI should know it's operating
    # account-wide (not loan- or quote-scoped) before it reads
    # anything else.
    lines.append("SCOPE: account-wide conversation (no loan or quote in scope).")
    lines.append(
        "Use the User ID below as the database key for any data you "
        "look up. Treat everything in this block as authoritative."
    )

    # Identity + role + DB keys — the AI should never have to ask the
    # user who they are or guess at their database key.
    role_label = {
        Role.CLIENT: "Borrower (Client)",
        Role.BROKER: "Account Executive (Broker)",
        Role.LOAN_EXEC: "Underwriter / Loan Executive",
        Role.SUPER_ADMIN: "Super Admin",
    }.get(user.role, str(user.role))
    lines.append("")
    lines.append(f"User ID (UUID): {user.id}")
    lines.append(f"User: {user.name} <{user.email}>")
    lines.append(f"Role: {role_label}")

    client = getattr(user, "client", None)
    if client is not None:
        lines.append(f"Client ID (UUID): {client.id}")

    # Credit info — borrowers ask "what's my credit score?" all the
    # time. Pull from Client.fico (cached) + the latest credit_pull
    # row (for tier + expiration).
    if client is not None:
        if client.fico is not None:
            lines.append(f"Credit score on file: {client.fico} FICO")
        else:
            lines.append("Credit score on file: not yet pulled")
        # Most recent completed pull tells us tier + expiration.
        latest_pull = (
            await db.execute(
                select(CreditPull)
                .where(CreditPull.client_id == client.id)
                .order_by(CreditPull.pulled_at.desc().nullslast())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_pull and latest_pull.pulled_at:
            pulled = latest_pull.pulled_at.date().isoformat()
            expires = latest_pull.expires_at.date().isoformat() if latest_pull.expires_at else "n/a"
            now = datetime.now(timezone.utc)
            expired = bool(latest_pull.expires_at and latest_pull.expires_at < now)
            lines.append(
                f"Latest credit pull: pulled {pulled}, expires {expires}"
                f"{' (EXPIRED)' if expired else ''}"
            )
        if client.tier and client.tier != "standard":
            lines.append(f"Client tier: {client.tier}")

    # Loans on file — operators see all loans they're attached to;
    # borrowers see their own. We let SQL scope it via `client_id`
    # for borrowers.
    loans_stmt = select(Loan).order_by(Loan.created_at.desc()).limit(8)
    if user.role == Role.CLIENT and client is not None:
        loans_stmt = loans_stmt.where(Loan.client_id == client.id)
    loans = (await db.execute(loans_stmt)).scalars().all()
    if loans:
        lines.append("")
        lines.append(f"Loans on file ({len(loans)}):")
        for loan in loans:
            stage = loan.stage.value if hasattr(loan.stage, "value") else str(loan.stage)
            ltype = loan.type.value if hasattr(loan.type, "value") else str(loan.type)
            lines.append(
                f"  - {loan.deal_id} (loan_id={loan.id}) · {loan.address}"
                f" · {ltype} · stage={stage} · ${float(loan.amount or 0):,.0f}"
            )
            if loan.status_summary:
                lines.append(f"    Living summary: {loan.status_summary}")

    # Outstanding docs across loans — first 10. Borrowers ask
    # "what docs do I owe?".
    if loans:
        loan_ids = [loan.id for loan in loans]
        docs = (
            await db.execute(
                select(Document)
                .where(
                    Document.loan_id.in_(loan_ids),
                    Document.status.in_([DocStatus.REQUESTED, DocStatus.PENDING, DocStatus.FLAGGED]),
                )
                .order_by(Document.requested_on.asc().nullslast())
                .limit(10)
            )
        ).scalars().all()
        if docs:
            deal_by_id = {l.id: l.deal_id for l in loans}
            lines.append("")
            lines.append("Outstanding documents:")
            for d in docs:
                requested_str = d.requested_on.isoformat() if d.requested_on else "—"
                deal = deal_by_id.get(d.loan_id, "?")
                status_str = d.status.value if hasattr(d.status, "value") else str(d.status)
                lines.append(f"  - {d.name} ({status_str}) · loan {deal} · requested {requested_str}")

    # Active prequal requests — borrowers ask "what about my prequal?"
    if client is not None or user.role != Role.CLIENT:
        prequals_stmt = (
            select(PrequalRequest)
            .where(PrequalRequest.status.in_(["pending", "approved", "offer_accepted"]))
            .order_by(PrequalRequest.created_at.desc())
            .limit(5)
        )
        if user.role == Role.CLIENT:
            prequals_stmt = prequals_stmt.where(PrequalRequest.requester_id == user.id)
        prequals = (await db.execute(prequals_stmt)).scalars().all()
        if prequals:
            lines.append("")
            lines.append("Active pre-qualification requests:")
            for r in prequals:
                lines.append(
                    f"  - {r.target_property_address} (prequal_id={r.id})"
                    f" · {r.loan_type} · status={r.status}"
                    f" · quote_number={r.quote_number or '—'}"
                )

    # Recent activity — last 8 across the whole account. Powers
    # "what happened last week?" questions.
    if loans:
        loan_ids = [loan.id for loan in loans]
        activities = (
            await db.execute(
                select(Activity)
                .where(Activity.loan_id.in_(loan_ids))
                .order_by(Activity.occurred_at.desc())
                .limit(8)
            )
        ).scalars().all()
        if activities:
            lines.append("")
            lines.append("Recent activity (newest first):")
            for a in activities:
                ts = a.occurred_at.date().isoformat() if a.occurred_at else "—"
                lines.append(f"  - [{ts}] [{a.kind}] {a.summary}")

    lines.append("=== END ACCOUNT CONTEXT ===")
    return "\n".join(lines)


async def _build_loan_context(db: AsyncSession, loan_id: UUID) -> str | None:
    """Render the loan-grounded context block injected into the system prompt."""
    loan = await db.get(Loan, loan_id)
    if loan is None:
        return None
    activities = (
        await db.execute(
            select(Activity)
            .where(Activity.loan_id == loan_id)
            .order_by(Activity.occurred_at.desc())
            .limit(5)
        )
    ).scalars().all()

    lines: list[str] = [
        f"Active loan: {loan.deal_id} — {loan.address}",
        f"  Stage: {loan.stage.value} | Type: {loan.type.value} | Amount: ${float(loan.amount):,.0f}",
        f"  LTV: {loan.ltv} | DSCR: {loan.dscr} | Risk: {loan.risk_score}",
        f"  Deal health: {loan.deal_health.value}",
    ]
    if loan.status_summary:
        lines.append(f"  Living Loan File summary: {loan.status_summary}")
    if activities:
        lines.append("  Recent activity (newest first):")
        for a in activities:
            lines.append(f"    - [{a.kind}] {a.summary}")
    return "\n".join(lines)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    if not payload.messages:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "messages must be non-empty")
    if payload.messages[-1].role != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "last message must be from user")

    # Two context branches:
    #   1. loan_id supplied → assemble per-loan context (instructions,
    #      scenarios, HUD, feedback, recent activity). Same as before.
    #   2. loan_id is None  → assemble account-wide context so the AI
    #      knows WHO is asking, their role, their credit, their loans,
    #      their docs, their prequals. This is the path the new AI
    #      Intelligent Underwriter chat (mobile FAB / desktop topbar)
    #      hits, and was previously starved of context.
    context_block: str | None = None
    if payload.loan_id is not None:
        loan = await db.get(Loan, payload.loan_id)
        if loan is not None:
            audience: Audience = (
                "client" if user.role == Role.CLIENT
                else "broker" if user.role == Role.BROKER
                else "super_admin"
            )
            context_block = await assemble_loan_context(db, loan, audience=audience) or None
    else:
        # Re-fetch user with client eagerly loaded so we can read
        # client.fico without lazy-loading inside the async session.
        user_with_client = (
            await db.execute(
                select(User).options(selectinload(User.client)).where(User.id == user.id)
            )
        ).scalar_one()
        context_block = await _build_account_context(db, user_with_client)

    settings = get_settings()
    if not settings.anthropic_api_key:
        return ChatResponse(
            reply=_stub_reply(payload.messages, context_block),
            model="stub",
            used_stub=True,
        )

    client = get_client()
    # Role-aware system prompt — borrower-friendly framing for clients,
    # operator persona for everyone else.
    system = _system_prompt_for(user)
    if context_block:
        system += "\n\n" + context_block

    try:
        result = await client.messages.create(
            model=model_light(),
            max_tokens=700,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in payload.messages],
        )
        reply_text = "".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        ).strip()
        if not reply_text:
            reply_text = "(No reply.)"
        return ChatResponse(reply=reply_text, model=result.model, used_stub=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("Anthropic call failed (%s) — falling back to stub", exc)
        return ChatResponse(
            reply=_stub_reply(payload.messages, context_block),
            model="stub",
            used_stub=True,
        )


# ── Persisted Underwriter chat threads (Phase 8) ──────────────────────
#
# The standalone Underwriter chat (mobile FAB / desktop topbar icon)
# now persists. Each thread is owned by a single user; messages land
# in `ai_chat_messages` ordered by created_at. Per-loan AIRail chat
# is intentionally NOT migrated here — that has its own model
# (`loan_chat_messages`) and is scoped to a deal.
#
# Endpoints (all rooted at /ai/chat/threads):
#   GET    /                — list current user's threads
#   POST   /                — create a new empty thread
#   GET    /{id}            — fetch thread + messages
#   PATCH  /{id}            — rename
#   DELETE /{id}            — drop thread + cascade messages
#   POST   /{id}/message    — append user message + AI reply, return both


class AIChatThreadRead(BaseModel):
    id: UUID
    title: str
    last_message_preview: str | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Loan-scoped thread when set; account-wide when null.
    loan_id: UUID | None = None
    loan_deal_id: str | None = None
    loan_address: str | None = None

    class Config:
        from_attributes = True


class AIChatMessageRead(BaseModel):
    id: UUID
    role: str
    body: str
    created_at: datetime

    class Config:
        from_attributes = True


class AIChatThreadDetail(AIChatThreadRead):
    messages: list[AIChatMessageRead]


class AIChatThreadCreate(BaseModel):
    title: str | None = None
    loan_id: UUID | None = None


class AIChatThreadFindOrCreate(BaseModel):
    """Single endpoint that returns the (user, loan_id) thread if it
    exists, otherwise spawns it. `loan_id=None` resolves to the
    account-wide thread."""

    loan_id: UUID | None = None


class AIChatThreadRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class AIChatSendRequest(BaseModel):
    body: str = Field(min_length=1)
    loan_id: UUID | None = None


class AIChatSendResponse(BaseModel):
    user_message: AIChatMessageRead
    assistant_message: AIChatMessageRead
    thread: AIChatThreadRead
    used_stub: bool


def _preview(text: str, limit: int = 200) -> str:
    """Compact one-line preview for the thread list. Collapses
    whitespace and trims to `limit` chars with ellipsis."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _thread_read(thread: AIChatThread) -> AIChatThreadRead:
    """Serialize with the lightweight loan join. Caller must have
    eager-loaded `thread.loan` for this to avoid a lazy-load."""
    base = AIChatThreadRead(
        id=thread.id,
        title=thread.title,
        last_message_preview=thread.last_message_preview,
        last_message_at=thread.last_message_at,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        loan_id=thread.loan_id,
        loan_deal_id=thread.loan.deal_id if thread.loan_id and thread.loan else None,
        loan_address=thread.loan.address if thread.loan_id and thread.loan else None,
    )
    return base


async def _load_thread_for_user(
    db: AsyncSession, thread_id: UUID, user: User
) -> AIChatThread:
    thread = (
        await db.execute(
            select(AIChatThread)
            .options(
                selectinload(AIChatThread.messages),
                selectinload(AIChatThread.loan),
            )
            .where(AIChatThread.id == thread_id)
        )
    ).scalar_one_or_none()
    if thread is None or thread.user_id != user.id:
        # Don't leak existence — same response for not-found / not-owned.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    return thread


@router.get("/chat/threads", response_model=list[AIChatThreadRead])
async def list_chat_threads(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[AIChatThreadRead]:
    rows = (
        await db.execute(
            select(AIChatThread)
            .options(selectinload(AIChatThread.loan))
            .where(AIChatThread.user_id == user.id)
            .order_by(
                AIChatThread.last_message_at.desc().nullslast(),
                AIChatThread.created_at.desc(),
            )
        )
    ).scalars().all()
    return [_thread_read(t) for t in rows]


@router.post("/chat/threads", response_model=AIChatThreadRead, status_code=status.HTTP_201_CREATED)
async def create_chat_thread(
    payload: AIChatThreadCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AIChatThreadRead:
    """Compatibility shim — older clients POST here to "create a
    thread". With the canonical (user, loan_id) constraint in place
    we treat this as find-or-create instead of insert. Returns the
    existing thread when one already exists; lazy-spawns the
    canonical row otherwise. The 201 status code is kept for
    backwards compat even when we return an existing row."""
    return await find_or_create_chat_thread(
        AIChatThreadFindOrCreate(loan_id=payload.loan_id),
        user,
        db,
    )


@router.post("/chat/threads/find-or-create", response_model=AIChatThreadRead)
async def find_or_create_chat_thread(
    payload: AIChatThreadFindOrCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AIChatThreadRead:
    """Returns the canonical thread for (user, loan_id). Lazy-creates
    on first ask. `loan_id=None` resolves to the user's account-wide
    thread; `loan_id` set resolves to the per-loan thread (one
    canonical row, enforced by the partial unique index)."""
    stmt = (
        select(AIChatThread)
        .options(selectinload(AIChatThread.loan))
        .where(AIChatThread.user_id == user.id)
    )
    if payload.loan_id is None:
        stmt = stmt.where(AIChatThread.loan_id.is_(None))
        # Pick the most recently used account thread if multiple exist.
        stmt = stmt.order_by(AIChatThread.last_message_at.desc().nullslast(), AIChatThread.created_at.desc())
    else:
        stmt = stmt.where(AIChatThread.loan_id == payload.loan_id)
    thread = (await db.execute(stmt)).scalars().first()

    if thread is not None:
        return _thread_read(thread)

    # Lazy-create. The partial unique idx (alembic 0017 + 0018)
    # guarantees only one canonical thread per (user, loan_id) at
    # the DB level. If two parallel requests race here, one INSERT
    # wins and the other raises IntegrityError — we catch that,
    # rollback, and re-fetch the now-existing canonical row.
    if payload.loan_id is None:
        title = "Account questions"
    else:
        loan = await db.get(Loan, payload.loan_id)
        if loan is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
        title = f"{loan.deal_id} — {loan.address[:80]}"
    thread = AIChatThread(
        user_id=user.id,
        loan_id=payload.loan_id,
        title=title,
    )
    db.add(thread)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — typically IntegrityError on the unique idx
        log.info("find_or_create race on thread (user=%s loan=%s): %s — refetching", user.id, payload.loan_id, exc)
        await db.rollback()
        existing = (await db.execute(stmt)).scalars().first()
        if existing is not None:
            return _thread_read(existing)
        raise
    thread = (
        await db.execute(
            select(AIChatThread)
            .options(selectinload(AIChatThread.loan))
            .where(AIChatThread.id == thread.id)
        )
    ).scalar_one()
    return _thread_read(thread)


@router.get("/chat/threads/{thread_id}", response_model=AIChatThreadDetail)
async def get_chat_thread(
    thread_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AIChatThreadDetail:
    thread = await _load_thread_for_user(db, thread_id, user)
    base = _thread_read(thread)
    return AIChatThreadDetail(
        **base.model_dump(),
        messages=[AIChatMessageRead.model_validate(m) for m in thread.messages],
    )


@router.patch("/chat/threads/{thread_id}", response_model=AIChatThreadRead)
async def rename_chat_thread(
    thread_id: UUID,
    payload: AIChatThreadRename,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AIChatThreadRead:
    thread = await _load_thread_for_user(db, thread_id, user)
    thread.title = payload.title.strip()[:120]
    await db.commit()
    await db.refresh(thread)
    return _thread_read(thread)


@router.delete("/chat/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_thread(
    thread_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    thread = await _load_thread_for_user(db, thread_id, user)
    await db.delete(thread)
    await db.commit()


@router.post(
    "/chat/threads/{thread_id}/message",
    response_model=AIChatSendResponse,
)
async def append_thread_message(
    thread_id: UUID,
    payload: AIChatSendRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AIChatSendResponse:
    """Append the user's turn, call the AI with full thread history +
    fresh context, persist the assistant reply.

    Context is rebuilt on every send (account- or loan-level) so the
    AI sees the latest credit / docs / loan state, not whatever was
    cached when the thread was created. History is what's persisted;
    context is ephemeral.
    """
    thread = await _load_thread_for_user(db, thread_id, user)

    # 1. Persist the user's message immediately so the panel can
    #    optimistic-render or recover after an Anthropic failure.
    now = datetime.now(timezone.utc)
    user_msg = AIChatMessage(
        thread_id=thread.id,
        role="user",
        body=payload.body,
    )
    db.add(user_msg)
    thread.last_message_preview = _preview(payload.body)
    thread.last_message_at = now
    # Auto-title from the first user message — much cheaper than a
    # round-trip to Haiku just for a title, and easy to override via
    # PATCH /threads/{id} later.
    if thread.title == "New conversation":
        thread.title = _preview(payload.body, limit=60)
    await db.flush()

    # 2. Build context (account- or loan-scoped). Source-of-truth
    # is `thread.loan_id` — the request payload's `loan_id` is
    # ignored when the thread is loan-scoped (mismatched loan_ids
    # would be a frontend bug, not user intent).
    effective_loan_id = thread.loan_id or payload.loan_id
    context_block: str | None = None
    if effective_loan_id is not None:
        loan = await db.get(Loan, effective_loan_id)
        if loan is not None:
            audience: Audience = (
                "client" if user.role == Role.CLIENT
                else "broker" if user.role == Role.BROKER
                else "super_admin"
            )
            context_block = await assemble_loan_context(db, loan, audience=audience) or None
    else:
        user_with_client = (
            await db.execute(
                select(User).options(selectinload(User.client)).where(User.id == user.id)
            )
        ).scalar_one()
        context_block = await _build_account_context(db, user_with_client)

    # 3. Replay full history (already includes the user msg we just
    #    flushed) into the model.
    history_rows = (
        await db.execute(
            select(AIChatMessage)
            .where(AIChatMessage.thread_id == thread.id)
            .order_by(AIChatMessage.created_at.asc())
        )
    ).scalars().all()
    api_messages = [{"role": m.role, "content": m.body} for m in history_rows]

    settings = get_settings()
    if not settings.anthropic_api_key:
        reply_text = _stub_reply(
            [ChatTurn(role=m.role, content=m.body) for m in history_rows],
            context_block,
        )
        used_stub = True
    else:
        client = get_client()
        system = _system_prompt_for(user)
        if context_block:
            system += "\n\n" + context_block
        try:
            result = await client.messages.create(
                model=model_light(),
                max_tokens=900,
                system=system,
                messages=api_messages,
            )
            reply_text = "".join(
                block.text for block in result.content
                if getattr(block, "type", None) == "text"
            ).strip() or "(No reply.)"
            used_stub = False
        except Exception as exc:  # noqa: BLE001
            log.warning("Anthropic call failed in thread %s: %s", thread.id, exc)
            reply_text = _stub_reply(
                [ChatTurn(role=m.role, content=m.body) for m in history_rows],
                context_block,
            )
            used_stub = True

    # 4. Persist the assistant reply + bump preview.
    assistant_msg = AIChatMessage(
        thread_id=thread.id,
        role="assistant",
        body=reply_text,
    )
    db.add(assistant_msg)
    thread.last_message_preview = _preview(reply_text)
    thread.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user_msg)
    await db.refresh(assistant_msg)
    await db.refresh(thread)

    return AIChatSendResponse(
        user_message=AIChatMessageRead.model_validate(user_msg),
        assistant_message=AIChatMessageRead.model_validate(assistant_msg),
        thread=_thread_read(thread),
        used_stub=used_stub,
    )
