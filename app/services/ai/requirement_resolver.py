"""Requirement resolver — replaces the hardcoded `_LENDING_REQUIRED_FIELDS` /
`STARTER_BUYER_DOCS` / `STARTER_SELLER_DOCS` constants with a
playbook-driven, layered model.

Walks the resolution order:

  1. Platform rules (firm-wide defaults)
  2. Funding / loan-product requirements (UW authority)
  3. Agent playbook (relationship preferences)
  4. Client-specific overrides (per-deal — applied later by the
     plan builder, not here; this resolver returns the merged
     PRE-override list)
  5. AI-detected needs / evidence / stage — also handled by the plan
     builder which folds in `client_requirement_status` rows.

Each result row carries `source` so the plan builder can decide
whether a given requirement is overridable by the agent or only by
underwriting.

Pure DB read. No mutation. Used by:

  - Realtor AI's `compute_missing_facts` (Phase 4 will wire this in)
  - Lending AI's `build_handoff_packet`
  - Plan builder's `rebuild()` and `preview()`
  - Cadence engine (Phase 5) — picks active requirements then walks
    cadence rules looking for triggers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate


# ── Public types ───────────────────────────────────────────────────


@dataclass
class ResolvedRequirement:
    """One requirement, with its source attribution."""
    requirement_key: str
    label: str
    category: str
    required_level: str
    blocks_stage: str | None
    visibility: list[str]
    can_agent_override: bool
    can_underwriter_waive: bool
    verification_required: bool
    expiration_days: int | None
    ai_request_message_template: str | None
    display_order: int

    # Provenance (set by the resolver, not the requirement row).
    source: str
    """One of: platform | funding_required | agent_playbook."""
    playbook_id: UUID
    playbook_version: int
    playbook_name: str

    # `applies_when` is left out of the public type because it's
    # already evaluated by the resolver (it filters the list before
    # returning). Caller doesn't need to re-evaluate.


# ── Resolver ───────────────────────────────────────────────────────


async def resolve_requirements(
    db: AsyncSession,
    *,
    client_id: UUID,
    loan_id: UUID | None,
    phase: Literal["realtor", "lending"],
    loan_product: str | None = None,
    side: Literal["buyer", "seller"] | None = None,
    agent_id: UUID | None = None,
    funding_owner_id: UUID | None = None,
    pinned_versions: dict[str, int] | None = None,
    context: dict[str, Any] | None = None,
) -> list[ResolvedRequirement]:
    """Return the merged active requirement list for this (client,
    loan, phase). Each result carries source attribution.

    Args:
        client_id / loan_id: identifies the deal scope.
        phase: realtor (pre-handoff) or lending (post-handoff).
        loan_product: when phase=lending, the product key (dscr_purchase
            etc.) selects the loan-product playbook.
        side: when phase=realtor, picks the buyer or seller playbook.
        agent_id: when set, the agent's overlay playbook is included.
        funding_owner_id: when set, the funding team's overlay is
            included instead of the platform default for that
            playbook_type.
        pinned_versions: {playbook_id_str: version}. When provided,
            the resolver loads those exact versions; otherwise it
            picks the latest published version of each playbook.
        context: free-form facts used to evaluate `applies_when` —
            e.g. {"under_contract": True, "borrower_type": "entity",
            "financing_needed": False}. Caller assembles this from
            the realtor profile + loan row + buyer profile.

    Returns: list of ResolvedRequirement, deduped by requirement_key
    (later entries in the resolution order override earlier — funding
    overrides platform; agent overrides funding for items where
    can_agent_override=true, otherwise agent's same-key entry is
    DROPPED in favor of the higher-authority row)."""

    context = context or {}
    pinned_versions = pinned_versions or {}

    # ── Step 1: gather playbook IDs to load ────────────────────────
    playbooks = await _select_active_playbooks(
        db,
        phase=phase,
        loan_product=loan_product,
        side=side,
        agent_id=agent_id,
        funding_owner_id=funding_owner_id,
        pinned_versions=pinned_versions,
    )

    # ── Step 2: load requirements per playbook ─────────────────────
    by_key: dict[str, ResolvedRequirement] = {}

    # Walk in resolution order: platform → funding → agent. For each
    # requirement_key, the LAST writer wins UNLESS the existing entry
    # is funding-required and the incoming agent entry has
    # can_agent_override=False on the funding row (we keep the
    # funding row).
    for pb in playbooks:
        source = _source_for(pb)
        rows = (
            await db.execute(
                select(AICollectionRequirement)
                .where(AICollectionRequirement.playbook_id == pb.id)
                .order_by(AICollectionRequirement.display_order, AICollectionRequirement.requirement_key)
            )
        ).scalars().all()
        for r in rows:
            if not _applies_when_matches(r.applies_when, context):
                continue
            existing = by_key.get(r.requirement_key)
            if existing is not None and existing.source == "funding_required" and not existing.can_agent_override and source == "agent_playbook":
                # Funding owns this key and disabled agent overrides — keep the funding row.
                continue
            by_key[r.requirement_key] = ResolvedRequirement(
                requirement_key=r.requirement_key,
                label=r.label,
                category=r.category,
                required_level=r.required_level,
                blocks_stage=r.blocks_stage,
                visibility=list(r.visibility or []),
                can_agent_override=r.can_agent_override,
                can_underwriter_waive=r.can_underwriter_waive,
                verification_required=r.verification_required,
                expiration_days=r.expiration_days,
                ai_request_message_template=r.ai_request_message_template,
                display_order=r.display_order,
                source=source,
                playbook_id=pb.id,
                playbook_version=pb.version,
                playbook_name=pb.name,
            )

    # Stable order: by display_order, then requirement_key.
    return sorted(by_key.values(), key=lambda x: (x.display_order, x.requirement_key))


# ── Internal helpers ───────────────────────────────────────────────


async def _select_active_playbooks(
    db: AsyncSession,
    *,
    phase: Literal["realtor", "lending"],
    loan_product: str | None,
    side: Literal["buyer", "seller"] | None,
    agent_id: UUID | None,
    funding_owner_id: UUID | None,
    pinned_versions: dict[str, int],
) -> list[AIPlaybookTemplate]:
    """Pick the playbooks that apply, in resolution order
    (platform → funding → agent).

    Each (playbook_type, product_key) slot resolves to a single
    playbook per owner; later slots in the order LAYER on top of
    earlier ones."""
    out: list[AIPlaybookTemplate] = []

    # Decide which playbook_types we need.
    types_wanted: list[tuple[str, str | None]] = []
    if phase == "realtor":
        if side in ("buyer", None):
            types_wanted.append(("buyer", None))
        if side in ("seller", None):
            types_wanted.append(("seller", None))
    else:  # lending
        if loan_product:
            types_wanted.append(("loan_product", loan_product))
        else:
            # No product chosen yet — fall back to all loan_product
            # playbooks; caller's plan builder usually narrows once
            # the product is known.
            types_wanted.append(("loan_product", None))

    for playbook_type, product_key in types_wanted:
        # Platform default first.
        platform_pb = await _pick_one(
            db,
            owner_type="platform",
            owner_id=None,
            playbook_type=playbook_type,
            product_key=product_key,
            pinned_versions=pinned_versions,
        )
        if platform_pb is not None:
            out.append(platform_pb)

        # Funding overlay (only relevant for lending playbooks).
        if phase == "lending":
            funding_pb = await _pick_one(
                db,
                owner_type="funding",
                owner_id=funding_owner_id,
                playbook_type=playbook_type,
                product_key=product_key,
                pinned_versions=pinned_versions,
            )
            if funding_pb is not None:
                out.append(funding_pb)

        # Agent overlay (only relevant for realtor playbooks).
        if phase == "realtor" and agent_id is not None:
            agent_pb = await _pick_one(
                db,
                owner_type="agent",
                owner_id=agent_id,
                playbook_type=playbook_type,
                product_key=product_key,
                pinned_versions=pinned_versions,
            )
            if agent_pb is not None:
                out.append(agent_pb)

    return out


async def _pick_one(
    db: AsyncSession,
    *,
    owner_type: str,
    owner_id: UUID | None,
    playbook_type: str,
    product_key: str | None,
    pinned_versions: dict[str, int],
) -> AIPlaybookTemplate | None:
    """Pick the single playbook to use for this owner+type slot.

    If a pinned version exists for any matching playbook_id, prefer
    that exact (id, version). Otherwise pick the latest published
    version."""
    q = (
        select(AIPlaybookTemplate)
        .where(
            AIPlaybookTemplate.owner_type == owner_type,
            AIPlaybookTemplate.playbook_type == playbook_type,
            AIPlaybookTemplate.is_active.is_(True),
        )
    )
    if owner_id is None:
        q = q.where(AIPlaybookTemplate.owner_id.is_(None))
    else:
        q = q.where(AIPlaybookTemplate.owner_id == owner_id)
    if product_key is None:
        q = q.where(AIPlaybookTemplate.product_key.is_(None))
    else:
        q = q.where(AIPlaybookTemplate.product_key == product_key)

    candidates = (await db.execute(q)).scalars().all()
    if not candidates:
        return None

    # Prefer a pinned version if present.
    for c in candidates:
        pinned = pinned_versions.get(str(c.id))
        if pinned is not None and c.version == pinned:
            return c

    # Otherwise pick the latest PUBLISHED version. If nothing is
    # published, fall back to the highest version (covers dev/test
    # data where seeds may not have flipped status).
    published = [c for c in candidates if c.status == "published"]
    pool = published or candidates
    return max(pool, key=lambda c: c.version)


def _source_for(pb: AIPlaybookTemplate) -> str:
    """Map a playbook's owner_type to the resolution-attribution
    label used downstream."""
    if pb.owner_type == "platform":
        return "platform"
    if pb.owner_type == "funding":
        return "funding_required"
    if pb.owner_type == "agent":
        return "agent_playbook"
    return pb.owner_type  # pragma: no cover — defensive


def _applies_when_matches(applies_when: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    """Evaluate the requirement's `applies_when` JSONB against the
    caller's context dict.

    Semantics (intentionally minimal in v1 — Phase 2 expands the
    condition vocabulary):

      - NULL / {} → always applies
      - {"key": value} → context[key] must equal value
      - {"key": [v1, v2]} → context[key] must be in the list
      - Multiple keys are AND-ed.

    Missing context keys are treated as "doesn't match" — i.e. the
    requirement does NOT apply, opting OUT of the gate by default.
    Phase 2's `<AppliesWhenEditor>` will surface this to admins."""
    if not applies_when:
        return True
    for key, expected in applies_when.items():
        actual = context.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        else:
            if actual != expected:
                return False
    return True
