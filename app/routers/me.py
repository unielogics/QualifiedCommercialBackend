"""Per-user personal settings endpoints.

Today this is just the broker-settings overlay (`brokers.settings_data`,
alembic 0023) — the agent's checklist additions/disables, AI cadence
overrides, and personal letterhead. Replaces the v1 localStorage
persistence on the desktop /agent-settings page.

Scoped to `Role.BROKER` — super-admins use the firm-wide /settings
page, not this one.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings as get_app_config
from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.broker import Broker
from app.schemas.broker_settings import AgentSettingsData, AgentSettingsRead

router = APIRouter(prefix="/me", tags=["me"])
log = logging.getLogger(__name__)


async def _broker_for_user(db: AsyncSession, user_id) -> Broker | None:
    return (
        await db.execute(select(Broker).where(Broker.user_id == user_id))
    ).scalar_one_or_none()


@router.get("/broker-settings", response_model=AgentSettingsRead)
async def get_broker_settings(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> AgentSettingsRead:
    """Returns the calling broker's overlay data, or empty defaults
    when the row is fresh / no overlay configured yet."""
    if user.role != Role.BROKER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Broker-settings is broker-only — super-admins use /settings.",
        )
    broker = await _broker_for_user(db, user.id)
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile for this user")
    raw = broker.settings_data
    data = AgentSettingsData() if not raw else AgentSettingsData.model_validate(raw)
    return AgentSettingsRead(data=data)


@router.put("/broker-settings", response_model=AgentSettingsRead)
async def put_broker_settings(
    payload: AgentSettingsData,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentSettingsRead:
    """Whole-document replacement — the desktop sends the full overlay
    every save. Validates that checklist keys are `buyer` | `seller`
    only (post-codex-PR shape). The Pydantic schema's
    `_migrate_v1_shapes` validator already strips legacy
    `loan_type:side` keys, so payloads from old clients are tolerated
    silently.
    """
    if user.role != Role.BROKER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Broker-settings is broker-only.",
        )
    broker = await _broker_for_user(db, user.id)
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile for this user")

    errors: list[str] = []
    for key, overlay in (payload.checklists or {}).items():
        if key not in ("buyer", "seller"):
            errors.append(
                f"checklist key {key!r} must be 'buyer' or 'seller'"
            )
            continue
        # Agent extras carry a `side` tag for downstream resolvers;
        # the wizard pre-fills it but defensively allow either the
        # current tab's side or 'both'.
        for it in overlay.extra_items:
            if it.side not in ("buyer", "seller", "both"):
                errors.append(
                    f"checklist[{key}].extra_items[{it.name!r}].side "
                    f"must be buyer | seller | both"
                )
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"errors": errors},
        )

    broker.settings_data = payload.model_dump(mode="json")
    await db.commit()
    await db.refresh(broker)
    return AgentSettingsRead(
        data=AgentSettingsData.model_validate(broker.settings_data or {})
    )


# ── Headshot upload (S3) ──────────────────────────────────────────────
#
# Two-step flow mirrors /settings/letterhead/signature/upload-init:
#   1. POST /me/broker-settings/headshot/upload-init  → presigned PUT URL
#   2. Browser PUTs PNG/JPEG bytes directly to S3
#   3. Frontend PUT /me/broker-settings with letterhead.headshot_s3_key
#
# Key is deterministic per broker — re-uploads overwrite. Prequal PDFs
# pull the key off broker.settings_data.letterhead.headshot_s3_key and
# composit the headshot beside the firm logo on co-branded letters.


def _headshot_s3_key_for(broker_id) -> str:
    """Deterministic key per broker so re-uploads overwrite cleanly."""
    return f"brokers/{broker_id}/headshot.png"


class _HeadshotUploadInitRequest(BaseModel):
    content_type: Literal["image/png", "image/jpeg"] = "image/png"


class HeadshotUploadInitResponse(BaseModel):
    """Same shape as the firm-signature upload-init response.

    s3_key      — caller PUTs bytes to upload_url then PUTs
                  /me/broker-settings with letterhead.headshot_s3_key=<this>.
    upload_url  — presigned PUT URL (5-min TTL). None when the backend
                  is running without S3 credentials (local dev).
    """
    s3_key: str
    upload_url: str | None


@router.post(
    "/broker-settings/headshot/upload-init",
    response_model=HeadshotUploadInitResponse,
)
async def headshot_upload_init(
    payload: _HeadshotUploadInitRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> HeadshotUploadInitResponse:
    """Mint a presigned PUT URL for the broker's headshot."""
    if user.role != Role.BROKER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Headshot upload is broker-only.",
        )
    broker = await _broker_for_user(db, user.id)
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile for this user")

    s3_key = _headshot_s3_key_for(broker.id)
    cfg = get_app_config()
    if not cfg.s3_bucket:
        return HeadshotUploadInitResponse(s3_key=s3_key, upload_url=None)

    import boto3

    s3 = boto3.client("s3", region_name=cfg.aws_region)
    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": cfg.s3_bucket,
                "Key": s3_key,
                "ContentType": payload.content_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=300,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Could not mint headshot upload URL: {exc}",
        ) from exc

    return HeadshotUploadInitResponse(s3_key=s3_key, upload_url=upload_url)


# ── Agent AI Playbook overlay (Phase 2) ────────────────────────────


from typing import Any  # noqa: E402
from uuid import UUID  # noqa: E402

from datetime import datetime  # noqa: E402


# Closed enums mirrored from app/enums.py for the agent overlay. The
# super-admin path in lending_admin.py uses the same set — kept inline
# here too rather than imported across router files.
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


class PlaybookRequirementOut(BaseModel):
    id: UUID
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


class AgentPlaybookOut(BaseModel):
    """Combined platform playbook + agent overlay for ONE
    playbook_type (buyer | seller | cadence). The UI renders the
    platform rows as a read-only top section, then the agent's
    overlay rows below as fully editable."""
    playbook_type: str
    platform_id: UUID | None
    platform_version: int | None
    agent_id: UUID | None
    agent_version: int | None
    rules: dict
    platform_requirements: list[PlaybookRequirementOut]
    agent_requirements: list[PlaybookRequirementOut]


@router.get("/ai-playbook/{playbook_type}", response_model=AgentPlaybookOut)
async def get_agent_playbook(
    playbook_type: Literal["buyer", "seller", "cadence"],
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentPlaybookOut:
    """Return the platform playbook + the agent's overlay for the given
    type. Creates an empty agent playbook on demand the first time the
    agent opens this surface."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")

    from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
    plat = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "platform",
            AIPlaybookTemplate.playbook_type == playbook_type,
            AIPlaybookTemplate.is_active.is_(True),
        )
    )).scalars().all()
    plat_pb = max(plat, key=lambda p: p.version) if plat else None
    agent_pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == playbook_type,
        )
    )).scalar_one_or_none()
    if agent_pb is None:
        agent_pb = AIPlaybookTemplate(
            owner_type="agent", owner_id=user.id,
            playbook_type=playbook_type,
            name=f"My {playbook_type} playbook",
            description="Agent overlay — overrides platform defaults where allowed.",
            rules={},
            version=1, status="published",
        )
        db.add(agent_pb)
        await db.flush()

    plat_reqs: list[AICollectionRequirement] = []
    if plat_pb is not None:
        plat_reqs = list((await db.execute(
            select(AICollectionRequirement)
            .where(AICollectionRequirement.playbook_id == plat_pb.id)
            .order_by(AICollectionRequirement.display_order)
        )).scalars().all())
    agent_reqs = list((await db.execute(
        select(AICollectionRequirement)
        .where(AICollectionRequirement.playbook_id == agent_pb.id)
        .order_by(AICollectionRequirement.display_order)
    )).scalars().all())

    def _ser(r: AICollectionRequirement) -> PlaybookRequirementOut:
        return PlaybookRequirementOut(
            id=r.id, requirement_key=r.requirement_key, label=r.label,
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
        )

    return AgentPlaybookOut(
        playbook_type=playbook_type,
        platform_id=plat_pb.id if plat_pb else None,
        platform_version=plat_pb.version if plat_pb else None,
        agent_id=agent_pb.id, agent_version=agent_pb.version,
        rules=agent_pb.rules or {},
        platform_requirements=[_ser(r) for r in plat_reqs],
        agent_requirements=[_ser(r) for r in agent_reqs],
    )


class AgentRequirementUpsert(BaseModel):
    """Either creates a new agent-overlay requirement (no id) or
    edits an existing one (id supplied + caller must own it).

    AI Deal Secretary fields are optional on the wire — server applies
    sensible defaults (owner=human, channels=["portal"], 48h cadence,
    no link, empty objective/completion, ai_can_complete) when omitted."""
    id: UUID | None = None
    requirement_key: str
    label: str
    category: _RequirementCategoryLiteral
    required_level: Literal["required", "recommended", "optional"]
    applies_when: dict | None = None
    blocks_stage: str | None = None
    visibility: list[str] | None = None
    expiration_days: int | None = None
    ai_request_message_template: str | None = None
    display_order: int = 100
    # AI Deal Secretary fields (alembic 0038). All optional.
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


@router.post("/ai-playbook/{playbook_type}/requirements", response_model=PlaybookRequirementOut)
async def upsert_agent_requirement(
    playbook_type: Literal["buyer", "seller", "cadence"],
    payload: AgentRequirementUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaybookRequirementOut:
    """Add or edit a requirement on the agent's overlay playbook."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
    from app.services.ai.audit import record_event

    agent_pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == playbook_type,
        )
    )).scalar_one_or_none()
    if agent_pb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No agent overlay — GET first to provision")

    if payload.id is not None:
        req = await db.get(AICollectionRequirement, payload.id)
        if req is None or req.playbook_id != agent_pb.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found on your overlay")
        old = {
            "label": req.label,
            "required_level": req.required_level,
            "applies_when": req.applies_when,
        }
        req.requirement_key = payload.requirement_key
        req.label = payload.label
        req.category = payload.category
        req.required_level = payload.required_level
        req.applies_when = payload.applies_when
        req.blocks_stage = payload.blocks_stage
        if payload.visibility is not None:
            req.visibility = payload.visibility
        req.expiration_days = payload.expiration_days
        req.ai_request_message_template = payload.ai_request_message_template
        req.display_order = payload.display_order
        # AI Deal Secretary fields (alembic 0038).
        req.default_owner_type = payload.default_owner_type
        if payload.default_channels is not None:
            req.default_channels = payload.default_channels
        req.default_cadence_hours = payload.default_cadence_hours
        req.link_url = payload.link_url
        req.link_label = payload.link_label
        req.link_kind = payload.link_kind
        req.objective_text = payload.objective_text
        req.completion_criteria = payload.completion_criteria
        req.completion_mode = payload.completion_mode
        req.wrong_upload_response_template = payload.wrong_upload_response_template
        await record_event(
            db, event_type="requirement_added", actor_type="user",
            actor_id=user.id, playbook_id=agent_pb.id,
            requirement_key=req.requirement_key,
            old_value=old,
            new_value={"label": req.label, "required_level": req.required_level},
        )
    else:
        req = AICollectionRequirement(
            playbook_id=agent_pb.id,
            requirement_key=payload.requirement_key,
            label=payload.label,
            category=payload.category,
            required_level=payload.required_level,
            applies_when=payload.applies_when,
            blocks_stage=payload.blocks_stage,
            visibility=payload.visibility or ["agent"],
            can_agent_override=True,
            can_underwriter_waive=True,
            verification_required=False,
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
        )
        db.add(req)
        await db.flush()
        await record_event(
            db, event_type="requirement_added", actor_type="user",
            actor_id=user.id, playbook_id=agent_pb.id,
            requirement_key=req.requirement_key,
            new_value={"label": req.label, "required_level": req.required_level},
        )
    await db.flush()
    return PlaybookRequirementOut(
        id=req.id, requirement_key=req.requirement_key, label=req.label,
        category=req.category, required_level=req.required_level,
        applies_when=req.applies_when, blocks_stage=req.blocks_stage,
        visibility=list(req.visibility or []),
        can_agent_override=req.can_agent_override,
        can_underwriter_waive=req.can_underwriter_waive,
        verification_required=req.verification_required,
        expiration_days=req.expiration_days,
        ai_request_message_template=req.ai_request_message_template,
        display_order=req.display_order,
        # AI Deal Secretary fields (alembic 0038).
        default_owner_type=req.default_owner_type,
        default_channels=list(req.default_channels or []),
        default_cadence_hours=req.default_cadence_hours,
        link_url=req.link_url,
        link_label=req.link_label,
        link_kind=req.link_kind,
        objective_text=req.objective_text or "",
        completion_criteria=req.completion_criteria or "",
        completion_mode=req.completion_mode,
        wrong_upload_response_template=req.wrong_upload_response_template,
    )


@router.delete("/ai-playbook/{playbook_type}/requirements/{requirement_id}")
async def delete_agent_requirement(
    playbook_type: Literal["buyer", "seller", "cadence"],
    requirement_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
    from app.services.ai.audit import record_event

    req = await db.get(AICollectionRequirement, requirement_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    pb = await db.get(AIPlaybookTemplate, req.playbook_id)
    if pb is None or pb.owner_type != "agent" or pb.owner_id != user.id or pb.playbook_type != playbook_type:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not yours")
    key = req.requirement_key
    await db.delete(req)
    await record_event(
        db, event_type="requirement_removed", actor_type="user",
        actor_id=user.id, playbook_id=pb.id, requirement_key=key,
    )
    await db.flush()
    return {"ok": True}


class AgentPlaybookRulesPatch(BaseModel):
    """Free-form rules JSONB on the agent's overlay (e.g. before_handoff
    list, message style, cadence presets). The UI surfaces specific
    fields in this blob via the agent-settings tabs."""
    rules: dict


@router.patch("/ai-playbook/{playbook_type}/rules", response_model=AgentPlaybookOut)
async def patch_agent_playbook_rules(
    playbook_type: Literal["buyer", "seller", "cadence"],
    payload: AgentPlaybookRulesPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentPlaybookOut:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_playbook import AIPlaybookTemplate
    from app.services.ai.audit import record_event

    pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == playbook_type,
        )
    )).scalar_one_or_none()
    if pb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No agent overlay — GET first")
    old = pb.rules or {}
    pb.rules = payload.rules or {}
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, old_value=old, new_value=payload.rules,
    )
    await db.flush()
    return await get_agent_playbook(playbook_type, user, db)  # type: ignore[arg-type]


# ── Agent cadence rules CRUD (overlay) ─────────────────────────────


class AgentCadenceRuleOut(BaseModel):
    id: UUID
    trigger_event: str
    applies_to_requirement_key: str | None
    condition: dict | None
    wait_hours: int
    action_type: str
    approval_required: bool
    message_template: str | None
    visibility: str
    is_active: bool


class AgentCadenceRuleUpsert(BaseModel):
    id: UUID | None = None
    trigger_event: str
    applies_to_requirement_key: str | None = None
    condition: dict | None = None
    wait_hours: int = 0
    action_type: Literal["draft_message", "create_task", "escalate", "mark_stalled", "auto_send_reminder"]
    approval_required: bool = True
    message_template: str | None = None
    visibility: Literal["internal", "agent", "borrower"] = "agent"
    is_active: bool = True


@router.get("/ai-playbook/cadence/rules", response_model=list[AgentCadenceRuleOut])
async def list_agent_cadence_rules(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[AgentCadenceRuleOut]:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_cadence_rule import AICadenceRule
    from app.models.ai_playbook import AIPlaybookTemplate

    pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == "cadence",
        )
    )).scalar_one_or_none()
    if pb is None:
        return []
    rows = (await db.execute(
        select(AICadenceRule).where(AICadenceRule.playbook_id == pb.id).order_by(AICadenceRule.created_at)
    )).scalars().all()
    return [AgentCadenceRuleOut(
        id=r.id, trigger_event=r.trigger_event,
        applies_to_requirement_key=r.applies_to_requirement_key,
        condition=r.condition, wait_hours=r.wait_hours,
        action_type=r.action_type, approval_required=r.approval_required,
        message_template=r.message_template, visibility=r.visibility,
        is_active=r.is_active,
    ) for r in rows]


@router.post("/ai-playbook/cadence/rules", response_model=AgentCadenceRuleOut)
async def upsert_agent_cadence_rule(
    payload: AgentCadenceRuleUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentCadenceRuleOut:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_cadence_rule import AICadenceRule
    from app.models.ai_playbook import AIPlaybookTemplate

    pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == "cadence",
        )
    )).scalar_one_or_none()
    if pb is None:
        pb = AIPlaybookTemplate(
            owner_type="agent", owner_id=user.id, playbook_type="cadence",
            name="My cadence playbook", rules={}, version=1, status="published",
        )
        db.add(pb)
        await db.flush()

    if payload.id is not None:
        row = await db.get(AICadenceRule, payload.id)
        if row is None or row.playbook_id != pb.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
        for f in (
            "trigger_event", "applies_to_requirement_key", "condition",
            "wait_hours", "action_type", "approval_required",
            "message_template", "visibility", "is_active",
        ):
            setattr(row, f, getattr(payload, f))
    else:
        row = AICadenceRule(
            playbook_id=pb.id,
            trigger_event=payload.trigger_event,
            applies_to_requirement_key=payload.applies_to_requirement_key,
            condition=payload.condition,
            wait_hours=payload.wait_hours,
            action_type=payload.action_type,
            approval_required=payload.approval_required,
            message_template=payload.message_template,
            visibility=payload.visibility,
            is_active=payload.is_active,
        )
        db.add(row)
    await db.flush()
    return AgentCadenceRuleOut(
        id=row.id, trigger_event=row.trigger_event,
        applies_to_requirement_key=row.applies_to_requirement_key,
        condition=row.condition, wait_hours=row.wait_hours,
        action_type=row.action_type, approval_required=row.approval_required,
        message_template=row.message_template, visibility=row.visibility,
        is_active=row.is_active,
    )


@router.delete("/ai-playbook/cadence/rules/{rule_id}")
async def delete_agent_cadence_rule(
    rule_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_cadence_rule import AICadenceRule
    from app.models.ai_playbook import AIPlaybookTemplate
    row = await db.get(AICadenceRule, rule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    pb = await db.get(AIPlaybookTemplate, row.playbook_id)
    if pb is None or pb.owner_type != "agent" or pb.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not yours")
    await db.delete(row)
    await db.flush()
    return {"ok": True}
