"""Realtor AI tools — registered when system_prompt = REALTOR_SYSTEM_PROMPT.

Three categories:

  1. Profile-write tools — `update_buyer_intent`, `update_seller_property`,
     `record_known_fact`. The AI calls these as it learns; they patch
     `Client.realtor_profile` JSONB directly via apply_profile_patch.
     No agent approval needed — the AI is just structuring what the
     agent already told it.

  2. Read-only tools — `compute_next_best_question`,
     `summarize_pipeline`. The AI uses these to decide what to ask
     next or to summarize the agent's lead pipeline. Pure reads.

  3. ChatAction-emitting tools — `propose_*`. These DON'T fire side
     effects. They append a ChatAction dict to `accumulated_actions`,
     which the message-send handler attaches to the assistant's reply.
     The agent's tap on the card is what actually fires the endpoint.
     Mirrors the safety pattern the existing chat tools already use.

The orchestrator wires these in conditionally — only when the active
thread's system prompt is REALTOR_SYSTEM_PROMPT — so the Bank AI
isn't accidentally running with realtor-flavored capabilities.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.services.ai.realtor_profile import (
    apply_profile_patch,
    compute_finance_ready,
    compute_listing_ready,
    compute_missing_facts,
    empty_profile,
)

log = logging.getLogger(__name__)


# ── Tool schemas (Anthropic tool_use format) ────────────────────────

UPDATE_BUYER_INTENT_TOOL = {
    "name": "update_buyer_intent",
    "description": (
        "Patch the active client's buyer_profile in the Realtor Client "
        "Intelligence Profile. Call this every time the agent tells you "
        "a new fact about what the buyer is looking for. Pass only the "
        "fields you've just learned — leave others unset. Don't pass "
        "values you're guessing — ask the agent first if you're unsure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_property_type": {"type": "string", "description": "e.g. multifamily, mixed_use, commercial, retail, single_family"},
            "target_location": {"type": "string", "description": "Free-text — neighborhood, city, region"},
            "target_budget": {"type": "number", "description": "Exact target price in USD"},
            "target_budget_range_low": {"type": "number"},
            "target_budget_range_high": {"type": "number"},
            "purchase_timeline": {"type": "string", "enum": ["asap", "0_30", "30_60", "60_plus"]},
            "financing_needed": {"type": "boolean"},
            "buyer_agreement_status": {"type": "string", "enum": ["not_sent", "sent", "signed", "n/a"]},
            "proof_of_funds_status": {"type": "string", "enum": ["not_collected", "verbal", "received"]},
            "urgency_level": {"type": "string", "enum": ["high", "medium", "low"]},
        },
    },
}

UPDATE_SELLER_PROPERTY_TOOL = {
    "name": "update_seller_property",
    "description": (
        "Patch the active client's seller_profile. Call when the agent "
        "shares facts about the property they're selling, the listing "
        "agreement status, picture day, the CMA, etc. Pass only fields "
        "you've just learned."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "property_address": {"type": "string"},
            "property_type": {"type": "string"},
            "desired_list_price": {"type": "number"},
            "selling_timeline": {"type": "string"},
            "listing_agreement_status": {"type": "string", "enum": ["not_sent", "sent", "signed"]},
            "photos_status": {"type": "string", "enum": ["not_scheduled", "scheduled", "complete"]},
            "cma_status": {"type": "string", "enum": ["not_started", "in_progress", "complete"]},
            "showing_instructions": {"type": "string"},
            "occupancy_status": {"type": "string", "enum": ["owner", "tenant", "vacant"]},
            "payoff_amount": {"type": "number"},
        },
    },
}

RECORD_KNOWN_FACT_TOOL = {
    "name": "record_known_fact",
    "description": (
        "Append a structured fact to known_facts. Use this for anything "
        "that doesn't fit the buyer_profile / seller_profile schema — "
        "e.g. lender preferences, prior agent name, decision-maker, "
        "spouse involvement, special considerations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "field": {"type": "string", "description": "snake_case identifier"},
            "value": {"type": "string"},
            "source": {"type": "string", "enum": ["agent", "ai", "borrower"], "description": "Who told you"},
        },
        "required": ["field", "value"],
    },
}

SET_CLIENT_TYPE_TOOL = {
    "name": "set_client_type",
    "description": (
        "Set the client_type once you've established whether the lead "
        "is a buyer, seller, or both. Initializes the matching "
        "sub-profile so subsequent update_* calls have a place to write."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "client_type": {"type": "string", "enum": ["buyer", "seller", "buyer_and_seller", "unknown"]},
            "intent_summary": {"type": "string", "description": "Single-line gist of the lead"},
        },
        "required": ["client_type"],
    },
}

# ── ChatAction-emitting tools ───────────────────────────────────────
#
# Each of these tools doesn't actually fire a side effect — it tells
# the orchestrator "render an action card the agent can tap." The
# tap-handler in the frontend hits the matching endpoint.

PROPOSE_REQUEST_PREQUAL_TOOL = {
    "name": "propose_request_prequalification",
    "description": (
        "Render an action card titled 'Send {client name} for prequalification'. "
        "Use when the buyer is finance-ready (call compute_next_best_question "
        "first if unsure). Fires the existing /clients/{id}/request-prequalification "
        "handoff when the agent taps it."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

PROPOSE_SEND_BUYER_AGREEMENT_TOOL = {
    "name": "propose_send_buyer_agreement",
    "description": (
        "Render an action card to send a buyer agency agreement to the "
        "client. Use when buyer_agreement_status is 'not_sent' and the "
        "agent has confirmed they want to represent this buyer."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

PROPOSE_SEND_LISTING_AGREEMENT_TOOL = {
    "name": "propose_send_listing_agreement",
    "description": (
        "Render an action card to send a listing agreement. Use for "
        "seller-side leads where listing_agreement_status is 'not_sent' "
        "and you've confirmed the seller wants the agent to list."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

PROPOSE_LISTING_PREP_CHECKLIST_TOOL = {
    "name": "propose_create_listing_prep_checklist",
    "description": (
        "Render an action card to bulk-create the standard listing prep "
        "checklist (CMA + listing agreement + picture day + showing "
        "instructions + launch date). Use when a seller's listing_ready "
        "is false and there's no checklist yet."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

PROPOSE_DRAFT_FOLLOW_UP_TEXT_TOOL = {
    "name": "propose_draft_follow_up_text",
    "description": (
        "Render an action card with a drafted SMS to send to the client. "
        "The agent reviews + edits + sends. Use for cold-lead revival, "
        "showing follow-up, or quick check-ins. Pass the drafted body."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "draft_body": {"type": "string", "description": "The SMS text — keep under 320 chars."},
        },
        "required": ["draft_body"],
    },
}

PROPOSE_DRAFT_FOLLOW_UP_EMAIL_TOOL = {
    "name": "propose_draft_follow_up_email",
    "description": (
        "Render an action card with a drafted email. The agent reviews "
        "+ edits + sends. Use for property matches, listing updates, "
        "consultation invites."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "draft_subject": {"type": "string"},
            "draft_body": {"type": "string", "description": "The email body, plain text."},
        },
        "required": ["draft_body"],
    },
}

PROPOSE_MARK_FINANCE_READY_TOOL = {
    "name": "propose_mark_finance_ready",
    "description": (
        "Render an action card to flip relationship_stage='finance_ready'. "
        "Use when the buyer has all the data the funding team needs but "
        "the agent hasn't yet confirmed they want to hand off."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

REALTOR_TOOL_SCHEMAS = [
    UPDATE_BUYER_INTENT_TOOL,
    UPDATE_SELLER_PROPERTY_TOOL,
    RECORD_KNOWN_FACT_TOOL,
    SET_CLIENT_TYPE_TOOL,
    PROPOSE_REQUEST_PREQUAL_TOOL,
    PROPOSE_SEND_BUYER_AGREEMENT_TOOL,
    PROPOSE_SEND_LISTING_AGREEMENT_TOOL,
    PROPOSE_LISTING_PREP_CHECKLIST_TOOL,
    PROPOSE_DRAFT_FOLLOW_UP_TEXT_TOOL,
    PROPOSE_DRAFT_FOLLOW_UP_EMAIL_TOOL,
    PROPOSE_MARK_FINANCE_READY_TOOL,
]

# Set of profile-write tool names — these mutate state directly.
PROFILE_WRITE_TOOLS = {
    "update_buyer_intent",
    "update_seller_property",
    "record_known_fact",
    "set_client_type",
}

# Map: tool name → ChatAction kind that the propose_* tools emit.
# ChatActions don't fire side effects; the agent's tap does.
PROPOSE_TOOL_TO_ACTION_KIND = {
    "propose_request_prequalification": "request_prequalification",
    "propose_send_buyer_agreement": "send_buyer_agreement",
    "propose_send_listing_agreement": "send_listing_agreement",
    "propose_create_listing_prep_checklist": "create_listing_prep_checklist",
    "propose_draft_follow_up_text": "draft_follow_up_text",
    "propose_draft_follow_up_email": "draft_follow_up_email",
    "propose_mark_finance_ready": "mark_client_finance_ready",
}


# ── Tool execution ──────────────────────────────────────────────────


async def execute_realtor_tool(
    db: AsyncSession,
    *,
    tool_name: str,
    tool_input: dict,
    client_id: UUID,
    agent_id: UUID,
    accumulated_actions: list[dict],
) -> dict:
    """Dispatch a Realtor AI tool call. Profile-write tools mutate
    Client.realtor_profile directly. ChatAction tools append a card
    dict to `accumulated_actions` and return a 'queued' result."""
    if tool_name in PROFILE_WRITE_TOOLS:
        return await _execute_profile_write(
            db,
            tool_name=tool_name,
            tool_input=tool_input,
            client_id=client_id,
            agent_id=agent_id,
        )
    if tool_name in PROPOSE_TOOL_TO_ACTION_KIND:
        return _execute_propose_action(
            tool_name=tool_name,
            tool_input=tool_input,
            client_id=client_id,
            accumulated_actions=accumulated_actions,
        )
    log.warning("realtor_tool: unknown tool %s — no-op", tool_name)
    return {"ok": False, "error": "unknown_tool"}


async def _execute_profile_write(
    db: AsyncSession,
    *,
    tool_name: str,
    tool_input: dict,
    client_id: UUID,
    agent_id: UUID,
) -> dict:
    """Apply a tool-call to the Client's realtor_profile JSONB.
    Routes by tool name → patch shape consumed by apply_profile_patch."""
    client = await db.get(Client, client_id)
    if client is None:
        return {"ok": False, "error": "client_not_found"}

    current = client.realtor_profile or empty_profile(str(client_id), str(agent_id))

    patch: dict[str, Any] = {}
    if tool_name == "set_client_type":
        patch["client_type"] = tool_input.get("client_type", "unknown")
        if tool_input.get("intent_summary"):
            patch["intent_summary"] = tool_input["intent_summary"]
    elif tool_name == "update_buyer_intent":
        bp_patch: dict[str, Any] = {}
        # Allowed fields — anything else is dropped.
        for k in (
            "target_property_type",
            "target_location",
            "target_budget",
            "purchase_timeline",
            "financing_needed",
            "buyer_agreement_status",
            "proof_of_funds_status",
            "urgency_level",
        ):
            if k in tool_input and tool_input[k] is not None:
                bp_patch[k] = tool_input[k]
        # Range fields combine into a sub-object.
        lo = tool_input.get("target_budget_range_low")
        hi = tool_input.get("target_budget_range_high")
        if lo is not None and hi is not None:
            bp_patch["target_budget_range"] = {"low": lo, "high": hi}
        patch["buyer_profile"] = bp_patch
    elif tool_name == "update_seller_property":
        sp_patch: dict[str, Any] = {}
        for k in (
            "property_address",
            "property_type",
            "desired_list_price",
            "selling_timeline",
            "listing_agreement_status",
            "photos_status",
            "cma_status",
            "showing_instructions",
            "occupancy_status",
            "payoff_amount",
        ):
            if k in tool_input and tool_input[k] is not None:
                sp_patch[k] = tool_input[k]
        patch["seller_profile"] = sp_patch
    elif tool_name == "record_known_fact":
        patch["known_fact"] = {
            "field": tool_input.get("field"),
            "value": str(tool_input.get("value", "")),
            "source": tool_input.get("source", "ai"),
        }

    new_profile = apply_profile_patch(
        current,
        patch,
        client_id=str(client_id),
        agent_id=str(agent_id),
    )
    client.realtor_profile = new_profile
    await db.flush()

    return {
        "ok": True,
        "client_type": new_profile.get("client_type"),
        "readiness_score": new_profile.get("readiness_score"),
        "missing_facts": new_profile.get("missing_facts", []),
        "finance_ready": compute_finance_ready(new_profile),
        "listing_ready": compute_listing_ready(new_profile),
    }


def _execute_propose_action(
    *,
    tool_name: str,
    tool_input: dict,
    client_id: UUID,
    accumulated_actions: list[dict],
) -> dict:
    """Append a ChatAction dict to accumulated_actions. The
    message-send handler attaches the list to the assistant message
    at end-of-turn; the frontend renders one button per action."""
    action_kind = PROPOSE_TOOL_TO_ACTION_KIND[tool_name]
    label_map = {
        "request_prequalification": "Send for prequalification",
        "send_buyer_agreement": "Send buyer agreement",
        "send_listing_agreement": "Send listing agreement",
        "create_listing_prep_checklist": "Create listing prep checklist",
        "draft_follow_up_text": "Send SMS",
        "draft_follow_up_email": "Send email",
        "mark_client_finance_ready": "Mark finance-ready",
    }
    action: dict[str, Any] = {
        "kind": action_kind,
        "label": label_map.get(action_kind, "Confirm"),
        "client_id": str(client_id),
        "confirm": True,
    }
    # Drafted message bodies ride on the action so the frontend can
    # render an editable textarea inside the card.
    if tool_name == "propose_draft_follow_up_text":
        action["draft_body"] = str(tool_input.get("draft_body", ""))[:320]
    if tool_name == "propose_draft_follow_up_email":
        action["draft_body"] = str(tool_input.get("draft_body", ""))[:4000]
        if tool_input.get("draft_subject"):
            action["draft_subject"] = str(tool_input["draft_subject"])[:200]

    accumulated_actions.append(action)
    return {"ok": True, "action_kind": action_kind, "queued": True}
