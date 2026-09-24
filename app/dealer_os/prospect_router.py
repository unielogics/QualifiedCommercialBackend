"""Field Desk dealer prospect pipeline API."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from pydantic import EmailStr
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db import get_db
from app.deps import CurrentUser
from app.enums import CalendarEventKind, CalendarEventSource, CalendarEventStatus, Role
from app.models.dealer_prospect import (
    DealerProspect,
    DealerProspectActivity,
    DealerProspectOutcomeDefinition,
    DealerProspectStageDefinition,
)
from app.models.event import CalendarEvent
from app.models.notification import Notification
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.prospect_outreach import ProspectEmailDraftCreate
from app.services import booking_operations, booking_reminders
from app.services import prospect_outreach as outreach_service
from app.services.notifications import notify_users
from app.services.user_access import record_access_event, request_metadata

from .models import (
    DealerBusiness,
    DealerRepAppointment,
    DealerRepCompany,
    DealerRepContact,
    DealerRepContactAssignment,
)
from .prospect_schemas import (
    ProspectActivityCreate,
    ProspectActivityRead,
    ProspectAppointmentCreate,
    ProspectAppointmentDeliveryRead,
    ProspectAppointmentResult,
    ProspectCallAttemptCreate,
    ProspectConversionCandidate,
    ProspectConversionCandidateList,
    ProspectConversionRequest,
    ProspectConversionResult,
    ProspectCreate,
    ProspectDefinitionReorder,
    ProspectDuplicateCheckRead,
    ProspectDuplicateMatchRead,
    ProspectFollowUpSuggestionRead,
    ProspectGeneralConversionRequest,
    ProspectGeneralConversionResult,
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
    ProspectReassignmentRequestCreate,
    ProspectReassignmentRequestRead,
    ProspectStageCreate,
    ProspectStagePatch,
    ProspectStageRead,
    ProspectTimelineRead,
    ProspectUndoRequest,
    ProspectUserAccessList,
    ProspectUserAccessPatch,
    ProspectUserAccessRead,
)
from .schemas import RepAppointmentRead
from .services import prospect_conversion as conversion_service
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


def _prospect_search_filters(query: str, owner: Any) -> list[Any]:
    """Return AND-able term predicates across live and snapshot identity fields."""

    filters: list[Any] = []
    for term in query.casefold().split():
        like = f"%{term}%"
        filters.append(
            or_(
                func.lower(DealerRepContact.full_name).like(like),
                func.lower(DealerRepCompany.name).like(like),
                func.lower(func.coalesce(DealerRepContact.email, "")).like(like),
                func.lower(func.coalesce(DealerRepContact.phone_e164, "")).like(like),
                func.lower(DealerProspect.email_normalized).like(like),
                func.lower(DealerProspect.phone_normalized).like(like),
                func.lower(func.coalesce(owner.name, "")).like(like),
                func.lower(func.coalesce(owner.email, "")).like(like),
            )
        )
    return filters


def _can_restore_prospect_match(user: User, row: DealerProspect) -> bool:
    return bool(
        getattr(row, "archived_at", None) is not None
        and getattr(user, "dealer_prospect_pipeline_enabled", False)
    )


def _can_restore_contact_match(user: User, row: DealerRepContact) -> bool:
    return bool(
        getattr(row, "archived_at", None) is not None
        and (user.role in service.TEAM_ROLES or row.owner_user_id == user.id)
    )


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
        requires_follow_up=bool(
            config.get("requires_follow_up")
            or config.get("follow_up_delay_hours")
        ),
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
    server_now = service.now_utc()
    follow_up_timezone = await service.firm_booking_timezone(db)
    owner = aliased(User, name="prospect_owner")
    last_outcome = aliased(DealerProspectOutcomeDefinition, name="prospect_last_outcome")
    filters: list[Any] = [
        DealerProspect.archived_at.is_(None),
        DealerRepContact.archived_at.is_(None),
        service.prospect_access_filter(user),
    ]
    if q.strip():
        # Match every pasted term across canonical contact data and the
        # prospect snapshots. This supports combined searches such as
        # ``Rocio rocio@dealer.com`` and legacy prospects whose snapshot was
        # not refreshed after the contact changed.
        filters.extend(_prospect_search_filters(q, owner))
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
                server_now=server_now,
                follow_up_timezone=follow_up_timezone,
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
    items = [
        ProspectRead.model_validate(item)
        for item in await service.prospects_read(db, rows, reference_time=server_now)
    ]
    stages = [_stage_read(row) for row in await service.active_stages(db)]
    outcomes = [_outcome_read(row) for row in await service.active_outcomes(db)]
    return ProspectListRead(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        stages=stages,
        outcomes=outcomes,
        server_now=server_now,
        follow_up_timezone=follow_up_timezone,
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
            include_archived=True,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=service.duplicate_detail(
                duplicates,
                user,
                known_contact_id=payload.contact_id,
                email_normalized=service.normalize_email(str(payload.email)),
                phone_normalized=payload.phone,
            ),
        ) from exc
    await _refresh_for_read(db, prospect)
    return ProspectRead.model_validate(
        await service.prospect_read(db, prospect, include_activities=True)
    )


@router.get("/prospects/duplicate-check", response_model=ProspectDuplicateCheckRead)
async def check_prospect_duplicate(
    user: CurrentUser,
    db: DbSession,
    email: Annotated[EmailStr | None, Query()] = None,
    phone: Annotated[str | None, Query(max_length=48)] = None,
    contact_id: UUID | None = None,
) -> ProspectDuplicateCheckRead:
    """Privacy-preserving, advisory identity check for quick-add forms.

    Creation repeats the same check under transaction-scoped advisory locks;
    this endpoint improves UX but is never trusted for integrity.
    """

    # Reassignment is the safe exit for a blocked generic contact workflow;
    # it must remain available even when this user's Marketing package is off.
    service.require_team_or_rep(user)
    normalized_email = service.normalize_email(str(email)) if email else None
    normalized_phone = service.normalize_phone(phone) if phone and phone.strip() else None
    if not normalized_email and not normalized_phone and contact_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "identity_required",
                "message": "Enter an email address or phone number to check for duplicates.",
            },
        )
    if phone and phone.strip() and normalized_phone is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_phone", "message": "Enter a valid phone number."},
        )
    if contact_id is not None:
        await service.load_visible_contact(db, user, contact_id)
    rows = await service.find_duplicates(
        db,
        email_normalized=normalized_email or "",
        phone_normalized=normalized_phone or "",
        primary_contact_id=contact_id,
        include_archived=True,
    )
    prospect_contact_ids = {row.primary_contact_id for row in rows}
    contact_rows = [
        row
        for row in await service.find_contact_identity_matches(
            db,
            email_normalized=normalized_email or "",
            phone_normalized=normalized_phone or "",
            exclude_contact_id=contact_id,
        )
        if row.id not in prospect_contact_ids
    ]
    if not rows and not contact_rows:
        return ProspectDuplicateCheckRead(
            blocked=False,
            state="clear",
            email_normalized=normalized_email,
            phone_normalized=normalized_phone,
            message="No matching Marketing prospect was found.",
        )

    row_ids = {row.id for row in rows}
    visible_ids = set(
        (
            await db.execute(
                select(DealerProspect.id).where(
                    DealerProspect.id.in_(row_ids), service.prospect_access_filter(user)
                )
            )
        )
        .scalars()
        .all()
    )
    visible = [
        row
        for row in rows
        if row.id in visible_ids or (contact_id and row.primary_contact_id == contact_id)
    ]
    visible_contact_id_set = await service.visible_contact_ids(
        db, user, {row.id for row in contact_rows}
    )
    visible_contacts = [row for row in contact_rows if row.id in visible_contact_id_set]
    email_identity_keys = {
        f"prospect:{row.id}"
        for row in rows
        if normalized_email and row.email_normalized == normalized_email
    } | {
        f"contact:{row.id}"
        for row in contact_rows
        if normalized_email and service.normalize_email(row.email or "") == normalized_email
    }
    phone_identity_keys = {
        f"prospect:{row.id}"
        for row in rows
        if normalized_phone and row.phone_normalized == normalized_phone
    } | {
        f"contact:{row.id}"
        for row in contact_rows
        if normalized_phone and row.phone_e164 == normalized_phone
    }
    split_identity = bool(
        email_identity_keys
        and phone_identity_keys
        and email_identity_keys.isdisjoint(phone_identity_keys)
    )
    actual_state = (
        "identity_conflict"
        if split_identity
        else service.duplicate_state(
            rows,
            email_normalized=normalized_email,
            phone_normalized=normalized_phone,
        )
        if rows
        else "archived_match"
        if contact_rows and all(
            getattr(row, "archived_at", None) is not None for row in contact_rows
        )
        else "active_match"
    )
    any_visible = bool(visible or visible_contacts)
    state = actual_state if any_visible or user.role in service.TEAM_ROLES else "hidden_match"
    active_visible = any(row.archived_at is None for row in visible) or any(
        getattr(row, "archived_at", None) is None for row in visible_contacts
    )
    return ProspectDuplicateCheckRead(
        blocked=True,
        state=state,
        email_normalized=normalized_email,
        phone_normalized=normalized_phone,
        visible_matches=[
            ProspectDuplicateMatchRead(
                prospect_id=row.id,
                contact_id=row.primary_contact_id,
                owner_user_id=row.owner_user_id,
                archived=getattr(row, "archived_at", None) is not None,
                can_restore=_can_restore_prospect_match(user, row),
                version=row.version,
                matched_on=service.identity_match_reasons(
                    row,
                    email_normalized=normalized_email,
                    phone_normalized=normalized_phone,
                ),
            )
            for row in visible
        ]
        + [
            ProspectDuplicateMatchRead(
                entity_type="contact",
                prospect_id=None,
                contact_id=row.id,
                owner_user_id=row.owner_user_id,
                archived=getattr(row, "archived_at", None) is not None,
                can_restore=_can_restore_contact_match(user, row),
                version=None,
                matched_on=service.contact_identity_match_reasons(
                    row,
                    email_normalized=normalized_email,
                    phone_normalized=normalized_phone,
                ),
            )
            for row in visible_contacts
        ],
        assignment_required=not any_visible,
        can_restore=(
            not active_visible
            and (
                any(
                    _can_restore_prospect_match(user, row) for row in visible
                )
                or any(
                    _can_restore_contact_match(user, row) for row in visible_contacts
                )
            )
        ),
        message=(
            "The email and phone belong to different Marketing prospects. Correct the identity "
            "or ask a Super Admin to review it."
            if state == "identity_conflict"
            else "A matching archived Marketing contact can be restored."
            if state == "archived_match" and (visible or visible_contacts)
            else "A matching prospect exists. Request reassignment from an administrator."
            if state == "hidden_match"
            else "A contact already uses this email address or phone number. Open that contact "
            "to add it to Marketing."
            if visible_contacts and not visible
            else "A Marketing prospect already uses this email address or phone number."
        ),
    )


@router.post(
    "/prospects/reassignment-requests",
    response_model=ProspectReassignmentRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_prospect_reassignment(
    payload: ProspectReassignmentRequestCreate,
    user: CurrentUser,
    db: DbSession,
) -> ProspectReassignmentRequestRead:
    """Request access to an identity match without exposing its owner or data."""

    # A disabled Marketing package must stop prospect creation and outreach,
    # but it must not strand an employee who encounters an existing identity
    # from another contact-producing workflow.  Reassignment is the
    # privacy-preserving recovery path and does not expose or mutate the
    # matched contact.
    service.require_team_or_rep(user)
    normalized_email = service.normalize_email(str(payload.email)) if payload.email else None
    normalized_phone = service.normalize_phone(payload.phone) if payload.phone else None
    if payload.phone and payload.phone.strip() and normalized_phone is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_phone", "message": "Enter a valid phone number."},
        )
    identity_hash = hashlib.sha256(
        f"{normalized_email or ''}\x1f{normalized_phone or ''}".encode()
    ).hexdigest()
    request_token = hashlib.sha256(
        f"{user.id}\x1f{payload.idempotency_key}".encode()
    ).hexdigest()[:32]
    batch_key = f"marketing-reassignment:{user.id}:{request_token}"
    existing = (
        await db.execute(
            select(Notification)
            .where(
                Notification.event_type == "marketing.contact_reassignment_requested",
                Notification.batch_key == batch_key,
            )
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (existing.meta or {}).get("identity_hash") != identity_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "idempotency_key_reused",
                    "message": "Use a new request key for a different contact identity.",
                },
            )
        return ProspectReassignmentRequestRead(request_token=request_token)

    prospect_rows = await service.find_duplicates(
        db,
        email_normalized=normalized_email or "",
        phone_normalized=normalized_phone or "",
        include_archived=True,
    )
    prospect_contact_ids = {row.primary_contact_id for row in prospect_rows}
    contact_rows = await service.find_contact_identity_matches(
        db,
        email_normalized=normalized_email or "",
        phone_normalized=normalized_phone or "",
    )
    all_contact_ids = prospect_contact_ids | {row.id for row in contact_rows}
    visible_prospect_ids: set[UUID] = set()
    if prospect_rows:
        visible_prospect_ids = set(
            (
                await db.execute(
                    select(DealerProspect.id).where(
                        DealerProspect.id.in_({row.id for row in prospect_rows}),
                        service.prospect_access_filter(user),
                    )
                )
            )
            .scalars()
            .all()
        )
    visible_contact_ids = await service.visible_contact_ids(db, user, all_contact_ids)
    visible_contact_ids |= {
        row.primary_contact_id for row in prospect_rows if row.id in visible_prospect_ids
    }
    hidden_contact_ids = all_contact_ids - visible_contact_ids
    if not hidden_contact_ids:
        raise HTTPException(
            status.HTTP_409_CONFLICT if all_contact_ids else status.HTTP_404_NOT_FOUND,
            detail={
                "code": "reassignment_not_required" if all_contact_ids else "identity_not_found",
                "message": (
                    "This contact is already available to you. Open the existing record."
                    if all_contact_ids
                    else "No existing contact requires reassignment."
                ),
            },
        )

    admin_ids = set(
        (
            await db.execute(
                select(User.id).where(
                    User.role == Role.SUPER_ADMIN,
                    User.deleted_at.is_(None),
                    or_(User.account_status.is_(None), User.account_status == "active"),
                )
            )
        )
        .scalars()
        .all()
    )
    if not admin_ids:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "reassignment_unavailable",
                "message": "No active Super Admin is available to review this request.",
            },
        )
    target_contact_id = sorted(hidden_contact_ids, key=str)[0]
    await notify_users(
        db,
        recipient_ids=admin_ids,
        event_type="marketing.contact_reassignment_requested",
        category="marketing",
        priority="high",
        title="Marketing contact reassignment requested",
        body=f"{user.name} requested access to an existing Marketing contact.",
        target_type="dealer_contact_reassignment",
        target_id=str(target_contact_id),
        deep_link=f"/marketing/{target_contact_id}",
        batch_key=batch_key,
        push=True,
        actor_user_id=user.id,
        meta={
            "request_token": request_token,
            "requester_user_id": str(user.id),
            "requester_name": user.name,
            "identity_hash": identity_hash,
            "matched_contact_ids": [str(value) for value in sorted(hidden_contact_ids, key=str)],
            "reason": payload.reason,
        },
    )
    return ProspectReassignmentRequestRead(request_token=request_token)


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


@router.post("/prospects/{prospect_id}/restore", response_model=ProspectRead)
async def restore_prospect(
    prospect_id: UUID,
    payload: ProspectUndoRequest,
    user: CurrentUser,
    db: DbSession,
) -> ProspectRead:
    service.require_prospect_actor(user)
    # Take identity locks before the row lock.  Creation follows the same
    # order, so a concurrent restore and create cannot deadlock while each is
    # waiting on the other's lock.
    prospect = await service.load_visible_prospect_history(db, user, prospect_id)
    service.assert_expected_version(prospect, payload.expected_version)
    if prospect.archived_at is None:
        return ProspectRead.model_validate(
            await service.prospect_read(db, prospect, include_activities=True)
        )
    await service.resolve_contact_identity(
        db,
        actor_user=user,
        owner_user_id=prospect.owner_user_id or user.id,
        email=prospect.email_normalized,
        phone=prospect.phone_normalized,
        exclude_contact_id=prospect.primary_contact_id,
    )
    prospect = await service.load_visible_prospect_history(
        db, user, prospect_id, for_update=True
    )
    service.assert_expected_version(prospect, payload.expected_version)
    if prospect.archived_at is None:
        return ProspectRead.model_validate(
            await service.prospect_read(db, prospect, include_activities=True)
        )
    active_matches = await service.find_duplicates(
        db,
        email_normalized=prospect.email_normalized,
        phone_normalized=prospect.phone_normalized,
        primary_contact_id=prospect.primary_contact_id,
        for_update=True,
    )
    active_matches = [row for row in active_matches if row.id != prospect.id]
    if active_matches:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=service.duplicate_detail(
                active_matches,
                user,
                email_normalized=prospect.email_normalized,
                phone_normalized=prospect.phone_normalized,
            ),
        )
    version_before = prospect.version
    prospect.archived_at = None
    prospect.archived_by_user_id = None
    contact = await db.get(DealerRepContact, prospect.primary_contact_id, with_for_update=True)
    if contact is not None:
        contact.archived_at = None
        contact.restored_at = service.now_utc()
        contact.restored_by_user_id = user.id
    prospect.version += 1
    await service.add_activity(
        db,
        prospect,
        user,
        "prospect_restored",
        metadata={
            "version_before": version_before,
            "version_after": prospect.version,
        },
    )
    await _refresh_for_read(db, prospect)
    return ProspectRead.model_validate(
        await service.prospect_read(db, prospect, include_activities=True)
    )


def _prospect_booking_delivery(appointment: dict[str, Any]) -> ProspectAppointmentDeliveryRead:
    google = str(appointment.get("google_sync_status") or "pending")
    email = str(appointment.get("confirmation_email_status") or "disabled")
    sms = str(appointment.get("confirmation_sms_status") or "disabled")
    error = appointment.get("google_sync_error") or appointment.get("delivery_error")
    meeting_mode = appointment.get("meeting_mode") or "video"
    meet_url = appointment.get("join_url")
    if (
        email == "failed"
        or sms == "failed"
        or google == "action_required"
        or error == "google_calendar_action_required"
        or (meeting_mode == "video" and google == "unavailable")
    ):
        state = "action_required"
    elif meeting_mode == "video" and meet_url:
        state = "meet_ready"
    else:
        state = "queued"
    return ProspectAppointmentDeliveryRead(
        state=state,
        google_sync_status=google,
        email_status=email,
        sms_status=sms,
        meet_url=meet_url,
        error=error,
    )


def _prospect_booking_replay_fingerprint(
    *,
    prospect_id: UUID,
    contact_id: UUID,
    owner_user_id: UUID,
    actor_user_id: UUID,
    invitee_email: str | None,
    invitee_phone: str | None,
    starts_at: datetime,
    duration_min: int,
    meeting_mode: str,
    location: str | None,
    notes: str | None,
    transactional_sms_consent: bool,
    trigger_outcome_key: str | None,
) -> str:
    aware_start = (
        starts_at if starts_at.tzinfo is not None else starts_at.replace(tzinfo=UTC)
    )
    state = {
        "prospect_id": str(prospect_id),
        "contact_id": str(contact_id),
        "owner_user_id": str(owner_user_id),
        "actor_user_id": str(actor_user_id),
        "invitee_email": (invitee_email or "").strip().lower() or None,
        "invitee_phone": service.normalize_phone(invitee_phone),
        "starts_at": aware_start.astimezone(UTC)
        .replace(second=0, microsecond=0)
        .isoformat(),
        "duration_min": int(duration_min),
        "meeting_mode": meeting_mode,
        "location": (location or "").strip() or None,
        "notes": (notes or "").strip() or None,
        "transactional_sms_consent": bool(transactional_sms_consent),
        "trigger_outcome_key": trigger_outcome_key,
    }
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _prospect_appointment_result(
    db: AsyncSession,
    *,
    prospect: DealerProspect,
    appointment: DealerRepAppointment,
    idempotent_replay: bool,
) -> ProspectAppointmentResult:
    # Importing here avoids making the general Dealer OS router depend on the
    # prospect router during application startup.
    from .router import _appointment_read_rows

    await _refresh_for_read(db, prospect)
    await db.refresh(appointment)
    appointment_read = (await _appointment_read_rows(db, [appointment]))[0]
    prospect_read = ProspectRead.model_validate(
        await service.prospect_read(db, prospect, include_activities=True)
    )
    return ProspectAppointmentResult(
        appointment=appointment_read,
        prospect=prospect_read,
        delivery=_prospect_booking_delivery(appointment_read),
        idempotent_replay=idempotent_replay,
    )


@router.get(
    "/prospects/{prospect_id}/appointments",
    response_model=list[RepAppointmentRead],
)
async def list_prospect_appointments(
    prospect_id: UUID,
    user: CurrentUser,
    db: DbSession,
    include_cancelled: bool = Query(default=True),
) -> list[dict[str, Any]]:
    from .router import _appointment_read_rows

    prospect = await service.load_visible_prospect(db, user, prospect_id)
    query = select(DealerRepAppointment).where(
        DealerRepAppointment.prospect_id == prospect.id
    )
    if not include_cancelled:
        query = query.where(
            DealerRepAppointment.archived_at.is_(None),
            DealerRepAppointment.status != "cancelled",
        )
    appointments = list(
        (
            await db.execute(
                query.order_by(
                    DealerRepAppointment.starts_at.desc(),
                    DealerRepAppointment.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return await _appointment_read_rows(db, appointments)


@router.post(
    "/prospects/{prospect_id}/appointments",
    response_model=ProspectAppointmentResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_prospect_appointment(
    prospect_id: UUID,
    payload: ProspectAppointmentCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    user: CurrentUser,
    db: DbSession,
) -> ProspectAppointmentResult:
    from app.services.team_calendar import lock_calendar_owner

    from .router import (
        _appointment_slot_is_available,
        _appointment_title,
        _booking_settings_for,
        _record_appointment_activity,
        _rep_host_for,
        _to_utc_minute,
    )

    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    existing = (
        await db.execute(
            select(DealerRepAppointment).where(
                DealerRepAppointment.creation_idempotency_key == payload.idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        replay_contact = await db.get(
            DealerRepContact, prospect.primary_contact_id
        )
        replay_host = await _rep_host_for(db, None, user)
        replay_booking = await _booking_settings_for(db, replay_host)
        replay_starts_at = _to_utc_minute(payload.starts_at)
        replay_duration = (
            payload.duration_min or replay_booking.duration_min or 20
        )
        replay_fingerprint = (
            _prospect_booking_replay_fingerprint(
                prospect_id=prospect.id,
                contact_id=prospect.primary_contact_id,
                owner_user_id=replay_host.id,
                actor_user_id=user.id,
                invitee_email=(
                    replay_contact.email or prospect.email_normalized
                    if replay_contact is not None
                    else prospect.email_normalized
                ),
                invitee_phone=(
                    replay_contact.phone_e164 or prospect.phone_normalized
                    if replay_contact is not None
                    else prospect.phone_normalized
                ),
                starts_at=replay_starts_at,
                duration_min=replay_duration,
                meeting_mode=payload.meeting_mode,
                location=payload.location,
                notes=payload.notes,
                transactional_sms_consent=payload.transactional_sms_consent,
                trigger_outcome_key=payload.trigger_outcome_key,
            )
            if replay_contact is not None
            else None
        )
        stored_fingerprint = (existing.precall_application_data or {}).get(
            "prospect_booking_replay_fingerprint"
        )
        if (
            existing.prospect_id != prospect.id
            or existing.contact_id != prospect.primary_contact_id
            or existing.owner_user_id != replay_host.id
            or existing.booked_by_user_id != user.id
            or not stored_fingerprint
            or stored_fingerprint != replay_fingerprint
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "idempotency_key_reused",
                    "message": "Use a new booking request identifier.",
                },
            )
        booking_operations.record_idempotency_replay(
            operation_type="create",
            appointment_id=existing.id,
            surface="prospect_appointment",
        )
        operation = await booking_operations.find_by_idempotency_key(
            db, f"booking:create:{existing.id}"
        )
        if operation is None and existing.calendar_event_id is not None:
            event = await db.get(CalendarEvent, existing.calendar_event_id)
            if event is not None:
                operation = await booking_operations.enqueue(
                    db,
                    appointment=existing,
                    event=event,
                    actor_user_id=user.id,
                    operation_type="create",
                    idempotency_key=f"booking:create:{existing.id}",
                    delivery_payload={"notes": existing.notes},
                )
                await db.commit()
        if operation is not None:
            background_tasks.add_task(
                booking_operations.wake_operation,
                operation.id,
            )
        return await _prospect_appointment_result(
            db,
            prospect=prospect,
            appointment=existing,
            idempotent_replay=True,
        )

    active_appointment = (
        await db.execute(
            select(DealerRepAppointment)
            .where(
                DealerRepAppointment.prospect_id == prospect.id,
                DealerRepAppointment.archived_at.is_(None),
                DealerRepAppointment.status.in_(["pending", "confirmed"]),
            )
            .order_by(
                DealerRepAppointment.starts_at.desc(),
                DealerRepAppointment.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_appointment is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "active_appointment_exists",
                "message": (
                    "This prospect already has an active appointment. "
                    "Reschedule that appointment instead of creating another one."
                ),
                "appointment_id": str(active_appointment.id),
            },
        )

    service.assert_expected_version(prospect, payload.expected_version)
    contact = await db.get(DealerRepContact, prospect.primary_contact_id)
    company = await db.get(DealerRepCompany, prospect.company_id)
    current_stage = await db.get(
        DealerProspectStageDefinition, prospect.stage_definition_id
    )
    if contact is None or company is None or current_stage is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Prospect references are incomplete")
    invitee_email = (contact.email or prospect.email_normalized or "").strip().lower() or None
    invitee_phone = contact.phone_e164 or prospect.phone_normalized
    if not invitee_email and not invitee_phone:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The prospect needs an email address or phone number before booking.",
        )
    suppression = (
        await outreach_service.is_suppressed(db, invitee_email)
        if invitee_email
        else None
    )
    if (
        prospect.do_not_contact
        or current_stage.key == "not_interested"
        or suppression is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "prospect_contact_blocked",
                "message": (
                    "This prospect is blocked from contact. An authorized reactivation "
                    "must clear the restriction before booking."
                ),
            },
        )
    booked_stage = (
        await db.execute(
            select(DealerProspectStageDefinition).where(
                DealerProspectStageDefinition.key == "booked",
                DealerProspectStageDefinition.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if booked_stage is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The Booked stage is unavailable")

    outcome = None
    outcome_config: dict[str, Any] = {}
    if payload.trigger_outcome_key:
        outcome = (
            await db.execute(
                select(DealerProspectOutcomeDefinition).where(
                    DealerProspectOutcomeDefinition.key == payload.trigger_outcome_key,
                    DealerProspectOutcomeDefinition.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if outcome is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The selected booking outcome is unavailable.",
            )
        outcome_config = service.booking_outcome_config(outcome.action_config)

    host = await _rep_host_for(db, None, user)
    booking = await _booking_settings_for(db, host)
    if payload.meeting_mode == "video" and not booking.google_meet_enabled:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Video meetings are unavailable until Google Meet is enabled.",
        )
    starts_at = _to_utc_minute(payload.starts_at)
    duration = payload.duration_min or booking.duration_min or 20
    if not await _appointment_slot_is_available(
        db,
        host,
        booking,
        starts_at=starts_at,
        duration_min=duration,
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "That time is no longer available.")
    # Google is consulted before the lock. Once the shared owner lock is held,
    # repeat only the local check to serialize concurrent QC bookings without
    # pinning a database connection on provider latency.
    await lock_calendar_owner(db, host.id)
    if not await _appointment_slot_is_available(
        db,
        host,
        booking,
        starts_at=starts_at,
        duration_min=duration,
        check_google=False,
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "That time is no longer available.")

    title = _appointment_title("intro_call", contact.full_name, None)
    who = contact.full_name
    if invitee_email:
        who = f"{contact.full_name} <{invitee_email}>"
    description = "\n".join(
        line
        for line in (
            f"Marketing prospect: {company.name}",
            f"Contact: {contact.full_name}",
            f"Booked by: {user.name or user.email}",
            f"Prospect ID: {prospect.id}",
            f"Contact ID: {contact.id}",
            "",
            "Agent notes:",
            payload.notes or "(none)",
        )
    )
    event = CalendarEvent(
        loan_id=None,
        kind=CalendarEventKind.CALL,
        title=title,
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=duration,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.MANUAL,
        owner_user_id=host.id,
        external_ref_kind="dealer_rep_appointment",
        external_ref_id=secrets.token_urlsafe(12),
    )
    db.add(event)
    await db.flush()
    appointment = DealerRepAppointment(
        dealer_id=None,
        prospect_id=prospect.id,
        creation_idempotency_key=payload.idempotency_key,
        return_stage_id=current_stage.id,
        owner_user_id=host.id,
        calendar_event_id=event.id,
        contact_id=contact.id,
        kind="intro_call",
        title=title,
        starts_at=starts_at,
        duration_min=duration,
        timezone=booking.timezone,
        invitee_name=contact.full_name,
        invitee_email=invitee_email,
        invitee_phone=invitee_phone,
        company=company.name,
        join_url=None,
        meeting_mode=payload.meeting_mode,
        location=payload.location,
        notes=payload.notes,
        status="pending",
        client_rsvp_status="needs_action" if invitee_email else "unknown",
        origin="field_desk",
        booked_by_user_id=user.id,
        precall_application_data={
            "prospect_booking_replay_fingerprint": (
                _prospect_booking_replay_fingerprint(
                    prospect_id=prospect.id,
                    contact_id=contact.id,
                    owner_user_id=host.id,
                    actor_user_id=user.id,
                    invitee_email=invitee_email,
                    invitee_phone=invitee_phone,
                    starts_at=starts_at,
                    duration_min=duration,
                    meeting_mode=payload.meeting_mode,
                    location=payload.location,
                    notes=payload.notes,
                    transactional_sms_consent=payload.transactional_sms_consent,
                    trigger_outcome_key=payload.trigger_outcome_key,
                )
            )
        },
    )
    db.add(appointment)
    await db.flush()
    event.external_ref_id = str(appointment.id)
    _record_appointment_activity(
        db,
        appointment,
        event_type="appointment_created",
        user=user,
        body=appointment.title,
        after={"crm_status": appointment.crm_status, "prospect_id": str(prospect.id)},
    )
    await booking_reminders.register_booking(
        db,
        event=event,
        booking=booking,
        invitee_name=contact.full_name,
        invitee_email=invitee_email,
        invitee_phone=invitee_phone,
        sms_consent=payload.transactional_sms_consent,
        sms_consent_method=(
            "in_person_device" if payload.transactional_sms_consent else None
        ),
        sms_consent_ip=request.client.host if request.client else None,
        sms_consent_user_agent=request.headers.get("user-agent"),
        booked_by_user_id=user.id,
    )

    before_version = prospect.version
    prospect.stage_definition_id = booked_stage.id
    prospect.appointment_id = appointment.id
    prospect.next_follow_up_at = None
    if outcome is not None:
        prospect.last_outcome_definition_id = outcome.id
        prospect.last_outcome_at = datetime.now(UTC)
        if outcome_config.get("increment_call_attempt"):
            prospect.call_attempt_count += 1
    prospect.version += 1
    await service.add_activity(
        db,
        prospect,
        user,
        "appointment_booked",
        body=payload.notes,
        metadata={
            "appointment_id": str(appointment.id),
            "from_stage_key": current_stage.key,
            "to_stage_key": "booked",
            "starts_at": starts_at.isoformat(),
            "outcome_key": outcome.key if outcome else None,
            "action_config": outcome_config,
            "version_before": before_version,
            "version_after": prospect.version,
        },
    )
    assigned_notifications = await notify_users(
        db,
        recipient_ids={prospect.owner_user_id} if prospect.owner_user_id else set(),
        event_type="marketing_prospect_appointment_booked",
        category="calendar",
        priority="high",
        title=f"Appointment booked: {company.name}",
        body=(
            f"{contact.full_name} is scheduled for "
            f"{starts_at.isoformat()}."
        ),
        target_type="dealer_prospect",
        target_id=str(prospect.id),
        deep_link=f"/marketing/prospects/{prospect.id}",
        email=True,
        defer_email=True,
        push=True,
        actor_user_id=user.id,
        meta={
            "appointment_id": str(appointment.id),
            "prospect_id": str(prospect.id),
            "booked_by_user_id": str(user.id),
        },
    )
    delivery_operation = await booking_operations.enqueue(
        db,
        appointment=appointment,
        event=event,
        actor_user_id=user.id,
        operation_type="create",
        idempotency_key=f"booking:create:{appointment.id}",
        notification_ids=[row.id for row in assigned_notifications],
        delivery_payload={"notes": payload.notes},
    )
    await db.commit()

    result = await _prospect_appointment_result(
        db,
        prospect=prospect,
        appointment=appointment,
        idempotent_replay=False,
    )
    # Every provider effect was committed with the appointment. The wake-up
    # uses the per-effect claim ledger; the scheduler recovers a lost task.
    background_tasks.add_task(
        booking_operations.wake_operation,
        delivery_operation.id,
    )
    return result


@router.patch("/prospects/{prospect_id}", response_model=ProspectRead)
async def patch_prospect(
    prospect_id: UUID,
    payload: ProspectPatch,
    user: CurrentUser,
    db: DbSession,
) -> ProspectRead:
    # Identity locks must precede row locks everywhere.  Lock both the old and
    # proposed values so concurrent edits and quick-adds cannot swap or claim
    # either signal while this update is in flight.
    prospect = await service.load_visible_prospect(db, user, prospect_id)
    service.assert_expected_version(prospect, payload.expected_version)
    changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    proposed_email = (
        service.normalize_email(str(changes["email"]))
        if "email" in changes
        else prospect.email_normalized
    )
    proposed_phone = (
        service.normalize_phone(changes["phone"])
        if "phone" in changes
        else prospect.phone_normalized
    )
    _, proposed_email, proposed_phone = await service.resolve_contact_identity(
        db,
        actor_user=user,
        owner_user_id=prospect.owner_user_id or user.id,
        email=proposed_email,
        phone=proposed_phone,
        exclude_contact_id=prospect.primary_contact_id,
        additional_emails=(prospect.email_normalized,),
        additional_phones=(prospect.phone_normalized,),
    )
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    service.assert_expected_version(prospect, payload.expected_version)
    contact = await db.get(DealerRepContact, prospect.primary_contact_id)
    company = await db.get(DealerRepCompany, prospect.company_id)
    if contact is None or company is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Prospect contact is incomplete")
    changed_fields: list[str] = []

    if "owner_user_id" in changes:
        owner_id = changes["owner_user_id"]
        if owner_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Owner cannot be cleared")
        if user.role not in service.TEAM_ROLES and owner_id != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the team can reassign prospects")
        await _validate_owner(db, owner_id)
        previous_owner_id = prospect.owner_user_id
        if previous_owner_id != owner_id:
            # Ownership is itself sufficient Marketing access.  The legacy
            # reassignment path also left a contact assignment behind, which
            # meant every former owner retained the prospect and its complete
            # email history forever.  A transfer removes that owner-derived
            # assignment before granting the new owner contact-directory
            # access.  An administrator can explicitly share the contact
            # again after the transfer when continuing access is intended.
            if previous_owner_id is not None:
                await db.execute(
                    delete(DealerRepContactAssignment).where(
                        DealerRepContactAssignment.contact_id == contact.id,
                        DealerRepContactAssignment.user_id == previous_owner_id,
                        DealerRepContactAssignment.assignment_kind == "prospect_owner",
                    )
                )
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
                        assignment_kind="prospect_owner",
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
        follow_up = changes["next_follow_up_at"]
        if follow_up is not None:
            stage = await db.get(
                DealerProspectStageDefinition,
                prospect.stage_definition_id,
            )
            if (
                prospect.do_not_contact
                or stage is None
                or stage.key in {"booked", "converted", "not_interested"}
            ):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "code": "follow_up_not_allowed",
                        "message": "Follow-up is unavailable for this prospect state.",
                    },
                )
            follow_up = service.normalize_custom_follow_up(
                follow_up,
                timezone_name=await service.firm_booking_timezone(db),
            )
        prospect.next_follow_up_at = follow_up
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
        include_archived=True,
        for_update=True,
    )
    conflicts = [row for row in duplicates if row.id != prospect.id]
    if conflicts:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=service.duplicate_detail(
                conflicts,
                user,
                email_normalized=prospect.email_normalized,
                phone_normalized=prospect.phone_normalized,
            ),
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
    next_follow_up_at = payload.next_follow_up_at
    if next_follow_up_at is not None:
        # Legacy clients may still attach a follow-up to a stage move for one
        # release. Keep the field compatible, but enforce the same firm-time
        # workday rules as the new outcome flow.
        next_follow_up_at = service.normalize_custom_follow_up(
            next_follow_up_at,
            timezone_name=await service.firm_booking_timezone(db),
        )
    prospect = await service.move_stage(
        db,
        user,
        prospect,
        stage_key=payload.stage_key,
        expected_version=payload.expected_version,
        note=payload.note,
        next_follow_up_at=next_follow_up_at,
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
    "/prospects/{prospect_id}/call-attempts",
    response_model=ProspectActivityRead,
    status_code=status.HTTP_201_CREATED,
)
async def record_prospect_call_attempt(
    prospect_id: UUID,
    payload: ProspectCallAttemptCreate,
    user: CurrentUser,
    db: DbSession,
) -> ProspectActivityRead:
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    if prospect.do_not_contact:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "prospect_contact_blocked",
                "message": "This prospect is marked do-not-contact.",
            },
        )
    if payload.idempotency_key:
        existing = (
            await db.execute(
                select(DealerProspectActivity).where(
                    DealerProspectActivity.prospect_id == prospect.id,
                    DealerProspectActivity.actor_user_id == user.id,
                    DealerProspectActivity.kind == "call.initiated",
                    DealerProspectActivity.metadata_json["idempotency_key"].as_string()
                    == payload.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing_method = (existing.metadata_json or {}).get("method")
            if existing_method and existing_method != payload.method:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "code": "idempotency_key_reused",
                        "message": "Use a new call request identifier.",
                    },
                )
            return ProspectActivityRead.model_validate(
                await service.activity_read(existing, actor_name=user.name)
            )
    row = await service.add_activity(
        db,
        prospect,
        user,
        "call.initiated",
        body=(
            "Call initiated via Google Voice; connection not confirmed."
            if payload.method == "google_voice"
            else "Call initiated via the device dialer; connection not confirmed."
        ),
        metadata={
            "method": payload.method,
            "phone": prospect.phone_normalized,
            "connected": None,
            "idempotency_key": payload.idempotency_key,
        },
    )
    return ProspectActivityRead.model_validate(
        await service.activity_read(row, actor_name=user.name)
    )


@router.get(
    "/prospects/{prospect_id}/follow-up-suggestion",
    response_model=ProspectFollowUpSuggestionRead,
)
async def get_prospect_follow_up_suggestion(
    prospect_id: UUID,
    user: CurrentUser,
    db: DbSession,
    choice: Literal["next_business_day", "two_business_days"] = "next_business_day",
) -> ProspectFollowUpSuggestionRead:
    await service.load_visible_prospect(db, user, prospect_id)
    timezone_name = await service.firm_booking_timezone(db)
    business_days = 1 if choice == "next_business_day" else 2
    return ProspectFollowUpSuggestionRead(
        scheduled_at=service.business_follow_up_at(
            business_days=business_days,
            timezone_name=timezone_name,
        ),
        timezone=timezone_name,
        business_days=business_days,
    )


@router.get("/prospects/{prospect_id}/timeline", response_model=ProspectTimelineRead)
async def get_prospect_timeline(
    prospect_id: UUID,
    user: CurrentUser,
    db: DbSession,
    cursor: str | None = Query(default=None, max_length=1000),
    limit: int = Query(default=50, ge=1, le=100),
) -> ProspectTimelineRead:
    prospect = await service.load_visible_prospect_history(db, user, prospect_id)
    items, next_cursor = await service.prospect_timeline(
        db,
        prospect,
        cursor=cursor,
        limit=limit,
    )
    return ProspectTimelineRead(items=items, next_cursor=next_cursor)


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
    follow_up_timezone = await service.firm_booking_timezone(db)
    prospect, email_action, workflow_action = await service.apply_outcome(
        db,
        user,
        prospect,
        outcome=outcome,
        expected_version=payload.expected_version,
        note=payload.note,
        next_follow_up_at=payload.next_follow_up_at,
        appointment_id=payload.appointment_id,
        follow_up_choice=payload.follow_up_choice,
        timezone_name=follow_up_timezone,
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


def _conversion_candidate_read(
    target: Literal["portfolio_application", "dealer_ai_intake"],
    row: DealerBusiness | PublicUnderwritingIntake,
    *,
    prospect: DealerProspect,
) -> ProspectConversionCandidate:
    if target == "portfolio_application":
        application = row
        assert isinstance(application, DealerBusiness)
        return ProspectConversionCandidate(
            id=application.id,
            target=target,
            status="archived" if application.archived_at else application.status,
            archived=application.archived_at is not None,
            display_name=application.name,
            email=application.email,
            phone=application.phone,
            created_at=application.created_at,
            match_reasons=conversion_service.portfolio_candidate_match_reasons(
                prospect, application
            ),
            route=conversion_service.conversion_route(target, application.id),
        )
    intake = row
    assert isinstance(intake, PublicUnderwritingIntake)
    return ProspectConversionCandidate(
        id=intake.id,
        target=target,
        status=intake.status,
        archived=conversion_service.intake_archived(intake),
        display_name=intake.business_name or intake.full_name,
        email=intake.email,
        phone=intake.phone,
        created_at=intake.created_at,
        match_reasons=service.intake_candidate_match_reasons(prospect, intake),
        route=conversion_service.conversion_route(target, intake.id),
    )


async def _conversion_candidates(
    db: AsyncSession,
    prospect: DealerProspect,
    user: User,
    target: Literal["portfolio_application", "dealer_ai_intake"],
) -> list[DealerBusiness | PublicUnderwritingIntake]:
    if target == "portfolio_application":
        return list(await conversion_service.portfolio_candidates(db, prospect, user))
    return list(await service.intake_candidates(db, prospect, user))


def _prospect_conversion_destination(
    prospect: DealerProspect,
) -> tuple[Literal["portfolio_application", "dealer_ai_intake"], UUID] | None:
    if prospect.converted_application_id is not None:
        return "portfolio_application", prospect.converted_application_id
    if prospect.converted_intake_id is not None:
        return "dealer_ai_intake", prospect.converted_intake_id
    return None


async def _restricted_conversion_match_exists(
    db: AsyncSession,
    prospect: DealerProspect,
    user: User,
    target: Literal["portfolio_application", "dealer_ai_intake"],
) -> bool:
    if target == "portfolio_application":
        return await conversion_service.portfolio_restricted_match_exists(
            db, prospect, user
        )
    return await service.intake_restricted_match_exists(db, prospect, user)


def _raise_restricted_conversion_match() -> None:
    # Intentionally do not include a record id, owner, match reason, count, or
    # destination metadata.  Scoped agents may learn only that creating a
    # separate destination requires an explicit choice.
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "prospect_conversion_restricted_match",
            "message": (
                "A matching file exists outside your available records. "
                "Choose create separate only if a new file is intentional."
            ),
        },
    )


@router.get(
    "/prospects/{prospect_id}/conversion-candidates",
    response_model=ProspectConversionCandidateList,
)
async def list_prospect_conversion_candidates(
    prospect_id: UUID,
    user: CurrentUser,
    db: DbSession,
    target: Literal["portfolio_application", "dealer_ai_intake"] = Query(...),
) -> ProspectConversionCandidateList:
    prospect = await service.load_visible_prospect(db, user, prospect_id)
    converted = _prospect_conversion_destination(prospect)
    if converted is not None:
        return ProspectConversionCandidateList(
            target=target,
            prospect_id=prospect.id,
            already_converted=True,
            candidates=[],
        )
    rows = await _conversion_candidates(db, prospect, user, target)
    return ProspectConversionCandidateList(
        target=target,
        prospect_id=prospect.id,
        candidates=[
            _conversion_candidate_read(target, row, prospect=prospect) for row in rows
        ],
    )


@router.post(
    "/prospects/{prospect_id}/convert",
    response_model=ProspectGeneralConversionResult,
)
async def convert_prospect(
    prospect_id: UUID,
    payload: ProspectGeneralConversionRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> ProspectGeneralConversionResult:
    prospect = await service.load_visible_prospect(db, user, prospect_id, for_update=True)
    converted = _prospect_conversion_destination(prospect)
    if converted is not None:
        converted_target, destination_id = converted
        await _refresh_for_read(db, prospect)
        return ProspectGeneralConversionResult(
            status="already_converted",
            conversion_target=converted_target,
            prospect=ProspectRead.model_validate(await service.prospect_read(db, prospect)),
            application_id=(destination_id if converted_target == "portfolio_application" else None),
            intake_id=(destination_id if converted_target == "dealer_ai_intake" else None),
            route=conversion_service.conversion_route(converted_target, destination_id),
        )
    service.assert_expected_version(prospect, payload.expected_version)

    # A transaction-scoped identity lock closes the gap between a prior
    # candidate lookup and destination creation.  The candidate queries below
    # are deliberately rerun only after this lock is held.
    await conversion_service.acquire_conversion_identity_lock(db, prospect)
    rows = await _conversion_candidates(db, prospect, user, payload.target)
    candidates = [
        _conversion_candidate_read(payload.target, row, prospect=prospect) for row in rows
    ]
    if payload.action == "detect":
        if candidates:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "prospect_conversion_choice_required",
                    "target": payload.target,
                    "candidates": [row.model_dump(mode="json") for row in candidates],
                    "allowed_actions": ["link", "reactivate", "create"],
                },
            )
        if await _restricted_conversion_match_exists(
            db, prospect, user, payload.target
        ):
            _raise_restricted_conversion_match()

    selected = None
    if payload.action in {"link", "reactivate"}:
        selected = next((row for row in rows if row.id == payload.candidate_id), None)
        if selected is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Selected conversion candidate does not match this dealer contact.",
            )
        selected_read = _conversion_candidate_read(payload.target, selected, prospect=prospect)
        if selected_read.archived and payload.action != "reactivate":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "prospect_conversion_reactivation_required",
                    "message": "The selected file is archived. Choose reactivate to continue.",
                },
            )
        if not selected_read.archived and payload.action == "reactivate":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "prospect_conversion_candidate_active",
                    "message": "The selected file is already active. Choose link to continue.",
                },
            )

    result_status: Literal["linked", "reactivated", "created"]
    destination_id: UUID
    if payload.target == "portfolio_application":
        if selected is None:
            if payload.portfolio_application is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "code": "portfolio_application_details_required",
                        "message": "Application details and a six-digit room PIN are required.",
                    },
                )
            application = await conversion_service.create_portfolio_application(
                db, prospect, user, payload.portfolio_application
            )
            result_status = "created"
        else:
            assert isinstance(selected, DealerBusiness)
            application = selected
            await conversion_service.link_portfolio_application(
                db,
                prospect,
                application,
                user,
                reactivate=payload.action == "reactivate",
            )
            result_status = "reactivated" if payload.action == "reactivate" else "linked"
        destination_id = application.id
    else:
        if selected is None:
            intake = await service.create_intake_from_prospect(db, request, prospect, user)
            result_status = "created"
        else:
            assert isinstance(selected, PublicUnderwritingIntake)
            intake = selected
            result_status = "linked"
            if payload.action == "reactivate":
                intake.status = "collecting"
                intake.outcome_status = "submitted"
                intake.delete_requested_at = None
                intake.delete_requested_by_user_id = None
                intake.client_contact_suppressed = False
                result_status = "reactivated"
        destination_id = intake.id

    await service.complete_target_conversion(
        db,
        user,
        prospect,
        target=payload.target,
        destination_id=destination_id,
        event_kind=f"{payload.target}_{result_status}",
        note=payload.note,
    )
    await _refresh_for_read(db, prospect)
    return ProspectGeneralConversionResult(
        status=result_status,
        conversion_target=payload.target,
        prospect=ProspectRead.model_validate(
            await service.prospect_read(db, prospect, include_activities=True)
        ),
        application_id=(destination_id if payload.target == "portfolio_application" else None),
        intake_id=(destination_id if payload.target == "dealer_ai_intake" else None),
        route=conversion_service.conversion_route(payload.target, destination_id),
    )


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
    if prospect.converted_application_id is not None:
        await _refresh_for_read(db, prospect)
        return ProspectConversionResult(
            status="already_converted",
            conversion_target="portfolio_application",
            prospect=ProspectRead.model_validate(await service.prospect_read(db, prospect)),
            application_id=prospect.converted_application_id,
            route=conversion_service.conversion_route(
                "portfolio_application", prospect.converted_application_id
            ),
        )
    if prospect.converted_intake_id is not None:
        await _refresh_for_read(db, prospect)
        return ProspectConversionResult(
            status="already_converted",
            conversion_target="dealer_ai_intake",
            prospect=ProspectRead.model_validate(await service.prospect_read(db, prospect)),
            intake_id=prospect.converted_intake_id,
            route=f"/admin/ai-underwriter-leads?lead={prospect.converted_intake_id}",
        )
    service.assert_expected_version(prospect, payload.expected_version)

    # Keep this compatibility endpoint under the same race/privacy contract as
    # the generalized conversion endpoint.  Candidates are rescanned only
    # after the identity-level transaction lock is acquired.
    await conversion_service.acquire_conversion_identity_lock(db, prospect)
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
        if await service.intake_restricted_match_exists(db, prospect, user):
            _raise_restricted_conversion_match()
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
        archived = conversion_service.intake_archived(intake)
        if archived and payload.action != "reactivate":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "prospect_conversion_reactivation_required",
                    "message": "The selected file is archived. Choose reactivate to continue.",
                },
            )
        if not archived and payload.action == "reactivate":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "prospect_conversion_candidate_active",
                    "message": "The selected file is already active. Choose link to continue.",
                },
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
        conversion_target="dealer_ai_intake",
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
