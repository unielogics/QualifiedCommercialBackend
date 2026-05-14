"""Structured extraction of action items from lender ↔ brokerage email.

Runs after every lender-thread state change (inbound from the lender,
outbound we sent, save_draft). Pulls the full thread + the new event,
asks Claude for a JSON list of action items, and persists onto
`loans.living_profile.lender_extract` so:

  * the LenderThread right-column panel can render them as a to-do list
  * the general AI chat (loan-chat, deal-chat) can answer client /
    realtor questions like "what's the lender waiting on?" with real
    grounded answers

Each action item is tagged `sensitivity` of:
  - "external" — client / realtor can see + act on it (e.g., "borrower
                 needs to upload 2023 tax return", "schedule appraisal")
  - "internal" — only super_admin + loan_exec see it (e.g., "lender
                 lowered LTV from 70 to 65", "rate pricing changed",
                 "verify lender pipeline status")

The "external" filter is the same load-bearing principle as the
existing one-way mirror PII redaction: anything that would expose
internal commercial mechanics or lender identity stays internal.

Storage:
  loans.living_profile.lender_extract = {
    "generated_at": "<iso>",
    "message_count": int,
    "action_items": [
      {
        "id": "<slug-uuid>",
        "summary": "...",
        "owner": "broker|client|lender|super_admin",
        "sensitivity": "internal|external",
        "priority": "high|med|low",
        "due_date": "YYYY-MM-DD" | null,
        "requested_documents": ["..."],
        "named_people": ["..."],
        "amounts": ["$..."],
        "source_message_id": "<uuid>" | null,
        "confidence": 0.0-1.0
      }, ...
    ],
    "status_changes": [
      {"summary": "...", "kind": "approved|declined|rate_locked|...",
       "sensitivity": "internal|external"}
    ],
    "current_situation": "<2-sentence plain-English summary for AI grounding>"
  }
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.enums import MessageFrom
from app.models.email_draft import EmailDraft
from app.models.loan import Loan
from app.models.message import Message
from app.services.ai.anthropic_client import get_client, model_heavy, model_light

log = logging.getLogger(__name__)

_EXTRACT_SYSTEM_PROMPT = """You extract structured action items from the email thread between a lender (counterparty) and a brokerage (us) for a single commercial real estate loan.

OUTPUT: STRICT JSON. No prose, no markdown, no preamble. The schema:

{
  "current_situation": "2 short sentences. Plain English. What's the loan's lender-side state right now?",
  "action_items": [
    {
      "summary": "imperative, short (8-15 words)",
      "owner": "broker" | "client" | "lender" | "super_admin",
      "sensitivity": "internal" | "external",
      "priority": "high" | "med" | "low",
      "due_date": "YYYY-MM-DD" | null,
      "requested_documents": ["file name 1", ...] | [],
      "named_people": ["Name (role)", ...] | [],
      "amounts": ["$320,000", "6.60%", "70% LTV", ...] | [],
      "confidence": 0.0 to 1.0
    },
    ...
  ],
  "status_changes": [
    {
      "summary": "short phrase",
      "kind": "approved" | "declined" | "rate_locked" | "rate_changed" | "ltv_changed" | "doc_requested" | "term_changed" | "other",
      "sensitivity": "internal" | "external"
    }
  ]
}

SENSITIVITY RULES — critical, do not deviate:
  - "external" = something a client (borrower) OR a realtor (broker's deal partner) needs to know or act on. Document requests, deadlines, scheduling, status milestones they care about.
  - "internal" = anything that would expose internal commercial mechanics: lender identity, rate pricing logic, LTV negotiations, lender-pipeline status, our internal next steps that don't require client/realtor action.

When in doubt, prefer "internal". The default for anything mentioning lender pricing, internal underwriting, or lender pushback is "internal".

OWNERSHIP RULES:
  - "client" = the borrower must do it (e.g., "upload 2023 tax return")
  - "broker" = the brokerage operator must do it (e.g., "send rate-lock confirmation")
  - "super_admin" = privileged operator-only action (e.g., "renegotiate rate floor with lender")
  - "lender" = WE are waiting on the lender (e.g., "lender to send final term sheet")

If no action items can be confidently extracted, return action_items: []. Do not invent items."""


async def extract_and_persist(
    db: AsyncSession,
    *,
    loan_id: UUID,
    trigger: str,
) -> dict[str, Any] | None:
    """Re-run extraction over the loan's lender thread and stash the
    result on `loans.living_profile.lender_extract`. Returns the new
    extract dict (or None if extraction was skipped).

    `trigger` is logged for audit (e.g., "inbound_poller",
    "post_reply", "manual"). It does not affect the extraction logic.

    Idempotent — safe to call repeatedly. Each call regenerates from
    the current thread state.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        log.debug("lender_extractor: ANTHROPIC_API_KEY missing — skipping")
        return None

    loan = (
        await db.execute(select(Loan).where(Loan.id == loan_id))
    ).scalar_one_or_none()
    if loan is None:
        return None

    msg_rows = (
        await db.execute(
            select(Message)
            .where(Message.loan_id == loan.id)
            .order_by(Message.sent_at.asc())
        )
    ).scalars().all()
    if not msg_rows:
        return None

    # Pull the matched outbound drafts so the AI knows what we
    # explicitly committed to. (Inbound from the lender doesn't go
    # through EmailDraft, so this list represents OUR outbound text.)
    draft_rows = (
        await db.execute(
            select(EmailDraft)
            .where(EmailDraft.loan_id == loan.id)
            .order_by(EmailDraft.created_at.asc())
        )
    ).scalars().all()

    transcript = _format_transcript(msg_rows, draft_rows)
    if not transcript.strip():
        return None

    extract = await _call_llm(transcript)
    if extract is None:
        return None

    # Tag every action item with a synthetic stable id so the UI can
    # key on it.
    for item in extract.get("action_items", []):
        item["id"] = item.get("id") or str(uuid.uuid4())

    extract["generated_at"] = datetime.now(timezone.utc).isoformat()
    extract["message_count"] = len(msg_rows)
    extract["trigger"] = trigger

    # Persist onto loans.living_profile.lender_extract — preserve the
    # rest of the living_profile JSONB.
    current = dict(loan.living_profile or {})
    current["lender_extract"] = extract
    # Maintain a derived externals-only view that the loan-chat AI
    # uses for client / realtor grounding. Cheaper than re-filtering
    # at every chat request.
    current["lender_extract_external"] = _filter_external(extract)
    loan.living_profile = current
    await db.flush()
    log.info(
        "lender_extractor: loan=%s items=%d external=%d trigger=%s",
        loan.id,
        len(extract.get("action_items", [])),
        len(current["lender_extract_external"].get("action_items", [])),
        trigger,
    )
    return extract


def _filter_external(extract: dict[str, Any]) -> dict[str, Any]:
    """Build a 'safe to show client + realtor' view that drops internal
    items + softens any remaining wording. The full extract stays on
    loans.living_profile.lender_extract for super_admin / loan_exec."""
    items = extract.get("action_items", []) or []
    status_changes = extract.get("status_changes", []) or []
    return {
        "current_situation": extract.get("current_situation") or "",
        "action_items": [i for i in items if i.get("sensitivity") == "external"],
        "status_changes": [s for s in status_changes if s.get("sensitivity") == "external"],
        "generated_at": extract.get("generated_at"),
    }


def _format_transcript(
    msg_rows: list[Message],
    draft_rows: list[EmailDraft],
) -> str:
    """Render the thread for the LLM. We keep it compact and time-
    ordered. Outbound drafts are merged into the message stream by
    timestamp so the model sees both sides of the conversation."""
    events: list[tuple[datetime, str]] = []
    for m in msg_rows:
        if m.is_draft:
            continue
        prefix = {
            MessageFrom.LENDER: "LENDER",
            MessageFrom.BROKER: "US (broker)",
            MessageFrom.AI: "US (AI on behalf of broker)",
            MessageFrom.CLIENT: "CLIENT",
        }.get(m.from_role, str(m.from_role))
        ts = m.sent_at or datetime.now(timezone.utc)
        events.append(
            (ts, f"[{ts.strftime('%Y-%m-%d %H:%M')}] {prefix}: {m.body.strip()}")
        )
    for d in draft_rows:
        if d.status not in ("sent", "approved"):
            # Pending drafts haven't gone out; not part of the
            # conversation we're reasoning about.
            continue
        ts = d.updated_at or d.created_at or datetime.now(timezone.utc)
        events.append(
            (
                ts,
                f"[{ts.strftime('%Y-%m-%d %H:%M')}] US OUTBOUND DRAFT "
                f"(subject={d.subject!r}, to={d.to_email}): {d.body.strip()}",
            )
        )
    events.sort(key=lambda e: e[0])
    return "\n\n".join(line for _, line in events)


async def _call_llm(transcript: str) -> dict[str, Any] | None:
    """Run the heavy model over the transcript. Falls back to None on
    any failure — callers leave loans.living_profile.lender_extract
    untouched in that case so the UI continues showing the last good
    snapshot."""
    settings = get_settings()
    try:
        client = get_client()
        resp = await client.messages.create(
            model=model_heavy(),
            max_tokens=1500,
            system=_EXTRACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": transcript}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return None
        # Defensive shape: ensure the two arrays always exist.
        parsed.setdefault("action_items", [])
        parsed.setdefault("status_changes", [])
        parsed.setdefault("current_situation", "")
        return parsed
    except Exception:  # noqa: BLE001 — LLM hiccups must not poison ingestion
        log.exception("lender_extractor: LLM call failed; leaving existing extract intact")
        return None
