"""Advanced staff corrections of source-bound financial calculation inputs."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.schemas.financial_cockpit import FinancialCockpitRead
from app.services import application_profiles as profiles
from app.services import financial_cockpit, financial_metric_review

router = APIRouter(prefix="/application-profiles/{profile_id}/financial-metrics", tags=["financial-metrics"])
Db = Annotated[AsyncSession, Depends(get_db)]


class FinancialMetricCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    input_key: str = Field(min_length=1, max_length=200)
    action: Literal["correct", "restore"]
    value: float | None = None
    mark_unknown: bool = False
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)
    source_stamp: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("value", mode="before")
    @classmethod
    def numeric_not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("A financial correction must be numeric")
        return value


class FinancialMetricReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    input_key: str = Field(min_length=1, max_length=200)
    action: Literal["correct", "restore"]
    value: float | None = None
    mark_unknown: bool = False
    source_stamp: str = Field(min_length=64, max_length=64)

    @field_validator("value", mode="before")
    @classmethod
    def numeric_not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("A financial correction must be numeric")
        return value


class FinancialMetricBatchReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrections: list[FinancialMetricReviewInput] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128)


class FinancialMetricComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_key: str = Field(min_length=1, max_length=200)
    included: bool


class FinancialMetricAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    amount: float

    @field_validator("amount", mode="before")
    @classmethod
    def numeric_not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("An adjustment must be numeric")
        return value


class FinancialMetricComposition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calculation_key: str = Field(min_length=1, max_length=200)
    action: Literal["compose", "restore"]
    components: list[FinancialMetricComponent] = Field(default_factory=list, max_length=60)
    adjustments: list[FinancialMetricAdjustment] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)
    source_stamp: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)


@router.get("", response_model=FinancialCockpitRead)
async def read_metrics(profile_id: UUID, user: CurrentUser, db: Db):
    if user.role in {Role.CLIENT, Role.DEALER, Role.LENDER, Role.VENDOR, Role.CAPITAL_PARTNER}:
        raise HTTPException(403, "Financial metric review requires a staff account")
    profile = await profiles.load_profile(db, profile_id, user)
    return await financial_cockpit.load_cockpit(db, profile)


@router.post("/corrections", response_model=FinancialCockpitRead)
async def correct_metrics(profile_id: UUID, payload: FinancialMetricCorrection, user: CurrentUser, db: Db):
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    profile = await profiles.load_profile(db, profile_id, user)
    cockpit = await financial_metric_review.correct(db, profile, user, **payload.model_dump())
    await db.commit()
    return cockpit


@router.post("/corrections/batch", response_model=FinancialCockpitRead)
async def review_inputs(profile_id: UUID, payload: FinancialMetricBatchReview, user: CurrentUser, db: Db):
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    profile = await profiles.load_profile(db, profile_id, user)
    cockpit = await financial_metric_review.save_review(db, profile, user, **payload.model_dump())
    await db.commit()
    return cockpit


@router.post("/compositions", response_model=FinancialCockpitRead)
async def review_composition(profile_id: UUID, payload: FinancialMetricComposition, user: CurrentUser, db: Db):
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    profile = await profiles.load_profile(db, profile_id, user)
    cockpit = await financial_metric_review.save_review(db, profile, user, composition=True, **payload.model_dump())
    await db.commit()
    return cockpit
