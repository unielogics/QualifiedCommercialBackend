"""Staff-only operating assumptions and source-bound adjustment review."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.schemas.earnings_improvement import BaselineTreatment, OperatingPlanBody
from app.services import application_profiles as profiles
from app.services import operating_plan_service

router = APIRouter(prefix="/application-profiles/{profile_id}/operating-plan", tags=["operating-plan"])
Db = Annotated[AsyncSession, Depends(get_db)]


def staff(user):
    if user.role in {Role.CLIENT, Role.DEALER, Role.LENDER, Role.VENDOR, Role.CAPITAL_PARTNER}:
        raise HTTPException(403, "Operating-plan review requires a staff account")


class Evaluate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: OperatingPlanBody
    family_id: UUID | None = None


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family_id: UUID
    expected_revision: int = Field(ge=0)
    action_ids: list[UUID] = Field(min_length=1, max_length=300)
    decision: Literal["approved", "rejected", "proposed"]
    reason: str = Field(min_length=1, max_length=2000)
    context_stamp: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)
    baseline_treatment: BaselineTreatment = "unreconciled"
    baseline_reconciliation_note: str = Field(default="", max_length=2000)


@router.get("/evidence")
async def evidence(profile_id: UUID, user: CurrentUser, db: Db):
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    from app.services.financial_cockpit import load_cockpit

    return (await load_cockpit(db, profile)).get("operating_evidence") or {}


@router.get("/summary")
async def summary(profile_id: UUID, user: CurrentUser, db: Db, family_id: UUID | None = None):
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    return await operating_plan_service.summary(db, profile, family_id=family_id)


@router.post("/evaluate")
async def evaluate(profile_id: UUID, payload: Evaluate, user: CurrentUser, db: Db):
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    # Evaluation cannot manufacture trusted approvals in a posted body.
    from app.models.financial_form_version import FinancialFormDraft
    from app.schemas.earnings_improvement import empty_body
    from app.services.financial_workspace import existing_family

    family = await existing_family(db, profile.id, "operating_plan")
    if payload.family_id and (family is None or payload.family_id != family.id):
        # Multiple owner/reporting-period families are valid: scope the explicit one.
        from app.models.financial_form_version import FinancialFormFamily
        family = await db.get(FinancialFormFamily, payload.family_id)
        if family is None or family.profile_id != profile.id or family.kind != "operating_plan":
            raise HTTPException(404, "Operating plan not found")
    draft = await db.get(FinancialFormDraft, family.id) if family else None
    body = operating_plan_service.protect_reviews(payload.body.model_dump(mode="json"), draft.body if draft else empty_body())
    return await operating_plan_service.preview(db, profile, body)


@router.post("/review")
async def review(profile_id: UUID, payload: Review, user: CurrentUser, db: Db):
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    profile = await profiles.load_profile(db, profile_id, user)
    result = await operating_plan_service.review(db, profile, user, **payload.model_dump())
    await db.commit()
    return result
