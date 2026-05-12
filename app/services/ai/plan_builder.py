"""Plan builder — rebuilds the per-client `client_ai_plan` row from
the resolver output + per-requirement statuses + the realtor profile.

Two entry points:

  rebuild(...)   Recomputes and PERSISTS the client_ai_plan row.
                 Called on every chat turn, document upload, cadence
                 pass. Cheap — a few DB reads + one upsert.

  preview(...)   Same logic, but does NOT persist. Used by AI Preview
                 panels in both portals so admins/agents see exactly
                 what the AI will do before they save changes.

`rebuild` and `preview` MUST produce identical output for identical
inputs (with overrides treated equivalently). A parity test enforces
this.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.client_ai_plan import ClientAIPlan
from app.models.client_requirement_status import ClientRequirementStatus
from app.services.ai.requirement_resolver import (
    ResolvedRequirement,
    resolve_requirements,
)


# ── Public types ───────────────────────────────────────────────────


@dataclass
class PlanSnapshot:
    """Plain-data view of the plan for callers that don't want to
    handle the ORM row directly. Mirrors the JSONB columns 1:1."""
    client_id: UUID
    loan_id: UUID | None
    deal_id: UUID | None
    agent_id: UUID | None
    current_phase: str
    active_playbook_versions: list[dict[str, Any]]
    custom_instructions: str | None
    required_items: list[dict[str, Any]]
    waived_items: list[dict[str, Any]]
    ai_suggested_items: list[dict[str, Any]]
    next_best_question: str | None
    next_best_action: dict[str, Any] | None
    readiness_score: int | None
    computed_at: datetime


# ── Public API ─────────────────────────────────────────────────────


async def rebuild(
    db: AsyncSession,
    *,
    client_id: UUID,
    loan_id: UUID | None = None,
    deal_id: UUID | None = None,
    loan_product: str | None = None,
    side: Literal["buyer", "seller"] | None = None,
) -> ClientAIPlan:
    """Recompute and persist the client_ai_plan row for this scope.

    Scope is exactly one of (client / deal / loan):
      - loan_id set → lending phase, loan-scoped plan
      - deal_id set → realtor phase, deal-scoped plan
      - neither → realtor phase, true client-level plan

    Caller controls the transaction (no commit here)."""
    snapshot = await _compute(
        db,
        client_id=client_id,
        loan_id=loan_id,
        deal_id=deal_id,
        loan_product=loan_product,
        side=side,
        unsaved_overrides=None,
    )
    return await _upsert_plan(db, snapshot)


async def preview(
    db: AsyncSession,
    *,
    client_id: UUID,
    loan_id: UUID | None = None,
    deal_id: UUID | None = None,
    loan_product: str | None = None,
    side: Literal["buyer", "seller"] | None = None,
    overrides: dict[str, Any] | None = None,
) -> PlanSnapshot:
    """Same as rebuild but does NOT persist. Used by AI Preview /
    Test Mode in both portals.

    `overrides` lets the UI feed in unsaved per-requirement changes
    without writing them to the DB. Shape:

        {
            "waived_keys": ["buyer_agency_agreement"],
            "custom_instructions": "...",
        }
    """
    return await _compute(
        db,
        client_id=client_id,
        loan_id=loan_id,
        deal_id=deal_id,
        loan_product=loan_product,
        side=side,
        unsaved_overrides=overrides,
    )


# ── Core computation ───────────────────────────────────────────────


async def _compute(
    db: AsyncSession,
    *,
    client_id: UUID,
    loan_id: UUID | None,
    deal_id: UUID | None = None,
    loan_product: str | None,
    side: Literal["buyer", "seller"] | None,
    unsaved_overrides: dict[str, Any] | None,
) -> PlanSnapshot:
    client = (
        await db.execute(select(Client).where(Client.id == client_id))
    ).scalar_one_or_none()
    if client is None:
        raise ValueError(f"plan_builder: client_id={client_id} not found")

    profile = client.realtor_profile or {}
    phase: Literal["realtor", "lending"] = "lending" if loan_id is not None else "realtor"

    # Buyer/seller side derived from the realtor profile if caller didn't pin it.
    derived_side: Literal["buyer", "seller"] | None = side
    if derived_side is None:
        ctype = profile.get("client_type") or client.client_type
        if ctype == "seller":
            derived_side = "seller"
        elif ctype in ("buyer", "buyer_and_seller"):
            derived_side = "buyer"

    # Build the context dict that drives applies_when evaluation.
    context = _build_resolver_context(client, profile)

    # ── Pull the existing plan row's pinned versions, if any ───────
    existing = await _find_existing_plan(
        db, client_id=client_id, loan_id=loan_id, deal_id=deal_id,
    )
    pinned_versions: dict[str, int] = {}
    if existing is not None:
        for entry in existing.active_playbook_versions or []:
            if isinstance(entry, dict) and entry.get("playbook_id") and entry.get("version"):
                pinned_versions[str(entry["playbook_id"])] = int(entry["version"])

    # ── Resolve the active requirement list ────────────────────────
    resolved = await resolve_requirements(
        db,
        client_id=client_id,
        loan_id=loan_id,
        phase=phase,
        loan_product=loan_product,
        side=derived_side,
        agent_id=client.agent_id if hasattr(client, "agent_id") else None,
        pinned_versions=pinned_versions,
        context=context,
    )

    # ── Pull per-requirement statuses for THIS scope ──────────────
    statuses = await _load_statuses(
        db, client_id=client_id, loan_id=loan_id, deal_id=deal_id,
    )
    status_by_key = {s.requirement_key: s for s in statuses}

    # ── Apply unsaved overrides on top (preview-only path) ─────────
    extra_waived = set((unsaved_overrides or {}).get("waived_keys") or [])
    custom_instr = (unsaved_overrides or {}).get("custom_instructions")
    if custom_instr is None and existing is not None:
        custom_instr = existing.custom_instructions

    # ── Bucket the resolved + status-merged list ───────────────────
    required_items: list[dict[str, Any]] = []
    waived_items: list[dict[str, Any]] = []
    for r in resolved:
        st = status_by_key.get(r.requirement_key)
        status_value = (st.status if st else "missing")
        source = (st.source if st else r.source)
        evidence_id = str(st.evidence_id) if (st and st.evidence_id) else None

        # Honor the agent's "waive" only if the requirement allows it.
        is_unsaved_waiver = r.requirement_key in extra_waived
        is_persisted_waiver = status_value in ("waived", "not_applicable")
        if (is_unsaved_waiver or is_persisted_waiver) and r.can_agent_override:
            waived_items.append(_serialize_requirement(r, status="waived", source="client_custom", evidence_id=None))
            continue

        required_items.append(
            _serialize_requirement(
                r,
                status=status_value,
                source=source,
                evidence_id=evidence_id,
            )
        )

    # ── Pick the next-best question + action ───────────────────────
    next_q, next_a = _pick_next_best(required_items, profile)

    # ── Readiness score ────────────────────────────────────────────
    score = _compute_readiness_score(required_items)

    # ── Active playbook version pinning (carry-over from existing if
    # present, else pin to whatever the resolver actually picked). ──
    active_versions = (existing.active_playbook_versions if existing and existing.active_playbook_versions else None)
    if not active_versions:
        seen: dict[str, int] = {}
        for r in resolved:
            seen[str(r.playbook_id)] = r.playbook_version
        active_versions = [{"playbook_id": pid, "version": v} for pid, v in seen.items()]

    return PlanSnapshot(
        client_id=client_id,
        loan_id=loan_id,
        deal_id=deal_id,
        agent_id=getattr(client, "agent_id", None),
        current_phase=phase,
        active_playbook_versions=active_versions,
        custom_instructions=custom_instr,
        required_items=required_items,
        waived_items=waived_items,
        ai_suggested_items=[],  # Phase 4 wires AI-suggested adds.
        next_best_question=next_q,
        next_best_action=next_a,
        readiness_score=score,
        computed_at=datetime.now(timezone.utc),
    )


# ── Helpers ────────────────────────────────────────────────────────


def _build_resolver_context(client: Client, profile: dict[str, Any]) -> dict[str, Any]:
    """Assemble the dict the resolver evaluates `applies_when` against.

    Keep this PURE (no DB) — caller passes the already-loaded client
    and its realtor_profile."""
    bp = profile.get("buyer_profile") or {}
    sp = profile.get("seller_profile") or {}
    ctx: dict[str, Any] = {
        "client_type": profile.get("client_type") or client.client_type,
    }
    # Buyer-side facts.
    if "financing_needed" in bp:
        ctx["financing_needed"] = bp.get("financing_needed")
    if bp.get("target_property_type"):
        ctx["target_property_type"] = bp.get("target_property_type")
    # under_contract — we infer from buyer_profile.under_contract when set,
    # OR from purchase_contract status on documents (Phase 6 wires that).
    if "under_contract" in bp:
        ctx["under_contract"] = bp.get("under_contract")
    # Borrower-type: defaults to "individual" unless realtor profile
    # captured an entity. Used by lending playbooks that gate on entity.
    known_facts = profile.get("known_facts") or []
    for f in known_facts:
        if isinstance(f, dict) and f.get("field") == "borrower_entity_type":
            ctx["borrower_type"] = "entity" if str(f.get("value")).lower() not in ("individual", "personal") else "individual"
            break
    # Seller-side scalars don't gate any platform requirements today
    # but still pass through for any agent overlay rules that reference
    # them.
    if sp.get("occupancy_status"):
        ctx["occupancy_status"] = sp.get("occupancy_status")
    return ctx


async def _load_statuses(
    db: AsyncSession,
    *,
    client_id: UUID,
    loan_id: UUID | None,
    deal_id: UUID | None = None,
) -> list[ClientRequirementStatus]:
    """Fetch status rows scoped to one of (client / deal / loan):
      - loan_id set → loan-scoped rows (deal_id IS NULL)
      - deal_id set → deal-scoped rows (loan_id IS NULL)
      - neither → true client-level rows (both NULL)
    Matches the partial unique indexes added in alembic 0049."""
    q = select(ClientRequirementStatus).where(ClientRequirementStatus.client_id == client_id)
    if loan_id is not None:
        q = q.where(
            ClientRequirementStatus.loan_id == loan_id,
            ClientRequirementStatus.deal_id.is_(None),
        )
    elif deal_id is not None:
        q = q.where(
            ClientRequirementStatus.deal_id == deal_id,
            ClientRequirementStatus.loan_id.is_(None),
        )
    else:
        q = q.where(
            ClientRequirementStatus.loan_id.is_(None),
            ClientRequirementStatus.deal_id.is_(None),
        )
    return list((await db.execute(q)).scalars().all())


async def _find_existing_plan(
    db: AsyncSession,
    *,
    client_id: UUID,
    loan_id: UUID | None,
    deal_id: UUID | None = None,
) -> ClientAIPlan | None:
    q = select(ClientAIPlan).where(ClientAIPlan.client_id == client_id)
    if loan_id is not None:
        q = q.where(
            ClientAIPlan.loan_id == loan_id,
            ClientAIPlan.deal_id.is_(None),
        )
    elif deal_id is not None:
        q = q.where(
            ClientAIPlan.deal_id == deal_id,
            ClientAIPlan.loan_id.is_(None),
        )
    else:
        q = q.where(
            ClientAIPlan.loan_id.is_(None),
            ClientAIPlan.deal_id.is_(None),
        )
    return (await db.execute(q)).scalar_one_or_none()


async def _upsert_plan(db: AsyncSession, snap: PlanSnapshot) -> ClientAIPlan:
    """Insert or update the client_ai_plan row for this scope."""
    existing = await _find_existing_plan(
        db, client_id=snap.client_id, loan_id=snap.loan_id, deal_id=snap.deal_id,
    )
    if existing is None:
        row = ClientAIPlan(
            id=uuid.uuid4(),
            client_id=snap.client_id,
            loan_id=snap.loan_id,
            deal_id=snap.deal_id,
            agent_id=snap.agent_id,
            current_phase=snap.current_phase,
            active_playbook_versions=snap.active_playbook_versions,
            custom_instructions=snap.custom_instructions,
            required_items=snap.required_items,
            waived_items=snap.waived_items,
            ai_suggested_items=snap.ai_suggested_items,
            next_best_question=snap.next_best_question,
            next_best_action=snap.next_best_action,
            readiness_score=snap.readiness_score,
            computed_at=snap.computed_at,
        )
        db.add(row)
        await db.flush()
        return row

    existing.agent_id = snap.agent_id
    existing.current_phase = snap.current_phase
    existing.active_playbook_versions = snap.active_playbook_versions
    existing.custom_instructions = snap.custom_instructions
    existing.required_items = snap.required_items
    existing.waived_items = snap.waived_items
    existing.ai_suggested_items = snap.ai_suggested_items
    existing.next_best_question = snap.next_best_question
    existing.next_best_action = snap.next_best_action
    existing.readiness_score = snap.readiness_score
    existing.computed_at = snap.computed_at
    await db.flush()
    return existing


def _serialize_requirement(
    r: ResolvedRequirement,
    *,
    status: str,
    source: str,
    evidence_id: str | None,
) -> dict[str, Any]:
    """Stable JSONB shape for `client_ai_plan.required_items` /
    `waived_items`. Mirrors what the UI renders."""
    return {
        "requirement_key": r.requirement_key,
        "label": r.label,
        "category": r.category,
        "required_level": r.required_level,
        "blocks_stage": r.blocks_stage,
        "visibility": r.visibility,
        "can_agent_override": r.can_agent_override,
        "can_underwriter_waive": r.can_underwriter_waive,
        "verification_required": r.verification_required,
        "expiration_days": r.expiration_days,
        "ai_request_message_template": r.ai_request_message_template,
        "display_order": r.display_order,
        "status": status,
        "source": source,
        "evidence_id": evidence_id,
        "playbook_id": str(r.playbook_id),
        "playbook_version": r.playbook_version,
        "playbook_name": r.playbook_name,
    }


def _pick_next_best(
    required_items: list[dict[str, Any]],
    profile: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Pick the highest-leverage missing requirement and surface it as
    the next-best question + action.

    Heuristic (matches the spec):
      1. Required-level "required" comes before "recommended" / "optional"
      2. Items that block an earlier stage come before items that block a later stage
      3. Items already "asked" but not yet "provided" jump to the front
      4. Stable tie-break: display_order, then requirement_key

    The actual question text uses the requirement's
    `ai_request_message_template` if present; otherwise a generic
    "Walk me through {label}" prompt."""
    if not required_items:
        return None, None

    open_items = [
        i for i in required_items
        if i["status"] in ("missing", "asked", "needed_later", "provided_unverified")
    ]
    if not open_items:
        return None, None

    stage_order = {
        "showings": 0,
        "prequalification": 1,
        "term_sheet": 2,
        "underwriting": 3,
        "closing": 4,
        "listed": 5,
    }
    level_score = {"required": 0, "recommended": 1, "optional": 2}
    status_priority = {"asked": 0, "missing": 1, "needed_later": 2, "provided_unverified": 3}

    def sort_key(it: dict[str, Any]) -> tuple:
        return (
            level_score.get(it["required_level"], 9),
            stage_order.get(it.get("blocks_stage") or "", 99),
            status_priority.get(it["status"], 9),
            it.get("display_order", 0),
            it["requirement_key"],
        )

    pick = sorted(open_items, key=sort_key)[0]

    template = pick.get("ai_request_message_template")
    question = template if template else f"Quick one — can you confirm {pick['label']}?"
    action = {
        "kind": "request_requirement",
        "requirement_key": pick["requirement_key"],
        "label": pick["label"],
        "category": pick["category"],
    }
    return question, action


def _compute_readiness_score(required_items: list[dict[str, Any]]) -> int:
    """0-100 based on how many `required` items are satisfied
    (verified or uploaded). Recommended/optional items don't count
    against the score — they're nice-to-haves."""
    required_only = [i for i in required_items if i["required_level"] == "required"]
    if not required_only:
        return 0
    satisfied = sum(
        1 for i in required_only
        if i["status"] in ("verified", "uploaded", "provided_unverified")
    )
    return round((satisfied / len(required_only)) * 100)
