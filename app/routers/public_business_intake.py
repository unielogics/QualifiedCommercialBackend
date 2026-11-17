"""Public term-loan application and program-aware, PIN-scoped AI review."""
# ruff: noqa: B008
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.dealer_ai_intake import ClientRoomPinChoice
from app.schemas.phone import RequiredPhone
from app.services import business_public_intake as service
from app.services.business_profile import RequiredBusinessProfileFields


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/public/business", tags=["public-business-intake"], dependencies=[Depends(no_store)])


class BusinessIntakeStart(RequiredBusinessProfileFields, ClientRoomPinChoice):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: UUID
    goal: Literal["term_loan"] = "term_loan"
    full_name: str = Field(min_length=2, max_length=160)
    business_name: str = Field(min_length=2, max_length=180)
    email: EmailStr
    phone: RequiredPhone
    employee_count: int = Field(ge=0, le=2147483647)
    business_phone: RequiredPhone
    secure_room_pin: str = Field(pattern=r"^[0-9]{6}$", repr=False)
    locale: Literal["en", "es"] = "en"
    requested_amount: float | None = Field(default=None, gt=0, le=100_000_000, allow_inf_nan=False)
    years_in_business: float | None = Field(default=None, ge=0, le=150, allow_inf_nan=False)
    credit_band: str | None = Field(default=None, max_length=40)
    consent: bool = True
    terms_accepted: Literal[True]
    privacy_accepted: Literal[True]
    application_email_consent: Literal[True]
    sms_transactional_consent: Literal[True]
    sms_marketing_consent: bool = False
    resource_handoff_token: str | None = Field(default=None, max_length=180, repr=False)
    attribution: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attribution")
    @classmethod
    def validate_attribution(cls, value):
        from app.services.resource_assessments import validate_json_object
        return validate_json_object(value)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class RoomAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passcode: str = Field(pattern=r"^[0-9]{6}$", repr=False)


class RoomChat(RoomAccess):
    message: str = Field(min_length=1, max_length=6000)

    @field_validator("message")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Enter a message.")
        return value.strip()


@router.post("/intake/start", status_code=201)
async def start_business_intake(payload: BusinessIntakeStart, request: Request, db: AsyncSession = Depends(get_db)):
    return await service.start(db, payload, request)


@router.post("/intake/resume")
async def resume_business_intake(payload: ResumeRequest, request: Request, db: AsyncSession = Depends(get_db)):
    return await service.resume(db, str(payload.email), request)


@router.post("/room/{token}/review")
async def business_room_review(token: str, payload: RoomAccess, request: Request, db: AsyncSession = Depends(get_db)):
    intake, link, profile = await service.authorize_room(db, token, payload.passcode, request)
    return await service.room_state(db, intake, link, profile)


@router.post("/room/{token}/chat")
async def business_room_chat(token: str, payload: RoomChat, request: Request, db: AsyncSession = Depends(get_db)):
    from app.routers import dealer_ai_intake as core
    intake, link, profile = await service.authorize_room(db, token, payload.passcode, request)
    core._throttle_or_429(service._CHATS, str(link.id), 2, "Please wait a moment before sending another message.")
    intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **core._main_street_context(intake),
                               **service.term_context()}
    locale = getattr(profile, "communication_locale", None)
    await core._client_chat_turn(db, intake, payload.message, actor_name=intake.full_name,
                                preferred_language=locale if locale in {"en", "es"} else None)
    await db.commit()
    return await service.room_state(db, intake, link, profile)


@router.post("/room/{token}/run")
async def business_room_run(token: str, payload: RoomAccess, request: Request, background: BackgroundTasks,
                            db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select

    from app.models.bucket import BucketAIReview
    from app.routers import dealer_ai_intake as core
    intake, link, profile = await service.authorize_room(db, token, payload.passcode, request)
    await db.execute(select(core.Bucket.id).where(core.Bucket.id == link.bucket_id).with_for_update())
    pending = (await db.execute(select(BucketAIReview).where(BucketAIReview.bucket_id == link.bucket_id,
        BucketAIReview.status.in_(["queued", "running", "processing"])).limit(1))).scalar_one_or_none()
    if pending is None:
        core._throttle_or_429(core._REVIEW_LAST_BY_TOKEN, str(link.id), core._REVIEW_MIN_INTERVAL_SECONDS,
                             "A review was just started. Please wait before starting another.")
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **service.term_context()}
        review = await core._create_queued_review(db, intake, request=request, actor_name=intake.full_name,
            actor_email=intake.email, actor_role="uploader", log_event="business_term_review_requested",
            detail="Client requested a term-loan review of the business evidence")
        background.add_task(core._run_review_background, review.id, intake.id)
    return await service.room_state(db, intake, link, profile)
