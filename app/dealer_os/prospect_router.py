"""Field Desk dealer prospect pipeline API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.dealer_prospect import (
    DealerProspect,
    DealerProspectOutcomeDefinition,
    DealerProspectStageDefinition,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.prospect_outreach import ProspectEmailDraftCreate
from app.services import prospect_outreach as outreach_service
from app.services.user_access import record_access_event, request_metadata

from .models import DealerRepCompany, DealerRepContact, DealerRepContactAssignment
from .prospect_schemas import (
    ProspectActivityCreate,
    ProspectActivityRead,
    ProspectConversionRequest,
    ProspectConversionResult,
    ProspectCreate,
    ProspectDefinitionReorder,
    ProspectListRead,
    ProspectMoveResult,
    ProspectMoveStage,
    ProspectOutcomeApply,
    ProspectOutcomeCreate,
    ProspectOutcomePatch,
    ProspectOutcomeRead,
    ProspectOutcomeResult,
    ProspectOwnerRead,
    ProspectPatch,
    ProspectRead,
    ProspectStageCreate,
    ProspectStagePatch,
    ProspectStageRead,
    ProspectUndoRequest,
    ProspectUserAccessList,
    ProspectUserAccessPatch,
    ProspectUserAccessRead,
)
from .services import prospects as service

_PROSPECT_ACCESS_ADMIN_ROOT = "/dealer-os/admin/prospect-access"


def _require_pipeline_master(request: Request) -> None:
    """Keep the pilot control reachable before the master switch is enabled."""

    path = request.url.path
    if path.endswith(_PROSPECT_ACCESS_ADMIN_ROOT) or f"{_PROSPECT_ACCESS_ADMIN_ROOT}/" in path:
        return
    service.require_pipeline_enabled()


router = APIRouter(
    prefix="/dealer-os",
    tags=["dealer-prospects"],
    dependencies=[Depends(_require_pipeline_master)],
)
DbSession = Annotated[AsyncSession, Depends(get_db)]


def _prospect_access_read(user: User) -> ProspectUserAccessRead:
    eligible = service.is_active_prospect_owner(user)
    assigned = bool(getattr(user, "dealer_prospect_pipeline_enabled", False))
    return ProspectUserAccessRead(
        user_id=user.id,
        name=user.name,
        email=user.email,
        role=user.role.value if isinstance(user.role, Role) else str(user.role),
        account_status=user.account_status or "active",
        field_desk_access=eligible,
        eligible=eligible,
        enabled=assigned,
        effective_enabled=service.pipeline_effective_enabled(user),
        updated_at=getattr(user, "updated_at", None),
    )


@router.get("/admin/prospect-access", response_model=ProspectUserAccessList)
async def list_prospect_user_access(
    user: CurrentUser,
    db: DbSession,
) -> ProspectUserAccessList:
    """List potential pipeline operators, including brokers awaiting Field Desk access."""

    service.require_config_admin(user)
    rows = list(
        (
            await db.execute(
                select(User)
                .where(
                    User.deleted_at.is_(None),
                    User.role.in_(
                        [Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP, Role.BROKER]
                    ),
                )
                .order_by(func.lower(User.name), func.lower(User.email), User.id)
            )
        )
        .scalars()
        .all()
    )
    return ProspectUserAccessList(
        global_enabled=bool(service.get_settings().dealer_prospect_pipeline_enabled),
        items=[_prospect_access_read(row) for row in rows],
    )


@router.patch(
    "/admin/prospect-access/{user_id}",
    response_model=ProspectUserAccessRead,
)
async def update_prospect_user_access(
    user_id: UUID,
    payload: ProspectUserAccessPatch,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> ProspectUserAccessRead:
    """Grant or revoke one eligible operator's pipeline entitlement."""

    service.require_config_admin(user)
    target = (
        await db.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if target is None or target.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if payload.enabled and not service.is_active_prospect_owner(target):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Pipeline access requires an active user with Field Desk access.",
        )

    before = bool(getattr(target, "dealer_prospect_pipeline_enabled", False))
    if before != payload.enabled:
        target.dealer_prospect_pipeline_enabled = payload.enabled
        record_access_event(
            db,
            user_id=target.id,
            actor_user_id=user.id,
            action=(
                "dealer_prospect_pipeline.enabled"
                if payload.enabled
                else "dealer_prospect_pipeline.disabled"
            ),
            reason=payload.reason,
            before_state={"dealer_prospect_pipeline_enabled": before},
            after_state={"dealer_prospect_pipeline_enabled": payload.enabled},
            metadata=request_metadata(
                ip_address=(request.headers.get("x-forwarded-for") or "")
                .split(",", 1)[0]
                .strip()
                or (request.client.host if request.client else None),
                user_agent=request.headers.get("user-agent"),
            ),
        )
    await db.flush()
    await db.refresh(target)
    return _prospect_access_read(target)


async def _refresh_for_read(db: AsyncSession, prospect: DealerProspect) -> None:
    # TimestampMixin expires updated_at after an UPDATE; explicit refresh keeps
    # async response serialization from attempting forbidden lazy I/O.
    await db.refresh(prospect)


def _stage_read(row: DealerProspectStageDefinition) -> ProspectStageRead:
    return ProspectStageRead(
        id=row.id,
        key=row.key,
        label=row.label,
        sort_order=row.sort_order,
        position=row.sort_order,
        is_active=row.is_active,
        is_terminal=row.is_terminal,
        is_system=row.is_system,
        behavior=row.behavior or {},
    )


def _outcome_read(row: DealerProspectOutcomeDefinition) -> ProspectOutcomeRead:
    config = row.action_config or {}
    return ProspectOutcomeRead(
        id=row.id,
        key=row.key,
        label=row.label,
        sort_order=row.sort_order,
        position=row.sort_order,
        is_active=row.is_active,
        is_system=row.is_system,
        action_config=config,
        requires_follow_up=bool(config.get("requires_follow_up")),
        requires_appointment=bool(config.get("requires_appointment")),
        creates_email_draft=bool(config.get("email_action")),
    )


async def _validate_owner(db: AsyncSession, owner_user_id: UUID) -> User:
    owner = await db.get(User, owner_user_id)
    if not service.is_active_prospect_owner(owner) or not bool(
        getattr(owner, "dealer_prospect_pipeline_enabled", False)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Owner must have enabled Dealer Pipeline access",
        )
    return owner


async def _validate_outcome_target(
    db: AsyncSession, action_config: dict[str, Any] | None
) -> dict[str, Any]:
    config = service.validate_action_config(action_config)
    target = config.get("target_stage_key")
    if target:
        exists_row = (
            await db.execute(
                select(DealerProspectStageDefinition.id).where(
                    DealerProspectStageDefinition.key == target,
                    DealerProspectStageDefinition.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if exists_row is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "Outcome target stage is unavailable"
            )
    return config


_EMAIL_PURPOSES = {
    "dealer_information_pack": "dealer_information",
    "missed_call": "missed_call",
    "callback_confirmation": "callback_confirmation",
    "booking_link": "booking",
}


async def _create_action_draft(
    db: AsyncSession,
    *,
    prospect: DealerProspect,
    user: User,
    action: str,
):
    purpose = _EMAIL_PURPOSES.get(action, "dealer_information")
    try:
        return await outreach_service.create_draft(
            db,
            prospect=prospect,
            actor=user,
            payload=ProspectEmailDraftCreate(purpose=purpose),
        )
    except outreach_service.OutreachConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "draft_conflict", "message": str(exc)},
        ) from exc
    except outreach_service.OutreachBlocked as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.detail},
        ) from exc


@router.get("/prospect-owners", response_model=list[ProspectOwnerRead])
async def list_prospect_owners(
    user: CurrentUser,
    db: DbSession,
) -> list[ProspectOwnerRead]:
    """Assignment choices, including brokers explicitly granted Field Desk."""
    service.require_config_admin(user)
    rows = list(
        (
            await db.execute(
                select(User)
                .where(
                    User.deleted_at.is_(None),
                    User.account_status == "active",
                    User.dealer_prospect_pipeline_enabled.is_(True),
                    or_(
                        User.role.in_([Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP]),
                        and_(
                            User.role == Role.BROKER,
                            User.account_access_types.contains(["field_desk"]),
                        ),
                    ),
                )
                .order_by(func.lower(User.name), func.lower(User.email))
            )
        )
        .scalars()
        .all()
    )
    return [
        ProspectOwnerRead(
            id=row.id,
            name=row.name,
            email=row.email,
            phone=row.phone,
            title=row.title,
            role=row.role.value if isinstance(row.role, Role) else str(row.role),
        )
        for row in rows
    ]


@router.get("/prospects", response_model=ProspectListRead)
async def list_prospects(
    user: CurrentUser,
    db: DbSession,
    q: str = Query(default="", max_length=160),
    stage_key: str | None = Query(default=None, max_length=64),
    outcome_key: str | None = Query(default=None, max_length=64),
    owner_user_id: UUID | None = None,
    follow_up_before: datetime | None = None,
    sort_by: Literal[
        "dealer_name",
        "contact_name",
        "stage",
        "outcome",
        "owner",
        "next_follow_up",
        "last_activity",
        "updated",
        "created",
        "attempts",
    ] = "stage",
    sort_dir: Literal["asc", "desc"] = "asc",
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> ProspectListRead:
    service.require_prospect_actor(user)
    await service.ensure_default_definitions(db)
    owner = aliased(User, name="prospect_owner")
    last_outcome = aliased(DealerProspectOutcomeDefinition, name="prospect_last_outcome")
    filters: list[Any] = [
        DealerProspect.archived_at.is_(None),
        service.prospect_access_filter(user),
    ]
    if q.strip():
        like = f"%{q.strip().lower()}%"
        filters.append(
            or_(
                func.lower(DealerRepContact.full_name).like(like),
                func.lower(DealerRepCompany.name).like(like),
                func.lower(DealerProspect.email_normalized).like(like),
                func.lower(DealerProspect.phone_normalized).like(like),
                func.lower(func.coalesce(owner.name, "")).like(like),
                func.lower(func.coalesce(owner.email, "")).like(like),
            )
        )
    if stage_key:
        filters.append(DealerProspectStageDefinition.key == service.definition_key(stage_key))
    if outcome_key:
        outcome_id = (
            await db.execute(
                select(DealerProspectOutcomeDefinition.id).where(
                    DealerProspectOutcomeDefinition.key == service.definition_key(outcome_key)
                )
            )
        ).scalar_one_or_none()
        if outcome_id is None:
            return ProspectListRead(
                items=[],
                total=0,
                limit=limit,
                offset=offset,
                stages=[_stage_read(row) for row in await service.active_stages(db)],
                outcomes=[_outcome_read(row) for row in await service.active_outcomes(db)],
            )
        filters.append(DealerProspect.last_outcome_definition_id == outcome_id)
    if owner_user_id:
        # Reps may ask for their own value, but never use this filter to probe
        # another rep's book.
        if user.role not in service.TEAM_ROLES and owner_user_id != user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Cannot filter another owner's prospects"
            )
        filters.append(DealerProspect.owner_user_id == owner_user_id)
    if follow_up_before:
        filters.append(DealerProspect.next_follow_up_at <= follow_up_before)

    joined = (
        select(DealerProspect)
        .join(DealerRepContact, DealerRepContact.id == DealerProspect.primary_contact_id)
        .join(DealerRepCompany, DealerRepCompany.id == DealerProspect.company_id)
        .join(
            DealerProspectStageDefinition,
            DealerProspectStageDefinition.id == DealerProspect.stage_definition_id,
        )
        .outerjoin(owner, owner.id == DealerProspect.owner_user_id)
        .outerjoin(
            last_outcome,
            last_outcome.id == DealerProspect.last_outcome_definition_id,
        )
        .where(*filters)
    )
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(DealerProspect)
                .join(DealerRepContact, DealerRepContact.id == DealerProspect.primary_contact_id)
                .join(DealerRepCompany, DealerRepCompany.id == DealerProspect.company_id)
                .join(
                    DealerProspectStageDefinition,
                    DealerProspectStageDefinition.id == DealerProspect.stage_definition_id,
                )
                .outerjoin(owner, owner.id == DealerProspect.owner_user_id)
                .outerjoin(
                    last_outcome,
                    last_outcome.id == DealerProspect.last_outcome_definition_id,
                )
                .where(*filters)
            )
        ).scalar_one()
    )
    sort_columns = {
        "dealer_name": func.lower(DealerRepCompany.name),
        "contact_name": func.lower(DealerRepContact.full_name),
        "stage": DealerProspectStageDefinition.sort_order,
        "outcome": func.lower(last_outcome.label),
        "owner": func.lower(func.coalesce(func.nullif(owner.name, ""), owner.email)),
        "next_follow_up": DealerProspect.next_follow_up_at,
        "last_activity": DealerProspect.last_activity_at,
        "updated": DealerProspect.updated_at,
        "created": DealerProspect.created_at,
        "attempts": DealerProspect.call_attempt_count,
    }
    primary_order = (
        sort_columns[sort_by].desc().nullslast()
        if sort_dir == "desc"
        else sort_columns[sort_by].asc().nullslast()
    )
    tie_breakers = [DealerProspect.updated_at.desc(), DealerProspect.id.asc()]
    if sort_by == "stage":
        tie_breakers.insert(0, DealerProspect.next_follow_up_at.asc().nullslast())
    rows = list(
        (
            await db.execute(
                joined.order_by(primary_order, *tie_breakers).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    items = [ProspectRead.model_validate(item) for item in await service.prospects_read(db, rows)]
    stages = [_stage_read(row) for row in await service.active_stages(db)]
    outcomes = [_outcome_read(row) for row in await service.active_outcomes(db)]
    return ProspectListRead(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        stages=stages,
        outcomes=outcomes,
    )


@router.post("/prospects", response_model=ProspectRead, status_code=status.HTTP_201_CREATED)
async def quick_add_prospect(
    payload: ProspectCreate,
    user: CurrentUser,
    db: DbSession,
) -> ProspectRead:
    try:
        prospect = await service.create_prospect(
            db,
            user,
            contact_name=payload.contact_name,
            dealer_name=payload.dealer_name,
            email=str(payload.email),
            phone=payload.phone,
            source=payload.source,
            owner_user_id=payload.owner_user_id,
            contact_id=payload.contact_id,
            initial_note=payload.initial_note,
        )
    except IntegrityError as exc:
        # The scoped unique indexes are the race-proof second line after the
        # helpful preflight duplicate check.
        await db.rollback()
        duplicates = await service.find_duplicates(
            db,
            dealer_name_normalized=service.normalize_dealer_name(payload.dealer_name),
            email_normalized=service.normalize_email(str(payload.email)),
            phone_normalized=payload.phone,
            primary_contact_id=payload.contact_id,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=service.duplicate_detail(duplicates, user, known_contact_id=payload.contact_id),
        ) from exc
    await _refresh_for_read(db, prospect)
    return ProspectRead.model_validate(
        await service.prospect_read(db, prospect, include_activities=True)
    )


@router.get("/prospects/{prospect_id}", response_model=ProspectRead)
async def get_prospect(
    prospect_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> ProspectRead:
    prospect = await service.load_visible_prospect(db, user, prospect_id)
    return ProspectRead.model_validate(
        await service.prospect_read(db, prospect, include_activities=True)
    )


@router.patch("/prospects/{prospect_id}", response_model=ProspectRead)
async def patch_prospect(
    prospect_id: UUID,
    payload: ProspectPatch,
    user: CurrentUser,
    db: DbSession,
) -> ProspectRead:
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    service.assert_expected_version(prospect, payload.expected_version)
    contact = await db.get(DealerRepContact, prospect.primary_contact_id)
    company = await db.get(DealerRepCompany, prospect.company_id)
    if contact is None or company is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Prospect contact is incomplete")
    changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    changed_fields: list[str] = []

    if "owner_user_id" in changes:
        owner_id = changes["owner_user_id"]
        if owner_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Owner cannot be cleared")
        if user.role not in service.TEAM_ROLES and owner_id != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the team can reassign prospects")
        await _validate_owner(db, owner_id)
        prospect.owner_user_id = owner_id
        existing_assignment = (
            await db.execute(
                select(DealerRepContactAssignment).where(
                    DealerRepContactAssignment.contact_id == contact.id,
                    DealerRepContactAssignment.user_id == owner_id,
                )
            )
        ).scalar_one_or_none()
        if existing_assignment is None and contact.owner_user_id != owner_id:
            db.add(
                DealerRepContactAssignment(
                    contact_id=contact.id,
                    user_id=owner_id,
                    assigned_by_user_id=user.id,
                )
            )
        changed_fields.append("owner_user_id")
    if "contact_name" in changes:
        contact.full_name = changes["contact_name"]
        changed_fields.append("contact_name")
    if "dealer_name" in changes:
        company.name = changes["dealer_name"]
        contact.company = changes["dealer_name"]
        prospect.dealer_name_normalized = service.normalize_dealer_name(changes["dealer_name"])
        changed_fields.append("dealer_name")
    if "email" in changes:
        prospect.email_normalized = service.normalize_email(str(changes["email"]))
        contact.email = prospect.email_normalized
        changed_fields.append("email")
    if "phone" in changes:
        prospect.phone_normalized = changes["phone"]
        contact.phone_e164 = changes["phone"]
        changed_fields.append("phone")
    if "next_follow_up_at" in changes:
        prospect.next_follow_up_at = changes["next_follow_up_at"]
        changed_fields.append("next_follow_up_at")

    if not changed_fields:
        return ProspectRead.model_validate(
            await service.prospect_read(db, prospect, include_activities=True)
        )
    duplicates = await service.find_duplicates(
        db,
        dealer_name_normalized=prospect.dealer_name_normalized,
        email_normalized=prospect.email_normalized,
        phone_normalized=prospect.phone_normalized,
    )
    conflicts = [row for row in duplicates if row.id != prospect.id]
    if conflicts:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=service.duplicate_detail(conflicts, user)
        )
    before = prospect.version
    prospect.version += 1
    await service.add_activity(
        db,
        prospect,
        user,
        "prospect_updated",
        metadata={
            "changed_fields": changed_fields,
            "version_before": before,
            "version_after": prospect.version,
        },
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "A matching prospect already exists") from exc
    await _refresh_for_read(db, prospect)
    return ProspectRead.model_validate(
        await service.prospect_read(db, prospect, include_activities=True)
    )


@router.post("/prospects/{prospect_id}/move-stage", response_model=ProspectMoveResult)
async def move_prospect_stage(
    prospect_id: UUID,
    payload: ProspectMoveStage,
    user: CurrentUser,
    db: DbSession,
) -> ProspectMoveResult:
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    prospect = await service.move_stage(
        db,
        user,
        prospect,
        stage_key=payload.stage_key,
        expected_version=payload.expected_version,
        note=payload.note,
        next_follow_up_at=payload.next_follow_up_at,
        action=payload.action or "none",
        appointment_id=payload.appointment_id,
        confirm_do_not_contact=payload.confirm_do_not_contact,
    )
    email_draft = None
    if payload.action == "draft_email":
        email_draft = await _create_action_draft(
            db,
            prospect=prospect,
            user=user,
            action="dealer_information_pack",
        )
        await service.attach_draft_to_transition(
            db,
            prospect,
            event_kind="stage_moved",
            draft_id=email_draft.id,
        )
    await _refresh_for_read(db, prospect)
    prospect_read = ProspectRead.model_validate(
        await service.prospect_read(db, prospect, include_activities=True)
    )
    return ProspectMoveResult(
        **prospect_read.model_dump(),
        prospect=prospect_read,
        email_draft_id=email_draft.id if email_draft else None,
    )


@router.post(
    "/prospects/{prospect_id}/activities",
    response_model=ProspectActivityRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_prospect_activity(
    prospect_id: UUID,
    payload: ProspectActivityCreate,
    user: CurrentUser,
    db: DbSession,
) -> ProspectActivityRead:
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    row = await service.add_activity(db, prospect, user, payload.kind, body=payload.body)
    return ProspectActivityRead.model_validate(
        await service.activity_read(row, actor_name=user.name)
    )


@router.post(
    "/prospects/{prospect_id}/activities/{activity_id}/undo",
    response_model=ProspectRead,
)
async def undo_prospect_activity(
    prospect_id: UUID,
    activity_id: UUID,
    payload: ProspectUndoRequest,
    user: CurrentUser,
    db: DbSession,
) -> ProspectRead:
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    prospect = await service.undo_activity(
        db,
        user,
        prospect,
        activity_id,
        expected_version=payload.expected_version,
    )
    await _refresh_for_read(db, prospect)
    return ProspectRead.model_validate(
        await service.prospect_read(db, prospect, include_activities=True)
    )


@router.post("/prospects/{prospect_id}/outcomes", response_model=ProspectOutcomeResult)
async def apply_prospect_outcome(
    prospect_id: UUID,
    payload: ProspectOutcomeApply,
    user: CurrentUser,
    db: DbSession,
) -> ProspectOutcomeResult:
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    outcome = (
        await db.execute(
            select(DealerProspectOutcomeDefinition).where(
                DealerProspectOutcomeDefinition.key == payload.outcome_key,
                DealerProspectOutcomeDefinition.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if outcome is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Call outcome is unavailable")
    prospect, email_action, workflow_action = await service.apply_outcome(
        db,
        user,
        prospect,
        outcome=outcome,
        expected_version=payload.expected_version,
        note=payload.note,
        next_follow_up_at=payload.next_follow_up_at,
        appointment_id=payload.appointment_id,
    )
    email_draft = None
    if email_action:
        email_draft = await _create_action_draft(
            db,
            prospect=prospect,
            user=user,
            action=email_action,
        )
        await service.attach_draft_to_transition(
            db,
            prospect,
            event_kind="outcome_applied",
            draft_id=email_draft.id,
        )
    await _refresh_for_read(db, prospect)
    return ProspectOutcomeResult(
        prospect=ProspectRead.model_validate(
            await service.prospect_read(db, prospect, include_activities=True)
        ),
        outcome=_outcome_read(outcome),
        email_action=email_action,
        workflow_action=workflow_action,
        email_draft_id=email_draft.id if email_draft else None,
    )


def _candidate_read(row: PublicUnderwritingIntake, *, prospect: DealerProspect) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "outcome_status": row.outcome_status,
        "full_name": row.full_name,
        "business_name": row.business_name,
        "email": row.email,
        "phone": row.phone,
        "created_at": row.created_at,
        "match_reasons": service.intake_candidate_match_reasons(prospect, row),
    }


@router.post(
    "/prospects/{prospect_id}/convert-to-ai-intake",
    response_model=ProspectConversionResult,
)
async def convert_prospect_to_ai_intake(
    prospect_id: UUID,
    payload: ProspectConversionRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> ProspectConversionResult:
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    service.assert_expected_version(prospect, payload.expected_version)
    if prospect.converted_intake_id is not None:
        await _refresh_for_read(db, prospect)
        return ProspectConversionResult(
            status="already_converted",
            prospect=ProspectRead.model_validate(await service.prospect_read(db, prospect)),
            intake_id=prospect.converted_intake_id,
            route=f"/admin/ai-underwriter-leads?lead={prospect.converted_intake_id}",
        )

    candidates = await service.intake_candidates(db, prospect, user)
    if payload.action == "detect":
        if candidates:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "prospect_conversion_choice_required",
                    "candidates": [
                        {
                            "id": str(row.id),
                            "full_name": row.full_name,
                            "business_name": row.business_name,
                            "status": row.status,
                            "outcome_status": row.outcome_status,
                            "created_at": row.created_at.isoformat(),
                            "match_reasons": service.intake_candidate_match_reasons(prospect, row),
                        }
                        for row in candidates
                    ],
                    "allowed_actions": ["link", "reactivate", "create"],
                },
            )
        # No match means there is no decision to ask the agent to make. The
        # default detect request creates exactly one new dealer intake.
        intake = await service.create_intake_from_prospect(db, request, prospect, user)
        result_status = "created"

    elif payload.action in {"link", "reactivate"}:
        intake = next((row for row in candidates if row.id == payload.intake_id), None)
        if intake is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Selected AI intake does not match this dealer contact.",
            )
        result_status = "linked"
        if payload.action == "reactivate":
            intake.status = "collecting"
            intake.outcome_status = "submitted"
            intake.delete_requested_at = None
            intake.delete_requested_by_user_id = None
            intake.client_contact_suppressed = False
            result_status = "reactivated"
    else:
        # ``create`` is an explicit user choice.  It never happens implicitly
        # when candidates exist, which prevents the historical force_new path
        # from silently duplicating a dealer file.
        intake = await service.create_intake_from_prospect(db, request, prospect, user)
        result_status = "created"

    await service.complete_conversion(
        db,
        user,
        prospect,
        intake,
        event_kind=f"ai_intake_{result_status}",
        note=payload.note,
    )
    await _refresh_for_read(db, prospect)
    return ProspectConversionResult(
        status=result_status,
        prospect=ProspectRead.model_validate(
            await service.prospect_read(db, prospect, include_activities=True)
        ),
        intake_id=intake.id,
        route=f"/admin/ai-underwriter-leads?lead={intake.id}",
    )


@router.get("/prospect-stages", response_model=list[ProspectStageRead])
async def list_prospect_stages(
    user: CurrentUser,
    db: DbSession,
    include_inactive: bool = False,
) -> list[ProspectStageRead]:
    service.require_prospect_actor(user)
    if include_inactive:
        service.require_config_admin(user)
    return [
        _stage_read(row)
        for row in await service.active_stages(db, include_inactive=include_inactive)
    ]


@router.post(
    "/prospect-stages", response_model=ProspectStageRead, status_code=status.HTTP_201_CREATED
)
async def create_prospect_stage(
    payload: ProspectStageCreate,
    user: CurrentUser,
    db: DbSession,
) -> ProspectStageRead:
    service.require_config_admin(user)
    await service.ensure_default_definitions(db)
    key = service.definition_key(payload.key or payload.label)
    if (
        await db.execute(
            select(DealerProspectStageDefinition.id).where(DealerProspectStageDefinition.key == key)
        )
    ).scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A pipeline stage with this key already exists"
        )
    maximum = int(
        (await db.execute(select(func.max(DealerProspectStageDefinition.sort_order)))).scalar_one()
        or 0
    )
    row = DealerProspectStageDefinition(
        key=key,
        label=payload.label.strip(),
        sort_order=maximum + 10,
        is_active=True,
        is_terminal=payload.is_terminal,
        is_system=False,
        behavior=payload.behavior,
    )
    db.add(row)
    await db.flush()
    return _stage_read(row)


@router.patch("/prospect-stages/{stage_id}", response_model=ProspectStageRead)
async def patch_prospect_stage(
    stage_id: UUID,
    payload: ProspectStagePatch,
    user: CurrentUser,
    db: DbSession,
) -> ProspectStageRead:
    service.require_config_admin(user)
    row = await db.get(DealerProspectStageDefinition, stage_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline stage not found")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("is_active") is False and row.is_active:
        if row.key == "new":
            raise HTTPException(status.HTTP_409_CONFLICT, "The New stage cannot be retired")
        assigned = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(DealerProspect)
                    .where(
                        DealerProspect.stage_definition_id == row.id,
                        DealerProspect.archived_at.is_(None),
                    )
                )
            ).scalar_one()
        )
        if assigned:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "stage_in_use", "prospect_count": assigned},
            )
        outcomes = list(
            (
                await db.execute(
                    select(DealerProspectOutcomeDefinition).where(
                        DealerProspectOutcomeDefinition.is_active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        if any((item.action_config or {}).get("target_stage_key") == row.key for item in outcomes):
            raise HTTPException(status.HTTP_409_CONFLICT, "An active call outcome uses this stage")
    for field, value in changes.items():
        setattr(row, field, value.strip() if field == "label" and value else value)
    await db.flush()
    return _stage_read(row)


@router.post("/prospect-stages/reorder", response_model=list[ProspectStageRead])
async def reorder_prospect_stages(
    payload: ProspectDefinitionReorder,
    user: CurrentUser,
    db: DbSession,
) -> list[ProspectStageRead]:
    service.require_config_admin(user)
    rows = await service.active_stages(db)
    if set(payload.ordered_ids) != {row.id for row in rows}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Reorder must include every active stage exactly once.",
        )
    by_id = {row.id: row for row in rows}
    for index, row_id in enumerate(payload.ordered_ids):
        by_id[row_id].sort_order = index * 10
    await db.flush()
    return [_stage_read(by_id[row_id]) for row_id in payload.ordered_ids]


@router.get("/prospect-outcomes", response_model=list[ProspectOutcomeRead])
async def list_prospect_outcomes(
    user: CurrentUser,
    db: DbSession,
    include_inactive: bool = False,
) -> list[ProspectOutcomeRead]:
    service.require_prospect_actor(user)
    if include_inactive:
        service.require_config_admin(user)
    return [
        _outcome_read(row)
        for row in await service.active_outcomes(db, include_inactive=include_inactive)
    ]


@router.post(
    "/prospect-outcomes", response_model=ProspectOutcomeRead, status_code=status.HTTP_201_CREATED
)
async def create_prospect_outcome(
    payload: ProspectOutcomeCreate,
    user: CurrentUser,
    db: DbSession,
) -> ProspectOutcomeRead:
    service.require_config_admin(user)
    await service.ensure_default_definitions(db)
    key = service.definition_key(payload.key or payload.label)
    if (
        await db.execute(
            select(DealerProspectOutcomeDefinition.id).where(
                DealerProspectOutcomeDefinition.key == key
            )
        )
    ).scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A call outcome with this key already exists")
    config = await _validate_outcome_target(db, payload.action_config)
    maximum = int(
        (
            await db.execute(select(func.max(DealerProspectOutcomeDefinition.sort_order)))
        ).scalar_one()
        or 0
    )
    row = DealerProspectOutcomeDefinition(
        key=key,
        label=payload.label.strip(),
        sort_order=maximum + 10,
        is_active=True,
        is_system=False,
        action_config=config,
    )
    db.add(row)
    await db.flush()
    return _outcome_read(row)


@router.patch("/prospect-outcomes/{outcome_id}", response_model=ProspectOutcomeRead)
async def patch_prospect_outcome(
    outcome_id: UUID,
    payload: ProspectOutcomePatch,
    user: CurrentUser,
    db: DbSession,
) -> ProspectOutcomeRead:
    service.require_config_admin(user)
    row = await db.get(DealerProspectOutcomeDefinition, outcome_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call outcome not found")
    changes = payload.model_dump(exclude_unset=True)
    if "action_config" in changes:
        changes["action_config"] = await _validate_outcome_target(db, changes["action_config"])
    for field, value in changes.items():
        setattr(row, field, value.strip() if field == "label" and value else value)
    await db.flush()
    return _outcome_read(row)


@router.post("/prospect-outcomes/reorder", response_model=list[ProspectOutcomeRead])
async def reorder_prospect_outcomes(
    payload: ProspectDefinitionReorder,
    user: CurrentUser,
    db: DbSession,
) -> list[ProspectOutcomeRead]:
    service.require_config_admin(user)
    rows = await service.active_outcomes(db)
    if set(payload.ordered_ids) != {row.id for row in rows}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Reorder must include every active outcome exactly once.",
        )
    by_id = {row.id: row for row in rows}
    for index, row_id in enumerate(payload.ordered_ids):
        by_id[row_id].sort_order = index * 10
    await db.flush()
    return [_outcome_read(by_id[row_id]) for row_id in payload.ordered_ids]
