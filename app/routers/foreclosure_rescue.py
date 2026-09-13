# FastAPI dependency injection intentionally uses callable defaults.
# ruff: noqa: B008
"""Fast, two-part commercial foreclosure-rescue intake."""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.dealer_os.services import client_room
from app.deps import CurrentUser
from app.enums import ContractSubjectType, ContractType, Role
from app.models.bucket import (
    Bucket,
    BucketActivityLog,
    BucketNote,
    BucketRequestedDocument,
    BucketUploadLink,
)
from app.models.contract_agreement import ContractAgreement
from app.models.professional_partner_application import ProfessionalPartnerApplication
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.referral_partner_company import KIND_REFERRAL_PARTNER, ReferralPartnerCompany
from app.models.user import User
from app.routers.buckets import _generate_passcode
from app.routers.dealer_ai_intake import _hash_token, _new_public_token, _record_resume_email
from app.services import clerk as clerk_service
from app.services.foreclosure_rescue import (
    AMORTIZATION_MONTHS,
    CLOSING_DOCUMENTS,
    FORECLOSURE_RESCUE_VARIANT,
    INITIAL_REVIEW_DOCUMENTS,
    MAX_LTV_PCT,
    NOTE_RATE_PCT,
    PROCEEDS_POLICY,
    STATUS_LABELS,
    TERM_MONTHS,
    program_terms,
    urgency_for,
)
from app.services.payment_authorization import primary_super_admin

public_router = APIRouter(prefix="/public/foreclosure-rescue", tags=["public-foreclosure-rescue"])
operator_router = APIRouter(prefix="/foreclosure-rescues", tags=["foreclosure-rescue-operations"])
partner_router = APIRouter(prefix="/professional/foreclosure-rescues", tags=["professional-foreclosure-rescue"])

SubmitterType = Literal["attorney", "broker", "owner_direct"]
PropertyType = Literal["multifamily_5_plus", "mixed_use", "retail", "industrial", "office", "other"]
LegalStatus = Literal["notice_of_default", "lawsuit_lis_pendens", "chapter_11_subchapter_v", "auction_sale_scheduled"]
ExitStrategy = Literal["market_and_sell", "conventional_refinance", "partner_buyout_cash_infusion"]


class ForeclosureRescueIntakeCreate(BaseModel):
    submitter_type: SubmitterType
    contact_name: str = Field(min_length=1, max_length=180)
    firm_name: str | None = Field(default=None, max_length=180)
    contact_phone: str = Field(min_length=7, max_length=48)
    contact_email: EmailStr
    borrower_name: str = Field(min_length=1, max_length=180)
    client_email: EmailStr | None = None
    client_phone: str | None = Field(default=None, min_length=7, max_length=48)
    holding_entity: str = Field(min_length=1, max_length=180)
    ownership_structure: str = Field(min_length=1, max_length=1200)
    property_addresses: list[str] = Field(min_length=1, max_length=30)
    parcel_building_count: int = Field(ge=1, le=5000)
    property_type: PropertyType
    property_type_other: str | None = Field(default=None, max_length=120)
    occupancy_rate_pct: float = Field(ge=0, le=100)
    estimated_market_value: float = Field(gt=0)
    senior_lender: str = Field(min_length=1, max_length=180)
    payoff_balance: float = Field(gt=0)
    legal_statuses: list[LegalStatus] = Field(min_length=1)
    case_docket_number: str | None = Field(default=None, max_length=180)
    scheduled_sale_date: date | None = None
    has_other_liens_or_back_taxes: bool
    other_liens_amount: float | None = Field(default=None, ge=0)
    requested_loan_amount: float = Field(gt=0)
    exit_strategy: ExitStrategy
    selling_broker: str | None = Field(default=None, max_length=180)
    narrative: str | None = Field(default=None, max_length=3000)
    authority_attested: bool
    owner_contact_consent: bool = False
    terms_accepted: bool
    privacy_accepted: bool

    @model_validator(mode="after")
    def validate_conditional_fields(self):
        if self.submitter_type != "owner_direct" and (not self.client_email or not self.client_phone):
            raise ValueError("Client email and phone are required for attorney and broker submissions")
        if self.submitter_type == "owner_direct" and not self.owner_contact_consent:
            raise ValueError("Owner-direct submissions require contact consent")
        if "auction_sale_scheduled" in self.legal_statuses and self.scheduled_sale_date is None:
            raise ValueError("Scheduled sale date is required when an auction or sale is scheduled")
        if self.has_other_liens_or_back_taxes and self.other_liens_amount is None:
            raise ValueError("Estimated other liens or back taxes are required when Yes is selected")
        if self.property_type == "other" and not (self.property_type_other or "").strip():
            raise ValueError("Describe the property type")
        if not self.authority_attested or not self.terms_accepted or not self.privacy_accepted:
            raise ValueError("Authority, Terms, and Privacy acknowledgments are required")
        self.property_addresses = [item.strip() for item in self.property_addresses if item.strip()]
        if not self.property_addresses:
            raise ValueError("At least one property address is required")
        return self


class RescueStatusUpdate(BaseModel):
    status: Literal[
        "new_rescue", "initial_docs_pending", "ready_for_initial_review", "in_underwriting",
        "term_sheet_issued", "closing_docs_pending", "clear_to_close", "funded", "declined",
        "withdrawn_expired",
    ]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def terminal_reason(self):
        if self.status in {"declined", "withdrawn_expired"} and not (self.reason or "").strip():
            raise ValueError("A reason is required for declined or withdrawn files")
        return self


class RescueAssignmentUpdate(BaseModel):
    assigned_underwriter_user_id: UUID | None


class RescueDocumentStatusUpdate(BaseModel):
    status: Literal["missing", "requested", "received_unverified", "verified", "waived", "not_applicable"]
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def disposition_reason(self):
        if self.status in {"waived", "not_applicable"} and not (self.reason or "").strip():
            raise ValueError("A reason is required when a requirement is waived or not applicable")
        return self


class ProfessionalMemberCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)


class ProfessionalMemberRead(BaseModel):
    id: UUID
    name: str
    email: str
    phone: str | None
    is_company_admin: bool
    active: bool


class ProfessionalMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class ProfessionalMessageRead(BaseModel):
    id: UUID
    author_name: str
    content: str
    created_at: datetime


class ProfessionalUploadLinkRead(BaseModel):
    url: str
    passcode: str


class ForeclosureRescueCreated(BaseModel):
    id: UUID
    status: str
    status_label: str
    urgency: dict[str, str | int | None]
    resume_url: str
    program: dict[str, str | int | float]


class ProfessionalPartnerApplicationCreate(BaseModel):
    company_name: str = Field(min_length=2, max_length=255)
    firm_type: Literal["law_firm", "mortgage_brokerage", "receivership", "restructuring_advisory", "other"]
    specialties: list[str] = Field(min_length=1, max_length=12)
    geographic_states: list[str] = Field(min_length=1, max_length=60)
    estimated_annual_referrals: int | None = Field(default=None, ge=0, le=100000)
    website: str | None = Field(default=None, max_length=320)
    contact_name: str = Field(min_length=2, max_length=180)
    contact_title: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr
    contact_phone: str = Field(min_length=7, max_length=48)
    notes: str | None = Field(default=None, max_length=3000)
    consent: bool

    @model_validator(mode="after")
    def consent_required(self):
        if not self.consent:
            raise ValueError("Consent to be contacted is required")
        return self


class ProfessionalPartnerApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_name: str
    firm_type: str
    specialties: list[str]
    geographic_states: list[str]
    estimated_annual_referrals: int | None
    website: str | None
    contact_name: str
    contact_title: str | None
    contact_email: str
    contact_phone: str
    notes: str | None
    status: str
    review_notes: str | None
    promoted_company_id: UUID | None
    promoted_user_id: UUID | None
    created_at: datetime


class ProfessionalPartnerDecision(BaseModel):
    decision: Literal["approved", "denied"]
    review_notes: str | None = Field(default=None, max_length=3000)


class RescueTermSheetIssue(BaseModel):
    approved_amount: float = Field(gt=0)
    conditions: list[str] = Field(default_factory=list, max_length=50)


class RescueTermSheetRead(BaseModel):
    approved_amount: float
    note_rate_pct: float
    term_months: int
    amortization_months: int
    proceeds_policy: str
    conditions: list[str]
    issued_at: datetime


class RescueDocumentRead(BaseModel):
    id: UUID
    name: str
    category: str | None
    required: bool
    status: str


class ForeclosureRescueRead(BaseModel):
    id: UUID
    status: str
    status_label: str
    urgency: dict[str, str | int | None]
    sale_date: date | None
    property_addresses: list[str]
    property_type: str
    payoff_balance: float
    estimated_market_value: float
    calculated_ltv_pct: float
    requested_loan_amount: float
    submitter_type: str
    submitter_name: str
    submitter_email: str
    firm_name: str | None
    borrower_name: str
    holding_entity: str
    assigned_underwriter_user_id: UUID | None
    assigned_underwriter_name: str | None
    client_contact_suppressed: bool
    documents: list[RescueDocumentRead]
    messages: list[ProfessionalMessageRead]
    created_at: datetime
    updated_at: datetime


def _require_operator(user: User) -> None:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Underwriting access required")


async def _require_partner(user: User, db: AsyncSession) -> UUID:
    if user.role != Role.PROFESSIONAL_REFERRAL_PARTNER or user.referral_partner_company_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Professional partner access required")
    individual_signed = (
        await db.execute(
            select(ContractAgreement.id).where(
                ContractAgreement.contract_type == ContractType.PLATFORM_ACCESS,
                ContractAgreement.subject_type == ContractSubjectType.USER,
                ContractAgreement.subject_id == user.id,
            )
        )
    ).first()
    if individual_signed is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign the Platform Access Agreement before using the workspace")
    company_signed = (
        await db.execute(
            select(ContractAgreement.id).where(
                ContractAgreement.contract_type == ContractType.REFERRAL_PROTECTION,
                ContractAgreement.subject_type == ContractSubjectType.COMPANY,
                ContractAgreement.subject_id == user.referral_partner_company_id,
            )
        )
    ).first()
    if company_signed is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your firm must have a signed Referral Protection Agreement on file")
    return user.referral_partner_company_id


def _details(intake: PublicUnderwritingIntake) -> dict:
    state = intake.intake_state or {}
    value = state.get("foreclosure_rescue")
    return value if isinstance(value, dict) else {}


def _read(intake: PublicUnderwritingIntake) -> ForeclosureRescueRead:
    details = _details(intake)
    value = float(details.get("estimated_market_value") or 0)
    payoff = float(details.get("payoff_balance") or 0)
    documents = [
        RescueDocumentRead(
            id=row.id,
            name=row.name,
            category=row.category,
            required=row.required,
            status="received_unverified" if row.status == "uploaded" else row.status,
        )
        for row in intake.bucket.requested_documents
    ]
    messages = [
        ProfessionalMessageRead(id=row.id, author_name=row.author_name, content=row.content, created_at=row.created_at)
        for row in sorted(intake.bucket.notes, key=lambda note: note.created_at)
        if row.visibility == "shared"
    ]
    status_key = intake.foreclosure_rescue_status or "new_rescue"
    return ForeclosureRescueRead(
        id=intake.id,
        status=status_key,
        status_label=STATUS_LABELS.get(status_key, status_key.replace("_", " ").title()),
        urgency=urgency_for(intake.foreclosure_sale_date),
        sale_date=intake.foreclosure_sale_date,
        property_addresses=list(details.get("property_addresses") or []),
        property_type=str(details.get("property_type") or "other"),
        payoff_balance=payoff,
        estimated_market_value=value,
        calculated_ltv_pct=round((payoff / value) * 100, 2) if value else 0,
        requested_loan_amount=float(intake.requested_loan_amount or 0),
        submitter_type=str(details.get("submitter_type") or "owner_direct"),
        submitter_name=intake.full_name,
        submitter_email=intake.email,
        firm_name=details.get("firm_name"),
        borrower_name=str(details.get("borrower_name") or intake.business_name or ""),
        holding_entity=str(details.get("holding_entity") or intake.business_name or ""),
        assigned_underwriter_user_id=intake.assigned_underwriter_user_id,
        assigned_underwriter_name=intake.assigned_underwriter.name if intake.assigned_underwriter else None,
        client_contact_suppressed=intake.client_contact_suppressed,
        documents=documents,
        messages=messages,
        created_at=intake.created_at,
        updated_at=intake.updated_at,
    )


def _base_query():
    return (
        select(PublicUnderwritingIntake)
        .where(PublicUnderwritingIntake.variant == FORECLOSURE_RESCUE_VARIANT)
        .options(
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
            selectinload(PublicUnderwritingIntake.assigned_underwriter),
        )
    )


async def _audit(db: AsyncSession, intake: PublicUnderwritingIntake, user: User | None, action: str, detail: str) -> None:
    db.add(BucketActivityLog(
        bucket_id=intake.bucket_id,
        actor_user_id=user.id if user else None,
        actor_name=user.name if user else intake.full_name,
        actor_email=user.email if user else intake.email,
        actor_role=str(user.role) if user else "public_lead",
        action=action,
        target_type="foreclosure_rescue",
        target_id=str(intake.id),
        detail=detail,
        created_at=datetime.now(UTC),
    ))


@public_router.post("", response_model=ForeclosureRescueCreated, status_code=status.HTTP_201_CREATED)
async def create_foreclosure_rescue(
    payload: ForeclosureRescueIntakeCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ForeclosureRescueCreated:
    email = str(payload.contact_email).strip().lower()
    matching_user = (
        await db.execute(select(User).where(User.email == email, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    partner_user = matching_user if matching_user and matching_user.role == Role.PROFESSIONAL_REFERRAL_PARTNER else None
    owner = await primary_super_admin(db)
    bucket = Bucket(
        name=f"{payload.holding_entity} Foreclosure Rescue",
        bucket_type="commercial_foreclosure_rescue",
        client_name=payload.borrower_name,
        purpose="Commercial foreclosure bailout and payoff review",
        description="Two-part expedited rescue review; initial term-sheet evidence is separated from closing diligence.",
        ai_context={
            "review_type": FORECLOSURE_RESCUE_VARIANT,
            "deal_type": "commercial foreclosure rescue",
            "note_rate_pct": float(NOTE_RATE_PCT),
            "term_months": TERM_MONTHS,
            "amortization_months": AMORTIZATION_MONTHS,
            "max_ltv_pct": float(MAX_LTV_PCT),
            "proceeds_policy": PROCEEDS_POLICY,
            "client_contact_suppressed": payload.submitter_type != "owner_direct",
        },
        created_by_id=owner.id if owner else None,
    )
    db.add(bucket)
    await db.flush()
    for document in INITIAL_REVIEW_DOCUMENTS:
        db.add(BucketRequestedDocument(
            bucket_id=bucket.id,
            name=document["name"],
            category="Initial Review",
            description=document["description"],
            required=bool(document["required"]),
            allow_multiple_files=True,
            status="requested",
            is_custom=False,
        ))
    for name in CLOSING_DOCUMENTS:
        db.add(BucketRequestedDocument(
            bucket_id=bucket.id,
            name=name,
            category="Due Diligence & Closing",
            description="Required before closing when applicable to the file.",
            required=True,
            allow_multiple_files=True,
            status="requested",
            is_custom=False,
        ))
    link = BucketUploadLink(
        bucket_id=bucket.id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.contact_name.strip(),
        recipient_email=email,
        allow_notes=True,
        allow_multiple_sessions=True,
        can_use_ai_chat=True,
        can_view_ai_tasks=True,
    )
    client_room._store_passcode(link, _generate_passcode())
    db.add(link)
    await db.flush()
    token = _new_public_token()
    rescue_details = payload.model_dump(mode="json")
    rescue_details["client_email"] = str(payload.client_email) if payload.client_email else None
    intake = PublicUnderwritingIntake(
        source_kind="partner" if partner_user else "public_form",
        source_detail="Commercial foreclosure rescue form",
        source_actor_name=payload.contact_name.strip(),
        source_user_id=partner_user.id if partner_user else None,
        referral_partner_company_id=partner_user.referral_partner_company_id if partner_user else None,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=_hash_token(token),
        variant=FORECLOSURE_RESCUE_VARIANT,
        status="collecting",
        foreclosure_rescue_status="new_rescue",
        foreclosure_sale_date=payload.scheduled_sale_date,
        client_contact_suppressed=payload.submitter_type != "owner_direct",
        full_name=payload.contact_name.strip(),
        email=email,
        phone=payload.contact_phone,
        business_name=payload.holding_entity,
        loan_purpose="commercial_foreclosure_bailout",
        requested_loan_amount=payload.requested_loan_amount,
        referral_source=payload.submitter_type,
        asset_rows=[{"address": address, "estimated_property_value": payload.estimated_market_value} for address in payload.property_addresses],
        intake_state={
            "messages": [],
            "source": "foreclosure_rescue",
            "foreclosure_rescue": rescue_details,
            "program_terms": program_terms(),
            "legal_acceptance": {
                "authority_attested": payload.authority_attested,
                "terms_accepted": payload.terms_accepted,
                "privacy_accepted": payload.privacy_accepted,
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        },
    )
    db.add(intake)
    await db.flush()
    await _audit(db, intake, partner_user, "foreclosure_rescue_created", "Five-minute foreclosure rescue intake submitted")
    await db.commit()
    await _record_resume_email(
        intake,
        token=token,
        request=request,
        reason="foreclosure_rescue_created",
        public_path="/funding-review",
        review_label="commercial foreclosure rescue",
        room_label="commercial foreclosure rescue file",
    )
    await db.commit()
    return ForeclosureRescueCreated(
        id=intake.id,
        status="new_rescue",
        status_label=STATUS_LABELS["new_rescue"],
        urgency=urgency_for(payload.scheduled_sale_date),
        resume_url=f"/funding-review?token={token}",
        program=program_terms(),
    )


@public_router.get("/program")
async def get_foreclosure_rescue_program() -> dict[str, str | int | float]:
    return program_terms()


@public_router.post("/partner-applications", response_model=ProfessionalPartnerApplicationRead, status_code=status.HTTP_201_CREATED)
async def create_professional_partner_application(
    payload: ProfessionalPartnerApplicationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ProfessionalPartnerApplication:
    row = ProfessionalPartnerApplication(
        **payload.model_dump(mode="json", exclude={"contact_email"}),
        contact_email=str(payload.contact_email).strip().lower(),
        status="pending",
        ip_address=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@operator_router.get("", response_model=list[ForeclosureRescueRead])
async def list_operator_rescues(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    rescue_status: str | None = None,
) -> list[ForeclosureRescueRead]:
    _require_operator(user)
    stmt = _base_query()
    if rescue_status:
        stmt = stmt.where(PublicUnderwritingIntake.foreclosure_rescue_status == rescue_status)
    stmt = stmt.order_by(PublicUnderwritingIntake.foreclosure_sale_date.asc().nullslast(), PublicUnderwritingIntake.created_at.asc())
    return [_read(row) for row in (await db.execute(stmt)).scalars().unique().all()]


@operator_router.get("/partner-applications", response_model=list[ProfessionalPartnerApplicationRead])
async def list_professional_partner_applications(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    application_status: str | None = None,
) -> list[ProfessionalPartnerApplication]:
    _require_operator(user)
    stmt = select(ProfessionalPartnerApplication)
    if application_status:
        stmt = stmt.where(ProfessionalPartnerApplication.status == application_status)
    return list((await db.execute(stmt.order_by(ProfessionalPartnerApplication.created_at.desc()))).scalars().all())


@operator_router.post("/partner-applications/{application_id}/decision", response_model=ProfessionalPartnerApplicationRead)
async def decide_professional_partner_application(
    application_id: UUID,
    payload: ProfessionalPartnerDecision,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProfessionalPartnerApplication:
    _require_operator(user)
    row = await db.get(ProfessionalPartnerApplication, application_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional partner application not found")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "This partner application has already been decided")
    row.status = payload.decision
    row.review_notes = payload.review_notes.strip() if payload.review_notes else None
    row.reviewed_by_id = user.id
    row.reviewed_at = datetime.now(UTC)
    if payload.decision == "approved":
        company = (
            await db.execute(select(ReferralPartnerCompany).where(ReferralPartnerCompany.name.ilike(row.company_name)))
        ).scalar_one_or_none()
        if company is None:
            company = ReferralPartnerCompany(
                name=row.company_name,
                kind=KIND_REFERRAL_PARTNER,
                notice_email=row.contact_email,
                notice_attention=row.contact_name,
                phone=row.contact_phone,
            )
            db.add(company)
            await db.flush()
        member = (await db.execute(select(User).where(User.email == row.contact_email))).scalar_one_or_none()
        if member is not None and member.referral_partner_company_id not in {None, company.id}:
            raise HTTPException(status.HTTP_409_CONFLICT, "This email is already linked to another organization")
        if member is not None and member.role not in {Role.CLIENT, Role.PROFESSIONAL_REFERRAL_PARTNER}:
            raise HTTPException(status.HTTP_409_CONFLICT, "This email already belongs to an operator account")
        if member is None:
            member = User(
                email=row.contact_email,
                name=row.contact_name,
                phone=row.contact_phone,
                role=Role.PROFESSIONAL_REFERRAL_PARTNER,
                clerk_id=None,
                referral_partner_company_id=company.id,
                referral_partner_company_admin=True,
                account_access_types=[],
            )
            db.add(member)
        else:
            member.deleted_at = None
            member.role = Role.PROFESSIONAL_REFERRAL_PARTNER
            member.referral_partner_company_id = company.id
            member.referral_partner_company_admin = True
        await db.flush()
        row.promoted_company_id = company.id
        row.promoted_user_id = member.id
        await db.execute(
            PublicUnderwritingIntake.__table__.update()
            .where(
                PublicUnderwritingIntake.variant == FORECLOSURE_RESCUE_VARIANT,
                PublicUnderwritingIntake.email == row.contact_email,
                PublicUnderwritingIntake.referral_partner_company_id.is_(None),
            )
            .values(referral_partner_company_id=company.id, source_user_id=member.id, source_kind="partner")
        )
        await clerk_service.invite_user(
            email=row.contact_email,
            name=row.contact_name,
            role=Role.PROFESSIONAL_REFERRAL_PARTNER,
            redirect_url=None,
            account_types=[],
            account_status="active",
        )
    await db.commit()
    await db.refresh(row)
    return row


@operator_router.patch("/{intake_id}/status", response_model=ForeclosureRescueRead)
async def update_rescue_status(
    intake_id: UUID,
    payload: RescueStatusUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ForeclosureRescueRead:
    _require_operator(user)
    intake = (await db.execute(_base_query().where(PublicUnderwritingIntake.id == intake_id))).scalars().unique().one_or_none()
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Foreclosure rescue not found")
    previous = intake.foreclosure_rescue_status or "new_rescue"
    intake.foreclosure_rescue_status = payload.status
    intake.status = {
        "new_rescue": "collecting", "initial_docs_pending": "reviewing", "ready_for_initial_review": "reviewing",
        "in_underwriting": "reviewed", "term_sheet_issued": "reviewed", "closing_docs_pending": "completed",
        "clear_to_close": "completed", "funded": "completed", "declined": "completed", "withdrawn_expired": "completed",
    }[payload.status]
    if payload.status == "funded":
        intake.outcome_status = "closed"
    elif payload.status == "declined":
        intake.outcome_status = "denied"
    elif payload.status == "withdrawn_expired":
        intake.outcome_status = "closed"
    await _audit(db, intake, user, "foreclosure_rescue_status_changed", f"{STATUS_LABELS[previous]} → {STATUS_LABELS[payload.status]}" + (f" · {payload.reason.strip()}" if payload.reason else ""))
    await db.commit()
    return _read(intake)


@operator_router.patch("/{intake_id}/assignment", response_model=ForeclosureRescueRead)
async def assign_rescue(
    intake_id: UUID,
    payload: RescueAssignmentUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ForeclosureRescueRead:
    _require_operator(user)
    intake = (await db.execute(_base_query().where(PublicUnderwritingIntake.id == intake_id))).scalars().unique().one_or_none()
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Foreclosure rescue not found")
    assignee = None
    if payload.assigned_underwriter_user_id:
        assignee = await db.get(User, payload.assigned_underwriter_user_id)
        if assignee is None or assignee.role not in {Role.LOAN_EXEC, Role.SUPER_ADMIN} or assignee.deleted_at is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Assignee must be an active underwriter or super admin")
    intake.assigned_underwriter_user_id = assignee.id if assignee else None
    intake.assigned_underwriter = assignee
    await _audit(db, intake, user, "foreclosure_rescue_assigned", f"Assigned to {assignee.name if assignee else 'unassigned queue'}")
    await db.commit()
    return _read(intake)


@operator_router.post("/{intake_id}/term-sheet", response_model=RescueTermSheetRead)
async def issue_rescue_term_sheet(
    intake_id: UUID,
    payload: RescueTermSheetIssue,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RescueTermSheetRead:
    _require_operator(user)
    intake = (
        await db.execute(_base_query().where(PublicUnderwritingIntake.id == intake_id))
    ).scalars().unique().one_or_none()
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Foreclosure rescue not found")
    issued_at = datetime.now(UTC)
    terms = {
        **program_terms(),
        "approved_amount": payload.approved_amount,
        "conditions": [condition.strip() for condition in payload.conditions if condition.strip()],
        "issued_at": issued_at.isoformat(),
        "issued_by_user_id": str(user.id),
    }
    state = dict(intake.intake_state or {})
    state["foreclosure_rescue_term_sheet"] = terms
    intake.intake_state = state
    intake.foreclosure_rescue_status = "term_sheet_issued"
    intake.status = "reviewed"
    await _audit(db, intake, user, "foreclosure_rescue_term_sheet_issued", f"Fixed 12.99% terms recorded for ${payload.approved_amount:,.2f}")
    await db.commit()
    return RescueTermSheetRead(**terms)


@operator_router.patch("/{intake_id}/documents/{document_id}", response_model=ForeclosureRescueRead)
async def update_rescue_document_status(
    intake_id: UUID,
    document_id: UUID,
    payload: RescueDocumentStatusUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ForeclosureRescueRead:
    _require_operator(user)
    intake = (
        await db.execute(_base_query().where(PublicUnderwritingIntake.id == intake_id))
    ).scalars().unique().one_or_none()
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Foreclosure rescue not found")
    requirement = next((row for row in intake.bucket.requested_documents if row.id == document_id), None)
    if requirement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document requirement not found")
    previous = requirement.status
    requirement.status = payload.status
    await _audit(
        db,
        intake,
        user,
        "foreclosure_rescue_document_status_changed",
        f"{requirement.name}: {previous} → {payload.status}"
        + (f" · {payload.reason.strip()}" if payload.reason else ""),
    )
    initial_rows = [row for row in intake.bucket.requested_documents if row.category == "Initial Review" and row.required]
    if initial_rows and all(row.status in {"verified", "waived", "not_applicable"} for row in initial_rows):
        if intake.foreclosure_rescue_status in {"new_rescue", "initial_docs_pending"}:
            intake.foreclosure_rescue_status = "ready_for_initial_review"
            intake.status = "reviewing"
            await _audit(
                db,
                intake,
                user,
                "foreclosure_rescue_auto_ready",
                "All applicable initial-review requirements are complete",
            )
    elif intake.foreclosure_rescue_status == "new_rescue":
        intake.foreclosure_rescue_status = "initial_docs_pending"
    await db.commit()
    return _read(intake)


@partner_router.get("", response_model=list[ForeclosureRescueRead])
async def list_partner_rescues(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[ForeclosureRescueRead]:
    company_id = await _require_partner(user, db)
    stmt = _base_query().where(PublicUnderwritingIntake.referral_partner_company_id == company_id).order_by(PublicUnderwritingIntake.updated_at.desc())
    return [_read(row) for row in (await db.execute(stmt)).scalars().unique().all()]


@partner_router.get("/members", response_model=list[ProfessionalMemberRead])
async def list_professional_members(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[ProfessionalMemberRead]:
    company_id = await _require_partner(user, db)
    rows = (
        await db.execute(
            select(User)
            .where(User.referral_partner_company_id == company_id, User.role == Role.PROFESSIONAL_REFERRAL_PARTNER)
            .order_by(User.name.asc())
        )
    ).scalars().all()
    return [
        ProfessionalMemberRead(
            id=row.id,
            name=row.name,
            email=row.email,
            phone=row.phone,
            is_company_admin=row.referral_partner_company_admin,
            active=row.deleted_at is None and row.account_status == "active",
        )
        for row in rows
    ]


@partner_router.post("/members", response_model=ProfessionalMemberRead, status_code=status.HTTP_201_CREATED)
async def invite_professional_member(
    payload: ProfessionalMemberCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProfessionalMemberRead:
    company_id = await _require_partner(user, db)
    if not user.referral_partner_company_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Firm administrator access required")
    email = str(payload.email).strip().lower()
    member = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if member is not None and member.deleted_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")
    if member is None:
        member = User(
            email=email,
            name=payload.name.strip(),
            phone=payload.phone,
            role=Role.PROFESSIONAL_REFERRAL_PARTNER,
            clerk_id=None,
            referral_partner_company_id=company_id,
            referral_partner_company_admin=False,
            account_access_types=[],
        )
        db.add(member)
    else:
        member.deleted_at = None
        member.account_status = "active"
        member.name = payload.name.strip()
        member.phone = payload.phone
        member.role = Role.PROFESSIONAL_REFERRAL_PARTNER
        member.referral_partner_company_id = company_id
        member.referral_partner_company_admin = False
    await db.flush()
    await clerk_service.invite_user(
        email=email,
        name=member.name,
        role=Role.PROFESSIONAL_REFERRAL_PARTNER,
        redirect_url=None,
        account_types=[],
        account_status="active",
    )
    await db.commit()
    return ProfessionalMemberRead(
        id=member.id, name=member.name, email=member.email, phone=member.phone,
        is_company_admin=False, active=True,
    )


@partner_router.delete("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_professional_member(
    member_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    company_id = await _require_partner(user, db)
    if not user.referral_partner_company_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Firm administrator access required")
    if member_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Firm administrators cannot deactivate themselves")
    member = await db.get(User, member_id)
    if member is None or member.referral_partner_company_id != company_id or member.role != Role.PROFESSIONAL_REFERRAL_PARTNER:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Firm member not found")
    member.deleted_at = datetime.now(UTC)
    member.account_status = "suspended"
    if member.clerk_id:
        await clerk_service.update_user_access_metadata(
            member.clerk_id,
            role=member.role,
            account_types=[],
            account_status="suspended",
        )
    await db.commit()


async def _partner_intake_or_404(db: AsyncSession, user: User, intake_id: UUID) -> PublicUnderwritingIntake:
    company_id = await _require_partner(user, db)
    stmt = _base_query().where(
        PublicUnderwritingIntake.id == intake_id,
        PublicUnderwritingIntake.referral_partner_company_id == company_id,
    )
    intake = (await db.execute(stmt)).scalars().unique().one_or_none()
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Foreclosure rescue not found")
    return intake


@partner_router.get("/{intake_id}", response_model=ForeclosureRescueRead)
async def get_partner_rescue(intake_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ForeclosureRescueRead:
    intake = await _partner_intake_or_404(db, user, intake_id)
    return _read(intake)


@partner_router.post("/{intake_id}/messages", response_model=ProfessionalMessageRead, status_code=status.HTTP_201_CREATED)
async def create_partner_message(
    intake_id: UUID,
    payload: ProfessionalMessageCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketNote:
    intake = await _partner_intake_or_404(db, user, intake_id)
    # Partner messages are internal file correspondence. They never target the
    # represented client and therefore cannot bypass contact suppression.
    note = BucketNote(
        bucket_id=intake.bucket_id,
        author_name=user.name,
        author_role=str(user.role),
        visibility="shared",
        channel="partner",
        content=payload.content.strip(),
    )
    db.add(note)
    await _audit(db, intake, user, "foreclosure_rescue_partner_message", "Professional partner added a file message")
    await db.commit()
    await db.refresh(note)
    return note


@partner_router.post("/{intake_id}/upload-link", response_model=ProfessionalUploadLinkRead)
async def create_partner_upload_link(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProfessionalUploadLinkRead:
    intake = await _partner_intake_or_404(db, user, intake_id)
    passcode = _generate_passcode()
    link = BucketUploadLink(
        bucket_id=intake.bucket_id,
        token=secrets.token_urlsafe(32),
        recipient_name=user.name,
        recipient_email=user.email,
        allow_notes=True,
        allow_multiple_sessions=True,
        can_use_ai_chat=True,
        can_view_ai_tasks=True,
    )
    client_room._store_passcode(link, passcode)
    db.add(link)
    await _audit(db, intake, user, "foreclosure_rescue_upload_link_created", "Professional partner opened a secure upload session")
    await db.commit()
    return ProfessionalUploadLinkRead(url=f"/buckets/request/{link.token}", passcode=passcode)
