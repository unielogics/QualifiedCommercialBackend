"""Dealer prospect pipeline domain rules.

The router stays intentionally thin.  Access scoping, optimistic concurrency,
duplicate prevention, outcome effects, and append-only activity are kept here
so table, board, mobile, and future automation callers share one contract.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import Role
from app.models.client import Client
from app.models.dealer_prospect import (
    DealerProspect,
    DealerProspectActivity,
    DealerProspectOutcomeDefinition,
    DealerProspectStageDefinition,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User

from ..deps import is_rep, require_team_or_rep
from ..models import (
    DealerBusiness,
    DealerRepAppointment,
    DealerRepCompany,
    DealerRepContact,
    DealerRepContactAssignment,
)
from .consent_delivery import normalize_phone

TEAM_ROLES = frozenset({Role.SUPER_ADMIN, Role.LOAN_EXEC})
SYSTEM_STAGE_KEYS = frozenset(
    {"new", "emailed", "follow_up_1", "follow_up_2", "booked", "converted", "not_interested"}
)

DEFAULT_STAGES: tuple[dict[str, Any], ...] = (
    {"key": "new", "label": "New", "sort_order": 0},
    {"key": "emailed", "label": "Emailed", "sort_order": 10},
    {"key": "follow_up_1", "label": "Follow-up 1", "sort_order": 20},
    {"key": "follow_up_2", "label": "Follow-up 2", "sort_order": 30},
    {"key": "booked", "label": "Booked", "sort_order": 40},
    {"key": "converted", "label": "Converted", "sort_order": 50, "is_terminal": True},
    {
        "key": "not_interested",
        "label": "Not interested",
        "sort_order": 60,
        "is_terminal": True,
    },
)

DEFAULT_OUTCOMES: tuple[dict[str, Any], ...] = (
    {
        "key": "not_connected",
        "label": "Not connected",
        "sort_order": 0,
        "action_config": {
            "stage_strategy": "advance_follow_up",
            "increment_call_attempt": True,
            "follow_up_delay_hours": 24,
            "email_action": "missed_call",
        },
    },
    {
        "key": "call_back",
        "label": "Not available / call back",
        "sort_order": 10,
        "action_config": {
            "increment_call_attempt": True,
            "requires_follow_up": True,
            "email_action": "callback_confirmation",
        },
    },
    {
        "key": "wants_to_book",
        "label": "Wants to book",
        "sort_order": 20,
        "action_config": {
            "workflow_action": "book_appointment",
            "email_action": "booking_link",
        },
    },
    {
        "key": "booked",
        "label": "Booked",
        "sort_order": 30,
        "action_config": {
            "target_stage_key": "booked",
            "requires_appointment": True,
        },
    },
    {
        "key": "interested_send_information",
        "label": "Interested / send information",
        "sort_order": 40,
        "action_config": {
            "target_stage_key": "emailed",
            "email_action": "dealer_information_pack",
        },
    },
    {
        "key": "not_interested",
        "label": "Not interested",
        "sort_order": 50,
        "action_config": {
            "target_stage_key": "not_interested",
            "set_do_not_contact": True,
            "clear_follow_up": True,
        },
    },
    {
        "key": "bad_contact_unsubscribe",
        "label": "Bad contact / unsubscribe",
        "sort_order": 60,
        "action_config": {
            "set_do_not_contact": True,
            "clear_follow_up": True,
            "suppress_email": True,
        },
    },
)

_ACTION_CONFIG_KEYS = frozenset(
    {
        "target_stage_key",
        "stage_strategy",
        "email_action",
        "workflow_action",
        "requires_follow_up",
        "requires_appointment",
        "increment_call_attempt",
        "set_do_not_contact",
        "clear_follow_up",
        "suppress_email",
        "follow_up_delay_hours",
    }
)
_STAGE_STRATEGIES = frozenset({"advance_follow_up"})
_EMAIL_ACTIONS = frozenset(
    {"dealer_information_pack", "missed_call", "callback_confirmation", "booking_link"}
)
_WORKFLOW_ACTIONS = frozenset({"book_appointment"})


def now_utc() -> datetime:
    return datetime.now(UTC)


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_dealer_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.strip().casefold().split())


def definition_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", value).lower()).strip("_")
    if not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A machine key is required")
    return key[:64]


def require_prospect_actor(user: User) -> None:
    require_team_or_rep(user)
    if not bool(getattr(user, "dealer_prospect_pipeline_enabled", False)):
        # Match the global rollout gate: an unassigned pilot is not
        # discoverable merely because the login can otherwise enter Field Desk.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prospect pipeline is not enabled")


def require_pipeline_enabled() -> None:
    if not get_settings().dealer_prospect_pipeline_enabled:
        # A 404 keeps a disabled pilot surface undiscoverable to users who are
        # otherwise entitled to Field Desk.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prospect pipeline is not enabled")


def pipeline_effective_enabled(user: User) -> bool:
    """Whether this user can use the agent-facing pipeline right now."""

    return bool(
        get_settings().dealer_prospect_pipeline_enabled
        and is_active_prospect_owner(user)
        and getattr(user, "dealer_prospect_pipeline_enabled", False)
    )


def require_config_admin(user: User) -> None:
    if user.role not in TEAM_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Pipeline configuration requires a team role"
        )


def is_active_prospect_owner(user: User | None) -> bool:
    return bool(
        user is not None
        and user.deleted_at is None
        and (user.account_status or "active") == "active"
        and (user.role in TEAM_ROLES or is_rep(user))
    )


def prospect_access_filter(user: User):
    """SQL predicate for the only prospect records ``user`` may discover."""
    if user.role in TEAM_ROLES:
        return True
    if not is_rep(user):
        return False
    return or_(
        DealerProspect.owner_user_id == user.id,
        exists(
            select(DealerRepContactAssignment.id).where(
                DealerRepContactAssignment.contact_id == DealerProspect.primary_contact_id,
                DealerRepContactAssignment.user_id == user.id,
            )
        ),
    )


def assert_expected_version(prospect: DealerProspect, expected_version: int) -> None:
    if prospect.version != expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "prospect_version_conflict",
                "message": "This prospect changed in another session. Refresh before retrying.",
                "current_version": prospect.version,
            },
        )


def validate_action_config(value: dict[str, Any] | None) -> dict[str, Any]:
    config = dict(value or {})
    unknown = sorted(set(config) - _ACTION_CONFIG_KEYS)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_outcome_action", "unsupported_keys": unknown},
        )
    strategy = config.get("stage_strategy")
    if strategy == "":
        config.pop("stage_strategy", None)
        strategy = None
    if strategy is not None and strategy not in _STAGE_STRATEGIES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported stage strategy")
    if config.get("target_stage_key"):
        config["target_stage_key"] = definition_key(str(config["target_stage_key"]))
    else:
        config.pop("target_stage_key", None)
    for key in (
        "requires_follow_up",
        "requires_appointment",
        "increment_call_attempt",
        "set_do_not_contact",
        "clear_follow_up",
        "suppress_email",
    ):
        if key in config and not isinstance(config[key], bool):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{key} must be boolean")
    if "follow_up_delay_hours" in config:
        delay = config["follow_up_delay_hours"]
        if isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= 8760:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "follow_up_delay_hours must be a whole number from 1 to 8760",
            )
    for key in ("email_action", "workflow_action"):
        if config.get(key):
            config[key] = definition_key(str(config[key]))
        else:
            config.pop(key, None)
    if config.get("email_action") not in _EMAIL_ACTIONS | {None}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported email action")
    if config.get("workflow_action") not in _WORKFLOW_ACTIONS | {None}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported workflow action")

    target = config.get("target_stage_key")
    if target and strategy:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Choose either a fixed target stage or an automatic stage strategy, not both",
        )

    email_action = config.get("email_action")
    if email_action and (config.get("set_do_not_contact") or config.get("suppress_email")):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "An outcome that blocks contact cannot also create an email draft",
        )

    if config.get("clear_follow_up") and (
        config.get("requires_follow_up") or "follow_up_delay_hours" in config
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "An outcome cannot clear follow-up while requiring or scheduling a follow-up",
        )
    if config.get("requires_follow_up") and "follow_up_delay_hours" in config:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Choose either a required follow-up time or an automatic follow-up delay, not both",
        )

    if target == "converted":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Converted must use the AI Intake conversion workflow, not an outcome automation",
        )
    if target == "booked" and not config.get("requires_appointment"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Moving to Booked requires a linked appointment",
        )
    if target == "not_interested":
        if not config.get("set_do_not_contact") or not config.get("clear_follow_up"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Moving to Not interested must mark do-not-contact and clear follow-up",
            )
        if email_action or config.get("workflow_action"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Moving to Not interested cannot create an email or start a workflow",
            )
    return config


async def ensure_default_definitions(db: AsyncSession) -> None:
    """Compatibility initializer for databases upgraded before seed rows existed."""
    stage_keys = set((await db.execute(select(DealerProspectStageDefinition.key))).scalars().all())
    for item in DEFAULT_STAGES:
        if item["key"] not in stage_keys:
            db.add(
                DealerProspectStageDefinition(
                    **item,
                    is_active=True,
                    is_system=True,
                    behavior={},
                )
            )
    outcome_keys = set(
        (await db.execute(select(DealerProspectOutcomeDefinition.key))).scalars().all()
    )
    for item in DEFAULT_OUTCOMES:
        if item["key"] not in outcome_keys:
            db.add(
                DealerProspectOutcomeDefinition(
                    **item,
                    is_active=True,
                    is_system=True,
                )
            )
    await db.flush()


async def active_stages(
    db: AsyncSession, *, include_inactive: bool = False
) -> list[DealerProspectStageDefinition]:
    await ensure_default_definitions(db)
    stmt = select(DealerProspectStageDefinition)
    if not include_inactive:
        stmt = stmt.where(DealerProspectStageDefinition.is_active.is_(True))
    return list(
        (
            await db.execute(
                stmt.order_by(
                    DealerProspectStageDefinition.sort_order,
                    DealerProspectStageDefinition.created_at,
                )
            )
        )
        .scalars()
        .all()
    )


async def active_outcomes(
    db: AsyncSession, *, include_inactive: bool = False
) -> list[DealerProspectOutcomeDefinition]:
    await ensure_default_definitions(db)
    stmt = select(DealerProspectOutcomeDefinition)
    if not include_inactive:
        stmt = stmt.where(DealerProspectOutcomeDefinition.is_active.is_(True))
    return list(
        (
            await db.execute(
                stmt.order_by(
                    DealerProspectOutcomeDefinition.sort_order,
                    DealerProspectOutcomeDefinition.created_at,
                )
            )
        )
        .scalars()
        .all()
    )


async def load_visible_prospect(
    db: AsyncSession,
    user: User,
    prospect_id: UUID,
    *,
    for_update: bool = False,
) -> DealerProspect:
    require_prospect_actor(user)
    stmt = select(DealerProspect).where(
        DealerProspect.id == prospect_id,
        DealerProspect.archived_at.is_(None),
        prospect_access_filter(user),
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prospect not found")
    return row


async def load_visible_contact(db: AsyncSession, user: User, contact_id: UUID) -> DealerRepContact:
    """Resolve an All Contacts row without widening the caller's CRM scope."""
    require_prospect_actor(user)
    access_filter = True
    if user.role not in TEAM_ROLES:
        access_filter = or_(
            DealerRepContact.owner_user_id == user.id,
            exists(
                select(DealerRepContactAssignment.id).where(
                    DealerRepContactAssignment.contact_id == DealerRepContact.id,
                    DealerRepContactAssignment.user_id == user.id,
                )
            ),
        )
    filters: list[Any] = [DealerRepContact.id == contact_id, access_filter]
    if user.role != Role.SUPER_ADMIN:
        filters.append(
            or_(
                DealerRepContact.dealer_id.is_(None),
                DealerRepContact.dealer_id.in_(
                    select(DealerBusiness.id).where(DealerBusiness.is_training.is_(False))
                ),
            )
        )
    row = (await db.execute(select(DealerRepContact).where(*filters))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return row


async def activity_read(
    row: DealerProspectActivity, *, actor_name: str | None = None
) -> dict[str, Any]:
    return {
        "id": row.id,
        "prospect_id": row.prospect_id,
        "actor_user_id": row.actor_user_id,
        "actor_name": actor_name,
        "kind": row.kind,
        "body": row.body,
        "metadata": row.metadata_json or {},
        "created_at": row.created_at,
    }


def _prospect_read_payload(
    prospect: DealerProspect,
    *,
    contact: DealerRepContact,
    company: DealerRepCompany,
    stage: DealerProspectStageDefinition,
    owner: User | None,
    last_outcome: DealerProspectOutcomeDefinition | None,
    activities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": prospect.id,
        "owner_user_id": prospect.owner_user_id,
        "company_id": prospect.company_id,
        "primary_contact_id": prospect.primary_contact_id,
        "contact_id": prospect.primary_contact_id,
        "contact_name": contact.full_name,
        "name": contact.full_name,
        "dealer_name": company.name,
        "email": contact.email or prospect.email_normalized,
        "phone": contact.phone_e164 or prospect.phone_normalized,
        "stage_id": stage.id,
        "stage_key": stage.key,
        "stage_label": stage.label,
        "stage_sort_order": stage.sort_order,
        "source": prospect.source,
        "next_follow_up_at": prospect.next_follow_up_at,
        "last_activity_at": prospect.last_activity_at,
        "call_attempt_count": prospect.call_attempt_count,
        "last_outcome_key": last_outcome.key if last_outcome else None,
        "last_outcome_label": last_outcome.label if last_outcome else None,
        "last_outcome_at": prospect.last_outcome_at,
        "do_not_contact": prospect.do_not_contact,
        "do_not_contact_reason": prospect.do_not_contact_reason,
        "appointment_id": prospect.appointment_id,
        "converted_intake_id": prospect.converted_intake_id,
        "converted_at": prospect.converted_at,
        "version": prospect.version,
        "owner_name": owner.name if owner else None,
        "marketing_sms_consent": bool(
            contact.sms_marketing_consented_at and contact.sms_opted_out_at is None
        ),
        "created_at": prospect.created_at,
        "updated_at": prospect.updated_at,
        "activities": activities or [],
    }


async def prospect_read(
    db: AsyncSession,
    prospect: DealerProspect,
    *,
    include_activities: bool = False,
) -> dict[str, Any]:
    contact = await db.get(DealerRepContact, prospect.primary_contact_id)
    company = await db.get(DealerRepCompany, prospect.company_id)
    stage = await db.get(DealerProspectStageDefinition, prospect.stage_definition_id)
    owner = await db.get(User, prospect.owner_user_id) if prospect.owner_user_id else None
    if contact is None or company is None or stage is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Prospect references are incomplete")
    activities: list[dict[str, Any]] = []
    if include_activities:
        rows = list(
            (
                await db.execute(
                    select(DealerProspectActivity)
                    .where(DealerProspectActivity.prospect_id == prospect.id)
                    .order_by(
                        DealerProspectActivity.created_at.desc(),
                        DealerProspectActivity.id.desc(),
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        actor_ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
        actors = {}
        if actor_ids:
            actors = {
                actor.id: actor.name
                for actor in (await db.execute(select(User).where(User.id.in_(actor_ids))))
                .scalars()
                .all()
            }
        activities = [
            await activity_read(row, actor_name=actors.get(row.actor_user_id)) for row in rows
        ]
    last_outcome = (
        await db.get(DealerProspectOutcomeDefinition, prospect.last_outcome_definition_id)
        if prospect.last_outcome_definition_id
        else None
    )
    return _prospect_read_payload(
        prospect,
        contact=contact,
        company=company,
        stage=stage,
        owner=owner,
        last_outcome=last_outcome,
        activities=activities,
    )


async def prospects_read(db: AsyncSession, rows: list[DealerProspect]) -> list[dict[str, Any]]:
    """Serialize a table page in a fixed number of queries.

    The dense table can return hundreds of rows.  Loading references per row
    made its latency grow linearly, so the list path resolves each reference
    table once while preserving the requested database order.
    """
    if not rows:
        return []

    async def load_by_id(model, ids: set[UUID]):
        if not ids:
            return {}
        loaded = (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()
        return {row.id: row for row in loaded}

    contacts = await load_by_id(DealerRepContact, {row.primary_contact_id for row in rows})
    companies = await load_by_id(DealerRepCompany, {row.company_id for row in rows})
    stages = await load_by_id(
        DealerProspectStageDefinition, {row.stage_definition_id for row in rows}
    )
    owners = await load_by_id(User, {row.owner_user_id for row in rows if row.owner_user_id})
    outcomes = await load_by_id(
        DealerProspectOutcomeDefinition,
        {row.last_outcome_definition_id for row in rows if row.last_outcome_definition_id},
    )

    payloads: list[dict[str, Any]] = []
    for prospect in rows:
        contact = contacts.get(prospect.primary_contact_id)
        company = companies.get(prospect.company_id)
        stage = stages.get(prospect.stage_definition_id)
        if contact is None or company is None or stage is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Prospect references are incomplete")
        payloads.append(
            _prospect_read_payload(
                prospect,
                contact=contact,
                company=company,
                stage=stage,
                owner=owners.get(prospect.owner_user_id),
                last_outcome=outcomes.get(prospect.last_outcome_definition_id),
            )
        )
    return payloads


async def add_activity(
    db: AsyncSession,
    prospect: DealerProspect,
    user: User,
    kind: str,
    *,
    body: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DealerProspectActivity:
    at = now_utc()
    row = DealerProspectActivity(
        prospect_id=prospect.id,
        actor_user_id=user.id,
        kind=kind,
        body=body,
        metadata_json=metadata or {},
    )
    db.add(row)
    prospect.last_activity_at = at
    await db.flush()
    return row


async def attach_draft_to_transition(
    db: AsyncSession,
    prospect: DealerProspect,
    *,
    event_kind: str,
    draft_id: UUID,
) -> None:
    """Bind a newly-created countdown draft to its triggering transition."""
    row = (
        (
            await db.execute(
                select(DealerProspectActivity)
                .where(
                    DealerProspectActivity.prospect_id == prospect.id,
                    DealerProspectActivity.kind == event_kind,
                )
                .order_by(
                    DealerProspectActivity.created_at.desc(), DealerProspectActivity.id.desc()
                )
                .limit(1)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if row is None or (row.metadata_json or {}).get("version_after") != prospect.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The email draft could not be bound to its pipeline transition.",
        )
    row.metadata_json = {
        **(row.metadata_json or {}),
        "draft_id": str(draft_id),
        # Dynamic draft status is rechecked under a row lock by undo_activity.
        "reversible": True,
    }
    await db.flush()


async def find_duplicates(
    db: AsyncSession,
    *,
    dealer_name_normalized: str,
    email_normalized: str,
    phone_normalized: str,
    primary_contact_id: UUID | None = None,
) -> list[DealerProspect]:
    identity_match = (
        DealerProspect.dealer_name_normalized == dealer_name_normalized,
        or_(
            DealerProspect.email_normalized == email_normalized,
            DealerProspect.phone_normalized == phone_normalized,
        ),
    )
    duplicate_match = and_(*identity_match)
    if primary_contact_id is not None:
        duplicate_match = or_(
            duplicate_match,
            DealerProspect.primary_contact_id == primary_contact_id,
        )
    return list(
        (
            await db.execute(
                select(DealerProspect).where(
                    DealerProspect.archived_at.is_(None),
                    duplicate_match,
                )
            )
        )
        .scalars()
        .all()
    )


def duplicate_detail(
    rows: Iterable[DealerProspect],
    user: User,
    *,
    known_contact_id: UUID | None = None,
) -> dict[str, Any]:
    visible = [
        row
        for row in rows
        if user.role in TEAM_ROLES
        or row.owner_user_id == user.id
        or (known_contact_id is not None and row.primary_contact_id == known_contact_id)
    ]
    return {
        "code": "duplicate_prospect",
        "message": "This dealer/contact already exists in the prospect pipeline.",
        "candidates": [
            {
                "prospect_id": str(row.id),
                "owner_user_id": str(row.owner_user_id) if row.owner_user_id else None,
            }
            for row in visible
        ],
        "assignment_required": not bool(visible),
    }


async def create_prospect(
    db: AsyncSession,
    user: User,
    *,
    contact_name: str,
    dealer_name: str,
    email: str,
    phone: str,
    source: str,
    owner_user_id: UUID | None,
    contact_id: UUID | None = None,
    initial_note: str | None = None,
) -> DealerProspect:
    require_prospect_actor(user)
    contact: DealerRepContact | None = None
    company: DealerRepCompany | None = None
    if contact_id is not None:
        contact = await load_visible_contact(db, user, contact_id)
        if contact.company_id is not None:
            company = await db.get(DealerRepCompany, contact.company_id)
            if company is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "Contact company is incomplete")
        contact_name = contact.full_name
        dealer_name = company.name if company else (contact.company or dealer_name)
        if contact.email:
            if normalize_email(contact.email) != normalize_email(email):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "code": "contact_identity_changed",
                        "message": "This contact's email changed. Refresh before adding it.",
                    },
                )
            email = contact.email
        else:
            contact.email = normalize_email(email)
        if contact.phone_e164:
            if normalize_phone(contact.phone_e164) != normalize_phone(phone):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "code": "contact_identity_changed",
                        "message": "This contact's phone changed. Refresh before adding it.",
                    },
                )
            phone = contact.phone_e164
        else:
            contact.phone_e164 = normalize_phone(phone)

    default_owner_id = (
        (contact.owner_user_id if contact else None)
        or (company.owner_user_id if company else None)
        or user.id
    )
    effective_owner_id = owner_user_id or default_owner_id
    if owner_user_id is not None and owner_user_id != user.id and user.role not in TEAM_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the team can assign another owner")
    owner = await db.get(User, effective_owner_id)
    if not is_active_prospect_owner(owner) or not bool(
        getattr(owner, "dealer_prospect_pipeline_enabled", False)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Owner must have enabled Dealer Pipeline access",
        )

    normalized_email = normalize_email(email)
    normalized_phone = normalize_phone(phone)
    if normalized_phone is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "A valid phone number is required"
        )
    normalized_dealer = normalize_dealer_name(dealer_name)
    duplicates = await find_duplicates(
        db,
        dealer_name_normalized=normalized_dealer,
        email_normalized=normalized_email,
        phone_normalized=normalized_phone,
        primary_contact_id=contact.id if contact else None,
    )
    if duplicates:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=duplicate_detail(duplicates, user, known_contact_id=contact_id),
        )

    await ensure_default_definitions(db)
    new_stage = (
        await db.execute(
            select(DealerProspectStageDefinition).where(
                DealerProspectStageDefinition.key == "new",
                DealerProspectStageDefinition.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if new_stage is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The New pipeline stage is unavailable")

    if company is None:
        company = (
            (
                await db.execute(
                    select(DealerRepCompany)
                    .where(
                        DealerRepCompany.owner_user_id == effective_owner_id,
                        func.lower(DealerRepCompany.name) == dealer_name.strip().lower(),
                    )
                    .order_by(DealerRepCompany.updated_at.desc())
                )
            )
            .scalars()
            .first()
        )
    if company is None:
        company = DealerRepCompany(
            owner_user_id=effective_owner_id,
            name=dealer_name.strip(),
            industry="auto_dealer",
            industry_label="Auto dealer",
            status="active",
        )
        db.add(company)
        await db.flush()

    if contact is None:
        contact = (
            (
                await db.execute(
                    select(DealerRepContact)
                    .where(
                        DealerRepContact.owner_user_id == effective_owner_id,
                        DealerRepContact.company_id == company.id,
                        or_(
                            func.lower(func.coalesce(DealerRepContact.email, ""))
                            == normalized_email,
                            DealerRepContact.phone_e164 == normalized_phone,
                        ),
                    )
                    .order_by(DealerRepContact.updated_at.desc())
                )
            )
            .scalars()
            .first()
        )
    at = now_utc()
    if contact is None:
        contact = DealerRepContact(
            owner_user_id=effective_owner_id,
            company_id=company.id,
            full_name=contact_name.strip(),
            company=company.name,
            email=normalized_email,
            phone_e164=normalized_phone,
            source="prospect_pipeline",
            last_activity_at=at,
        )
        db.add(contact)
        await db.flush()
    else:
        contact.company_id = contact.company_id or company.id
        contact.company = contact.company or company.name
        contact.email = contact.email or normalized_email
        contact.phone_e164 = contact.phone_e164 or normalized_phone
        contact.last_activity_at = at

    prospect = DealerProspect(
        owner_user_id=effective_owner_id,
        company_id=company.id,
        primary_contact_id=contact.id,
        stage_definition_id=new_stage.id,
        email_normalized=normalized_email,
        phone_normalized=normalized_phone,
        dealer_name_normalized=normalized_dealer,
        source=source,
        last_activity_at=at,
        version=1,
    )
    db.add(prospect)
    await db.flush()
    await add_activity(
        db,
        prospect,
        user,
        "prospect_created",
        metadata={
            "stage_key": "new",
            "owner_user_id": str(effective_owner_id),
            "source": source,
        },
    )
    if initial_note:
        await add_activity(
            db,
            prospect,
            user,
            "internal_note",
            body=initial_note,
            metadata={"private": True, "source": "prospect_creation"},
        )
    return prospect


async def validate_appointment(
    db: AsyncSession,
    prospect: DealerProspect,
    appointment_id: UUID | None,
) -> DealerRepAppointment:
    if appointment_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "appointment_required",
                "message": "Link or create an appointment first.",
            },
        )
    appointment = await db.get(DealerRepAppointment, appointment_id)
    if (
        appointment is None
        or appointment.archived_at is not None
        or appointment.contact_id != prospect.primary_contact_id
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Appointment does not belong to this prospect"
        )
    return appointment


async def move_stage(
    db: AsyncSession,
    user: User,
    prospect: DealerProspect,
    *,
    stage_key: str,
    expected_version: int,
    note: str | None,
    next_follow_up_at: datetime | None,
    action: str,
    appointment_id: UUID | None,
    confirm_do_not_contact: bool,
    event_kind: str = "stage_moved",
    extra_metadata: dict[str, Any] | None = None,
) -> DealerProspect:
    assert_expected_version(prospect, expected_version)
    current_stage = await db.get(DealerProspectStageDefinition, prospect.stage_definition_id)
    destination = (
        await db.execute(
            select(DealerProspectStageDefinition).where(
                DealerProspectStageDefinition.key == stage_key,
                DealerProspectStageDefinition.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if current_stage is None or destination is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pipeline stage is unavailable")
    before_version = prospect.version
    previous_follow_up = prospect.next_follow_up_at
    previous_do_not_contact = prospect.do_not_contact
    previous_do_not_contact_reason = prospect.do_not_contact_reason
    previous_appointment_id = prospect.appointment_id
    if destination.key == "converted" and prospect.converted_intake_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "conversion_required",
                "message": "Use Create AI Intake before moving this prospect to Converted.",
            },
        )
    if destination.key == "not_interested" and action != "none":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "do_not_contact_email_forbidden",
                "message": "A do-not-contact stage move cannot create an email draft.",
            },
        )
    if destination.key == "booked":
        appointment = await validate_appointment(db, prospect, appointment_id)
        prospect.appointment_id = appointment.id
    if destination.key == "not_interested":
        if not confirm_do_not_contact:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "do_not_contact_confirmation_required",
                    "message": "Confirm that this prospect should no longer be contacted.",
                },
            )
        prospect.do_not_contact = True
        prospect.do_not_contact_reason = "not_interested"
        prospect.next_follow_up_at = None
    elif next_follow_up_at is not None:
        prospect.next_follow_up_at = next_follow_up_at

    prospect.stage_definition_id = destination.id
    prospect.version += 1
    metadata = {
        "from_stage_key": current_stage.key,
        "to_stage_key": destination.key,
        "selected_action": action,
        "appointment_id": str(prospect.appointment_id) if prospect.appointment_id else None,
        "version_before": before_version,
        "version_after": prospect.version,
        "reversible": action == "none" and destination.key not in {"converted"},
        "next_follow_up_before": previous_follow_up.isoformat() if previous_follow_up else None,
        "do_not_contact_before": previous_do_not_contact,
        "do_not_contact_reason_before": previous_do_not_contact_reason,
        "appointment_id_before": (
            str(previous_appointment_id) if previous_appointment_id else None
        ),
    }
    metadata.update(extra_metadata or {})
    await add_activity(db, prospect, user, event_kind, body=note, metadata=metadata)
    await db.flush()
    return prospect


def outcome_target_stage(current_stage_key: str, config: dict[str, Any]) -> str | None:
    if config.get("stage_strategy") == "advance_follow_up":
        return {
            "new": "emailed",
            "emailed": "follow_up_1",
            "follow_up_1": "follow_up_2",
            "follow_up_2": "follow_up_2",
        }.get(current_stage_key)
    target = config.get("target_stage_key")
    return str(target) if target else None


def outcome_follow_up_at(
    config: dict[str, Any],
    explicit: datetime | None,
    *,
    current_time: datetime | None = None,
) -> datetime | None:
    if explicit is not None:
        return explicit
    delay = config.get("follow_up_delay_hours")
    if isinstance(delay, int) and not isinstance(delay, bool) and delay > 0:
        return (current_time or now_utc()) + timedelta(hours=delay)
    return None


async def apply_outcome(
    db: AsyncSession,
    user: User,
    prospect: DealerProspect,
    *,
    outcome: DealerProspectOutcomeDefinition,
    expected_version: int,
    note: str | None,
    next_follow_up_at: datetime | None,
    appointment_id: UUID | None,
) -> tuple[DealerProspect, str | None, str | None]:
    assert_expected_version(prospect, expected_version)
    if not outcome.is_active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Call outcome is inactive")
    config = validate_action_config(outcome.action_config)
    next_follow_up_at = outcome_follow_up_at(config, next_follow_up_at)
    if config.get("requires_follow_up") and next_follow_up_at is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "follow_up_required", "message": "Choose the next follow-up time."},
        )
    if config.get("requires_appointment"):
        await validate_appointment(db, prospect, appointment_id)

    current_stage = await db.get(DealerProspectStageDefinition, prospect.stage_definition_id)
    if current_stage is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Current pipeline stage is unavailable")
    target = outcome_target_stage(current_stage.key, config)
    before_version = prospect.version
    prior = {
        "call_attempt_count_before": prospect.call_attempt_count,
        "next_follow_up_before": (
            prospect.next_follow_up_at.isoformat() if prospect.next_follow_up_at else None
        ),
        "do_not_contact_before": prospect.do_not_contact,
        "do_not_contact_reason_before": prospect.do_not_contact_reason,
        "last_outcome_definition_id_before": (
            str(prospect.last_outcome_definition_id)
            if prospect.last_outcome_definition_id
            else None
        ),
        "last_outcome_at_before": (
            prospect.last_outcome_at.isoformat() if prospect.last_outcome_at else None
        ),
        "reversible": not bool(
            config.get("email_action")
            or config.get("workflow_action")
            or config.get("suppress_email")
        ),
    }

    if target is not None:
        prospect = await move_stage(
            db,
            user,
            prospect,
            stage_key=target,
            expected_version=expected_version,
            note=note,
            next_follow_up_at=next_follow_up_at,
            action="none",
            appointment_id=appointment_id,
            confirm_do_not_contact=bool(config.get("set_do_not_contact")),
            event_kind="outcome_applied",
            extra_metadata={"outcome_key": outcome.key, "action_config": config, **prior},
        )
    else:
        prospect.version += 1
        if next_follow_up_at is not None:
            prospect.next_follow_up_at = next_follow_up_at
        await add_activity(
            db,
            prospect,
            user,
            "outcome_applied",
            body=note,
            metadata={
                "outcome_key": outcome.key,
                "from_stage_key": current_stage.key,
                "to_stage_key": current_stage.key,
                "action_config": config,
                "version_before": before_version,
                "version_after": prospect.version,
                **prior,
            },
        )

    if config.get("increment_call_attempt"):
        prospect.call_attempt_count += 1
    if config.get("clear_follow_up"):
        prospect.next_follow_up_at = None
    if config.get("set_do_not_contact"):
        prospect.do_not_contact = True
        prospect.do_not_contact_reason = outcome.key
    prospect.last_outcome_definition_id = outcome.id
    prospect.last_outcome_at = now_utc()
    if appointment_id is not None:
        prospect.appointment_id = appointment_id
    if config.get("suppress_email"):
        # Lazy import avoids coupling the pipeline model layer to delivery
        # providers while still making a bad-contact outcome globally binding.
        from app.services.prospect_outreach import set_suppression

        await set_suppression(
            db,
            email=prospect.email_normalized,
            reason="bad_address",
            source="prospect_outcome",
            actor_user_id=user.id,
            details={"prospect_id": str(prospect.id), "outcome_key": outcome.key},
        )
    await db.flush()
    return prospect, config.get("email_action"), config.get("workflow_action")


def _metadata_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def undo_activity(
    db: AsyncSession,
    user: User,
    prospect: DealerProspect,
    activity_id: UUID,
    *,
    expected_version: int,
) -> DealerProspect:
    """Reverse the latest reversible stage/outcome mutation.

    Sent email, conversion, booking workflow, and stale-history events are
    deliberately not reversible.  Undo itself is another append-only event.
    """
    assert_expected_version(prospect, expected_version)
    activity = (
        await db.execute(
            select(DealerProspectActivity)
            .where(
                DealerProspectActivity.id == activity_id,
                DealerProspectActivity.prospect_id == prospect.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if activity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prospect activity not found")
    metadata = activity.metadata_json or {}
    if not metadata.get("reversible"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "prospect_activity_not_reversible",
                "message": "This action has effects that cannot be undone.",
            },
        )
    if metadata.get("version_after") != prospect.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "prospect_activity_not_latest",
                "message": "Only the latest unchanged action can be undone.",
            },
        )
    draft_id = metadata.get("draft_id")
    if draft_id:
        from app.models.prospect_outreach import DealerProspectEmailDraft
        from app.services import prospect_outreach as outreach_service

        try:
            draft_uuid = UUID(str(draft_id))
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Activity has an invalid email draft"
            ) from exc
        draft = (
            await db.execute(
                select(DealerProspectEmailDraft)
                .where(DealerProspectEmailDraft.id == draft_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if draft is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "The linked email draft no longer exists")
        if draft.status in {"sending", "sent"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "prospect_activity_not_reversible",
                    "message": "The linked email has already entered delivery and cannot be undone.",
                },
            )
        if draft.status in {"pending_review", "editing", "blocked", "failed"}:
            try:
                await outreach_service.cancel_draft(
                    db,
                    draft.id,
                    expected_version=draft.version,
                )
            except outreach_service.OutreachConflict as exc:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "code": "prospect_activity_not_reversible",
                        "message": str(exc),
                    },
                ) from exc
        elif draft.status != "cancelled":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "prospect_activity_not_reversible",
                    "message": f"A {draft.status} email draft cannot be undone.",
                },
            )
    target = metadata.get("from_stage_key")
    if not target:
        raise HTTPException(status.HTTP_409_CONFLICT, "Activity has no previous stage")
    appointment_id = prospect.appointment_id if target == "booked" else None
    prospect = await move_stage(
        db,
        user,
        prospect,
        stage_key=str(target),
        expected_version=expected_version,
        note=f"Undid {activity.kind}",
        next_follow_up_at=None,
        action="none",
        appointment_id=appointment_id,
        confirm_do_not_contact=target == "not_interested",
        event_kind="activity_undone",
        extra_metadata={
            "undone_activity_id": str(activity.id),
            "reversible": False,
        },
    )
    prospect.next_follow_up_at = _metadata_datetime(metadata.get("next_follow_up_before"))
    prospect.do_not_contact = bool(metadata.get("do_not_contact_before", False))
    prospect.do_not_contact_reason = metadata.get("do_not_contact_reason_before")
    if "appointment_id_before" in metadata:
        prior_appointment_id = metadata.get("appointment_id_before")
        prospect.appointment_id = UUID(prior_appointment_id) if prior_appointment_id else None
    if "call_attempt_count_before" in metadata:
        prospect.call_attempt_count = int(metadata["call_attempt_count_before"])
    if "last_outcome_definition_id_before" in metadata:
        prior_id = metadata.get("last_outcome_definition_id_before")
        prospect.last_outcome_definition_id = UUID(prior_id) if prior_id else None
        prospect.last_outcome_at = _metadata_datetime(metadata.get("last_outcome_at_before"))
    await db.flush()
    return prospect


async def intake_candidates(
    db: AsyncSession, prospect: DealerProspect, user: User
) -> list[PublicUnderwritingIntake]:
    digits = re.sub(r"\D", "", prospect.phone_normalized)
    normalized_business = func.lower(
        func.regexp_replace(
            func.btrim(func.coalesce(PublicUnderwritingIntake.business_name, "")),
            r"\s+",
            " ",
            "g",
        )
    )
    stmt = (
        select(PublicUnderwritingIntake)
        .where(
            PublicUnderwritingIntake.variant == "dealer_gatekeeper_v1",
            or_(
                func.lower(PublicUnderwritingIntake.email) == prospect.email_normalized,
                func.regexp_replace(
                    func.coalesce(PublicUnderwritingIntake.phone, ""), "[^0-9]", "", "g"
                )
                == digits,
                normalized_business == prospect.dealer_name_normalized,
            ),
        )
        .order_by(PublicUnderwritingIntake.created_at.desc())
        .limit(20)
    )
    if user.role not in TEAM_ROLES:
        # Reps may only discover/link an intake already attributed to them or
        # to the owner of the prospect they were explicitly assigned. Identity
        # matching alone must never expose or mutate another rep's file.
        visible_owner_ids = {user.id}
        if prospect.owner_user_id is not None:
            visible_owner_ids.add(prospect.owner_user_id)
        stmt = stmt.where(
            or_(
                PublicUnderwritingIntake.source_user_id.in_(visible_owner_ids),
                PublicUnderwritingIntake.broker_id.in_(visible_owner_ids),
                exists(
                    select(Client.id).where(
                        Client.id == PublicUnderwritingIntake.client_id,
                        or_(
                            Client.current_agent_id.in_(visible_owner_ids),
                            Client.originating_agent_id.in_(visible_owner_ids),
                        ),
                    )
                ),
            )
        )
    return list((await db.execute(stmt)).scalars().all())


def intake_candidate_match_reasons(
    prospect: DealerProspect, intake: PublicUnderwritingIntake
) -> list[str]:
    reasons: list[str] = []
    if normalize_email(intake.email) == prospect.email_normalized:
        reasons.append("email")
    intake_phone = normalize_phone(intake.phone) if intake.phone else None
    if intake_phone and intake_phone == prospect.phone_normalized:
        reasons.append("phone")
    if intake.business_name and normalize_dealer_name(intake.business_name) == (
        prospect.dealer_name_normalized
    ):
        reasons.append("dealer_name")
    return reasons


async def create_intake_from_prospect(
    db: AsyncSession,
    request: Request,
    prospect: DealerProspect,
    user: User,
) -> PublicUnderwritingIntake:
    contact = await db.get(DealerRepContact, prospect.primary_contact_id)
    company = await db.get(DealerRepCompany, prospect.company_id)
    if contact is None or company is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Prospect contact is incomplete")

    # Lazy imports keep the dealer CRM independent at startup while reusing the
    # exact bucket/checklist implementation used by the existing AI Intake.
    from app.routers.dealer_ai_intake import (
        DEALER_VARIANT,
        DealerIntakeStart,
        _create_bucket_for_intake,
        _find_or_create_client,
        _hash_token,
        _new_public_token,
    )

    adapter = DealerIntakeStart(
        full_name=contact.full_name,
        email=prospect.email_normalized,
        phone=prospect.phone_normalized,
        business_name=company.name,
    )
    client: Client = await _find_or_create_client(db, adapter)
    # Preserve rep attribution without stealing a client already assigned to
    # someone else.
    if client.originating_agent_id is None:
        client.originating_agent_id = prospect.owner_user_id or user.id
    if client.current_agent_id is None:
        client.current_agent_id = prospect.owner_user_id or user.id
    bucket, link = await _create_bucket_for_intake(db, client, adapter, request)
    token = _new_public_token()
    intake = PublicUnderwritingIntake(
        source_kind="dealer_prospect",
        source_detail=f"Field Desk prospect {prospect.id}"[:200],
        source_actor_name=(user.name or user.email or "")[:200],
        source_user_id=user.id,
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        broker_id=None,
        token_hash=_hash_token(token),
        variant=DEALER_VARIANT,
        full_name=contact.full_name,
        email=prospect.email_normalized,
        phone=prospect.phone_normalized,
        business_name=company.name,
        asset_rows=[],
        intake_state={
            "source": "dealer_prospect",
            "prospect_id": str(prospect.id),
            "messages": [],
        },
    )
    db.add(intake)
    await db.flush()
    return intake


async def complete_conversion(
    db: AsyncSession,
    user: User,
    prospect: DealerProspect,
    intake: PublicUnderwritingIntake,
    *,
    event_kind: str,
    note: str | None = None,
) -> None:
    converted_stage = (
        await db.execute(
            select(DealerProspectStageDefinition).where(
                DealerProspectStageDefinition.key == "converted",
                DealerProspectStageDefinition.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if converted_stage is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The Converted stage is unavailable")
    old_stage = await db.get(DealerProspectStageDefinition, prospect.stage_definition_id)
    prospect.converted_intake_id = intake.id
    prospect.converted_at = now_utc()
    prospect.stage_definition_id = converted_stage.id
    prospect.next_follow_up_at = None
    before = prospect.version
    prospect.version += 1
    await add_activity(
        db,
        prospect,
        user,
        event_kind,
        body=note,
        metadata={
            "intake_id": str(intake.id),
            "from_stage_key": old_stage.key if old_stage else None,
            "to_stage_key": "converted",
            "version_before": before,
            "version_after": prospect.version,
            "reversible": False,
        },
    )
    await db.flush()


def random_room_pin() -> str:
    """Six decimal digits; leading zeros retained."""
    return f"{secrets.randbelow(1_000_000):06d}"
