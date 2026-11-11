"""Public MCA acquisition form, saved into the existing CRM lead queue.

This is a request for a funding review, not an approval or a credit application.
Qualification is enforced before writes; no new database tables are required.
"""
from __future__ import annotations

# FastAPI dependencies are evaluated by the framework.
# ruff: noqa: B008
import time
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import request_context
from app.db import get_db
from app.models.activity import Activity
from app.models.client import Client
from app.schemas.phone import RequiredPhone
from app.services import mca_funding_policy
from app.services.payment_authorization import primary_super_admin

router = APIRouter(prefix="/public/mca", tags=["public-mca"])
CONSENT_VERSION = "mca-funding-review-v2"
CONSENT_TEXT = (
    "I authorize Qualified Commercial to contact me by phone or email about this funding review. "
    "I confirm that my business has no more than 2 existing active MCAs, no modified active MCA, "
    "and nonnegative reported ordinary business income on its prior-year business tax return before addbacks. "
    "I have read the Privacy Policy and Terms."
)
CONSENT_TEXT_ES = (
    "Autorizo a Qualified Commercial a contactarme por teléfono o correo sobre esta revisión. "
    "Confirmo que mi negocio tiene como máximo 2 MCA vigentes existentes, ningún MCA vigente modificado "
    "e ingresos ordinarios reportados no negativos en su declaración de impuestos del año anterior, antes de ajustes. "
    "He leído la Política de Privacidad y los Términos."
)
_LAST_SUBMIT: dict[str, float] = {}
_THROTTLE_SECONDS = 20
Range = Literal["under_25k", "25k_50k", "50k_100k", "100k_250k", "250k_plus"]


class Attribution(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: Literal["public_site"] = "public_site"
    page: str = Field(max_length=256, pattern=r"^/(?:es/)?merchant-cash-advance/?$")
    utm_source: str | None = Field(default=None, max_length=256)
    utm_medium: str | None = Field(default=None, max_length=256)
    utm_campaign: str | None = Field(default=None, max_length=256)
    utm_term: str | None = Field(default=None, max_length=256)
    utm_content: str | None = Field(default=None, max_length=256)
    gclid: str | None = Field(default=None, max_length=256)
    gbraid: str | None = Field(default=None, max_length=256)
    wbraid: str | None = Field(default=None, max_length=256)


class McaLeadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: UUID
    full_name: str = Field(min_length=2, max_length=160)
    business_name: str = Field(min_length=2, max_length=180)
    email: EmailStr
    phone: RequiredPhone
    # Strict rejects booleans, decimal counts, and string-based gate bypasses.
    active_mca_count: int = Field(strict=True, ge=0, le=2)
    has_modified_mca: bool = Field(strict=True)
    last_year_tax_earnings: Literal["nonnegative", "negative", "unknown"]
    tax_year: int = Field(strict=True)
    requested_amount: Range | Literal["not_sure"]
    monthly_revenue: Range
    time_in_business: Literal["under_6_months", "6_12_months", "1_2_years", "2_plus_years"]
    consent: Literal[True]
    locale: Literal["en", "es"] = "en"
    attribution: Attribution
    website: str = Field(default="", max_length=0)

    @field_validator("has_modified_mca")
    @classmethod
    def unmodified_active_mcas(cls, value: bool) -> bool:
        if value:
            raise ValueError("This product requires no modified active MCA.")
        return value

    @field_validator("last_year_tax_earnings")
    @classmethod
    def nonnegative_reported_income(cls, value: str) -> str:
        if value != "nonnegative":
            raise ValueError("Confirm nonnegative reported ordinary business tax income before addbacks.")
        return value

    @field_validator("tax_year")
    @classmethod
    def prior_calendar_tax_year(cls, value: int) -> int:
        if value != mca_funding_policy.required_tax_year():
            raise ValueError(f"Use the {mca_funding_policy.required_tax_year()} business tax return.")
        return value


class McaLeadResult(BaseModel):
    status: Literal["received"] = "received"


def _lead_id(key: UUID) -> UUID:
    # Deterministic primary key makes retries atomic without a schema migration.
    # Domain separation keeps caller-provided UUIDs out of the CRM's ID space.
    return uuid5(NAMESPACE_URL, f"https://qualifiedcommercial.com/public/mca/leads/{key}")


def _check_replay(client: Client, payload: McaLeadRequest) -> None:
    if client.source_channel != "mca_funding" or (client.email or "").casefold() != str(payload.email).casefold():
        raise HTTPException(status.HTTP_409_CONFLICT, "Start a new request for these contact details.")
    saved = client.lead_intake if isinstance(getattr(client, "lead_intake", None), dict) else {}
    if (saved.get("consent_version") != CONSENT_VERSION
            or (saved.get("mca_product_policy") or {}).get("policy_version") != mca_funding_policy.POLICY_VERSION
            or any(saved.get(key) != getattr(payload, key) for key in (
                "active_mca_count", "has_modified_mca", "last_year_tax_earnings", "tax_year"))):
        raise HTTPException(status.HTTP_409_CONFLICT, "Start a new request for the updated MCA policy answers.")


@router.post("/leads", status_code=status.HTTP_201_CREATED, response_model=McaLeadResult)
async def capture_mca_lead(
    payload: McaLeadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> McaLeadResult:
    client_id = _lead_id(payload.idempotency_key)
    existing = await db.get(Client, client_id)
    if existing is not None:
        _check_replay(existing, payload)
        return McaLeadResult()

    ip = request_context.client_ip(request) or "unknown"
    now = time.monotonic()
    last = _LAST_SUBMIT.get(ip)
    if last is not None and now - last < _THROTTLE_SECONDS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Please wait a moment before submitting again.")
    if len(_LAST_SUBMIT) > 10000:
        expired = [key for key, captured in _LAST_SUBMIT.items() if now - captured >= _THROTTLE_SECONDS]
        for key in expired:
            _LAST_SUBMIT.pop(key, None)
    _LAST_SUBMIT[ip] = now

    owner = await primary_super_admin(db)
    lead_details = payload.model_dump(mode="json", exclude={"idempotency_key", "website", "full_name", "email", "phone"})
    lead_details.update({
        "funding_intent": "merchant_cash_advance",
        "consent_version": CONSENT_VERSION,
        "consent_text": CONSENT_TEXT_ES if payload.locale == "es" else CONSENT_TEXT,
        "consented_at": datetime.now(UTC).isoformat(),
        "allowed_contact_channels": ["phone", "email"],
        "preferred_contact_channel": "email",
        "review_status": "pending_review",
        "mca_product_policy": mca_funding_policy.declaration_metadata(),
    })
    client = Client(
        id=client_id,
        name=payload.full_name,
        email=str(payload.email),
        phone=payload.phone,
        referral_source="mca_funding",
        source_channel="mca_funding",
        lead_source="other",
        lead_temperature="warm",
        financing_support_needed="yes",
        # Existing manual-introduction workflow permits operator follow-up,
        # surfaces a human-action alert, and triggers no invitation or SMS.
        contact_permission="agent_will_introduce_first",
        relationship_context="new_lead",
        originating_agent_id=owner.id if owner else None,
        current_agent_id=owner.id if owner else None,
        stage="lead",
        language="Spanish" if payload.locale == "es" else "English",
        lead_intake=lead_details,
    )
    try:
        db.add(client)
        await db.flush()
        db.add(Activity(
            client_id=client_id,
            actor_label="public_mca_lead",
            kind="mca.lead_received",
            summary=f"MCA funding review requested for {payload.business_name}",
            payload={"funding_intent": "merchant_cash_advance", "active_mca_count": payload.active_mca_count, "consent_version": CONSENT_VERSION},
        ))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.get(Client, client_id)
        if existing is None:
            raise
        _check_replay(existing, payload)
    return McaLeadResult()


class McaIntakeAttribution(Attribution):
    page: str = Field(default="/mca-funding-intake", max_length=256)
    entry_page: str | None = Field(default=None, max_length=256)
    campaign: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    cta: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    vertical: Literal["main_street"] = "main_street"
    program: Literal["merchant_cash_advance"] = "merchant_cash_advance"
    funding_intent: Literal["merchant_cash_advance"] = "merchant_cash_advance"
    profile: Literal["main_street_v1"] = "main_street_v1"

    @field_validator("page", "entry_page")
    @classmethod
    def safe_website_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = value.removesuffix("/") or "/"
        english = "/" if path == "/es" else path[3:] if path.startswith("/es/") else path
        if english not in {"/", "/programs", "/main-street", "/business-funding", "/merchant-cash-advance",
                           "/mca-funding-intake", "/programs/main-street", "/programs/merchant-cash-advance",
                           "/industries/main-street", "/working-capital-loans", "/manufacturing-business-loans",
                           "/medical-practice-financing", "/grocery-store-financing"}:
            raise ValueError("Use a known MCA or Main Street website path without query strings or fragments.")
        return path


class McaIntakeStart(McaLeadRequest):
    attribution: McaIntakeAttribution
    monthly_revenue: Range | None = None
    time_in_business: Literal["under_6_months", "6_12_months", "1_2_years", "2_plus_years"] | None = None
    active_mca_count: int | None = Field(default=None, strict=True, ge=0, le=2)
    has_modified_mca: bool | None = Field(default=None, strict=True)
    secure_room_pin: str = Field(pattern=r"^[0-9]{6}$", repr=False)
    terms_accepted: Literal[True]
    privacy_accepted: Literal[True]
    consent_version: Literal["mca-ai-intake-v3"] = "mca-ai-intake-v3"

    @field_validator("secure_room_pin")
    @classmethod
    def validate_room_pin(cls, value: str) -> str:
        from app.dealer_os.services.client_room import passcode_problem

        if problem := passcode_problem(value):
            raise ValueError(problem)
        return value

    @field_validator("has_modified_mca")
    @classmethod
    def unmodified_active_mcas(cls, value: bool | None) -> bool | None:
        if value is True:
            raise ValueError("This product requires no modified active MCA.")
        return value

    @field_validator("last_year_tax_earnings")
    @classmethod
    def nonnegative_reported_income(cls, value: str) -> str:
        if value == "negative":
            raise ValueError("This product requires nonnegative prior-year ordinary business tax income before addbacks.")
        return value


class McaIntakeResult(BaseModel):
    intake_id: UUID
    variant: Literal["main_street_v1"] = "main_street_v1"
    funding_intent: Literal["merchant_cash_advance"] = "merchant_cash_advance"
    room_url: str
    resume_url: str


class McaRoomAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passcode: str = Field(pattern=r"^[0-9]{6}$")


class McaRoomChat(McaRoomAccess):
    message: str = Field(min_length=1, max_length=6000)

    @field_validator("message")
    @classmethod
    def nonempty_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a message.")
        return value.strip()


class McaResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class McaReviewProgress(BaseModel):
    stage: str
    label: str
    percent: float | None = None
    files_total: int = 0
    files_done: int = 0


class McaReviewMessage(BaseModel):
    id: str
    role: str
    content: str
    sender_kind: str | None = None
    author_name: str | None = None
    created_at: datetime | None = None


class McaReviewRequirement(BaseModel):
    key: str
    label: str
    status: str
    received: int
    required: int
    unit: str


class McaReviewAction(BaseModel):
    kind: Literal["upload", "debt_schedule", "application"]
    label: str


class McaReviewState(BaseModel):
    funding_intent: Literal["merchant_cash_advance"] = "merchant_cash_advance"
    business_name: str | None = None
    preferred_language: Literal["en", "es"] = "en"
    payment_methods_enabled: bool = False
    status: Literal["not_started", "queued", "running", "completed", "failed"]
    progress: McaReviewProgress
    messages: list[McaReviewMessage] = Field(default_factory=list)
    summary: str | None = None
    requirements: list[McaReviewRequirement] = Field(default_factory=list)
    actions: list[McaReviewAction] = Field(default_factory=list)
    policy_status: Literal["pending_review", "needs_information", "not_eligible", "ready_for_staff_review"]


@router.post("/intake/start", status_code=201, response_model=McaIntakeResult)
async def start_mca_intake(payload: McaIntakeStart, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services import mca_public_intake

    return await mca_public_intake.start(db, payload, request)


@router.post("/intake/resume")
async def resume_mca_intake(payload: McaResumeRequest, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services import mca_public_intake

    return await mca_public_intake.resume(db, str(payload.email), request)


@router.post("/room/{token}/review", response_model=McaReviewState)
async def mca_room_review(token: str, payload: McaRoomAccess, request: Request, db: AsyncSession = Depends(get_db)):
    from app.services import mca_public_intake

    intake, link, profile = await mca_public_intake.authorize_room(db, token, payload.passcode, request)
    return await mca_public_intake.room_state(db, intake, link, profile)


@router.post("/room/{token}/chat", response_model=McaReviewState)
async def mca_room_chat(token: str, payload: McaRoomChat, request: Request, db: AsyncSession = Depends(get_db)):
    from app.routers import dealer_ai_intake as core
    from app.services import mca_public_intake

    intake, link, profile = await mca_public_intake.authorize_room(db, token, payload.passcode, request)
    core._throttle_or_429(mca_public_intake._CHATS, str(link.id), 2,
                         "Please wait a moment before sending another message.")
    intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **core._main_street_context(intake)}
    locale = getattr(profile, "communication_locale", None)
    await core._client_chat_turn(db, intake, payload.message.strip(), actor_name=intake.full_name,
                                preferred_language=locale if locale in {"en", "es"} else None)
    await db.commit()
    return await mca_public_intake.room_state(db, intake, link, profile)


@router.post("/room/{token}/run", response_model=McaReviewState)
async def mca_room_run(token: str, payload: McaRoomAccess, request: Request, background: BackgroundTasks,
                       db: AsyncSession = Depends(get_db)):
    from app.models.bucket import BucketAIReview
    from app.routers import dealer_ai_intake as core
    from app.services import mca_public_intake

    intake, link, profile = await mca_public_intake.authorize_room(db, token, payload.passcode, request)
    # One in-flight review per bucket, even across distinct authorized links.
    await db.execute(select(core.Bucket.id).where(core.Bucket.id == link.bucket_id).with_for_update())
    pending = (await db.execute(select(BucketAIReview).where(BucketAIReview.bucket_id == link.bucket_id,
        BucketAIReview.status.in_(["queued", "running", "processing"])).limit(1))).scalar_one_or_none()
    if pending is None:
        core._throttle_or_429(core._REVIEW_LAST_BY_TOKEN, str(link.id), core._REVIEW_MIN_INTERVAL_SECONDS,
                             "A review was just started. Please wait before starting another.")
        review = await core._create_queued_review(db, intake, request=request, actor_name=intake.full_name,
            actor_email=intake.email, actor_role="uploader", log_event="mca_funding_review_requested",
            detail="Client requested review of the standard MCA evidence")
        background.add_task(core._run_review_background, review.id, intake.id)
    return await mca_public_intake.room_state(db, intake, link, profile)
