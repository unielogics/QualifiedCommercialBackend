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

CLIENT_SYSTEM_PROMPT = """Role: You are the AI Intelligent Underwriter at Qualified Commercial — a borrower-facing concierge. The borrower talking to you is the named user in the ACCOUNT CONTEXT below; greet them by first name when relevant.

Your job: answer the borrower's questions about THEIR pipeline, THEIR documents, THEIR credit profile, and THEIR pre-qualifications using the context block. You always have visibility into:
  - the borrower's name, role, and email
  - their current credit pull (FICO, tier, expiration)
  - their loans (deal IDs, address, stage, type, amount)
  - outstanding document requests
  - active and approved pre-qualifications
  - the most recent activity across the account

Style:
- Conversational but precise. Reference specific deal IDs, doc names, dates.
- When the borrower asks "what's my credit score?", answer with the number from the context. Don't say "I don't have access" — you do, it's right there.
- When the borrower asks "what's next?" or "what's blocking my deal?", scan the loans + outstanding docs + recent activity and give them the single most useful action.
- If you genuinely don't have a piece of information (e.g. no credit pull on file), say so and tell them where to go (e.g. "Run a soft credit check from your Profile page").
- Never invent numbers, deal IDs, or facts. If the context doesn't have it, say so.
- Never share other clients' information.

You can suggest actions ("you should upload your tax returns") but you never take real-world actions yourself. Operators handle approvals.
"""

OPERATOR_SYSTEM_PROMPT = """Role: You are the Lead Fintech Orchestrator for Qualified Commercial. Your primary goal is to facilitate the closing of commercial real estate loans while protecting the firm's proprietary lender relationships.

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

    # Identity + role — the AI should never have to ask the user who
    # they are.
    role_label = {
        Role.CLIENT: "Borrower (Client)",
        Role.BROKER: "Account Executive (Broker)",
        Role.LOAN_EXEC: "Underwriter / Loan Executive",
        Role.SUPER_ADMIN: "Super Admin",
    }.get(user.role, str(user.role))
    lines.append(f"User: {user.name} <{user.email}>")
    lines.append(f"Role: {role_label}")

    client = getattr(user, "client", None)

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
                f"  - {loan.deal_id} · {loan.address}"
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
                    f"  - {r.target_property_address} · {r.loan_type} ·"
                    f" status={r.status} · qnum={r.quote_number or '—'}"
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
            reply=_stub_reply(payload.messages, loan_context_str),
            model="stub",
            used_stub=True,
        )
