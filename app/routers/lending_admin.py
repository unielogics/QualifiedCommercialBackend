"""Super Admin / Underwriter — Lending AI Settings (Phase 3).

CRUD for funding-side playbooks (loan products), their requirements,
conditions, cadence rules, verification rules, escalation rules, and
borrower communication rules.

Permission: super_admin and loan_exec only.

Versioning: editing a playbook creates a new draft version; admins
explicitly publish. Active client plans pin to a specific version so
edits don't disrupt in-flight deals.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.ai_cadence_rule import AICadenceRule
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.services.ai.audit import record_event

router = APIRouter(prefix="/lending-admin", tags=["lending-admin"])
log = logging.getLogger(__name__)


def _require_admin(user) -> None:
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Lending AI Settings is super-admin / loan-exec only.",
        )


# ── Loan Product Playbooks ─────────────────────────────────────────


class PlaybookOut(BaseModel):
    id: UUID
    owner_type: str
    owner_id: UUID | None
    playbook_type: str
    product_key: str | None
    name: str
    description: str | None
    rules: dict
    version: int
    status: str
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PlaybookCreate(BaseModel):
    playbook_type: Literal["loan_product", "cadence", "handoff"] = "loan_product"
    product_key: str | None = None
    name: str
    description: str | None = None
    rules: dict = {}


class PlaybookUpdate(BaseModel):
    """Editing a playbook either bumps the in-place draft or forks a
    NEW draft from the latest published version. Caller picks via
    `fork`. Forking is the safer default — published versions are
    immutable from the API surface."""
    name: str | None = None
    description: str | None = None
    rules: dict | None = None
    fork: bool = True


@router.get("/playbooks", response_model=list[PlaybookOut])
async def list_funding_playbooks(
    user: CurrentUser,
    playbook_type: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[PlaybookOut]:
    _require_admin(user)
    q = select(AIPlaybookTemplate).where(AIPlaybookTemplate.is_active.is_(True))
    if playbook_type:
        q = q.where(AIPlaybookTemplate.playbook_type == playbook_type)
    q = q.order_by(AIPlaybookTemplate.playbook_type, AIPlaybookTemplate.product_key, AIPlaybookTemplate.version.desc())
    rows = (await db.execute(q)).scalars().all()
    return [_serialize_playbook(r) for r in rows]


@router.post("/playbooks", response_model=PlaybookOut)
async def create_funding_playbook(
    payload: PlaybookCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaybookOut:
    _require_admin(user)
    pb = AIPlaybookTemplate(
        owner_type="funding",
        owner_id=user.id,
        playbook_type=payload.playbook_type,
        product_key=payload.product_key,
        name=payload.name,
        description=payload.description,
        rules=payload.rules or {},
        version=1,
        status="draft",
    )
    db.add(pb)
    await db.flush()
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, new_value={"name": pb.name, "version": pb.version},
    )
    return _serialize_playbook(pb)


@router.patch("/playbooks/{playbook_id}", response_model=PlaybookOut)
async def update_funding_playbook(
    playbook_id: UUID,
    payload: PlaybookUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaybookOut:
    """Edit a playbook. By default forks a new draft version from the
    requested playbook so published versions remain immutable.

    `fork=False` allows in-place edits ONLY if the row is still in
    `draft` status."""
    _require_admin(user)
    pb = await db.get(AIPlaybookTemplate, playbook_id)
    if pb is None or not pb.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Playbook not found")
    if pb.owner_type == "platform":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Platform playbooks are read-only. Use 'duplicate-from-platform' first.",
        )

    if payload.fork or pb.status != "draft":
        # Fork a new draft version. Reqs are deep-copied below.
        new_version = pb.version + 1
        forked = AIPlaybookTemplate(
            owner_type=pb.owner_type, owner_id=pb.owner_id,
            playbook_type=pb.playbook_type, product_key=pb.product_key,
            name=payload.name or pb.name,
            description=payload.description if payload.description is not None else pb.description,
            rules=payload.rules if payload.rules is not None else (pb.rules or {}),
            version=new_version, status="draft",
        )
        db.add(forked)
        await db.flush()
        # Copy requirements from the source.
        src_reqs = (await db.execute(
            select(AICollectionRequirement).where(AICollectionRequirement.playbook_id == pb.id)
        )).scalars().all()
        for r in src_reqs:
            db.add(AICollectionRequirement(
                playbook_id=forked.id,
                requirement_key=r.requirement_key, label=r.label,
                category=r.category, required_level=r.required_level,
                applies_when=r.applies_when, blocks_stage=r.blocks_stage,
                visibility=list(r.visibility or []),
                can_agent_override=r.can_agent_override,
                can_underwriter_waive=r.can_underwriter_waive,
                verification_required=r.verification_required,
                expiration_days=r.expiration_days,
                ai_request_message_template=r.ai_request_message_template,
                display_order=r.display_order,
                default_owner_type=r.default_owner_type,
                default_channels=list(r.default_channels or []),
                default_cadence_hours=r.default_cadence_hours,
                link_url=r.link_url,
                link_label=r.link_label,
                link_kind=r.link_kind,
                objective_text=r.objective_text,
                completion_criteria=r.completion_criteria,
                completion_mode=r.completion_mode,
                wrong_upload_response_template=r.wrong_upload_response_template,
                depends_on=list(r.depends_on or []),
                parent_key=r.parent_key,
                inferred_depends_on=list(r.inferred_depends_on or []),
                deps_confirmed=r.deps_confirmed,
            ))
        await db.flush()
        await record_event(
            db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
            playbook_id=forked.id, payload={"forked_from": str(pb.id), "version": new_version},
        )
        return _serialize_playbook(forked)

    if payload.name is not None:
        pb.name = payload.name
    if payload.description is not None:
        pb.description = payload.description
    if payload.rules is not None:
        pb.rules = payload.rules
    await db.flush()
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, new_value={"name": pb.name, "version": pb.version},
    )
    return _serialize_playbook(pb)


@router.post("/playbooks/{playbook_id}/publish", response_model=PlaybookOut)
async def publish_playbook(
    playbook_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaybookOut:
    _require_admin(user)
    pb = await db.get(AIPlaybookTemplate, playbook_id)
    if pb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Playbook not found")
    if pb.status == "published":
        return _serialize_playbook(pb)
    pb.status = "published"
    pb.published_at = datetime.now(timezone.utc)
    await record_event(
        db, event_type="playbook_published", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, new_value={"version": pb.version, "status": pb.status},
    )
    await db.flush()
    return _serialize_playbook(pb)


@router.post("/playbooks/duplicate-from-platform/{platform_playbook_id}", response_model=PlaybookOut)
async def duplicate_from_platform(
    platform_playbook_id: UUID,
    user: CurrentUser,
    name: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> PlaybookOut:
    """Fork the platform default into a funding-owned draft. Funding
    team can then edit + publish without touching the platform row."""
    _require_admin(user)
    src = await db.get(AIPlaybookTemplate, platform_playbook_id)
    if src is None or src.owner_type != "platform":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform playbook not found")

    pb = AIPlaybookTemplate(
        owner_type="funding", owner_id=user.id,
        playbook_type=src.playbook_type, product_key=src.product_key,
        name=name or f"{src.name} (funding overlay)",
        description=src.description, rules=src.rules or {},
        version=1, status="draft",
    )
    db.add(pb)
    await db.flush()
    src_reqs = (await db.execute(
        select(AICollectionRequirement).where(AICollectionRequirement.playbook_id == src.id)
    )).scalars().all()
    for r in src_reqs:
        db.add(AICollectionRequirement(
            playbook_id=pb.id,
            requirement_key=r.requirement_key, label=r.label,
            category=r.category, required_level=r.required_level,
            applies_when=r.applies_when, blocks_stage=r.blocks_stage,
            visibility=list(r.visibility or []),
            can_agent_override=r.can_agent_override,
            can_underwriter_waive=r.can_underwriter_waive,
            verification_required=r.verification_required,
            expiration_days=r.expiration_days,
            ai_request_message_template=r.ai_request_message_template,
            display_order=r.display_order,
            # AI Deal Secretary fields (alembic 0038) — carried through
            # so forked drafts and duplicate-from-platform copies
            # inherit the catalog's enriched defaults.
            default_owner_type=r.default_owner_type,
            default_channels=list(r.default_channels or []),
            default_cadence_hours=r.default_cadence_hours,
            link_url=r.link_url,
            link_label=r.link_label,
            link_kind=r.link_kind,
            objective_text=r.objective_text,
            completion_criteria=r.completion_criteria,
            completion_mode=r.completion_mode,
            wrong_upload_response_template=r.wrong_upload_response_template,
            # Timeline + grouping (alembic 0040) — preserve dependency graph on copy
            depends_on=list(r.depends_on or []),
            parent_key=r.parent_key,
            inferred_depends_on=list(r.inferred_depends_on or []),
            deps_confirmed=r.deps_confirmed,
        ))
    await db.flush()
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, payload={"forked_from_platform": str(src.id)},
    )
    return _serialize_playbook(pb)


# ── Requirements ───────────────────────────────────────────────────


class RequirementOut(BaseModel):
    id: UUID
    playbook_id: UUID
    requirement_key: str
    label: str
    category: str
    required_level: str
    applies_when: dict | None
    blocks_stage: str | None
    visibility: list[str]
    can_agent_override: bool
    can_underwriter_waive: bool
    verification_required: bool
    expiration_days: int | None
    ai_request_message_template: str | None
    display_order: int
    # AI Deal Secretary fields (alembic 0038)
    default_owner_type: str
    default_channels: list[str]
    default_cadence_hours: int
    link_url: str | None
    link_label: str | None
    link_kind: str | None
    objective_text: str
    completion_criteria: str
    completion_mode: str
    wrong_upload_response_template: str | None
    # Timeline + grouping (alembic 0040)
    depends_on: list[str] = []
    parent_key: str | None = None
    inferred_depends_on: list[str] = []
    deps_confirmed: bool = True


# Closed enum for the Deal Secretary taxonomy + valid completion modes.
# Keeping Literals in sync with app/enums.RequirementCategory + CompletionMode
# (Pydantic Literal doesn't accept a StrEnum directly without per-version
# Pydantic v2 plugin magic, so we mirror the values explicitly).
_RequirementCategoryLiteral = Literal[
    "borrower_info", "property_data", "financials", "credit", "agreements",
    "insurance", "title_and_escrow", "appraisal_and_inspection", "scheduling",
    "compliance", "communication", "ai_internal",
]
_TaskOwnerTypeLiteral = Literal["human", "ai", "shared", "funding_locked"]
_CompletionModeLiteral = Literal[
    "ai_can_complete", "requires_human_verify", "borrower_self_attest",
]
_LinkKindLiteral = Literal["docusign", "esign", "external_form", "reference"]


class RequirementUpsert(BaseModel):
    id: UUID | None = None
    requirement_key: str
    label: str
    category: _RequirementCategoryLiteral
    required_level: Literal["required", "recommended", "optional"]
    applies_when: dict | None = None
    blocks_stage: str | None = None
    visibility: list[str] | None = None
    can_agent_override: bool = False
    can_underwriter_waive: bool = True
    verification_required: bool = False
    expiration_days: int | None = None
    ai_request_message_template: str | None = None
    display_order: int = 0
    # AI Deal Secretary fields (alembic 0038). All optional on upsert
    # so a thin client can omit them and rely on server defaults.
    default_owner_type: _TaskOwnerTypeLiteral = "human"
    default_channels: list[str] | None = None
    default_cadence_hours: int = 48
    link_url: str | None = None
    link_label: str | None = None
    link_kind: _LinkKindLiteral | None = None
    objective_text: str = ""
    completion_criteria: str = ""
    completion_mode: _CompletionModeLiteral = "ai_can_complete"
    wrong_upload_response_template: str | None = None
    # Timeline + grouping (alembic 0040). All optional on upsert.
    depends_on: list[str] | None = None
    parent_key: str | None = None


@router.get("/playbooks/{playbook_id}/requirements", response_model=list[RequirementOut])
async def list_requirements(
    playbook_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[RequirementOut]:
    _require_admin(user)
    rows = (await db.execute(
        select(AICollectionRequirement)
        .where(AICollectionRequirement.playbook_id == playbook_id)
        .order_by(AICollectionRequirement.display_order)
    )).scalars().all()
    return [_serialize_requirement(r) for r in rows]


@router.post("/playbooks/{playbook_id}/requirements", response_model=RequirementOut)
async def upsert_requirement(
    playbook_id: UUID,
    payload: RequirementUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RequirementOut:
    _require_admin(user)
    pb = await db.get(AIPlaybookTemplate, playbook_id)
    if pb is None or pb.owner_type == "platform":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform playbooks are read-only")
    if pb.status == "published":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot edit requirements on a published playbook. PATCH the playbook with fork=true to fork a draft.",
        )

    if payload.id is not None:
        req = await db.get(AICollectionRequirement, payload.id)
        if req is None or req.playbook_id != playbook_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found on this playbook")
        old = {"label": req.label, "required_level": req.required_level}
        for k, v in payload.model_dump(exclude_unset=True, exclude={"id"}).items():
            setattr(req, k, v)
        await record_event(
            db, event_type="requirement_added", actor_type="user", actor_id=user.id,
            playbook_id=pb.id, requirement_key=req.requirement_key,
            old_value=old, new_value={"label": req.label, "required_level": req.required_level},
        )
    else:
        req = AICollectionRequirement(
            playbook_id=playbook_id,
            requirement_key=payload.requirement_key,
            label=payload.label, category=payload.category,
            required_level=payload.required_level,
            applies_when=payload.applies_when, blocks_stage=payload.blocks_stage,
            visibility=payload.visibility or ["agent", "underwriter"],
            can_agent_override=payload.can_agent_override,
            can_underwriter_waive=payload.can_underwriter_waive,
            verification_required=payload.verification_required,
            expiration_days=payload.expiration_days,
            ai_request_message_template=payload.ai_request_message_template,
            display_order=payload.display_order,
            # AI Deal Secretary fields (alembic 0038).
            default_owner_type=payload.default_owner_type,
            default_channels=payload.default_channels or ["portal"],
            default_cadence_hours=payload.default_cadence_hours,
            link_url=payload.link_url,
            link_label=payload.link_label,
            link_kind=payload.link_kind,
            objective_text=payload.objective_text,
            completion_criteria=payload.completion_criteria,
            completion_mode=payload.completion_mode,
            wrong_upload_response_template=payload.wrong_upload_response_template,
            # Timeline + grouping (alembic 0040)
            depends_on=payload.depends_on or [],
            parent_key=payload.parent_key,
        )
        db.add(req)
        await db.flush()
        await record_event(
            db, event_type="requirement_added", actor_type="user", actor_id=user.id,
            playbook_id=pb.id, requirement_key=req.requirement_key,
            new_value={"label": req.label},
        )
    await db.flush()
    return _serialize_requirement(req)


@router.delete("/playbooks/{playbook_id}/requirements/{requirement_id}")
async def delete_requirement(
    playbook_id: UUID, requirement_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(user)
    pb = await db.get(AIPlaybookTemplate, playbook_id)
    if pb is None or pb.owner_type == "platform":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform playbooks are read-only")
    if pb.status == "published":
        raise HTTPException(status.HTTP_409_CONFLICT, "Fork the playbook before deleting requirements")
    req = await db.get(AICollectionRequirement, requirement_id)
    if req is None or req.playbook_id != playbook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    await db.delete(req)
    await db.flush()
    return {"ok": True}


# ── Cadence rules (funding) ────────────────────────────────────────


class CadenceRuleOut(BaseModel):
    id: UUID
    playbook_id: UUID | None
    trigger_event: str
    applies_to_requirement_key: str | None
    condition: dict | None
    wait_hours: int
    action_type: str
    approval_required: bool
    message_template: str | None
    visibility: str
    is_active: bool
    requires_ai_owner: bool


class CadenceRuleUpsert(BaseModel):
    id: UUID | None = None
    playbook_id: UUID | None = None
    trigger_event: str
    applies_to_requirement_key: str | None = None
    condition: dict | None = None
    wait_hours: int = 0
    action_type: Literal["draft_message", "create_task", "escalate", "mark_stalled", "auto_send_reminder"]
    approval_required: bool = True
    message_template: str | None = None
    visibility: Literal["internal", "agent", "borrower"] = "agent"
    is_active: bool = True
    requires_ai_owner: bool = True


@router.get("/cadence-rules", response_model=list[CadenceRuleOut])
async def list_funding_cadence(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[CadenceRuleOut]:
    _require_admin(user)
    # Surface rules whose linked playbook is funding-owned + the
    # platform defaults (read-only — UI shows them as base layer).
    rows = (await db.execute(
        select(AICadenceRule, AIPlaybookTemplate)
        .join(AIPlaybookTemplate, AICadenceRule.playbook_id == AIPlaybookTemplate.id, isouter=True)
        .where(
            (AIPlaybookTemplate.owner_type == "funding")
            | (AIPlaybookTemplate.owner_type == "platform")
            | (AICadenceRule.playbook_id.is_(None))
        )
    )).all()
    return [_serialize_cadence(r) for r, _ in rows]


@router.post("/cadence-rules", response_model=CadenceRuleOut)
async def upsert_funding_cadence(
    payload: CadenceRuleUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CadenceRuleOut:
    _require_admin(user)
    if payload.playbook_id:
        pb = await db.get(AIPlaybookTemplate, payload.playbook_id)
        if pb is None or pb.owner_type == "platform":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot attach cadence rules to platform playbooks")

    if payload.id is not None:
        row = await db.get(AICadenceRule, payload.id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
        for f in (
            "trigger_event", "applies_to_requirement_key", "condition",
            "wait_hours", "action_type", "approval_required",
            "message_template", "visibility", "is_active", "playbook_id",
            "requires_ai_owner",
        ):
            setattr(row, f, getattr(payload, f))
    else:
        row = AICadenceRule(
            playbook_id=payload.playbook_id,
            trigger_event=payload.trigger_event,
            applies_to_requirement_key=payload.applies_to_requirement_key,
            condition=payload.condition, wait_hours=payload.wait_hours,
            action_type=payload.action_type,
            approval_required=payload.approval_required,
            message_template=payload.message_template,
            visibility=payload.visibility, is_active=payload.is_active,
            requires_ai_owner=payload.requires_ai_owner,
        )
        db.add(row)
    await db.flush()
    return _serialize_cadence(row)


@router.delete("/cadence-rules/{rule_id}")
async def delete_funding_cadence(
    rule_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(user)
    row = await db.get(AICadenceRule, rule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await db.delete(row)
    await db.flush()
    return {"ok": True}


# ── Verification + Escalation + Communication ──────────────────────
# All three are stored as JSONB blobs on a `cadence`-type playbook
# row owned by funding (no separate tables needed in v1). The UI's
# Verification / Escalation / Communication tabs project specific
# fields out of `rules`.


class FundingRulesOut(BaseModel):
    playbook_id: UUID
    rules: dict


class FundingRulesPatch(BaseModel):
    rules: dict


def _ensure_funding_meta_playbook(db: AsyncSession, user, key: str) -> Any:
    """Return (or create on demand) the funding-owned playbook used to
    park verification / escalation / communication rules. v1 keeps
    these in a single JSONB blob per (key, funding-user)."""
    return _ensure_funding_meta_playbook_async(db, user, key)


async def _ensure_funding_meta_playbook_async(db: AsyncSession, user, key: str):
    pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "funding",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == "cadence",
            AIPlaybookTemplate.product_key == key,
        )
    )).scalar_one_or_none()
    if pb is None:
        pb = AIPlaybookTemplate(
            owner_type="funding", owner_id=user.id,
            playbook_type="cadence", product_key=key,
            name=f"Funding {key} rules",
            rules={}, version=1, status="published",
        )
        db.add(pb)
        await db.flush()
    return pb


@router.get("/verification-rules", response_model=FundingRulesOut)
async def get_verification_rules(
    user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FundingRulesOut:
    _require_admin(user)
    pb = await _ensure_funding_meta_playbook_async(db, user, "verification")
    return FundingRulesOut(playbook_id=pb.id, rules=pb.rules or {})


@router.patch("/verification-rules", response_model=FundingRulesOut)
async def patch_verification_rules(
    payload: FundingRulesPatch,
    user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FundingRulesOut:
    _require_admin(user)
    pb = await _ensure_funding_meta_playbook_async(db, user, "verification")
    old = pb.rules or {}
    pb.rules = payload.rules or {}
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, old_value=old, new_value=payload.rules,
    )
    await db.flush()
    return FundingRulesOut(playbook_id=pb.id, rules=pb.rules or {})


@router.get("/escalation-rules", response_model=FundingRulesOut)
async def get_escalation_rules(
    user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FundingRulesOut:
    _require_admin(user)
    pb = await _ensure_funding_meta_playbook_async(db, user, "escalation")
    return FundingRulesOut(playbook_id=pb.id, rules=pb.rules or {})


@router.patch("/escalation-rules", response_model=FundingRulesOut)
async def patch_escalation_rules(
    payload: FundingRulesPatch,
    user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FundingRulesOut:
    _require_admin(user)
    pb = await _ensure_funding_meta_playbook_async(db, user, "escalation")
    old = pb.rules or {}
    pb.rules = payload.rules or {}
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, old_value=old, new_value=payload.rules,
    )
    await db.flush()
    return FundingRulesOut(playbook_id=pb.id, rules=pb.rules or {})


@router.get("/follow-up-rules", response_model=FundingRulesOut)
async def get_follow_up_rules(
    user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FundingRulesOut:
    """Firm-wide default AI re-engagement cadence. Shape:
        {
          "stall_threshold_minutes": int,      // wait this long after the last
                                                // borrower message before AI nudges
          "max_attempts_per_day":   int,       // skip if >= this many fired in 24h
          "max_days_without_reply": int,       // stop trying after this many days
                                                // without a borrower response
          "quiet_hours_start":       int|null, // 0-23, borrower-local
          "quiet_hours_end":         int|null
        }
    Per-file overrides live on ClientAIPlan.ai_secretary_settings.follow_up
    (loan side) or Client.ai_cadence_override.follow_up (agent side)."""
    _require_admin(user)
    pb = await _ensure_funding_meta_playbook_async(db, user, "follow_up")
    return FundingRulesOut(playbook_id=pb.id, rules=pb.rules or {})


@router.patch("/follow-up-rules", response_model=FundingRulesOut)
async def patch_follow_up_rules(
    payload: FundingRulesPatch,
    user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FundingRulesOut:
    _require_admin(user)
    pb = await _ensure_funding_meta_playbook_async(db, user, "follow_up")
    old = pb.rules or {}
    pb.rules = payload.rules or {}
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, old_value=old, new_value=payload.rules,
    )
    await db.flush()
    return FundingRulesOut(playbook_id=pb.id, rules=pb.rules or {})


@router.get("/communication-rules", response_model=FundingRulesOut)
async def get_communication_rules(
    user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FundingRulesOut:
    _require_admin(user)
    pb = await _ensure_funding_meta_playbook_async(db, user, "communication")
    return FundingRulesOut(playbook_id=pb.id, rules=pb.rules or {})


@router.patch("/communication-rules", response_model=FundingRulesOut)
async def patch_communication_rules(
    payload: FundingRulesPatch,
    user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> FundingRulesOut:
    _require_admin(user)
    pb = await _ensure_funding_meta_playbook_async(db, user, "communication")
    old = pb.rules or {}
    pb.rules = payload.rules or {}
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, old_value=old, new_value=payload.rules,
    )
    await db.flush()
    return FundingRulesOut(playbook_id=pb.id, rules=pb.rules or {})


# ── Audit log (Phase 7 — the search surface) ────────────────────


class AuditEventOut(BaseModel):
    id: UUID
    event_type: str
    actor_type: str
    actor_id: UUID | None
    client_id: UUID | None
    loan_id: UUID | None
    playbook_id: UUID | None
    requirement_key: str | None
    old_value: dict | None
    new_value: dict | None
    payload: dict | None
    created_at: datetime


@router.get("/audit", response_model=list[AuditEventOut])
async def list_audit_events(
    user: CurrentUser,
    client_id: UUID | None = None,
    loan_id: UUID | None = None,
    playbook_id: UUID | None = None,
    event_type: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
) -> list[AuditEventOut]:
    """Searchable audit feed for the super-admin portal. Phase 7."""
    _require_admin(user)
    from app.models.ai_audit_event import AIAuditEvent
    q = select(AIAuditEvent)
    if client_id:
        q = q.where(AIAuditEvent.client_id == client_id)
    if loan_id:
        q = q.where(AIAuditEvent.loan_id == loan_id)
    if playbook_id:
        q = q.where(AIAuditEvent.playbook_id == playbook_id)
    if event_type:
        q = q.where(AIAuditEvent.event_type == event_type)
    q = q.order_by(AIAuditEvent.created_at.desc()).limit(min(limit, 500))
    rows = (await db.execute(q)).scalars().all()
    return [
        AuditEventOut(
            id=r.id, event_type=r.event_type, actor_type=r.actor_type,
            actor_id=r.actor_id, client_id=r.client_id, loan_id=r.loan_id,
            playbook_id=r.playbook_id, requirement_key=r.requirement_key,
            old_value=r.old_value, new_value=r.new_value, payload=r.payload,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ── Serialization helpers ──────────────────────────────────────────


def _serialize_playbook(pb: AIPlaybookTemplate) -> PlaybookOut:
    return PlaybookOut(
        id=pb.id, owner_type=pb.owner_type, owner_id=pb.owner_id,
        playbook_type=pb.playbook_type, product_key=pb.product_key,
        name=pb.name, description=pb.description, rules=pb.rules or {},
        version=pb.version, status=pb.status, published_at=pb.published_at,
        created_at=pb.created_at, updated_at=pb.updated_at,
    )


def _serialize_requirement(r: AICollectionRequirement) -> RequirementOut:
    return RequirementOut(
        id=r.id, playbook_id=r.playbook_id,
        requirement_key=r.requirement_key, label=r.label,
        category=r.category, required_level=r.required_level,
        applies_when=r.applies_when, blocks_stage=r.blocks_stage,
        visibility=list(r.visibility or []),
        can_agent_override=r.can_agent_override,
        can_underwriter_waive=r.can_underwriter_waive,
        verification_required=r.verification_required,
        expiration_days=r.expiration_days,
        ai_request_message_template=r.ai_request_message_template,
        display_order=r.display_order,
        # AI Deal Secretary fields (alembic 0038).
        default_owner_type=r.default_owner_type,
        default_channels=list(r.default_channels or []),
        default_cadence_hours=r.default_cadence_hours,
        link_url=r.link_url,
        link_label=r.link_label,
        link_kind=r.link_kind,
        objective_text=r.objective_text or "",
        completion_criteria=r.completion_criteria or "",
        completion_mode=r.completion_mode,
        wrong_upload_response_template=r.wrong_upload_response_template,
        # Timeline + grouping (alembic 0040).
        depends_on=list(r.depends_on or []),
        parent_key=r.parent_key,
        inferred_depends_on=list(r.inferred_depends_on or []),
        deps_confirmed=r.deps_confirmed,
    )


def _serialize_cadence(r: AICadenceRule) -> CadenceRuleOut:
    return CadenceRuleOut(
        id=r.id, playbook_id=r.playbook_id,
        trigger_event=r.trigger_event,
        applies_to_requirement_key=r.applies_to_requirement_key,
        condition=r.condition, wait_hours=r.wait_hours,
        action_type=r.action_type, approval_required=r.approval_required,
        message_template=r.message_template, visibility=r.visibility,
        is_active=r.is_active, requires_ai_owner=r.requires_ai_owner,
    )


# ── AI training — per-task instructions + tone, and corrections review ──
#
# The "train the AI" interface. Config is stored on the funding
# 'communication' playbook rules.task_training JSONB (same row as the
# firm AI identity). Each AI task composes its prompt as
#   <base prompt in code> + render_training_block(config)
# so unconfigured tasks behave byte-identically to before. v1 exposes
# the three borrower-facing task types (see task_training.TASK_KEYS).


class AiTaskConfigItem(BaseModel):
    task_key: str
    label: str
    instructions: str = ""
    tone: str = ""
    dos: list[str] = []
    donts: list[str] = []
    examples: list[str] = []


class AiTaskConfigsOut(BaseModel):
    tasks: list[AiTaskConfigItem]


class AiTaskConfigSave(BaseModel):
    instructions: str = ""
    tone: str = ""
    dos: list[str] = []
    donts: list[str] = []
    examples: list[str] = []


class AiTrainingExampleAdd(BaseModel):
    text: str


class AiTrainingFeedbackItem(BaseModel):
    kind: str  # "rating" | "correction"
    output_type: str | None
    loan_id: UUID | None
    text: str
    created_at: datetime


def _ai_task_item(task_key: str, cfg: dict) -> AiTaskConfigItem:
    from app.services.ai.task_training import TASK_LABELS

    return AiTaskConfigItem(
        task_key=task_key,
        label=TASK_LABELS.get(task_key, task_key),
        instructions=cfg.get("instructions", "") or "",
        tone=cfg.get("tone", "") or "",
        dos=list(cfg.get("dos") or []),
        donts=list(cfg.get("donts") or []),
        examples=list(cfg.get("examples") or []),
    )


@router.get("/ai-training/tasks", response_model=AiTaskConfigsOut)
async def get_ai_training_tasks(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> AiTaskConfigsOut:
    """All trainable AI task configs for the training control panel."""
    _require_admin(user)
    from app.services.ai.task_training import TASK_KEYS, load_all_task_configs

    configs = await load_all_task_configs(db)
    return AiTaskConfigsOut(
        tasks=[_ai_task_item(k, configs.get(k, {})) for k in TASK_KEYS]
    )


async def _save_ai_task_config(
    db: AsyncSession, user, task_key: str, entry: dict
) -> None:
    """Write one task's config into the funding communication playbook
    rules.task_training JSONB + audit it."""
    pb = await _ensure_funding_meta_playbook_async(db, user, "communication")
    rules = dict(pb.rules or {})
    training = dict(rules.get("task_training") or {})
    entry = dict(entry)
    entry["updated_by"] = str(user.id)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    training[task_key] = entry
    rules["task_training"] = training
    old = pb.rules
    pb.rules = rules
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, old_value=old,
        new_value={"task_training": {task_key: entry}},
    )
    await db.flush()


@router.put("/ai-training/tasks/{task_key}", response_model=AiTaskConfigItem)
async def put_ai_training_task(
    task_key: str,
    payload: AiTaskConfigSave,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AiTaskConfigItem:
    """Save the instructions + tone for one AI task type."""
    _require_admin(user)
    from app.services.ai.task_training import TASK_KEYS

    if task_key not in TASK_KEYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown task_key {task_key!r}")
    entry = {
        "instructions": (payload.instructions or "").strip()[:4000],
        "tone": (payload.tone or "").strip()[:1000],
        "dos": [d.strip() for d in payload.dos if d.strip()][:30],
        "donts": [d.strip() for d in payload.donts if d.strip()][:30],
        "examples": [e.strip() for e in payload.examples if e.strip()][:30],
    }
    await _save_ai_task_config(db, user, task_key, entry)
    return _ai_task_item(task_key, entry)


@router.post("/ai-training/tasks/{task_key}/examples", response_model=AiTaskConfigItem)
async def add_ai_training_example(
    task_key: str,
    payload: AiTrainingExampleAdd,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AiTaskConfigItem:
    """Append one example phrasing to a task (the corrections-review
    'use as example' action)."""
    _require_admin(user)
    from app.services.ai.task_training import TASK_KEYS, load_all_task_configs

    if task_key not in TASK_KEYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown task_key {task_key!r}")
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "example text is empty")
    current = (await load_all_task_configs(db)).get(task_key, {})
    examples = list(current.get("examples") or [])
    if text not in examples:
        examples.append(text)
    entry = {
        "instructions": current.get("instructions", "") or "",
        "tone": current.get("tone", "") or "",
        "dos": list(current.get("dos") or []),
        "donts": list(current.get("donts") or []),
        "examples": examples[:30],
    }
    await _save_ai_task_config(db, user, task_key, entry)
    return _ai_task_item(task_key, entry)


@router.get("/ai-training/feedback", response_model=list[AiTrainingFeedbackItem])
async def get_ai_training_feedback(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = 60,
) -> list[AiTrainingFeedbackItem]:
    """Recent operator signal that should drive training — thumbs-down
    ratings (with comments) + super-admin corrections to AI turns."""
    _require_admin(user)
    from app.enums import FeedbackRating
    from app.models.ai_feedback import AIFeedback
    from app.models.ai_modify_correction import AIModifyCorrection

    limit = max(1, min(limit, 200))
    items: list[AiTrainingFeedbackItem] = []

    fb = (
        await db.execute(
            select(AIFeedback)
            .where(AIFeedback.rating == FeedbackRating.DOWN.value)
            .order_by(AIFeedback.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    for f in fb:
        items.append(
            AiTrainingFeedbackItem(
                kind="rating",
                output_type=str(getattr(f.output_type, "value", f.output_type)),
                loan_id=f.loan_id,
                text=(f.comment or "(thumbs-down, no comment)"),
                created_at=f.created_at,
            )
        )

    corr = (
        await db.execute(
            select(AIModifyCorrection)
            .order_by(AIModifyCorrection.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    for c in corr:
        items.append(
            AiTrainingFeedbackItem(
                kind="correction",
                output_type=None,
                loan_id=c.loan_id,
                text=c.correction,
                created_at=c.created_at,
            )
        )

    items.sort(key=lambda x: x.created_at, reverse=True)
    return items[:limit]


# ── Token-usage reporting (super-admin) ─────────────────────────────
#
# Reads both AI ledgers:
# - ai_token_usage: legacy Anthropic/orchestrator token rows
# - ai_usage_events: current Bedrock tracked usage rows
# Powers /admin/token-usage in QCDashboard over time.


def _usage_window(from_date: str | None, to_date: str | None) -> tuple[datetime, datetime]:
    """Parse optional ISO dates → [start, end). Defaults to last 30 days."""
    now = datetime.now(timezone.utc)
    end = now
    start = now - timedelta(days=30)
    try:
        if to_date:
            end = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
        if from_date:
            start = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return start, end


@router.get("/token-usage/summary")
async def token_usage_summary(
    user: CurrentUser,
    from_date: str | None = None,
    to_date: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from app.models.ai_usage_event import AIUsageEvent as E
    from app.models.ai_token_usage import AITokenUsage as U

    start, end = _usage_window(from_date, to_date)
    legacy_row = (
        await db.execute(
            select(
                func.count().label("calls"),
                func.coalesce(func.sum(U.input_tokens), 0),
                func.coalesce(func.sum(U.output_tokens), 0),
                func.coalesce(func.sum(U.cache_read_tokens), 0),
                func.coalesce(func.sum(U.cache_creation_tokens), 0),
                func.coalesce(func.sum(U.cost_usd), 0),
            )
            .where(U.created_at >= start)
            .where(U.created_at < end)
        )
    ).one()
    bedrock_row = (
        await db.execute(
            select(
                func.count().label("calls"),
                func.coalesce(func.sum(E.input_tokens), 0),
                func.coalesce(func.sum(E.output_tokens), 0),
                func.coalesce(func.sum(E.estimated_cost_usd), 0),
            )
            .where(E.created_at >= start)
            .where(E.created_at < end)
        )
    ).one()
    calls = int(legacy_row[0] or 0) + int(bedrock_row[0] or 0)
    inp = int(legacy_row[1] or 0) + int(bedrock_row[1] or 0)
    out = int(legacy_row[2] or 0) + int(bedrock_row[2] or 0)
    cread = int(legacy_row[3] or 0)
    cwrite = int(legacy_row[4] or 0)
    cost = float(legacy_row[5] or 0) + float(bedrock_row[3] or 0)
    cached_input = int(cread) + int(cwrite)
    total_input = int(inp) + cached_input
    cache_hit_pct = round(100.0 * cached_input / total_input, 1) if total_input else 0.0
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "calls": calls,
        "input_tokens": int(inp),
        "output_tokens": int(out),
        "cache_read_tokens": int(cread),
        "cache_creation_tokens": int(cwrite),
        "total_tokens": total_input + int(out),
        "cache_hit_pct": cache_hit_pct,
        "cost_usd": float(cost),
    }


@router.get("/token-usage/breakdown")
async def token_usage_breakdown(
    user: CurrentUser,
    dimension: Literal["activity", "model", "agent", "broker", "file"] = "activity",
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from app.models.ai_agent import AIAgent
    from app.models.ai_usage_event import AIUsageEvent as E
    from app.models.ai_token_usage import AITokenUsage as U
    from app.models.broker import Broker
    from app.models.deal import Deal
    from app.models.loan import Loan
    from app.models.user import User

    start, end = _usage_window(from_date, to_date)

    # Which column groups the rows, and whether we drop NULL keys.
    col = {
        "activity": U.activity,
        "model": U.model,
        "agent": U.ai_agent_id,
        "broker": U.broker_id,
        "file": U.loan_id,  # funding files; deals folded in below
    }[dimension]

    async def _agg(group_col, where_extra=None):
        stmt = (
            select(
                group_col.label("key"),
                func.count().label("calls"),
                func.coalesce(func.sum(U.input_tokens + U.cache_read_tokens + U.cache_creation_tokens), 0),
                func.coalesce(func.sum(U.output_tokens), 0),
                func.coalesce(func.sum(U.cost_usd), 0),
            )
            .where(U.created_at >= start)
            .where(U.created_at < end)
            .group_by(group_col)
        )
        if where_extra is not None:
            stmt = stmt.where(where_extra)
        return list((await db.execute(stmt)).all())

    async def _agg_events(group_col, where_extra=None):
        stmt = (
            select(
                group_col.label("key"),
                func.count().label("calls"),
                func.coalesce(func.sum(E.input_tokens), 0),
                func.coalesce(func.sum(E.output_tokens), 0),
                func.coalesce(func.sum(E.estimated_cost_usd), 0),
            )
            .where(E.created_at >= start)
            .where(E.created_at < end)
            .group_by(group_col)
        )
        if where_extra is not None:
            stmt = stmt.where(where_extra)
        return list((await db.execute(stmt)).all())

    def merge_row(
        bucket: dict[str, dict[str, Any]],
        *,
        key: str,
        label: str,
        calls: int,
        tokens: int,
        output_tokens: int,
        cost_usd: float,
        kind: str | None = None,
        row_id: str | None = None,
    ) -> None:
        row = bucket.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "calls": 0,
                "tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        row["calls"] += calls
        row["tokens"] += tokens
        row["output_tokens"] += output_tokens
        row["cost_usd"] += cost_usd
        if kind is not None:
            row["kind"] = kind
        if row_id is not None:
            row["id"] = row_id

    rows: list[dict[str, Any]] = []

    if dimension in ("activity", "model"):
        merged: dict[str, dict[str, Any]] = {}
        for key, calls, toks, out, cost in await _agg(col):
            merge_row(
                merged,
                key=f"{dimension}:{key or '—'}",
                label=str(key or "—"),
                calls=int(calls),
                tokens=int(toks),
                output_tokens=int(out),
                cost_usd=float(cost),
            )
        event_col = E.feature if dimension == "activity" else E.model
        for key, calls, toks, out, cost in await _agg_events(event_col):
            merge_row(
                merged,
                key=f"{dimension}:{key or '—'}",
                label=str(key or "—"),
                calls=int(calls),
                tokens=int(toks),
                output_tokens=int(out),
                cost_usd=float(cost),
            )
        rows.extend(merged.values())

    elif dimension == "agent":
        merged: dict[str, dict[str, Any]] = {}
        raw = await _agg(U.ai_agent_id, U.ai_agent_id.is_not(None))
        ids = [r[0] for r in raw]
        names = {}
        if ids:
            for a in (await db.execute(select(AIAgent.id, AIAgent.name).where(AIAgent.id.in_(ids)))).all():
                names[a[0]] = a[1]
        for key, calls, toks, out, cost in raw:
            merge_row(
                merged,
                key=str(key),
                label=names.get(key, "(deleted agent)"),
                calls=int(calls),
                tokens=int(toks),
                output_tokens=int(out),
                cost_usd=float(cost),
            )
        rows.extend(merged.values())

    elif dimension == "broker":
        merged: dict[str, dict[str, Any]] = {}
        raw = await _agg(U.broker_id, U.broker_id.is_not(None))
        event_raw = await _agg_events(E.broker_id, E.broker_id.is_not(None))
        ids = [r[0] for r in raw] + [r[0] for r in event_raw]
        names = {}
        if ids:
            q = (
                select(Broker.id, User.name)
                .join(User, User.id == Broker.user_id, isouter=True)
                .where(Broker.id.in_(ids))
            )
            for b in (await db.execute(q)).all():
                names[b[0]] = b[1] or "Agent"
        for key, calls, toks, out, cost in raw:
            merge_row(
                merged,
                key=str(key),
                label=names.get(key, "(unknown)"),
                calls=int(calls),
                tokens=int(toks),
                output_tokens=int(out),
                cost_usd=float(cost),
            )
        for key, calls, toks, out, cost in event_raw:
            merge_row(
                merged,
                key=str(key),
                label=names.get(key, "(unknown)"),
                calls=int(calls),
                tokens=int(toks),
                output_tokens=int(out),
                cost_usd=float(cost),
            )
        rows.extend(merged.values())

    else:  # file = loans + deals
        merged: dict[str, dict[str, Any]] = {}
        loan_raw = await _agg(U.loan_id, U.loan_id.is_not(None))
        event_loan_raw = await _agg_events(E.loan_id, E.loan_id.is_not(None))
        deal_raw = await _agg(U.deal_id, U.deal_id.is_not(None))
        loan_ids = [r[0] for r in loan_raw] + [r[0] for r in event_loan_raw]
        deal_ids = [r[0] for r in deal_raw]
        loan_lbl, deal_lbl = {}, {}
        if loan_ids:
            for l in (await db.execute(select(Loan.id, Loan.deal_id, Loan.address).where(Loan.id.in_(loan_ids)))).all():
                loan_lbl[l[0]] = (l[1] or "loan") + (f" · {l[2]}" if l[2] else "")
        if deal_ids:
            for d in (await db.execute(select(Deal.id, Deal.title).where(Deal.id.in_(deal_ids)))).all():
                deal_lbl[d[0]] = d[1] or "deal"
        for key, calls, toks, out, cost in loan_raw:
            merge_row(
                merged,
                key=f"loan:{key}",
                label=loan_lbl.get(key, "(loan)"),
                kind="loan",
                row_id=str(key),
                calls=int(calls),
                tokens=int(toks),
                output_tokens=int(out),
                cost_usd=float(cost),
            )
        for key, calls, toks, out, cost in event_loan_raw:
            merge_row(
                merged,
                key=f"loan:{key}",
                label=loan_lbl.get(key, "(loan)"),
                kind="loan",
                row_id=str(key),
                calls=int(calls),
                tokens=int(toks),
                output_tokens=int(out),
                cost_usd=float(cost),
            )
        for key, calls, toks, out, cost in deal_raw:
            merge_row(
                merged,
                key=f"deal:{key}",
                label=deal_lbl.get(key, "(deal)"),
                kind="deal",
                row_id=str(key),
                calls=int(calls),
                tokens=int(toks),
                output_tokens=int(out),
                cost_usd=float(cost),
            )
        rows.extend(merged.values())

    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    return rows[: max(1, min(200, limit))]


@router.get("/token-usage/timeseries")
async def token_usage_timeseries(
    user: CurrentUser,
    from_date: str | None = None,
    to_date: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from app.models.ai_usage_event import AIUsageEvent as E
    from app.models.ai_token_usage import AITokenUsage as U

    start, end = _usage_window(from_date, to_date)
    legacy_bucket = func.date_trunc("day", U.created_at).label("day")
    legacy_raw = (
        await db.execute(
            select(
                legacy_bucket,
                func.count().label("calls"),
                func.coalesce(func.sum(U.input_tokens + U.cache_read_tokens + U.cache_creation_tokens), 0),
                func.coalesce(func.sum(U.output_tokens), 0),
                func.coalesce(func.sum(U.cost_usd), 0),
            )
            .where(U.created_at >= start)
            .where(U.created_at < end)
            .group_by(legacy_bucket)
        )
    ).all()
    event_bucket = func.date_trunc("day", E.created_at).label("day")
    event_raw = (
        await db.execute(
            select(
                event_bucket,
                func.count().label("calls"),
                func.coalesce(func.sum(E.input_tokens), 0),
                func.coalesce(func.sum(E.output_tokens), 0),
                func.coalesce(func.sum(E.estimated_cost_usd), 0),
            )
            .where(E.created_at >= start)
            .where(E.created_at < end)
            .group_by(event_bucket)
        )
    ).all()
    merged: dict[str, dict[str, Any]] = {}
    for day, calls, toks, out, cost in [*legacy_raw, *event_raw]:
        key = day.date().isoformat() if hasattr(day, "date") else str(day)
        row = merged.setdefault(
            key,
            {"day": key, "calls": 0, "tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        )
        row["calls"] += int(calls)
        row["tokens"] += int(toks)
        row["output_tokens"] += int(out)
        row["cost_usd"] += float(cost)
    return [
        {
            **row,
            "cost_usd": round(float(row["cost_usd"]), 6),
        }
        for row in sorted(merged.values(), key=lambda item: item["day"])
    ]
