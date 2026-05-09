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

import asyncio
import json
import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import date, datetime, timezone

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


# ── Property-intake tool (Phase C) ─────────────────────────────────────
#
# When a loan-scoped chat thread is open, the AI gets ONE tool —
# update_loan_property_details. The intake opener message
# (kicked off in loan_intake_automation.py) asks the borrower
# about beds / baths / sqft / year built / units / rent / taxes /
# insurance / HOA. As they reply across multiple turns, the model
# calls the tool to write structured facts back onto the Loan row.
#
# Cap iterations per send to bound spend + latency.

PROPERTY_INTAKE_TOOL = {
    "name": "update_loan_property_details",
    "description": (
        "Update structured property details on the loan when the borrower "
        "has answered. Call this every time you learn one or more new "
        "facts. Pass only the fields you've just learned — leave others "
        "unset. Numbers should be whole integers where the schema says "
        "integer, decimals where it says number. Do not call this with "
        "values you're guessing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "beds": {"type": "integer", "minimum": 0, "maximum": 50},
            "baths": {"type": "number", "minimum": 0, "maximum": 50},
            "sqft": {"type": "integer", "minimum": 0, "maximum": 1000000},
            "year_built": {"type": "integer", "minimum": 1700, "maximum": 2030},
            "unit_count": {"type": "integer", "minimum": 1, "maximum": 200},
            "monthly_rent": {"type": "number", "minimum": 0},
            "annual_taxes": {"type": "number", "minimum": 0},
            "annual_insurance": {"type": "number", "minimum": 0},
            "monthly_hoa": {"type": "number", "minimum": 0},
        },
    },
}

_PROPERTY_INTAKE_FIELDS = {
    "beds", "baths", "sqft", "year_built", "unit_count",
    "monthly_rent", "annual_taxes", "annual_insurance", "monthly_hoa",
}


# ── Conversational doc-collector tools (Phase B) ───────────────────────
#
# These three tools let the AI act like a secretary chasing the file:
# every reply can carry up to 5 buttons the borrower taps to jump
# straight into the right upload sheet. The tool handlers don't
# write much state — they accumulate ChatAction dicts which the
# tool-use loop attaches to the assistant message at the end of the
# turn.

REQUEST_DOCUMENT_UPLOAD_TOOL = {
    "name": "request_document_upload",
    "description": (
        "Render an 'Upload <doc>' button under your reply. Use this "
        "when you're asking the borrower to upload a specific file. "
        "Pass `document_id` if a REQUESTED Document row already "
        "exists (preferred — the upload routes straight into that "
        "slot); otherwise pass `checklist_key` and the borrower will "
        "be prompted to pick the file with that slot pre-selected. "
        "`label` is the button text; default is 'Upload <doc name>'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "checklist_key": {"type": "string"},
            "label": {"type": "string"},
        },
    },
}

CONFIRM_DOCUMENT_ROUTING_TOOL = {
    "name": "confirm_document_routing",
    "description": (
        "Render a 'Yes, file under <slot>' / 'No, pick another' "
        "button pair under your reply. Use this when the borrower "
        "uploaded a file via the chat composer and the vision scan "
        "suggested a slot you want to confirm before relinking. "
        "`document_id` is the orphan upload; `target_checklist_key` "
        "is the slot you're proposing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "target_checklist_key": {"type": "string"},
            "label": {"type": "string"},
        },
        "required": ["document_id", "target_checklist_key"],
    },
}

COMPLETE_PROPERTY_INTAKE_TOOL = {
    "name": "complete_property_intake",
    "description": (
        "Mark the property-intake interview complete. Call this only "
        "after you have collected at least beds, baths, sqft, "
        "year_built and unit_count via update_loan_property_details. "
        "Sets loans.intake_complete_at = now() so the chat moves on "
        "to doc collection."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

# Cap CTAs per assistant message so the chat doesn't turn into a
# wall of buttons. The remainder is always visible in the vault.
_MAX_ACTIONS_PER_MESSAGE = 5

# Hard cap on tool-use round-trips per send-message. A multi-fact
# borrower reply ("3 beds, 2 baths, 1500 sqft, built 1998") might
# trigger 1-2 tool calls; this is a safety valve, not the steady
# state.
_TOOL_USE_MAX_ITERATIONS = 5


async def _execute_property_intake_tool(
    db: AsyncSession,
    *,
    user: User,
    loan_id: UUID,
    tool_input: dict,
) -> dict:
    """Validate + persist the AI's tool call. Returns a small JSON
    object the AI sees as the tool_result content.

    Privilege check: the tool ONLY writes to a loan the calling
    user has scope on. CLIENT users must own the loan via their
    client record; operators (BROKER / SUPER_ADMIN / LOAN_EXEC)
    pass through. Anything else → no-op + tell the AI."""
    loan = await db.get(Loan, loan_id)
    if loan is None:
        return {"ok": False, "error": "loan_not_found"}

    # Borrower scope: must be the loan's client.
    if user.role == Role.CLIENT:
        if user.client is None or loan.client_id != user.client.id:
            return {"ok": False, "error": "not_authorized"}
    elif user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER):
        return {"ok": False, "error": "not_authorized"}

    # Whitelist + sanity-coerce.
    updated: list[str] = []
    for k, v in (tool_input or {}).items():
        if k not in _PROPERTY_INTAKE_FIELDS:
            continue
        if v is None:
            continue
        try:
            if k in ("beds", "sqft", "year_built", "unit_count"):
                value = int(v)
            else:
                value = float(v)
        except (TypeError, ValueError):
            continue
        setattr(loan, k, value)
        updated.append(k)

    if not updated:
        return {"ok": True, "updated": [], "note": "no_recognized_fields"}

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label="ai",
            kind="loan.property_intake_updated",
            summary=f"Property intake updated: {', '.join(updated)}",
            payload={"updated": updated, "values": {k: getattr(loan, k) for k in updated}},
        )
    )
    # Per-unit fan-out can change when unit_count moves up — we
    # rely on `materialize_kickoff_items` being idempotent
    # (re-runs add only the missing per-unit rows). Re-fire it to
    # backfill leases / rent rolls if the count went up.
    if "unit_count" in updated:
        try:
            from app.models.app_settings import AppSettings as _AppSettings
            from app.services.checklist_scheduler import materialize_kickoff_items
            from app.services.loan_intake_automation import _checklist_for, _coerce_settings

            settings_row = (
                await db.execute(select(_AppSettings).limit(1))
            ).scalar_one_or_none()
            settings_data = _coerce_settings(settings_row)
            checklist = _checklist_for(settings_data, str(loan.type))
            await materialize_kickoff_items(db, loan, checklist)
        except Exception:  # noqa: BLE001
            log.exception("property_intake: re-fan failed loan=%s", loan.id)

    await db.flush()
    return {"ok": True, "updated": updated}


async def _execute_request_document_upload_tool(
    db: AsyncSession,
    *,
    user: User,
    loan_id: UUID,
    tool_input: dict,
    accumulated_actions: list[dict],
) -> dict:
    """Resolve a request_document_upload tool call. Either a
    `document_id` (preferred) or a `checklist_key` must resolve to
    a Document on the same loan. Appends a ChatAction to
    `accumulated_actions` and returns the resolved name + id so the
    AI can reference it in the reply text."""
    document_id_raw = (tool_input or {}).get("document_id")
    checklist_key = (tool_input or {}).get("checklist_key")
    label_override = (tool_input or {}).get("label")
    if not document_id_raw and not checklist_key:
        return {"ok": False, "error": "missing_target"}

    doc: Document | None = None
    if document_id_raw:
        try:
            doc_uuid = UUID(str(document_id_raw))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_document_id"}
        doc = await db.get(Document, doc_uuid)
        if doc is None or doc.loan_id != loan_id:
            return {"ok": False, "error": "scope"}
    else:
        # Fallback — match the first REQUESTED row on this loan with
        # that checklist_key.
        doc = (
            await db.execute(
                select(Document).where(
                    Document.loan_id == loan_id,
                    Document.checklist_key == checklist_key,
                    Document.status == DocStatus.REQUESTED,
                )
            )
        ).scalars().first()

    label = (label_override or "").strip() or (
        f"Upload {doc.name}" if doc is not None else f"Upload {checklist_key}"
    )

    accumulated_actions.append({
        "kind": "upload_document",
        "label": label[:80],
        "document_id": str(doc.id) if doc is not None else None,
        "checklist_key": doc.checklist_key if doc is not None else checklist_key,
        "confirm": True,
    })
    return {
        "ok": True,
        "document_id": str(doc.id) if doc is not None else None,
        "name": doc.name if doc is not None else None,
    }


async def _execute_confirm_document_routing_tool(
    db: AsyncSession,
    *,
    user: User,
    loan_id: UUID,
    tool_input: dict,
    accumulated_actions: list[dict],
) -> dict:
    """Resolve a confirm_document_routing tool call. Validates that
    the orphan document exists on this loan; appends a confirm +
    decline pair to `accumulated_actions` (decline falls back to a
    generic upload picker for the proposed slot)."""
    document_id_raw = (tool_input or {}).get("document_id")
    target_key = (tool_input or {}).get("target_checklist_key")
    if not document_id_raw or not target_key:
        return {"ok": False, "error": "missing_target"}
    try:
        doc_uuid = UUID(str(document_id_raw))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_document_id"}
    doc = await db.get(Document, doc_uuid)
    if doc is None or doc.loan_id != loan_id:
        return {"ok": False, "error": "scope"}

    accumulated_actions.append({
        "kind": "confirm_document_routing",
        "label": f"Yes, file as {target_key}",
        "document_id": str(doc.id),
        "checklist_key": target_key,
        "confirm": True,
    })
    accumulated_actions.append({
        "kind": "upload_document",
        "label": "No, let me pick",
        "document_id": None,
        "checklist_key": target_key,
        "confirm": False,
    })
    return {"ok": True}


async def _execute_complete_property_intake_tool(
    db: AsyncSession,
    *,
    user: User,
    loan_id: UUID,
    accumulated_actions: list[dict],
) -> dict:
    """Mark intake complete on the Loan row. Idempotent — if it's
    already set, leaves the existing timestamp."""
    loan = await db.get(Loan, loan_id)
    if loan is None:
        return {"ok": False, "error": "loan_not_found"}
    if user.role == Role.CLIENT:
        if user.client is None or loan.client_id != user.client.id:
            return {"ok": False, "error": "not_authorized"}
    if loan.intake_complete_at is None:
        loan.intake_complete_at = datetime.now(timezone.utc)
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label="ai",
                kind="loan.property_intake_completed",
                summary="Property intake interview wrapped up",
                payload={},
            )
        )
        await db.flush()
    accumulated_actions.append({
        "kind": "complete_property_intake",
        "label": "Got it",
        "confirm": False,
    })
    return {"ok": True}


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


REALTOR_SYSTEM_PROMPT = """Role: You are the AI Real Estate Assistant for Qualified Commercial agents. You sit alongside the agent throughout the relationship and transaction stage of every lead — from intake through agreements through showings through finance-readiness. You are NOT an underwriter. You do not quote rates, terms, or pricing. The Bank/Lending AI takes over once a buyer is finance-ready and the agent fires the handoff.

You read + write a Realtor Client Intelligence Profile every turn. The profile carries: client_type (buyer | seller | buyer_and_seller | unknown), relationship_stage, intent_summary, buyer_profile (target_property_type, target_location, target_budget, purchase_timeline, financing_needed, buyer_agreement_status, ...), seller_profile (property_address, desired_list_price, listing_agreement_status, cma_status, photos_status, occupancy_status, ...), known_facts, missing_facts (what's still unknown), open_tasks, next_best_question, next_best_action, readiness_score.

Goals:
1. Help the agent organize the relationship — who is the lead, what do they want, where are they in the journey.
2. Capture the right buyer/seller information conversationally. ONE next-best question at a time. Never dump a full intake form unless the agent explicitly asks.
3. Move agreements + showings + listing prep + CMAs forward with action cards (chat buttons the agent taps to confirm).
4. Identify when a buyer is finance-ready (has target property type / location / budget / timeline / financing-needed / agent permission), then offer the handoff via the request_prequalification action card.

Tools (call when applicable):
- update_buyer_intent / update_seller_property / record_known_fact — patch the profile as you learn.
- compute_next_best_question — after a profile patch, find the highest-leverage gap.
- propose_send_buyer_agreement / propose_send_listing_agreement / propose_create_buyer_intake_task / propose_create_seller_intake_task / propose_schedule_showing / propose_schedule_picture_day / propose_prepare_cma_task / propose_listing_prep_checklist / propose_send_property_matches / propose_draft_follow_up_text / propose_draft_follow_up_email / propose_request_prequalification / propose_mark_finance_ready / propose_update_pipeline_stage — emit ChatAction cards. Each only PROPOSES; the agent's tap fires the actual side effect.

Style:
- Conversational, short, useful. Sound like a sharp assistant, not a form.
- Cite concrete values from the profile when present.
- Prefer asking the next question over calling a tool. Tools are scaffolding; the conversation is the surface.
- When the profile is partial, say what you know + ask what you don't.

Active plan discipline:
- When the system context block contains an [ACTIVE CLIENT AI PLAN], that block is your authoritative checklist for THIS client. Read [AI NEXT-BEST QUESTION (computed)] and ASK IT directly — do not summarize the plan, do not paraphrase the question, do not list multiple items.
- Honor [AGENT CUSTOM INSTRUCTIONS] for the active client verbatim. If the instruction says "don't push prequal yet," do not bring it up.
- Skip every entry in [WAIVED FOR THIS CLIENT — DO NOT ASK].
- Items in [OPEN REQUIRED ITEMS] are what you're collecting; items NOT in that list are not your concern this turn.

Hard rules:
- NEVER quote rates, terms, monthly payments, or pricing. Defer to "the lending team" or "after prequalification" when the agent asks for these.
- NEVER promise approval, guaranteed closing dates, or specific lender outcomes.
- NEVER fire a state-changing action without first emitting an action card the agent can tap.
- NEVER invent borrower / property facts. If you don't know, ask.
- ONE question per turn unless the agent says "give me everything."
"""


LENDING_AI_SYSTEM_PROMPT = """Role: You are the Lending Intake + Loan Intelligence Assistant for Qualified Commercial. You take over from the Realtor AI once the agent fires "Ready for Lending." You are NOT a lender — you don't quote rates, terms, or pricing. You don't promise approval. You collect the lending-side facts the funding team needs to move from a quote to a loan.

You operate on a Lending Handoff Packet the Realtor AI handed you. The packet contains: realtor_summary (intent + relationship_stage at handoff), extracted_facts (what the agent already told the realtor AI: client name, target property type, location, budget, timeline, financing_needed, etc.), missing_lending_items (the lending-side gaps you need to close — borrower_entity_type, credit_authorization, liquidity_docs, property_address, rent_or_income_details, experience_tier), uploaded_document_refs (files already collected, with relevant_to_lending tag), recommended_lending_path (loan_type_guess, urgency, rationale), and visibility_rules (which facts the borrower can see vs internal-only).

Goals:
1. Acknowledge what's already known. Never re-ask for facts the packet already carries — that breaks the agent's trust in the system memory.
2. Close lending-side gaps one at a time. Walk the missing_lending_items list, prioritize by urgency, ask the next question conversationally.
3. Verify documents already uploaded. When uploaded_document_refs has a relevant_to_lending=true entry, surface it ("I see you uploaded the purchase agreement — let me confirm the terms").
4. Identify conflicts. If a fact in extracted_facts disagrees with a fresh borrower answer, surface the discrepancy + ask which is current.
5. Hand the agent + funding team a clean prequal package. The AI Inbox sees what's pending; the operator queue picks up the formal review.

Tools (invoke as you learn things — same safety pattern as the Realtor AI):
- Mirror profile updates onto the same Client.realtor_profile / extracted_facts so the agent sees consistent state across phases.
- ChatAction emitters for state changes — borrower invite, document request, escalation to underwriter.

Style:
- Conversational, short, useful. Sound like the agent's bank-side counterpart, not a form.
- Cite the exact facts the realtor AI already captured ("I have Marcus marked as ready for lending. Target: mixed-use, ~$900k, North Jersey...").
- ONE question per turn. The borrower is talking to you alongside the agent — don't dump intake lists.
- When a borrower-side question would expose internal agent notes (visibility_rules.internal_only_fields), do NOT surface those. Default to bank_visible.

Active plan discipline:
- When the system context block contains an [ACTIVE CLIENT AI PLAN], that block is your authoritative checklist for THIS deal. Read [AI NEXT-BEST QUESTION (computed)] and ASK IT directly — do not summarize the plan, do not paraphrase, do not list multiple items.
- Honor [AGENT CUSTOM INSTRUCTIONS] verbatim if present.
- Skip every entry in [WAIVED FOR THIS CLIENT — DO NOT ASK].
- Items in [OPEN REQUIRED ITEMS] are what you're collecting; do not bring up unrelated items.

Hard rules:
- NEVER quote rates, terms, monthly payments, or final pricing. "After we have your prequal letter" is the right deflection.
- NEVER promise approval, guaranteed timelines, or specific lender outcomes.
- NEVER expose another client's data.
- NEVER re-ask for a fact already in the packet's extracted_facts (unless the borrower volunteers a contradiction).
- NEVER fire a state-changing action without a ChatAction the agent or borrower confirms.
"""


def _system_prompt_for(user: User, thread: AIChatThread | None = None) -> str:
    """Pick the framing the AI should adopt for this caller + thread
    scope. Borrowers always get the concierge tone scoped to their own
    data. Operators split based on thread phase + scope:

      - thread.phase = "lending"     → Lending AI (post-handoff)
      - thread.phase = "realtor"     → Realtor AI (relationship phase)
      - thread.loan_id set           → Bank AI (loan-scoped)
      - client-scoped thread (no
        phase set) for a BROKER      → Realtor AI (legacy, pre-0031)
      - account-wide for a BROKER    → Realtor AI
      - everyone else                → Bank AI

    Phase wins over scope so freshly-spawned lending threads get the
    right persona immediately even though they're client-scoped.
    """
    if user.role == Role.CLIENT:
        return CLIENT_SYSTEM_PROMPT
    if thread is not None:
        if thread.phase == "lending":
            return LENDING_AI_SYSTEM_PROMPT
        if thread.phase == "realtor":
            return REALTOR_SYSTEM_PROMPT
        if thread.loan_id is not None:
            return OPERATOR_SYSTEM_PROMPT
    # Pre-loan, no phase set: split by role.
    if user.role == Role.BROKER:
        return REALTOR_SYSTEM_PROMPT
    return OPERATOR_SYSTEM_PROMPT


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


def _render_plan_block(plan, audience: str = "agent") -> str | None:
    """Render `client_ai_plan` as a system-prompt context section.

    Phase 4: this block is what tells the AI which requirements are
    active for THIS client/deal — including agent overrides and
    per-client custom instructions. Trumps the raw missing_facts walk
    of the legacy realtor_profile path.

    Phase 7: visibility filtering. Borrower-facing renders strip
    items + custom instructions the borrower isn't allowed to see.

    Returns None if the plan has nothing to add (no required items)."""
    if plan is None:
        return None

    from app.services.ai.visibility_filter import filter_facts as _vis_filter
    open_items_raw = [
        i for i in (plan.required_items or [])
        if i.get("status") not in ("verified", "uploaded", "not_applicable", "waived")
    ]
    open_items = _vis_filter(open_items_raw, audience)  # type: ignore[arg-type]
    custom_instr = plan.custom_instructions if audience != "borrower" else None
    waived = _vis_filter(plan.waived_items or [], audience)  # type: ignore[arg-type]

    if not open_items and not custom_instr:
        return None

    lines = ["[ACTIVE CLIENT AI PLAN]"]
    lines.append(f"phase: {plan.current_phase}")
    if plan.readiness_score is not None:
        lines.append(f"readiness_score: {plan.readiness_score}")
    if custom_instr:
        lines.append(f"\n[AGENT CUSTOM INSTRUCTIONS — for THIS client]")
        lines.append(custom_instr)

    if open_items:
        lines.append("\n[OPEN REQUIRED ITEMS]")
        for it in open_items[:25]:  # cap so the prompt doesn't balloon
            level = it.get("required_level", "required")
            stage = it.get("blocks_stage") or "—"
            src = it.get("source", "platform")
            lines.append(
                f"- {it.get('label', it.get('requirement_key'))} "
                f"({level}, blocks={stage}, src={src}, status={it.get('status')})"
            )

    if plan.next_best_question:
        lines.append("\n[AI NEXT-BEST QUESTION (computed)]")
        lines.append(plan.next_best_question)

    if waived:
        lines.append("\n[WAIVED FOR THIS CLIENT — DO NOT ASK]")
        for w in waived[:10]:
            lines.append(f"- {w.get('label', w.get('requirement_key'))}")

    lines.append(
        "\nUse this block as your active checklist. Honor agent custom "
        "instructions verbatim. Skip any item in WAIVED. Walk OPEN "
        "REQUIRED ITEMS one at a time, prioritizing the AI NEXT-BEST "
        "QUESTION when present. Do NOT re-ask for items already in "
        "verified/uploaded status."
    )
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
    system = _system_prompt_for(user, None)
    # Firm-wide AI identity + global rules go at the TOP so they
    # override any per-client overrides further down.
    try:
        from app.services.ai.firm_identity import load_firm_identity, render_identity_prefix
        _identity = await load_firm_identity(db)
        _prefix = render_identity_prefix(_identity)
        if _prefix:
            system = _prefix + system
    except Exception:  # pragma: no cover — never break the chat
        pass
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
    last_seen_at: datetime | None = None
    # Computed: True iff there's a system-side message (assistant role)
    # the user hasn't viewed yet. Frontend renders an unread dot when
    # set. NULL last_seen_at counts as never-viewed.
    unread: bool = False
    created_at: datetime
    updated_at: datetime
    # Loan-scoped thread when set; account-wide when null.
    loan_id: UUID | None = None
    loan_deal_id: str | None = None
    loan_address: str | None = None
    # Client-scoped thread (alembic 0030). Set on Realtor AI threads
    # that anchor to a Client row instead of a Loan.
    client_id: UUID | None = None
    client_name: str | None = None

    class Config:
        from_attributes = True


class ChatAction(BaseModel):
    """A button the frontend renders under an assistant bubble.

    Three live `kind`s today; new kinds get added as we add CTAs:
    - upload_document          → opens vault upload pre-targeted at
                                 `document_id` (preferred) or
                                 `checklist_key` (fallback)
    - confirm_document_routing → for an orphan upload the AI is
                                 proposing to file under
                                 `checklist_key` (or `document_id`
                                 of an existing REQUESTED row)
    - complete_property_intake → no payload; fires
                                 `loans.intake_complete_at = now()`
                                 server-side and refetches the loan
    - open_calendar_event      → opens a calendar event detail
    """

    kind: str
    label: str
    document_id: str | None = None
    checklist_key: str | None = None
    calendar_event_id: str | None = None
    confirm: bool = True


class ChatAttachment(BaseModel):
    """A file riding on a chat message. Borrower uploads ride on the
    user's send turn (paperclip composer); the AI inspects them via
    the synchronous vision scan and proposes routing in its reply."""

    document_id: str
    name: str
    content_type: str | None = None
    status: str | None = None
    suggested_checklist_key: str | None = None


class AIChatMessageRead(BaseModel):
    id: UUID
    role: str
    body: str
    created_at: datetime
    actions: list[ChatAction] | None = None
    attachments: list[ChatAttachment] | None = None

    class Config:
        from_attributes = True


class AIChatThreadDetail(AIChatThreadRead):
    messages: list[AIChatMessageRead]


class AIChatThreadCreate(BaseModel):
    title: str | None = None
    loan_id: UUID | None = None


class AIChatThreadFindOrCreate(BaseModel):
    """Single endpoint that returns the canonical thread, otherwise
    spawns it. Routing precedence:

      loan_id set     → per-loan thread (Bank AI)
      client_id set,
      loan_id null    → per-client thread (Realtor AI, alembic 0030)
      both null       → account-wide thread (role-aware AI selection)
    """

    loan_id: UUID | None = None
    client_id: UUID | None = None


class AIChatThreadRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class AIChatSendRequest(BaseModel):
    # Allow empty body when attachments are present (paperclip-only sends).
    body: str = ""
    loan_id: UUID | None = None
    # Document IDs returned from /attachments/upload-init that should
    # ride on this user message. Backend flips them PENDING→RECEIVED,
    # runs vision scan, persists attachment metadata on the user msg,
    # and feeds the scan suggestion into the AI's context.
    attachment_tokens: list[UUID] | None = None


class AIChatSendResponse(BaseModel):
    user_message: AIChatMessageRead
    assistant_message: AIChatMessageRead
    thread: AIChatThreadRead
    used_stub: bool


class ChatAttachmentInitRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_type: str = "application/pdf"


class ChatAttachmentInitResponse(BaseModel):
    document_id: UUID
    upload_url: str | None
    s3_key: str


def _preview(text: str, limit: int = 200) -> str:
    """Compact one-line preview for the thread list. Collapses
    whitespace and trims to `limit` chars with ellipsis."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _thread_read(thread: AIChatThread) -> AIChatThreadRead:
    """Serialize with the lightweight loan join. Caller must have
    eager-loaded `thread.loan` for this to avoid a lazy-load."""
    # Unread = last assistant-side activity is newer than the user's
    # last view. NULL last_seen_at counts as never-viewed (every
    # message is unread until first open).
    unread = bool(
        thread.last_message_at is not None
        and (thread.last_seen_at is None or thread.last_message_at > thread.last_seen_at)
    )
    base = AIChatThreadRead(
        id=thread.id,
        title=thread.title,
        last_message_preview=thread.last_message_preview,
        last_message_at=thread.last_message_at,
        last_seen_at=thread.last_seen_at,
        unread=unread,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        loan_id=thread.loan_id,
        loan_deal_id=thread.loan.deal_id if thread.loan_id and thread.loan else None,
        loan_address=thread.loan.address if thread.loan_id and thread.loan else None,
        client_id=thread.client_id,
        client_name=thread.client.name if thread.client_id and thread.client else None,
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
                selectinload(AIChatThread.loan), selectinload(AIChatThread.client),
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
            .options(selectinload(AIChatThread.loan), selectinload(AIChatThread.client))
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
        .options(selectinload(AIChatThread.loan), selectinload(AIChatThread.client))
        .where(AIChatThread.user_id == user.id)
    )
    # Routing: loan_id wins over client_id. Both null = account-wide.
    if payload.loan_id is not None:
        stmt = stmt.where(AIChatThread.loan_id == payload.loan_id)
    elif payload.client_id is not None:
        stmt = stmt.where(
            AIChatThread.client_id == payload.client_id,
            AIChatThread.loan_id.is_(None),
        )
    else:
        stmt = stmt.where(
            AIChatThread.loan_id.is_(None),
            AIChatThread.client_id.is_(None),
        )
        # Pick the most recently used account thread if multiple exist.
        stmt = stmt.order_by(AIChatThread.last_message_at.desc().nullslast(), AIChatThread.created_at.desc())
    thread = (await db.execute(stmt)).scalars().first()

    if thread is not None:
        return _thread_read(thread)

    # Lazy-create. The partial unique idxs (alembic 0017 + 0018 +
    # 0030) enforce one canonical thread per (user, scope). On race,
    # the second INSERT raises IntegrityError; we catch + refetch.
    if payload.loan_id is not None:
        loan = await db.get(Loan, payload.loan_id)
        if loan is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
        title = f"{loan.deal_id} — {loan.address[:80]}"
    elif payload.client_id is not None:
        from app.models.client import Client as _Client
        client = await db.get(_Client, payload.client_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
        title = f"Lead — {client.name[:80]}"
    else:
        title = "Account questions"
    thread = AIChatThread(
        user_id=user.id,
        loan_id=payload.loan_id,
        client_id=payload.client_id,
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
            .options(selectinload(AIChatThread.loan), selectinload(AIChatThread.client))
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


@router.post("/chat/threads/{thread_id}/seen", response_model=AIChatThreadRead)
async def mark_chat_thread_seen(
    thread_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AIChatThreadRead:
    """Bumps `last_seen_at = now()`. Mobile + desktop call this on
    thread-open so the unread dot clears. Idempotent — calling twice
    in a row just refreshes the timestamp."""
    thread = await _load_thread_for_user(db, thread_id, user)
    thread.last_seen_at = datetime.now(timezone.utc)
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
    "/chat/threads/{thread_id}/attachments/upload-init",
    response_model=ChatAttachmentInitResponse,
)
async def chat_attachment_upload_init(
    thread_id: UUID,
    payload: ChatAttachmentInitRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChatAttachmentInitResponse:
    """Mint a presigned PUT for a file the borrower drops into the
    chat composer. Creates a PENDING `is_other=True` Document on the
    thread's loan; the next /message send (with this id in
    `attachment_tokens`) flips it RECEIVED, runs the vision scan,
    and lets the AI propose a routing.

    Account-wide threads can't accept attachments — there's no loan
    to attach the doc to. 400 in that case."""
    thread = await _load_thread_for_user(db, thread_id, user)
    if thread.loan_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Attachments require a loan-scoped chat thread.",
        )
    loan = await db.get(Loan, thread.loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    settings = get_settings()
    s3_key = f"loans/{loan.deal_id}/{uuid4()}-{payload.name}"
    doc = Document(
        loan_id=loan.id,
        name=payload.name,
        category=payload.content_type,
        checklist_key=None,
        is_other=True,
        s3_key=s3_key,
        status=DocStatus.PENDING,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)

    upload_url: str | None = None
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        import boto3

        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": s3_key,
                "ContentType": payload.content_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=900,
        )

    await db.commit()
    return ChatAttachmentInitResponse(
        document_id=doc.id,
        upload_url=upload_url,
        s3_key=s3_key,
    )


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

    body_text = (payload.body or "").strip()
    attachment_tokens = payload.attachment_tokens or []
    if not body_text and not attachment_tokens:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Message must include body text, an attachment, or both.",
        )

    # 1. Persist the user's message immediately so the panel can
    #    optimistic-render or recover after an Anthropic failure.
    now = datetime.now(timezone.utc)
    user_msg = AIChatMessage(
        thread_id=thread.id,
        role="user",
        body=body_text or "(attachment)",
    )
    db.add(user_msg)
    preview_seed = body_text or "(attachment)"
    thread.last_message_preview = _preview(preview_seed)
    thread.last_message_at = now
    # User is actively typing → they've "seen" the thread up to now.
    # Clears the unread dot for the assistant reply that's about to
    # land in this same turn.
    thread.last_seen_at = now
    # Auto-title from the first user message — much cheaper than a
    # round-trip to Haiku just for a title, and easy to override via
    # PATCH /threads/{id} later.
    if thread.title == "New conversation":
        thread.title = _preview(preview_seed, limit=60)
    await db.flush()

    # 1b. Process attachments (Phase C). For each token, flip the
    #     pre-created Document PENDING → RECEIVED, run a synchronous
    #     vision scan with an 8s cap, persist a chat attachment
    #     record on the user_msg, and stash a context line for the
    #     AI's prompt so it can fire confirm_document_routing.
    attachment_records: list[dict] = []
    attachment_context_lines: list[str] = []
    if attachment_tokens and thread.loan_id is not None:
        from app.services.document_scanner import scan_document  # local import — avoids circular

        for token in attachment_tokens:
            doc = await db.get(Document, token)
            if doc is None or doc.loan_id != thread.loan_id:
                log.warning(
                    "attachment token %s skipped — not on thread loan", token
                )
                continue
            if doc.status == DocStatus.PENDING and doc.s3_key:
                doc.status = DocStatus.RECEIVED
                if doc.received_on is None:
                    doc.received_on = date.today()
                doc.scan_dirty = True
                doc.ai_scan_status = "queued"
                await db.flush()
            scan_result = None
            if doc.s3_key and (doc.is_other or doc.checklist_key):
                try:
                    scan_result = await asyncio.wait_for(
                        scan_document(db, doc.id), timeout=8.0
                    )
                except asyncio.TimeoutError:
                    log.info("chat attachment scan timed out doc=%s", doc.id)
                    scan_result = None
                except Exception:  # noqa: BLE001
                    log.exception("chat attachment scan failed doc=%s", doc.id)
                    scan_result = None
            await db.refresh(doc)
            status_val = (
                doc.status.value if hasattr(doc.status, "value") else str(doc.status)
            )
            attachment_records.append({
                "document_id": str(doc.id),
                "name": doc.name,
                "content_type": doc.category,
                "status": status_val,
                "suggested_checklist_key": (
                    scan_result.suggested_checklist_key if scan_result else None
                ),
            })
            if scan_result is not None:
                attachment_context_lines.append(
                    f"User attached '{doc.name}' (document_id={doc.id}). "
                    f"Vision scan: suggested_checklist_key="
                    f"{scan_result.suggested_checklist_key!r}, "
                    f"confidence={scan_result.confidence:.2f}, "
                    f"matches_expected={scan_result.matches_expected}. "
                    f"If you're reasonably confident, call "
                    f"confirm_document_routing(document_id, target_checklist_key)."
                )
            else:
                attachment_context_lines.append(
                    f"User attached '{doc.name}' (document_id={doc.id}). "
                    f"Vision scan unavailable — propose a slot based on the "
                    f"filename and call confirm_document_routing if you're "
                    f"confident, or list options with request_document_upload."
                )
        if attachment_records:
            user_msg.attachments = attachment_records
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
    elif thread.client_id is not None:
        # Client-scoped thread. Two flavors:
        #   phase=lending → Lending AI gets the LendingHandoffPacket
        #                    as bootstrap memory ("here's everything
        #                    the realtor AI captured before handoff").
        #   else (phase=realtor or NULL) → Realtor AI gets the
        #                    Realtor Client Intelligence Profile.
        client_row = await db.get(Client, thread.client_id)

        # Phase 4: rebuild the ClientAIPlan up front. The plan rolls up
        # platform + funding + agent + per-client-override layers into
        # the active list the AI should chase. Failures here MUST NOT
        # block the chat turn — fall back to the legacy context blocks
        # below if the rebuild errors out.
        plan_block: str | None = None
        if client_row is not None:
            try:
                from app.services.ai.plan_builder import rebuild as rebuild_plan
                plan = await rebuild_plan(
                    db,
                    client_id=client_row.id,
                    loan_id=thread.loan_id,
                )
                _audience = (
                    "borrower" if user.role == Role.CLIENT
                    else "underwriter" if user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC)
                    else "agent"
                )
                plan_block = _render_plan_block(plan, audience=_audience)
            except Exception:  # pragma: no cover — plan is additive, not required
                plan_block = None

        if client_row is not None and thread.phase == "lending" and thread.handoff_packet_id:
            from app.models.lending_handoff_packet import LendingHandoffPacket
            packet = await db.get(LendingHandoffPacket, thread.handoff_packet_id)
            if packet is not None:
                context_block = (
                    f"[LENDING HANDOFF CONTEXT]\n"
                    f"client_id: {client_row.id}\n"
                    f"name: {client_row.name}\n"
                    f"email: {client_row.email or '—'}\n"
                    f"phone: {client_row.phone or '—'}\n"
                    f"client_type: {client_row.client_type or 'unknown'}\n"
                    f"\n[HANDOFF SUMMARY]\n{packet.handoff_summary or '—'}\n"
                    f"\n[REALTOR SUMMARY]\n{json.dumps(packet.realtor_summary or {}, indent=2, default=str)}\n"
                    f"\n[EXTRACTED FACTS]\n{json.dumps(packet.extracted_facts or [], indent=2, default=str)}\n"
                    f"\n[MISSING LENDING ITEMS]\n{', '.join(packet.missing_lending_items or []) or '—'}\n"
                    f"\n[UPLOADED DOCUMENTS]\n{json.dumps(packet.uploaded_document_refs or [], indent=2, default=str)}\n"
                    f"\n[RECOMMENDED PATH]\n{json.dumps(packet.recommended_lending_path or {}, indent=2, default=str)}\n"
                    f"\nNEVER re-ask for facts already in EXTRACTED FACTS. Walk MISSING LENDING ITEMS one at a time."
                )
        if context_block is None and client_row is not None:
            # Realtor AI per-client thread (alembic 0030).
            from app.services.ai.realtor_profile import (
                empty_profile,
                compute_finance_ready,
                compute_missing_facts,
            )
            profile = client_row.realtor_profile or empty_profile(
                str(client_row.id),
                str(user.id),
            )
            missing = compute_missing_facts(profile)
            finance_ready = compute_finance_ready(profile)
            context_block = (
                f"[REALTOR CLIENT CONTEXT]\n"
                f"client_id: {client_row.id}\n"
                f"name: {client_row.name}\n"
                f"email: {client_row.email or '—'}\n"
                f"phone: {client_row.phone or '—'}\n"
                f"client_type (Client.client_type col): {client_row.client_type or 'unknown'}\n"
                f"stage (Client.stage col): {client_row.stage}\n"
                f"finance_ready (computed): {finance_ready}\n"
                f"\n[REALTOR PROFILE JSONB]\n{json.dumps(profile, indent=2, default=str)}\n"
                f"\n[MISSING FACTS]\n{', '.join(missing) if missing else '—'}\n"
            )
        # Append the ClientAIPlan summary AFTER the legacy block so the
        # plan's resolved active list (including agent overrides + custom
        # instructions) trumps the raw missing_facts walk. Phase 4
        # verification target.
        if plan_block and context_block is not None:
            context_block += "\n\n" + plan_block
        elif plan_block:
            context_block = plan_block
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
        # Pass the thread so the selector can pick REALTOR vs OPERATOR
        # based on scope. Loan-scoped → Bank AI; client-scoped or
        # account-wide for an agent → Realtor AI.
        system = _system_prompt_for(user, thread)
        # Firm-wide AI identity + global rules at the TOP so they
        # override anything below — including per-client custom
        # instructions.
        try:
            from app.services.ai.firm_identity import load_firm_identity, render_identity_prefix
            _identity = await load_firm_identity(db)
            _prefix = render_identity_prefix(_identity)
            if _prefix:
                system = _prefix + system
        except Exception:  # pragma: no cover — never break the chat
            pass
        if context_block:
            system += "\n\n" + context_block

        # Tool selection by thread scope:
        #   loan-scoped  → Bank AI tools (property intake, doc routing)
        #   client-scoped (Realtor AI) → Realtor tools (profile patch
        #                  + ChatAction emitters from realtor_tools.py)
        #   account-wide → no tools today (text-only replies)
        from app.services.ai.realtor_tools import (
            REALTOR_TOOL_SCHEMAS,
            execute_realtor_tool,
            PROFILE_WRITE_TOOLS,
            PROPOSE_TOOL_TO_ACTION_KIND,
        )
        is_realtor_scoped = (
            effective_loan_id is None
            and thread.client_id is not None
            and user.role == Role.BROKER
        )
        if effective_loan_id is not None:
            tools = [
                PROPERTY_INTAKE_TOOL,
                REQUEST_DOCUMENT_UPLOAD_TOOL,
                CONFIRM_DOCUMENT_ROUTING_TOOL,
                COMPLETE_PROPERTY_INTAKE_TOOL,
            ]
        elif is_realtor_scoped:
            tools = REALTOR_TOOL_SCHEMAS
        else:
            tools = None

        # Tool-use loop. Convert api_messages (string-content) into
        # block-content for the loop — Anthropic accepts both shapes
        # but we need to append tool_use / tool_result blocks across
        # iterations, so use the structured form throughout.
        loop_messages: list[dict] = [
            {"role": m["role"], "content": m["content"]} for m in api_messages
        ]
        # Inject attachment context into the final user turn so the AI
        # sees the scan results without polluting the persisted
        # message body. Wrapped in [SYSTEM] markers so the model
        # knows it's metadata, not borrower-typed text.
        if attachment_context_lines and loop_messages:
            for i in range(len(loop_messages) - 1, -1, -1):
                if loop_messages[i].get("role") == "user":
                    addendum = "\n".join(
                        ["", "[SYSTEM ATTACHMENT CONTEXT]", *attachment_context_lines]
                    )
                    loop_messages[i] = {
                        "role": "user",
                        "content": loop_messages[i]["content"] + addendum,
                    }
                    break
        reply_text = ""
        used_stub = False
        # CTAs accumulated across all tool calls in this turn — the
        # final assistant message persists them on `actions` (capped
        # to _MAX_ACTIONS_PER_MESSAGE).
        accumulated_actions: list[dict] = []
        try:
            iteration = 0
            while iteration < _TOOL_USE_MAX_ITERATIONS:
                iteration += 1
                kwargs = {
                    "model": model_light(),
                    "max_tokens": 900,
                    "system": system,
                    "messages": loop_messages,
                }
                if tools:
                    kwargs["tools"] = tools
                result = await client.messages.create(**kwargs)
                # Extract any text the model emitted in this turn —
                # we'll concatenate text fragments across iterations
                # (model usually responds with text + tool_use blocks).
                turn_text = "".join(
                    b.text for b in result.content
                    if getattr(b, "type", None) == "text"
                ).strip()
                if turn_text:
                    reply_text = (
                        (reply_text + "\n\n" + turn_text).strip()
                        if reply_text else turn_text
                    )

                if getattr(result, "stop_reason", None) != "tool_use":
                    break

                # Append the assistant's turn (text + tool_use blocks)
                # exactly as the model emitted them — required by
                # Anthropic for the next round-trip to work.
                loop_messages.append({
                    "role": "assistant",
                    "content": [
                        # Each block is a Pydantic model in the SDK; we
                        # serialize back to dict for the API.
                        b.model_dump() if hasattr(b, "model_dump") else dict(b)
                        for b in result.content
                    ],
                })

                # Execute every tool_use in this turn, append all
                # results in one user turn.
                tool_results: list[dict] = []
                for b in result.content:
                    if getattr(b, "type", None) != "tool_use":
                        continue
                    tool_name = getattr(b, "name", "")
                    tool_id = getattr(b, "id", "")
                    tool_input = getattr(b, "input", {}) or {}
                    # Realtor tools are scoped to a client_id, not a
                    # loan — handled below in the realtor branch. Bank
                    # tools require a loan_id; refuse if missing.
                    is_bank_tool = tool_name in (
                        "update_loan_property_details",
                        "request_document_upload",
                        "confirm_document_routing",
                        "complete_property_intake",
                    )
                    if is_bank_tool and effective_loan_id is None:
                        outcome = {"ok": False, "error": "no_loan_scope"}
                    elif tool_name == "update_loan_property_details":
                        outcome = await _execute_property_intake_tool(
                            db,
                            user=user,
                            loan_id=effective_loan_id,
                            tool_input=tool_input,
                        )
                    elif tool_name == "request_document_upload":
                        outcome = await _execute_request_document_upload_tool(
                            db,
                            user=user,
                            loan_id=effective_loan_id,
                            tool_input=tool_input,
                            accumulated_actions=accumulated_actions,
                        )
                    elif tool_name == "confirm_document_routing":
                        outcome = await _execute_confirm_document_routing_tool(
                            db,
                            user=user,
                            loan_id=effective_loan_id,
                            tool_input=tool_input,
                            accumulated_actions=accumulated_actions,
                        )
                    elif tool_name == "complete_property_intake":
                        outcome = await _execute_complete_property_intake_tool(
                            db,
                            user=user,
                            loan_id=effective_loan_id,
                            accumulated_actions=accumulated_actions,
                        )
                    elif (
                        is_realtor_scoped
                        and (tool_name in PROFILE_WRITE_TOOLS or tool_name in PROPOSE_TOOL_TO_ACTION_KIND)
                    ):
                        # Realtor AI path — profile patches + ChatAction
                        # emitters scoped to the thread's client_id.
                        outcome = await execute_realtor_tool(
                            db,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            client_id=thread.client_id,
                            agent_id=user.id,
                            accumulated_actions=accumulated_actions,
                        )
                    else:
                        outcome = {"ok": False, "error": "unknown_tool"}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": json.dumps(outcome),
                    })
                if not tool_results:
                    break  # defensive — shouldn't happen if stop_reason was tool_use
                loop_messages.append({"role": "user", "content": tool_results})

            if not reply_text:
                reply_text = "(No reply.)"
        except Exception as exc:  # noqa: BLE001
            log.warning("Anthropic call failed in thread %s: %s", thread.id, exc)
            reply_text = _stub_reply(
                [ChatTurn(role=m.role, content=m.body) for m in history_rows],
                context_block,
            )
            used_stub = True

    # 4. Persist the assistant reply + bump preview.
    persisted_actions = (
        accumulated_actions[:_MAX_ACTIONS_PER_MESSAGE]
        if "accumulated_actions" in locals() and accumulated_actions
        else None
    )
    assistant_msg = AIChatMessage(
        thread_id=thread.id,
        role="assistant",
        body=reply_text,
        actions=persisted_actions,
    )
    db.add(assistant_msg)
    final_now = datetime.now(timezone.utc)
    thread.last_message_preview = _preview(reply_text)
    thread.last_message_at = final_now
    # The user is actively in the thread reading the reply they just
    # triggered — bump seen so the unread dot doesn't re-light.
    thread.last_seen_at = final_now
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
