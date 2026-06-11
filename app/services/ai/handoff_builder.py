"""Lending Handoff Packet builder.

Reads the agent's Realtor Client Intelligence Profile + the realtor
thread history + already-uploaded documents and assembles a
structured packet the Lending AI inherits as memory.

Pure logic — no LLM call in v1. Future iterations may add a
Claude-summarization pass for `handoff_summary`; the field is
populated deterministically here from the realtor_profile.

Output shape mirrors LendingHandoffPacket model columns. Caller
(routers/clients.py:request_prequalification) persists the dict.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_chat_thread import AIChatMessage, AIChatThread
from app.models.client import Client
from app.models.document import Document


# ── Canonical lending-side fields the Lending AI needs to start ────

# These map onto the existing intake / prequal fields the funding team
# actually uses. The realtor profile usually doesn't carry these
# verbatim; the Lending AI picks them up in conversation, one at a
# time, after the handoff. The list seeds `missing_lending_items`.
_LENDING_REQUIRED_FIELDS = [
    "borrower_entity_type",     # individual / LLC / Corp / Trust
    "credit_authorization",     # signed soft-pull consent
    "liquidity_docs",           # bank statements / proof of funds
    "property_address",         # final subject property
    "rent_or_income_details",   # DSCR side
    "experience_tier",          # past investment deals
]

# Document types that are relevant to the LENDING side (vs the
# realtor side). Used to mark `relevant_to_lending` on each uploaded
# doc reference.
_LENDING_RELEVANT_DOC_NAMES = {
    "bank statements",
    "tax returns",
    "operating agreement",
    "purchase agreement",
    "purchase contract",
    "rent roll",
    "credit report",
    "proof of funds",
    "insurance binder",
    "title commitment",
    "appraisal",
}


async def build_handoff_packet(
    db: AsyncSession,
    *,
    client: Client,
    agent_id: UUID,
    realtor_thread_id: UUID | None,
) -> dict[str, Any]:
    """Construct the handoff packet payload for a Client. Returns a
    dict ready to be passed to LendingHandoffPacket(**packet)."""

    profile = client.realtor_profile or {}
    buyer_profile = profile.get("buyer_profile") or {}
    seller_profile = profile.get("seller_profile") or {}
    client_type = profile.get("client_type") or client.client_type or "unknown"
    relationship_stage = profile.get("relationship_stage") or "new_lead"

    # Realtor summary — light-weight snapshot the Lending AI sees as
    # "what the agent told us".
    realtor_summary = {
        "client_type": client_type,
        "relationship_stage": relationship_stage,
        "intent_summary": profile.get("intent_summary") or "",
        "agent_notes": _collect_agent_notes(profile),
        "readiness_score": profile.get("readiness_score") or 0,
    }

    extracted_facts = _extract_facts(client, buyer_profile, seller_profile, profile)
    missing = _compute_missing_lending_items(client, buyer_profile)
    docs = await _collect_documents_for_client(db, client.id)
    recommended = _recommend_path(client_type, buyer_profile, seller_profile)
    first_question = _first_lending_question(missing, buyer_profile, client_type)

    handoff_summary = _build_handoff_summary(
        client,
        client_type=client_type,
        buyer_profile=buyer_profile,
        seller_profile=seller_profile,
        intent_summary=profile.get("intent_summary"),
    )

    visibility_rules = _default_visibility_rules()

    return {
        "client_id": client.id,
        "agent_id": agent_id,
        "realtor_thread_id": realtor_thread_id,
        "handoff_reason": "agent_marked_ready_for_lending",
        "handoff_summary": handoff_summary,
        "realtor_summary": realtor_summary,
        "extracted_facts": extracted_facts,
        "missing_lending_items": missing,
        "uploaded_document_refs": docs,
        "recommended_lending_path": recommended,
        "first_lending_question": first_question,
        "visibility_rules": visibility_rules,
    }


# ── Helpers ────────────────────────────────────────────────────────


def _extract_facts(
    client: Client,
    buyer_profile: dict[str, Any],
    seller_profile: dict[str, Any],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Pull structured facts from the realtor profile + the Client row.
    Each fact carries source + confidence so the Lending AI can decide
    whether to verify."""
    facts: list[dict[str, Any]] = []

    def add(field: str, value: Any, source: str = "realtor_chat", confidence: float = 0.85, visibility: str = "bank_visible") -> None:
        if value in (None, "", 0):
            return
        facts.append({
            "field": field,
            "value": value,
            "source": source,
            "confidence": confidence,
            "visibility": visibility,
        })

    # From the Client row directly — high confidence, structured.
    add("client_name", client.name, source="client_row", confidence=1.0, visibility="bank_visible")
    add("client_email", client.email, source="client_row", confidence=1.0, visibility="bank_visible")
    add("client_phone", client.phone, source="client_row", confidence=1.0, visibility="bank_visible")
    add("client_type", client.client_type, source="client_row", confidence=1.0, visibility="bank_visible")
    if client.fico is not None:
        add("fico", client.fico, source="client_row", confidence=1.0, visibility="bank_visible")
    if client.tier and client.tier != "standard":
        add("tier", client.tier, source="client_row", confidence=1.0, visibility="bank_visible")

    # Buyer-profile facts — captured by the Realtor AI in conversation.
    if buyer_profile:
        add("target_property_type", buyer_profile.get("target_property_type"))
        add("target_location", buyer_profile.get("target_location"))
        add("target_budget", buyer_profile.get("target_budget"))
        if buyer_profile.get("target_budget_range"):
            add("target_budget_range", buyer_profile["target_budget_range"])
        add("purchase_timeline", buyer_profile.get("purchase_timeline"))
        if buyer_profile.get("financing_needed") is not None:
            add("financing_needed", buyer_profile["financing_needed"])
        if buyer_profile.get("buyer_agreement_status"):
            add("buyer_agreement_status", buyer_profile["buyer_agreement_status"])
        if buyer_profile.get("proof_of_funds_status"):
            add("proof_of_funds_status", buyer_profile["proof_of_funds_status"])

    # Seller-profile facts.
    if seller_profile:
        add("property_address", seller_profile.get("property_address"))
        add("property_type", seller_profile.get("property_type"))
        add("desired_list_price", seller_profile.get("desired_list_price"))
        add("listing_agreement_status", seller_profile.get("listing_agreement_status"))
        add("occupancy_status", seller_profile.get("occupancy_status"))
        if seller_profile.get("payoff_amount"):
            add("payoff_amount", seller_profile["payoff_amount"])

    # Free-form known_facts the Realtor AI captured. Source is whatever
    # the AI tagged on each fact.
    for f in profile.get("known_facts") or []:
        if not isinstance(f, dict):
            continue
        facts.append({
            "field": f.get("field"),
            "value": f.get("value"),
            "source": f.get("source") or "realtor_chat",
            "confidence": 0.7,
            "visibility": "bank_visible",
        })

    return facts


def _compute_missing_lending_items(
    client: Client,
    buyer_profile: dict[str, Any],
) -> list[str]:
    """Lending-side gaps the Realtor AI didn't fill. Seeded from
    `_LENDING_REQUIRED_FIELDS`; trimmed when the realtor profile or
    the Client row already covers a field."""
    missing = list(_LENDING_REQUIRED_FIELDS)
    # Property address — supplied if buyer/seller has one OR client.address is set.
    if buyer_profile.get("target_location") or client.address:
        if "property_address" in missing:
            missing.remove("property_address")
    # Rent/income — DSCR-relevant. If financing isn't needed (cash), we
    # don't need it either. If a target_budget is captured, that's
    # adjacent context.
    if buyer_profile.get("financing_needed") is False:
        if "rent_or_income_details" in missing:
            missing.remove("rent_or_income_details")
    return missing


async def _collect_documents_for_client(
    db: AsyncSession,
    client_id: UUID,
) -> list[dict[str, Any]]:
    """Walk the client's loans → documents and surface what's already
    uploaded. Each doc is tagged with relevant_to_lending so the
    Lending AI knows which to ask about and which were realtor-side."""
    rows = (
        await db.execute(
            select(Document)
            .join(Document.loan)
            .where(Document.loan.has(client_id=client_id))
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for d in rows:
        name_lower = (d.name or "").lower()
        relevant = any(k in name_lower for k in _LENDING_RELEVANT_DOC_NAMES)
        out.append({
            "document_id": str(d.id),
            "document_name": d.name,
            "document_type": d.checklist_key or "uncategorized",
            "status": d.status,
            "verified": d.verified_at is not None,
            "relevant_to_lending": relevant,
        })
    return out


def _recommend_path(
    client_type: str,
    buyer_profile: dict[str, Any],
    seller_profile: dict[str, Any],
) -> dict[str, Any]:
    """Best-guess at what lending path makes sense given what the
    realtor AI captured. Funding team can override on review."""
    if client_type == "seller":
        return {
            "path": "seller_listing",
            "loan_type_guess": None,
            "urgency": "low",
            "rationale": "Listings don't carry a lending leg. Funding-team review optional unless seller also needs financing.",
        }
    # Buyer-side guess.
    target_type = (buyer_profile.get("target_property_type") or "").lower()
    if target_type in ("multifamily", "mixed_use", "commercial", "retail", "office", "industrial"):
        loan_type = "commercial_purchase"  # Generic; UW assigns specific program.
    elif target_type in ("single_family", "sfr"):
        loan_type = "DSCR"
    else:
        loan_type = "DSCR"
    timeline = (buyer_profile.get("purchase_timeline") or "").lower()
    urgency = "high" if timeline == "asap" else "medium" if timeline in ("0_30", "30_60") else "low"
    return {
        "path": "prequalification",
        "loan_type_guess": loan_type,
        "urgency": urgency,
        "rationale": (
            f"Buyer-side lead with target_property_type='{target_type or 'unknown'}'. "
            f"Recommend {loan_type} prequal. Timeline drives urgency."
        ),
    }


def _first_lending_question(
    missing: list[str],
    buyer_profile: dict[str, Any],
    client_type: str,
) -> str:
    """Single, intelligent first question the Lending AI opens with —
    not a form dump, not generic. Based on the highest-leverage missing
    field. The AI will revise as it talks; this is the BOOTSTRAP
    question shown as the very first lending-thread message."""
    if client_type == "seller":
        return (
            "Got the listing context from your agent. Will the seller need any "
            "financing on the proceeds — bridge loan, 1031 exchange, refi on "
            "another property — or is this a clean sale?"
        )
    # Buyer path. Pick the FIRST important gap the AI should close.
    if "borrower_entity_type" in missing:
        return (
            "I have the lead context from the realtor side. To start the lending "
            "package correctly, I need one thing first: will this buyer be "
            "purchasing personally (in their own name) or through an entity (LLC, "
            "Corp, Trust)?"
        )
    if "property_address" in missing and not buyer_profile.get("target_location"):
        return (
            "I see the buyer's looking around but doesn't have a specific address "
            "locked yet. Once they pick a target property — or even a tight area "
            "with a price range — I can run a real prequal. Want me to start with "
            "a budget-based prequal in the meantime?"
        )
    return (
        "I've got the realtor-side context. Walk me through the first detail — "
        "is this purchase being made personally or through an entity?"
    )


def _build_handoff_summary(
    client: Client,
    *,
    client_type: str,
    buyer_profile: dict[str, Any],
    seller_profile: dict[str, Any],
    intent_summary: str | None,
) -> str:
    """Plain-English summary the Lending AI sees as "the agent's
    handoff narrative." 5-line max, structured so it's parseable by
    a human glancing at Elara Inbox."""
    lines = [
        f"Client: {client.name}" + (f" ({client.email})" if client.email else ""),
        f"Type: {client_type}",
    ]
    if intent_summary:
        lines.append(f"Intent: {intent_summary}")
    if client_type in ("buyer", "buyer_and_seller") and buyer_profile:
        bits = []
        if buyer_profile.get("target_property_type"):
            bits.append(buyer_profile["target_property_type"])
        if buyer_profile.get("target_location"):
            bits.append(buyer_profile["target_location"])
        if buyer_profile.get("target_budget"):
            bits.append(f"~${int(buyer_profile['target_budget']):,}")
        elif buyer_profile.get("target_budget_range"):
            r = buyer_profile["target_budget_range"]
            bits.append(f"${int(r['low']):,}–${int(r['high']):,}")
        if buyer_profile.get("purchase_timeline"):
            bits.append(buyer_profile["purchase_timeline"])
        if bits:
            lines.append("Buyer: " + " · ".join(bits))
    if client_type in ("seller", "buyer_and_seller") and seller_profile:
        bits = []
        if seller_profile.get("property_address"):
            bits.append(seller_profile["property_address"])
        if seller_profile.get("desired_list_price"):
            bits.append(f"list ~${int(seller_profile['desired_list_price']):,}")
        if seller_profile.get("listing_agreement_status"):
            bits.append(f"agreement: {seller_profile['listing_agreement_status']}")
        if bits:
            lines.append("Seller: " + " · ".join(bits))
    return "\n".join(lines)


def _collect_agent_notes(profile: dict[str, Any]) -> str:
    """Pull anything the agent wrote in known_facts that's tagged
    source='agent' — these are the agent's own observations the AI
    captured during conversation. Not for client visibility."""
    bits: list[str] = []
    for f in profile.get("known_facts") or []:
        if isinstance(f, dict) and f.get("source") == "agent":
            bits.append(f"{f.get('field')}: {f.get('value')}")
    return "; ".join(bits)


# ── Resolver-backed sibling (alembic 0032) ─────────────────────────
#
# Phase 4 will wire the lending packet build to call this instead of
# `_compute_missing_lending_items`. Both stay parallel during the
# transition so the existing test_lending_handoff_builder.py contracts
# still hold and we can swap callers one at a time.


async def compute_missing_lending_items_from_resolver(
    db: AsyncSession,
    *,
    client_id: UUID,
    loan_id: UUID | None,
    loan_product: str | None,
    context: dict[str, Any] | None = None,
) -> list[str]:
    """Resolver-backed missing-lending-items list. Returns the
    requirement_key of each non-satisfied required item from the
    resolved loan-product playbook.

    Falls back to the legacy `_LENDING_REQUIRED_FIELDS` set when no
    matching loan-product playbook is found — keeps the existing
    handoff packet shape stable until Phase 4 flips the caller."""
    from app.services.ai.requirement_resolver import resolve_requirements  # local: avoid circular import

    resolved = await resolve_requirements(
        db,
        client_id=client_id,
        loan_id=loan_id,
        phase="lending",
        loan_product=loan_product,
        context=context or {},
    )
    if not resolved:
        return list(_LENDING_REQUIRED_FIELDS)
    return [r.requirement_key for r in resolved if r.required_level == "required"]


def _default_visibility_rules() -> dict[str, Any]:
    """Per-fact visibility template. v1 is operator-default — every
    fact is bank_visible (the funding team can see it) and the
    client-facing AI is gated to client_visible only when the field
    is whitelisted. Future iterations expose this on the UI for the
    agent to tune per-fact."""
    return {
        "default": "bank_visible",
        "client_visible_fields": [
            "client_name",
            "target_property_type",
            "target_location",
            "target_budget",
            "purchase_timeline",
            "financing_needed",
            "property_address",
            "desired_list_price",
        ],
        "internal_only_fields": [
            # Anything the agent typed about the client's reliability /
            # liquidity / personal situation stays internal.
            "agent_notes",
        ],
    }
