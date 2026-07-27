"""Realtor Client Intelligence Profile — derived helpers.

The Realtor AI reads + writes `Client.realtor_profile` (JSONB) on
every conversational turn. This module owns the pure-Python helpers
that operate on the profile dict:

  - `compute_finance_ready(profile)` — has the AI gathered enough to
    hand off to the Bank AI?
  - `compute_listing_ready(profile)` — has the AI gathered enough to
    launch a listing?
  - `compute_missing_facts(profile)` — what gaps remain? Drives
    `next_best_question`.
  - `compute_readiness_score(profile)` — 0-100 percentage shown on
    the Client Readiness Card.
  - `apply_profile_patch(profile, patch)` — merge a patch from the AI
    (record_known_fact / update_buyer_intent / etc.) and recompute
    derived fields.

All helpers are pure: profile in, profile or computed value out. No
DB I/O. The router persists the result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ── Required-field maps ──────────────────────────────────────────────

# Buyer fields the AI must capture before finance_ready flips true.
# These are the minimum the funding team needs to triage a quote.
_BUYER_FINANCE_READY_FIELDS = (
    "target_property_type",
    "target_location",
    "purchase_timeline",
    "financing_needed",
)
# `target_budget` OR `target_budget_range` satisfies the budget field
# — checked in compute_finance_ready directly.

# Seller fields required before listing_ready flips true.
_SELLER_LISTING_READY_FIELDS = (
    "property_address",
    "desired_list_price",
)
# `listing_agreement_status` must be "signed", `cma_status` ≠ "not_started",
# `photos_status` ≠ "not_scheduled" — checked directly below.


# Default profile used when a Client has no realtor_profile yet.
def empty_profile(client_id: str, agent_id: str) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "agent_id": agent_id,
        "client_type": "unknown",
        "relationship_stage": "new_lead",
        "intent_summary": "",
        "buyer_profile": None,
        "seller_profile": None,
        "known_facts": [],
        "missing_facts": [],
        "documents": [],
        "open_tasks": [],
        "next_best_question": None,
        "next_best_action": None,
        "readiness_score": 0,
    }


# ── Derived predicates ──────────────────────────────────────────────


def compute_finance_ready(profile: dict[str, Any] | None) -> bool:
    """True when the AI has captured enough buyer context to hand the
    lead off to the funding team for prequalification.

    Required: contact info (assumed present on the parent Client row),
    target_property_type, target_location, purchase_timeline,
    financing_needed=True. Budget can be exact OR a range — either
    satisfies. Seller-side leads are never finance_ready (they go
    through listing_ready instead)."""
    if not profile:
        return False
    if profile.get("client_type") not in ("buyer", "buyer_and_seller"):
        return False
    bp = profile.get("buyer_profile") or {}
    # Budget: either exact OR a range with both bounds set.
    has_budget = bp.get("target_budget") is not None or (
        isinstance(bp.get("target_budget_range"), dict)
        and bp["target_budget_range"].get("low") is not None
        and bp["target_budget_range"].get("high") is not None
    )
    if not has_budget:
        return False
    for field in _BUYER_FINANCE_READY_FIELDS:
        if bp.get(field) in (None, "", False):
            # `financing_needed=False` (cash buyer) also blocks —
            # we only finance buyers who need financing.
            return False
    return True


def compute_listing_ready(profile: dict[str, Any] | None) -> bool:
    """True when the AI has captured enough seller-side context to
    launch the listing. Required: property_address,
    desired_list_price, listing_agreement_status="signed",
    cma_status≠"not_started", photos_status≠"not_scheduled"."""
    if not profile:
        return False
    if profile.get("client_type") not in ("seller", "buyer_and_seller"):
        return False
    sp = profile.get("seller_profile") or {}
    for field in _SELLER_LISTING_READY_FIELDS:
        if sp.get(field) in (None, "", 0):
            return False
    if sp.get("listing_agreement_status") != "signed":
        return False
    if sp.get("cma_status") in (None, "not_started"):
        return False
    if sp.get("photos_status") in (None, "not_scheduled"):
        return False
    return True


def compute_missing_facts(profile: dict[str, Any] | None) -> list[str]:
    """List of fields the AI hasn't yet captured — drives
    `next_best_question`. Buyer-side returns buyer fields; seller-side
    returns seller fields; buyer_and_seller returns both."""
    if not profile:
        return ["client_type"]
    out: list[str] = []
    if profile.get("client_type") == "unknown":
        out.append("client_type")
        return out
    ctype = profile.get("client_type")
    if ctype in ("buyer", "buyer_and_seller"):
        bp = profile.get("buyer_profile") or {}
        if not bp.get("target_property_type"):
            out.append("buyer.target_property_type")
        if not bp.get("target_location"):
            out.append("buyer.target_location")
        # Budget OR range satisfies.
        if bp.get("target_budget") is None and not bp.get("target_budget_range"):
            out.append("buyer.target_budget")
        if not bp.get("purchase_timeline"):
            out.append("buyer.purchase_timeline")
        if bp.get("financing_needed") is None:
            out.append("buyer.financing_needed")
        if bp.get("buyer_agreement_status") in (None, "not_sent"):
            out.append("buyer.buyer_agreement_status")
    if ctype in ("seller", "buyer_and_seller"):
        sp = profile.get("seller_profile") or {}
        if not sp.get("property_address"):
            out.append("seller.property_address")
        if sp.get("desired_list_price") in (None, 0):
            out.append("seller.desired_list_price")
        if sp.get("listing_agreement_status") in (None, "not_sent"):
            out.append("seller.listing_agreement_status")
        if sp.get("cma_status") in (None, "not_started"):
            out.append("seller.cma_status")
        if sp.get("photos_status") in (None, "not_scheduled"):
            out.append("seller.photos_status")
    return out


def compute_readiness_score(profile: dict[str, Any] | None) -> int:
    """0-100 readiness percentage. Buyer score = (captured_buyer_fields /
    total_buyer_fields) * 100. Seller score = same for seller fields.
    Mixed type returns the lower of the two so the lower-leverage side
    drives the agent's attention."""
    if not profile:
        return 0
    ctype = profile.get("client_type")
    if not ctype or ctype == "unknown":
        return 0

    def _buyer_score() -> int:
        # 7 dimensions. Each missing entry on the missing_facts list
        # subtracts a slot. Cap at 100 / floor at 0.
        bp = profile.get("buyer_profile") or {}
        total = 7
        captured = 0
        if bp.get("target_property_type"): captured += 1
        if bp.get("target_location"): captured += 1
        if bp.get("target_budget") is not None or bp.get("target_budget_range"):
            captured += 1
        if bp.get("purchase_timeline"): captured += 1
        if bp.get("financing_needed") is not None: captured += 1
        if bp.get("buyer_agreement_status") not in (None, "not_sent"):
            captured += 1
        if bp.get("prequalified"): captured += 1
        return round((captured / total) * 100)

    def _seller_score() -> int:
        sp = profile.get("seller_profile") or {}
        total = 5
        captured = 0
        if sp.get("property_address"): captured += 1
        if sp.get("desired_list_price"): captured += 1
        if sp.get("listing_agreement_status") not in (None, "not_sent"):
            captured += 1
        if sp.get("cma_status") not in (None, "not_started"):
            captured += 1
        if sp.get("photos_status") not in (None, "not_scheduled"):
            captured += 1
        return round((captured / total) * 100)

    if ctype == "buyer":
        return _buyer_score()
    if ctype == "seller":
        return _seller_score()
    # buyer_and_seller — return the lower (more friction) side.
    return min(_buyer_score(), _seller_score())


# ── Patch application ───────────────────────────────────────────────


def apply_profile_patch(
    profile: dict[str, Any] | None,
    patch: dict[str, Any],
    *,
    client_id: str,
    agent_id: str,
) -> dict[str, Any]:
    """Merge a patch from the AI tool layer onto the profile, then
    recompute derived fields (missing_facts + readiness_score).

    `patch` shape mirrors the public-facing tool schemas:

        {"client_type": "buyer"}
        {"buyer_profile": {"target_property_type": "commercial"}}
        {"seller_profile": {"property_address": "123 Main"}}
        {"known_fact": {"field": "lender_pref", "value": "Chase",
                        "source": "agent"}}
        {"document": {"name": "Buyer Agreement", "status": "sent"}}
        {"open_task": {"title": "Schedule consultation",
                       "due_date": "2026-05-12", "reason": "fresh lead"}}
        {"relationship_stage": "agreement_pending"}
        {"intent_summary": "Buying a 4-unit in Bushwick by August."}

    Every patch goes through here so derived fields stay in sync.
    Returns the new profile dict (caller persists)."""
    base = dict(profile) if profile else empty_profile(client_id, agent_id)
    base.setdefault("client_id", client_id)
    base.setdefault("agent_id", agent_id)

    if "client_type" in patch:
        base["client_type"] = patch["client_type"]
        # Auto-init the matching sub-profile so downstream patches
        # don't have to special-case None.
        if patch["client_type"] in ("buyer", "buyer_and_seller") and not base.get("buyer_profile"):
            base["buyer_profile"] = _empty_buyer_profile()
        if patch["client_type"] in ("seller", "buyer_and_seller") and not base.get("seller_profile"):
            base["seller_profile"] = _empty_seller_profile()

    if "intent_summary" in patch:
        base["intent_summary"] = str(patch["intent_summary"]).strip()[:400]

    if "relationship_stage" in patch:
        base["relationship_stage"] = patch["relationship_stage"]

    if "buyer_profile" in patch and isinstance(patch["buyer_profile"], dict):
        bp = base.get("buyer_profile") or _empty_buyer_profile()
        bp.update({k: v for k, v in patch["buyer_profile"].items() if v is not None})
        base["buyer_profile"] = bp

    if "seller_profile" in patch and isinstance(patch["seller_profile"], dict):
        sp = base.get("seller_profile") or _empty_seller_profile()
        sp.update({k: v for k, v in patch["seller_profile"].items() if v is not None})
        base["seller_profile"] = sp

    if "known_fact" in patch and isinstance(patch["known_fact"], dict):
        fact = patch["known_fact"]
        fact.setdefault("captured_at", datetime.now(timezone.utc).isoformat())
        # Visibility (default client_visible — preserves prior behavior for
        # AI-recorded facts, which inform the client-visible readiness view).
        # Callers that want a private note (e.g. add_agent_note) set
        # visibility="agent_private" on the patch dict explicitly.
        fact.setdefault("visibility", "client_visible")
        # Dedup by field — most-recent wins.
        existing = [f for f in (base.get("known_facts") or []) if f.get("field") != fact.get("field")]
        existing.append(fact)
        base["known_facts"] = existing

    if "document" in patch and isinstance(patch["document"], dict):
        doc = patch["document"]
        existing = [d for d in (base.get("documents") or []) if d.get("name") != doc.get("name")]
        existing.append(doc)
        base["documents"] = existing

    if "open_task" in patch and isinstance(patch["open_task"], dict):
        existing = base.get("open_tasks") or []
        existing.append(patch["open_task"])
        base["open_tasks"] = existing

    if "next_best_question" in patch:
        base["next_best_question"] = patch["next_best_question"]
    if "next_best_action" in patch:
        base["next_best_action"] = patch["next_best_action"]

    # Recompute derived fields every patch so the profile is always
    # internally consistent. missing_facts drives the AI's next prompt;
    # readiness_score drives the UI card.
    base["missing_facts"] = compute_missing_facts(base)
    base["readiness_score"] = compute_readiness_score(base)

    return base


# ── Resolver-backed sibling (alembic 0032) ──────────────────────────
#
# This is the playbook-driven version of `compute_missing_facts`. Phase
# 4 will wire the Realtor AI router to call this instead of the sync
# helper above. Both stay parallel during the transition so the
# existing test_realtor_profile_shape.py contracts still hold and we
# can swap callers one at a time.


async def compute_missing_facts_from_resolver(
    db: "Any",  # AsyncSession — typed as Any to avoid an import cycle in tests
    *,
    client_id: "Any",  # UUID
    agent_id: "Any" = None,  # UUID | None
    side: str | None = None,
    context: dict[str, Any] | None = None,
) -> list[str]:
    """Resolver-backed missing-facts list. Returns the requirement_key
    of each non-satisfied required item from the resolved buyer/seller
    playbook.

    `context` is the dict the resolver evaluates `applies_when` against.
    Caller usually passes the same dict that plan_builder builds (see
    plan_builder._build_resolver_context).

    Falls back to an empty list when no playbooks match — the legacy
    `compute_missing_facts` keeps the old hardcoded behavior for any
    caller that hasn't migrated yet."""
    from app.services.ai.requirement_resolver import resolve_requirements  # local: avoid circular import

    resolved = await resolve_requirements(
        db,
        client_id=client_id,
        loan_id=None,
        phase="realtor",
        side=side,  # type: ignore[arg-type]
        agent_id=agent_id,
        context=context or {},
    )
    return [r.requirement_key for r in resolved if r.required_level == "required"]


def filter_known_facts_for_client(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip agent-private known_facts before a profile reaches the CLIENT
    themselves (GET/PATCH /clients/me). Mirrors the exclude-agent_private
    half of services/handoff._filter_visibility, scoped to just this one
    field. Returns a new dict — never mutates the caller's profile, so this
    is safe to call on a transient response object without touching the
    persisted row."""
    if not isinstance(profile, dict):
        return profile
    known_facts = profile.get("known_facts")
    if not isinstance(known_facts, list):
        return profile
    filtered = [
        fact for fact in known_facts
        if not (isinstance(fact, dict) and fact.get("visibility") == "agent_private")
    ]
    if len(filtered) == len(known_facts):
        return profile
    return {**profile, "known_facts": filtered}


def _empty_buyer_profile() -> dict[str, Any]:
    return {
        "target_property_type": None,
        "target_location": None,
        "target_budget": None,
        "target_budget_range": None,
        "purchase_timeline": None,
        "financing_needed": None,
        "prequalified": False,
        "buyer_agreement_status": "not_sent",
        "proof_of_funds_status": "not_collected",
        "urgency_level": "medium",
        "showing_activity": [],
    }


def _empty_seller_profile() -> dict[str, Any]:
    return {
        "property_address": None,
        "property_type": None,
        "desired_list_price": None,
        "selling_timeline": None,
        "listing_agreement_status": "not_sent",
        "photos_status": "not_scheduled",
        "cma_status": "not_started",
        "showing_instructions": None,
        "occupancy_status": None,
        "payoff_amount": None,
    }
