from __future__ import annotations

# ruff: noqa: B008
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Language, Role
from app.models.application_profile import ApplicationProfile
from app.models.capital_readiness import ApplicationFinancialPeriod
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.application_profile import ApplicationProfileRead
from app.schemas.capital_readiness import (
    AddBackCreate,
    AddBackRead,
    AddBackTransition,
    CapitalReadinessRead,
    CapitalReadinessRecalculate,
    CapitalReadinessReviewRequest,
    CommunicationLocalePatch,
    FinancialPeriodCreate,
    FinancialPeriodRead,
    FinancialPeriodReview,
    ReadinessActionCreate,
    ReadinessActionPatch,
    ReadinessActionRead,
)
from app.services import application_profiles as profiles
from app.services import capital_readiness

router = APIRouter(tags=["capital-readiness"])

_FINANCIAL_EDIT_ROLES = {
    Role.SUPER_ADMIN,
    Role.LOAN_EXEC,
    Role.BROKER,
    Role.FIELD_REP,
}
_REVIEW_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
_RECOMPUTE_ROLES = {
    Role.SUPER_ADMIN,
    Role.LOAN_EXEC,
    Role.REGIONAL_MANAGER,
    Role.BROKER,
    Role.FIELD_REP,
}
_CLIENT_SAFE_ROLES = {Role.CLIENT, Role.DEALER}


def _require_role(user: CurrentUser, roles: set[Role], detail: str) -> None:
    if user.role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail)


async def _dealer_profile(
    db: AsyncSession, dealer_id: UUID, user: CurrentUser
) -> ApplicationProfile:
    profile_id = (
        await db.execute(
            select(ApplicationProfile.id)
            .where(ApplicationProfile.dealer_id == dealer_id)
            .order_by(
                ApplicationProfile.updated_at.desc(),
                ApplicationProfile.created_at.desc(),
                ApplicationProfile.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if profile_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application profile not found")
    return await profiles.load_profile(db, profile_id, user)


async def _current_or_404(
    db: AsyncSession, profile_id: UUID, user: CurrentUser
) -> CapitalReadinessRead:
    row = await capital_readiness.latest_snapshot(db, profile_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Capital Readiness has not been calculated",
                "action": "recalculate",
            },
        )
    return capital_readiness.read_snapshot(
        row,
        client_safe=user.role in _CLIENT_SAFE_ROLES,
        display_locale=getattr(user, "ui_locale", "en"),
    )


@router.get(
    "/application-profiles/{profile_id}/capital-readiness",
    response_model=CapitalReadinessRead,
)
async def get_capital_readiness(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CapitalReadinessRead:
    profile = await profiles.load_profile(db, profile_id, user)
    return await _current_or_404(db, profile.id, user)


@router.get(
    "/application-profiles/{profile_id}/capital-readiness/history",
    response_model=list[CapitalReadinessRead],
)
async def get_capital_readiness_history(
    profile_id: UUID,
    user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[CapitalReadinessRead]:
    profile = await profiles.load_profile(db, profile_id, user)
    rows = await capital_readiness.snapshot_history(db, profile.id, limit)
    return [
        capital_readiness.read_snapshot(
            row,
            client_safe=user.role in _CLIENT_SAFE_ROLES,
            display_locale=getattr(user, "ui_locale", "en"),
        )
        for row in rows
    ]


@router.post(
    "/application-profiles/{profile_id}/capital-readiness/recalculate",
    response_model=CapitalReadinessRead,
)
async def recalculate_capital_readiness(
    profile_id: UUID,
    payload: CapitalReadinessRecalculate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CapitalReadinessRead:
    _require_role(user, _RECOMPUTE_ROLES, "You cannot recalculate Capital Readiness")
    profile = await profiles.load_profile(db, profile_id, user)
    row = await capital_readiness.recompute(
        db,
        profile,
        idempotency_key=payload.idempotency_key,
        expected_snapshot_version=payload.expected_snapshot_version,
    )
    await db.commit()
    return capital_readiness.read_snapshot(
        row, display_locale=getattr(user, "ui_locale", "en")
    )


@router.post(
    "/application-profiles/{profile_id}/capital-readiness/review",
    response_model=CapitalReadinessRead,
)
async def review_capital_readiness(
    profile_id: UUID,
    payload: CapitalReadinessReviewRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CapitalReadinessRead:
    _require_role(user, _REVIEW_ROLES, "Only authorized underwriters can review Capital Readiness")
    profile = await profiles.load_profile(db, profile_id, user)
    row = await capital_readiness.review_snapshot(db, profile, payload, user)
    await db.commit()
    return capital_readiness.read_snapshot(
        row, display_locale=getattr(user, "ui_locale", "en")
    )


@router.get(
    "/dealer-os/dealers/{dealer_id}/capital-readiness",
    response_model=CapitalReadinessRead,
)
async def get_dealer_capital_readiness(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CapitalReadinessRead:
    profile = await _dealer_profile(db, dealer_id, user)
    return await _current_or_404(db, profile.id, user)


@router.post(
    "/dealer-os/dealers/{dealer_id}/capital-readiness/recalculate",
    response_model=CapitalReadinessRead,
)
async def recalculate_dealer_capital_readiness(
    dealer_id: UUID,
    payload: CapitalReadinessRecalculate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CapitalReadinessRead:
    _require_role(user, _RECOMPUTE_ROLES, "You cannot recalculate Capital Readiness")
    profile = await _dealer_profile(db, dealer_id, user)
    row = await capital_readiness.recompute(
        db,
        profile,
        idempotency_key=payload.idempotency_key,
        expected_snapshot_version=payload.expected_snapshot_version,
    )
    await db.commit()
    return capital_readiness.read_snapshot(
        row, display_locale=getattr(user, "ui_locale", "en")
    )


@router.get(
    "/application-profiles/{profile_id}/addback-verifications",
    response_model=list[AddBackRead],
)
async def get_addback_verifications(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[AddBackRead]:
    _require_role(user, _FINANCIAL_EDIT_ROLES, "You cannot view internal add-back verification")
    profile = await profiles.load_profile(db, profile_id, user)
    rows = await capital_readiness.list_addbacks(db, profile.id)
    return [capital_readiness.addback_read(row) for row in rows]


@router.post(
    "/application-profiles/{profile_id}/addback-verifications",
    response_model=AddBackRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_addback_verification(
    profile_id: UUID,
    payload: AddBackCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AddBackRead:
    _require_role(user, _FINANCIAL_EDIT_ROLES, "You cannot create an add-back candidate")
    profile = await profiles.load_profile(db, profile_id, user)
    row = await capital_readiness.create_addback(db, profile, payload, user)
    await capital_readiness.recompute(
        db,
        profile,
        idempotency_key=f"addback:{row.id}:{row.status}",
        expected_snapshot_version=None,
    )
    await db.commit()
    return capital_readiness.addback_read(row)


@router.patch(
    "/application-profiles/{profile_id}/addback-verifications/{addback_id}",
    response_model=AddBackRead,
)
async def transition_addback_verification(
    profile_id: UUID,
    addback_id: UUID,
    payload: AddBackTransition,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AddBackRead:
    _require_role(user, _REVIEW_ROLES, "Only authorized reviewers can verify add-backs")
    profile = await profiles.load_profile(db, profile_id, user)
    row = await capital_readiness.transition_addback(
        db, profile, addback_id, payload, user
    )
    await capital_readiness.recompute(
        db,
        profile,
        idempotency_key=f"addback:{row.id}:{row.status}",
        expected_snapshot_version=None,
    )
    await db.commit()
    return capital_readiness.addback_read(row)


@router.get(
    "/application-profiles/{profile_id}/financial-periods",
    response_model=list[FinancialPeriodRead],
)
async def get_financial_periods(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[FinancialPeriodRead]:
    _require_role(user, _FINANCIAL_EDIT_ROLES, "You cannot view internal financial periods")
    profile = await profiles.load_profile(db, profile_id, user)
    rows = await capital_readiness.list_financial_periods(db, profile.id)
    return [capital_readiness.period_read(row) for row in rows]


@router.post(
    "/application-profiles/{profile_id}/financial-periods",
    response_model=FinancialPeriodRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_financial_period(
    profile_id: UUID,
    payload: FinancialPeriodCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FinancialPeriodRead:
    _require_role(user, _FINANCIAL_EDIT_ROLES, "You cannot add financial periods")
    profile = await profiles.load_profile(db, profile_id, user)
    row = await capital_readiness.create_financial_period(db, profile, payload, user)
    await capital_readiness.recompute(
        db,
        profile,
        idempotency_key=f"period:{row.id}:{row.review_status}",
        expected_snapshot_version=None,
    )
    await db.commit()
    return capital_readiness.period_read(row)


@router.patch(
    "/application-profiles/{profile_id}/financial-periods/{period_id}/review",
    response_model=FinancialPeriodRead,
)
async def review_financial_period(
    profile_id: UUID,
    period_id: UUID,
    payload: FinancialPeriodReview,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FinancialPeriodRead:
    _require_role(user, _REVIEW_ROLES, "Only authorized underwriters can review financial periods")
    profile = await profiles.load_profile(db, profile_id, user)
    row = await db.get(ApplicationFinancialPeriod, period_id)
    if row is None or row.profile_id != profile.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Financial period not found")
    if row.review_status == payload.status:
        return capital_readiness.period_read(row)
    if row.review_status in {"rejected", "superseded"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "A final financial-period decision cannot be changed")
    row.review_status = payload.status
    row.reviewed_at = datetime.now(UTC)
    row.reviewed_by_user_id = user.id
    warnings = list(row.reconciliation_warnings or [])
    if payload.note:
        warnings.append({"code": "review_note", "severity": "information", "detail": payload.note})
    row.reconciliation_warnings = warnings
    await db.flush()
    await capital_readiness.recompute(
        db,
        profile,
        idempotency_key=f"period:{row.id}:{row.review_status}",
        expected_snapshot_version=None,
    )
    await db.commit()
    return capital_readiness.period_read(row)


@router.get(
    "/application-profiles/{profile_id}/capital-readiness/actions",
    response_model=list[ReadinessActionRead],
)
async def get_capital_readiness_actions(
    profile_id: UUID,
    user: CurrentUser,
    include_history: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> list[ReadinessActionRead]:
    _require_role(user, _FINANCIAL_EDIT_ROLES, "You cannot view internal roadmap actions")
    profile = await profiles.load_profile(db, profile_id, user)
    rows = await capital_readiness.list_actions(
        db, profile.id, current_only=not include_history
    )
    return [capital_readiness.action_read(row) for row in rows]


@router.post(
    "/application-profiles/{profile_id}/capital-readiness/actions",
    response_model=ReadinessActionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_capital_readiness_action(
    profile_id: UUID,
    payload: ReadinessActionCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ReadinessActionRead:
    _require_role(user, _FINANCIAL_EDIT_ROLES, "You cannot create roadmap actions")
    profile = await profiles.load_profile(db, profile_id, user)
    row = await capital_readiness.create_action(db, profile, payload, user)
    await capital_readiness.recompute(
        db,
        profile,
        idempotency_key=f"action:{row.action_key}:{row.version}",
        expected_snapshot_version=None,
    )
    await db.commit()
    return capital_readiness.action_read(row)


@router.patch(
    "/application-profiles/{profile_id}/capital-readiness/actions/{action_key}",
    response_model=ReadinessActionRead,
)
async def update_capital_readiness_action(
    profile_id: UUID,
    action_key: UUID,
    payload: ReadinessActionPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ReadinessActionRead:
    _require_role(user, _FINANCIAL_EDIT_ROLES, "You cannot update roadmap actions")
    profile = await profiles.load_profile(db, profile_id, user)
    row = await capital_readiness.patch_action(
        db, profile, action_key, payload, user
    )
    await capital_readiness.recompute(
        db,
        profile,
        idempotency_key=f"action:{row.action_key}:{row.version}",
        expected_snapshot_version=None,
    )
    await db.commit()
    return capital_readiness.action_read(row)


@router.patch(
    "/application-profiles/{profile_id}/communication-locale",
    response_model=ApplicationProfileRead,
)
async def update_communication_locale(
    profile_id: UUID,
    payload: CommunicationLocalePatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationProfileRead:
    profile = await profiles.load_profile(db, profile_id, user)
    if user.role in _CLIENT_SAFE_ROLES:
        if payload.source != "borrower_selection":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Clients can only record their own language selection",
            )
    elif user.role in _RECOMPUTE_ROLES:
        if payload.source != "staff_selection":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Operator changes must use staff_selection",
            )
    else:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You cannot change this file's communication language",
        )
    profile.communication_locale = payload.communication_locale
    profile.communication_locale_source = payload.source
    profile.communication_locale_updated_at = datetime.now(UTC)
    profile.communication_locale_updated_by_user_id = user.id
    # The public intake remains the compatibility source used by legacy
    # email/SMS/booking/resume builders. Keep the two file-scoped controls in
    # lockstep without ever touching the operator's independent UI locale.
    if profile.intake_id is not None:
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
        if intake is not None:
            intake.preferred_language = Language(payload.communication_locale)
    await db.flush()
    await capital_readiness.advisory_recompute_profiles(
        db,
        [profile],
        event_key=(
            f"communication-locale:{profile.id}:{payload.communication_locale}:"
            f"{profile.communication_locale_updated_at.isoformat()}"
        ),
    )
    await db.commit()
    return profiles.profile_read(profile)
