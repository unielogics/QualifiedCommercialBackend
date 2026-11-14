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
from app.schemas.operating_plan_goal import GoalWrite
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


class Suggest(Evaluate):
    expense_row_id: UUID


class ApplySuggestion(Suggest):
    suggestion_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    context_stamp: str = Field(pattern=r"^[a-f0-9]{64}$")


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


@router.get("/goal")
async def goal(profile_id: UUID, user: CurrentUser, db: Db):
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    from app.services.operating_plan_goals import read_goal

    return await read_goal(db, profile)


@router.put("/goal")
async def configure_goal(profile_id: UUID, payload: GoalWrite, user: CurrentUser, db: Db):
    from app.services.operating_plan_goals import WRITE_ROLES, set_goal

    if user.role not in WRITE_ROLES:
        raise HTTPException(403, "Underwriting role required to configure the operating-plan goal")
    profile = await profiles.load_profile(db, profile_id, user)
    result = await set_goal(db, profile, **payload.model_dump(), actor=user)
    await db.commit()
    return result


@router.get("/evidence")
async def evidence(profile_id: UUID, user: CurrentUser, db: Db):
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    from app.services.financial_cockpit import load_cockpit

    cockpit = await load_cockpit(db, profile)
    return {**(cockpit.get("operating_evidence") or {}), "goal": cockpit.get("_operating_goal")}


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


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: dict
    family_id: UUID | None = None


class PlanDraft(PlanBody):
    # None only for the first save of a plan nobody has edited yet.
    expected_revision: int | None = Field(default=None, ge=0)


def _plan_body(payload):
    from app.schemas import earnings_plan_v2

    if not earnings_plan_v2.is_v2(payload.body):
        raise HTTPException(422, "Send the earnings plan in the operating-plan-v2 model")
    return payload.body


@router.get("/v2")
async def plan_workspace(profile_id: UUID, user: CurrentUser, db: Db, family_id: UUID | None = None):
    """The plan in the v2 model with its figures. Reading never writes: a v1 plan is carried over in memory."""
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    from app.services import earnings_plan_service

    return await earnings_plan_service.workspace(db, profile, family_id=family_id, actor=user)


@router.post("/v2/evaluate")
async def plan_evaluate(profile_id: UUID, payload: PlanBody, user: CurrentUser, db: Db):
    """Figures for a plan as edited, without saving it. Reviews in the posted body are never trusted."""
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    from app.services import earnings_plan_service
    from app.services.financial_cockpit import load_cockpit

    current = await operating_plan_service.current_body(db, profile, payload.family_id)
    body = earnings_plan_service.protect_reviews(_plan_body(payload), current)
    evaluation = operating_plan_service.math.evaluate(body, await load_cockpit(db, profile))
    return {"body": body, "evaluation": evaluation, "completion": earnings_plan_service._completion(body, evaluation),
            "context_stamp": evaluation["context_stamp"]}


@router.post("/v2/resync")
async def plan_resync(profile_id: UUID, payload: PlanBody, user: CurrentUser, db: Db):
    """The same plan brought onto the file's current source figures. Returned, not saved."""
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    from app.services import earnings_plan_migrate, earnings_plan_service
    from app.services.financial_cockpit import load_cockpit

    cockpit = await load_cockpit(db, profile)
    current = await operating_plan_service.current_body(db, profile, payload.family_id)
    body = earnings_plan_service.protect_reviews(earnings_plan_migrate.resync(_plan_body(payload), cockpit), current)
    evaluation = operating_plan_service.math.evaluate(body, cockpit)
    return {"body": body, "evaluation": evaluation, "completion": earnings_plan_service._completion(body, evaluation),
            "context_stamp": evaluation["context_stamp"]}


@router.put("/v2/draft")
async def plan_save_draft(profile_id: UUID, payload: PlanDraft, user: CurrentUser, db: Db):
    """Save the working draft in the v2 model, creating the plan on its first save."""
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    from app.services import earnings_plan_service
    from app.services import financial_workspace as workspace

    body = _plan_body(payload)
    family, draft = await workspace.resolve_family(db, profile, kind="operating_plan", family_id=payload.family_id, actor=user)
    if payload.expected_revision is None:
        if draft.revision != 0:
            raise HTTPException(409, {"code": "financial_draft_conflict", "current_revision": draft.revision, "message": "This draft changed. Reload before saving."})
    else:
        workspace._check_revision(draft, payload.expected_revision)
    await workspace.stage_draft(db, profile, family, draft, body, actor=user)
    await db.commit()
    return await earnings_plan_service.workspace(db, profile, family_id=family.id, actor=user)


@router.post("/review")
async def review(profile_id: UUID, payload: Review, user: CurrentUser, db: Db):
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    profile = await profiles.load_profile(db, profile_id, user)
    result = await operating_plan_service.review(db, profile, user, **payload.model_dump())
    await db.commit()
    return result


@router.post("/suggestions")
async def suggestions(profile_id: UUID, payload: Suggest, user: CurrentUser, db: Db):
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    current = await operating_plan_service.current_body(db, profile, payload.family_id)
    return await operating_plan_service.suggest(db, profile, payload.body.model_dump(mode="json"), current=current,
        expense_row_id=payload.expense_row_id, actor_id=user.id)


@router.post("/suggestions/apply")
async def apply_suggestion(profile_id: UUID, payload: ApplySuggestion, user: CurrentUser, db: Db):
    staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    current = await operating_plan_service.current_body(db, profile, payload.family_id)
    return await operating_plan_service.suggest(db, profile, payload.body.model_dump(mode="json"), current=current,
        expense_row_id=payload.expense_row_id, actor_id=user.id, suggestion_id=payload.suggestion_id,
        context_stamp=payload.context_stamp)
