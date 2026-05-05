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

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.activity import Activity
from app.models.loan import Loan
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


# Fintech Orchestrator role prompt — the canonical system prompt for QC.
SYSTEM_PROMPT = """Role: You are the Lead Fintech Orchestrator for Qualified Commercial. Your primary goal is to facilitate the closing of commercial real estate loans while protecting the firm's proprietary lender relationships.

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

    # New unified context assembly — pulls instructions, scenarios, HUD,
    # negative feedback, AI-modify corrections, and recent activity. The
    # legacy _build_loan_context() below is kept for any test that still
    # imports it but is no longer called from the live path.
    loan_context_str: str | None = None
    if payload.loan_id is not None:
        loan = await db.get(Loan, payload.loan_id)
        if loan is not None:
            audience: Audience = (
                "client" if user.role == Role.CLIENT
                else "broker" if user.role == Role.BROKER
                else "super_admin"
            )
            loan_context_str = await assemble_loan_context(db, loan, audience=audience) or None

    settings = get_settings()
    if not settings.anthropic_api_key:
        return ChatResponse(
            reply=_stub_reply(payload.messages, loan_context_str),
            model="stub",
            used_stub=True,
        )

    client = get_client()
    system = SYSTEM_PROMPT
    if loan_context_str:
        system += "\n\n" + loan_context_str

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
