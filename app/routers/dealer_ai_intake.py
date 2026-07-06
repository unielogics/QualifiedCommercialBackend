from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, with_loader_criteria

from app.db import get_db
from app.deps import CurrentUser
from app.enums import CalendarEventKind, CalendarEventSource, CalendarEventStatus, Role
from app.models.activity import Activity
from app.models.booking_settings import BookingSettings
from app.models.bucket import Bucket, BucketAIReview, BucketFile, BucketNote, BucketRequestedDocument, BucketUploadLink
from app.models.client import Client
from app.models.event import CalendarEvent
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers.public import _available_booking_slots, _to_utc_minute
from app.routers.buckets import (
    _bucket_storage_config,
    _generate_passcode,
    _hash_passcode,
    _log,
    _public_url,
    _safe_filename,
    _upload_url,
)
from app.schemas.bucket import (
    BucketAIMessageRead,
    BucketAIReviewRead,
    BucketFileRead,
    BucketFileUploadInitResponse,
    BucketRequestUploadedFileRead,
    BucketRequestedDocumentRead,
)
from app.schemas.common import ORMModel
from app.services.bucket_ai import create_chat_reply, latest_review, run_bucket_ai_review, upload_link_visible_summary
from app.services.email.ses_client import send_email
from app.services.payment_authorization import primary_super_admin


router = APIRouter(prefix="/public/dealer-ai-intake", tags=["dealer-ai-intake"])
client_router = APIRouter(prefix="/buckets/client/intakes", tags=["client-bucket-intakes"])

TERMS_VERSION = "2026-05-19"
PRIVACY_VERSION = "2026-05-19"


REQUIRED_DOCUMENTS = [
    {
        "name": "Last 2 years tax returns",
        "category": "Financials",
        "description": "Upload business and personal tax returns for the last two years where available.",
        "allow_multiple_files": True,
    },
    {
        "name": "Current year P&L",
        "category": "Financials",
        "description": "Upload the current year profit and loss statement for the dealership/business.",
        "allow_multiple_files": True,
    },
    {
        "name": "Last 3 months bank statements",
        "category": "Bank Statements",
        "description": "Upload the last three months of business bank statements.",
        "allow_multiple_files": True,
    },
    {
        "name": "Asset and real estate schedule",
        "category": "Collateral",
        "description": "List owned real estate and assets, including estimated property values and current loan balances.",
        "allow_multiple_files": True,
    },
    {
        "name": "Mortgage notes or payoff statements",
        "category": "Collateral",
        "description": "Upload mortgage notes, payoff statements, or loan statements for real estate collateral where available.",
        "allow_multiple_files": True,
    },
]


class DealerAssetRow(BaseModel):
    id: str | None = None
    address: str = Field(default="", max_length=320)
    estimated_loan_amount: float | None = Field(default=None, ge=0)
    estimated_property_value: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)


class DealerEntityStructure(BaseModel):
    primary_operating_entity: str | None = Field(default=None, max_length=180)
    main_operating_bank_account: str | None = Field(default=None, max_length=180)
    related_entities: str | None = Field(default=None, max_length=1200)
    relationship_explanation: str | None = Field(default=None, max_length=1600)

    @field_validator("primary_operating_entity", "main_operating_bank_account", "related_entities", "relationship_explanation", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        return None if value == "" else value


class DealerIntakeStart(BaseModel):
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=48)
    business_name: str | None = Field(default=None, max_length=180)
    terms_accepted: bool = False
    privacy_accepted: bool = False
    terms_version: str = Field(default=TERMS_VERSION, max_length=32)
    privacy_version: str = Field(default=PRIVACY_VERSION, max_length=32)

    @field_validator("business_name", "phone", mode="before")
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


class DealerIntakePatch(BaseModel):
    business_name: str | None = Field(default=None, max_length=180)
    phone: str | None = Field(default=None, max_length=48)
    loan_purpose: str | None = Field(default=None, max_length=255)
    requested_loan_amount: float | None = Field(default=None, ge=0)
    estimated_credit_score: int | None = Field(default=None, ge=300, le=850)
    referral_source: str | None = Field(default=None, max_length=180)
    asset_rows: list[DealerAssetRow] | None = None
    entity_structure: DealerEntityStructure | None = None

    @field_validator("business_name", "phone", "loan_purpose", "referral_source", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        return None if value == "" else value


class DealerChatRequest(BaseModel):
    message: str | None = Field(default=None, max_length=4000)
    updates: DealerIntakePatch | None = None


class DealerResumeLinkRequest(BaseModel):
    email: EmailStr


class DealerResumeLinkResponse(BaseModel):
    ok: bool = True
    message: str = "If a matching secure intake exists, a resume link has been sent."


class DealerFileUploadInit(BaseModel):
    requested_document_id: UUID | None = None
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = "application/octet-stream"
    size_bytes: int = Field(default=0, ge=0)


class DealerUploadComplete(BaseModel):
    file_id: UUID
    note: str | None = Field(default=None, max_length=2000)


class DealerBookCallRequest(BaseModel):
    starts_at: datetime


class DealerIntakeRead(ORMModel):
    id: UUID
    client_id: UUID | None
    bucket_id: UUID
    bucket_upload_link_id: UUID | None
    latest_review_id: UUID | None
    variant: str
    status: str
    full_name: str
    email: str
    phone: str | None
    business_name: str | None
    loan_purpose: str | None
    requested_loan_amount: float | None
    estimated_credit_score: int | None
    referral_source: str | None
    asset_rows: list[dict[str, Any]] | None
    intake_state: dict[str, Any] | None
    result_snapshot: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class DealerIntakeResponse(BaseModel):
    intake: DealerIntakeRead
    token: str | None = None
    resume_url: str | None = None
    upload_url: str | None = None
    assistant_message: str
    widget: dict[str, Any] | None
    requested_documents: list[BucketRequestedDocumentRead]
    files: list[BucketRequestUploadedFileRead]
    ai_summary: dict[str, Any] | None = None
    latest_review: BucketAIReviewRead | None = None
    messages: list[BucketAIMessageRead] = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_public_token() -> str:
    return secrets.token_urlsafe(32)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _request_audit(request: Request) -> dict[str, Any]:
    return {
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "timestamp": _now().isoformat(),
    }


def _record_resume_email(
    intake: PublicUnderwritingIntake,
    *,
    token: str,
    request: Request,
    reason: str,
) -> dict[str, Any]:
    resume_url = _public_url(f"/dealer-ai-underwriter?token={token}")
    subject = "Your Qualified Commercial dealer funding review link"
    body_text = (
        f"Hi {intake.full_name},\n\n"
        "Use this secure link to resume your Qualified Commercial dealer funding review:\n"
        f"{resume_url}\n\n"
        "This link opens your encrypted AI underwriting room for the dealer financing file. "
        "If you did not request this link, you can ignore this email.\n\n"
        "Qualified Commercial LLC"
    )
    body_html = (
        f"<p>Hi {intake.full_name},</p>"
        "<p>Use this secure link to resume your Qualified Commercial dealer funding review:</p>"
        f'<p><a href="{resume_url}">Resume dealer funding review</a></p>'
        "<p>This link opens your encrypted AI underwriting room for the dealer financing file. "
        "If you did not request this link, you can ignore this email.</p>"
        "<p>Qualified Commercial LLC</p>"
    )
    result = send_email(to_email=intake.email, subject=subject, body_text=body_text, body_html=body_html)
    record = {
        "reason": reason,
        "status": result.detail,
        "ok": result.ok,
        "message_id": result.message_id,
        "sent_at": _now().isoformat(),
        **_request_audit(request),
    }
    state = _intake_state(intake)
    deliveries = state.get("resume_email_deliveries")
    if not isinstance(deliveries, list):
        deliveries = []
    deliveries.append(record)
    state["resume_email"] = record
    state["resume_email_deliveries"] = deliveries[-10:]
    intake.intake_state = state
    return record


def _active_files(bucket: Bucket) -> list[BucketFile]:
    return [file for file in bucket.files if file.status == "uploaded" and file.deleted_at is None]


def _uploaded_doc_ids(bucket: Bucket) -> set[UUID]:
    return {file.requested_document_id for file in _active_files(bucket) if file.requested_document_id is not None}


def _missing_required_docs(bucket: Bucket) -> list[BucketRequestedDocument]:
    uploaded = _uploaded_doc_ids(bucket)
    return [doc for doc in bucket.requested_documents if doc.required and doc.id not in uploaded]


def _asset_rows(intake: PublicUnderwritingIntake) -> list[dict[str, Any]]:
    rows = intake.asset_rows if isinstance(intake.asset_rows, list) else []
    return [row for row in rows if isinstance(row, dict)]


def _intake_state(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    return dict(intake.intake_state or {})


def _entity_structure(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    raw = _intake_state(intake).get("entity_structure")
    return raw if isinstance(raw, dict) else {}


def _entity_structure_complete(intake: PublicUnderwritingIntake) -> bool:
    entity = _entity_structure(intake)
    return all(
        str(entity.get(key) or "").strip()
        for key in (
            "primary_operating_entity",
            "main_operating_bank_account",
            "related_entities",
            "relationship_explanation",
        )
    )


def _has_real_estate_schedule(intake: PublicUnderwritingIntake) -> bool:
    return any(
        str(row.get("address") or "").strip()
        and row.get("estimated_loan_amount") is not None
        and row.get("estimated_property_value") is not None
        for row in _asset_rows(intake)
    )


def _has_uploaded_doc_name(intake: PublicUnderwritingIntake, needle: str) -> bool:
    wanted = needle.lower()
    docs = {doc.id: doc for doc in intake.bucket.requested_documents}
    for file in _active_files(intake.bucket):
        doc = docs.get(file.requested_document_id) if file.requested_document_id else None
        haystack = f"{file.file_name} {doc.name if doc else ''} {doc.category if doc else ''}".lower()
        if wanted in haystack:
            return True
    return False


def _call_booked(intake: PublicUnderwritingIntake) -> bool:
    booking = _intake_state(intake).get("call_booking")
    return isinstance(booking, dict) and bool(booking.get("event_id"))


def _next_widget(intake: PublicUnderwritingIntake) -> dict[str, Any] | None:
    files = _active_files(intake.bucket)
    missing = _missing_required_docs(intake.bucket)
    if not files and not intake.result_snapshot:
        return {
            "type": "upload_files",
            "title": "Upload baseline documents",
            "description": (
                "Upload only the baseline package: tax returns, current P&L, bank statements, real estate schedule or mortgage notes, "
                "and floorplan/MCA/inventory statements only if applicable."
            ),
            "missing_document_ids": [str(doc.id) for doc in missing],
        }
    if intake.result_snapshot:
        return {
            "type": "bankability_result",
            "title": "Preliminary bankability screen",
            "description": "Review the AI summary, missing items, product fit, and next steps.",
        }
    return None


def _widget_for_type(intake: PublicUnderwritingIntake, kind: str, *, source: str = "system_next_step", reason: str | None = None) -> dict[str, Any] | None:
    missing = _missing_required_docs(intake.bucket)
    widgets: dict[str, dict[str, Any]] = {
        "upload_files": {
            "type": "upload_files",
            "title": "Upload baseline documents",
            "description": (
                "Upload only the baseline package: tax returns, current P&L, bank statements, real estate schedule or mortgage notes, "
                "and floorplan/MCA/inventory statements only if applicable."
            ),
            "missing_document_ids": [str(doc.id) for doc in missing],
        },
        "entity_structure": {
            "type": "entity_structure",
            "title": "Dealer entity and bank account structure",
            "description": "Clarify the primary operating LLC, main operating bank account, related LLCs, and how the accounts work together.",
        },
        "deal_profile": {
            "type": "deal_profile",
            "title": "Essential funding facts",
            "description": "Answer only what you know: use of funds, desired capital amount, and estimated credit score. The AI will infer the likely lending path.",
            "fields": ["loan_purpose", "requested_loan_amount", "estimated_credit_score"],
        },
        "real_estate_schedule": {
            "type": "real_estate_schedule",
            "title": "Add real estate collateral",
            "description": "Type each property address, estimated amount owed, and estimated value. You can also upload mortgage notes, but estimated value is still needed.",
        },
        "referral": {
            "type": "referral",
            "title": "Referral credit",
            "description": "Who referred you to this link? If nobody did, enter self.",
        },
        "run_review": {
            "type": "run_review",
            "title": "Run preliminary AI screen",
            "description": (
                "Run this when there is enough evidence for a useful answer. Missing documents will be listed as gaps, "
                "not treated as a reason to stop the review."
            ),
        },
        "book_call": {
            "type": "book_call",
            "title": "Book the next underwriting call",
            "description": "Choose one of the next available times with Qualified Commercial to validate the preliminary screen.",
        },
        "bankability_result": {
            "type": "bankability_result",
            "title": "Preliminary bankability screen",
            "description": "Review the AI summary, missing items, product fit, and next steps.",
        },
    }
    widget = widgets.get(kind)
    if widget is None:
        return None
    return {**widget, "source": source, "reason": reason or source}


def _widget_intent_from_message(message: str | None, intake: PublicUnderwritingIntake) -> str | None:
    text = (message or "").lower()
    if not text.strip():
        return None
    property_terms = (
        "manual properties",
        "manually upload my properties",
        "enter properties",
        "add properties",
        "property line",
        "property list",
        "real estate schedule",
        "collateral schedule",
        "amount owed",
        "estimated value",
        "property value",
        "property address",
        "mortgage balance",
        "real estate",
    )
    if any(term in text for term in property_terms):
        return "real_estate_schedule"
    upload_terms = ("upload", "file", "document", "docs", "statement", "tax return", "p&l", "profit and loss", "mca", "floorplan", "inventory")
    if any(term in text for term in upload_terms):
        return "upload_files"
    entity_terms = ("llc", "entity", "entities", "bank account", "related company", "operating account", "company structure")
    if any(term in text for term in entity_terms):
        return "entity_structure"
    deal_terms = ("loan amount", "requested amount", "use of funds", "credit score", "capital amount", "funding amount")
    if any(term in text for term in deal_terms):
        return "deal_profile"
    referral_terms = ("referred", "referral", "who sent", "who referred")
    if any(term in text for term in referral_terms):
        return "referral"
    call_terms = ("book", "call", "appointment", "meeting", "schedule")
    if any(term in text for term in call_terms) and not _call_booked(intake):
        return "book_call"
    review_terms = ("review", "underwrite", "screen", "fundable", "bankable", "preliminary")
    if any(term in text for term in review_terms):
        return "bankability_result" if intake.result_snapshot else "run_review"
    return None


def _message_for_widget(widget: dict[str, Any] | None, intake: PublicUnderwritingIntake) -> str:
    if not widget:
        return (
            "I am reading the uploaded file set like a banking underwriter. I will classify the documents by what they actually are, "
            "then tell you what they support and what baseline items are still missing."
        )
    kind = widget.get("type")
    if kind == "upload_files":
        return (
            "Your secure file room is open. Upload the baseline package only: tax returns, current P&L, bank statements, "
            "real estate schedule or mortgage notes, and floorplan/MCA/inventory statements only if they apply. "
            "I will not ask for unlimited documents."
        )
    if kind == "entity_structure":
        return (
            "Next I need to understand the dealership structure: the main operating LLC, the main operating bank account, "
            "any related LLCs, and how those accounts/entities work together."
        )
    if kind == "deal_profile":
        return (
            "I have files to review. Next, give me the use of funds, rough requested amount, and estimated credit score. "
            "This is self-reported for now and will be validated during the intro call."
        )
    if kind == "real_estate_schedule":
        return "Now add the real estate collateral schedule: full address, estimated amount owed, and estimated value. You can upload mortgage notes, but I still need estimated values."
    if kind == "referral":
        return "Before I run the final screen, tell us who referred you so we can credit the right person."
    if kind == "run_review":
        return (
            "Run the preliminary screen when you are ready. I will classify likely program fit, available evidence, "
            "missing documents, underwriter questions, and next steps from the current file."
        )
    if kind == "bankability_result":
        status_label = (intake.result_snapshot or {}).get("bankability_assessment", {}).get("status")
        return f"The preliminary screen is ready{f': {status_label}' if status_label else ''}. Review the summary and next steps below."
    if kind == "book_call":
        return "The preliminary screen is ready. Choose one of the available call times so Qualified Commercial can validate the file and next steps with you."
    return "How can I help with this dealer financing file?"


def _dealer_context(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    return {
        "review_type": "dealer_gatekeeper_v1",
        "deal_type": "dealer financing with real estate collateral",
        "documentation_level": "full doc",
        "collateral_type": "real estate collateral and business assets",
        "loan_purpose": intake.loan_purpose,
        "requested_loan_amount": float(intake.requested_loan_amount) if intake.requested_loan_amount is not None else None,
        "estimated_credit_score": intake.estimated_credit_score,
        "business_name": intake.business_name,
        "referral_source": intake.referral_source,
        "entity_structure": _entity_structure(intake),
        "asset_rows": _asset_rows(intake),
        "baseline_document_policy": {
            "allowed_document_categories": [
                "last 2 years tax returns",
                "current year P&L",
                "last 3 months bank statements",
                "asset/real estate schedule or mortgage notes/payoff statements",
                "floorplan/MCA/inventory statements only when applicable",
            ],
            "do_not_request_other_document_categories": True,
        },
        "underwriting_focus": (
            "Strictly screen bankability for dealer capital without asking the client to choose a loan product. "
            "Infer likely paths such as real-estate-backed full doc, DSCR/collateral support, cash-out working capital, "
            "portfolio-backed funding, high-cost debt refinance, or floorplan support from the documents and answers. "
            "Do not request documents outside the approved baseline package. If core documents are missing, classify the file "
            "as incomplete or cannot determine based only on missing baseline items. Treat multiple dealership LLCs and bank "
            "accounts as normal, but flag unclear primary operating entity, main operating bank account, related-entity flows, "
            "ownership, collateral, credit, floorplan, MCA, and cash-flow gaps."
        ),
        "custom_instructions": (
            "This is a public lead-magnet strict underwriter for car dealers. Ask only for last 2 years tax returns, current-year P&L, "
            "last 3 months bank statements, real estate schedule or mortgage notes/payoff statements, and floorplan/MCA/inventory "
            "statements only when applicable. The user may not know which lending product fits. Collect evidence quickly, ask only "
            "essential follow-up questions about related LLC/account structure and real estate estimated values/debt, then return "
            "fundable, not fundable, or cannot determine from the current baseline evidence. Return a preliminary screen, not a commitment to lend."
        ),
    }


async def _find_or_create_client(db: AsyncSession, payload: DealerIntakeStart) -> Client:
    email = _normalize_email(str(payload.email))
    client = (await db.execute(select(Client).where(Client.email == email).order_by(Client.created_at.desc()))).scalars().first()
    owner = await primary_super_admin(db)
    if client is None:
        client = Client(
            name=payload.full_name.strip(),
            email=email,
            phone=payload.phone,
            referral_source="dealer_ai_intake",
            originating_agent_id=owner.id if owner else None,
            current_agent_id=owner.id if owner else None,
            source_channel="dealer_ai_intake",
            lead_source="other",
            lead_temperature="warm",
            financing_support_needed="yes",
            relationship_context="new_lead",
            client_experience_mode="self_directed",
            client_experience_mode_reason="dealer_ai_intake",
            client_experience_mode_locked_by="firm",
            lead_intake={
                "source": "dealer_ai_intake",
                "business_name": payload.business_name,
            },
        )
        db.add(client)
        await db.flush()
        return client
    if not client.phone and payload.phone:
        client.phone = payload.phone
    if payload.full_name and (not client.name or client.name.lower() == email):
        client.name = payload.full_name.strip()
    if client.current_agent_id is None and owner is not None:
        client.current_agent_id = owner.id
    intake = dict(client.lead_intake or {})
    intake.update({"source": "dealer_ai_intake", "business_name": payload.business_name or intake.get("business_name")})
    client.lead_intake = intake
    await db.flush()
    return client


async def _create_bucket_for_intake(db: AsyncSession, client: Client, payload: DealerIntakeStart, request: Request) -> tuple[Bucket, BucketUploadLink]:
    owner = await primary_super_admin(db)
    bucket = Bucket(
        name=f"{payload.business_name or payload.full_name} Dealer AI Intake",
        bucket_type="dealer_ai_intake",
        client_name=payload.business_name or payload.full_name,
        purpose="Dealer financing AI intake",
        description="Public AI gatekeeper intake for dealer financing with real estate collateral.",
        ai_context={
            "review_type": "dealer_gatekeeper_v1",
            "deal_type": "dealer financing with real estate collateral",
            "documentation_level": "full doc",
            "collateral_type": "real estate collateral and business assets",
            "client_email": client.email,
        },
        created_by_id=owner.id if owner else None,
    )
    db.add(bucket)
    await db.flush()
    for doc in REQUIRED_DOCUMENTS:
        db.add(
            BucketRequestedDocument(
                bucket_id=bucket.id,
                name=doc["name"],
                category=doc["category"],
                description=doc["description"],
                required=True,
                allow_multiple_files=bool(doc["allow_multiple_files"]),
                status="requested",
                is_custom=False,
            )
        )
    passcode = _generate_passcode()
    link = BucketUploadLink(
        bucket_id=bucket.id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.full_name.strip(),
        recipient_email=client.email,
        allow_notes=True,
        allow_multiple_sessions=True,
        can_use_ai_chat=True,
        can_view_ai_tasks=True,
        passcode_hash=_hash_passcode(passcode),
    )
    db.add(link)
    await _log(
        db,
        bucket.id,
        "dealer_ai_intake_created",
        request=request,
        actor_name=payload.full_name,
        actor_email=client.email,
        actor_role="public_lead",
        target_type="bucket",
        target_id=str(bucket.id),
        detail="Public dealer AI intake created",
    )
    await db.flush()
    return bucket, link


async def _load_public_intake(db: AsyncSession, token: str) -> PublicUnderwritingIntake:
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.token_hash == _hash_token(token))
            .options(
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
                selectinload(PublicUnderwritingIntake.bucket_upload_link),
                selectinload(PublicUnderwritingIntake.latest_review),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if intake is None or intake.bucket.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI intake not found")
    return intake


async def _load_client_intake(db: AsyncSession, user: CurrentUser, intake_id: UUID) -> PublicUnderwritingIntake:
    if user.client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Current user has no linked client record")
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == intake_id, PublicUnderwritingIntake.client_id == user.client.id)
            .options(
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
                selectinload(PublicUnderwritingIntake.bucket_upload_link),
                selectinload(PublicUnderwritingIntake.latest_review),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if intake is None or intake.bucket.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI intake not found")
    return intake


def _apply_updates(intake: PublicUnderwritingIntake, updates: DealerIntakePatch | None) -> None:
    if updates is None:
        return
    data = updates.model_dump(exclude_unset=True)
    for key in ("business_name", "phone", "loan_purpose", "requested_loan_amount", "estimated_credit_score", "referral_source"):
        if key in data:
            setattr(intake, key, data[key])
    if "asset_rows" in data:
        intake.asset_rows = [row.model_dump() if isinstance(row, DealerAssetRow) else row for row in updates.asset_rows or []]
    state = dict(intake.intake_state or {})
    if "entity_structure" in data:
        state["entity_structure"] = updates.entity_structure.model_dump() if updates.entity_structure else {}
    state["last_updates"] = data
    intake.intake_state = state
    intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **_dealer_context(intake)}


async def _log_dealer_update_events(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    data: dict[str, Any],
    *,
    request: Request | None = None,
    user: CurrentUser | None = None,
    actor_name: str | None = None,
    actor_email: str | None = None,
    actor_role: str | None = None,
) -> None:
    if "entity_structure" in data:
        await _log(
            db,
            intake.bucket_id,
            "dealer_ai_entity_structure_captured",
            request=request,
            user=user,
            actor_name=actor_name,
            actor_email=actor_email,
            actor_role=actor_role,
            target_type="dealer_ai_intake",
            target_id=str(intake.id),
            detail="Dealer entity and operating account structure captured",
        )
    if "asset_rows" in data:
        await _log(
            db,
            intake.bucket_id,
            "dealer_ai_real_estate_schedule_updated",
            request=request,
            user=user,
            actor_name=actor_name,
            actor_email=actor_email,
            actor_role=actor_role,
            target_type="dealer_ai_intake",
            target_id=str(intake.id),
            detail="Dealer real estate collateral schedule updated",
        )


async def _booking_settings_for_primary_admin(db: AsyncSession) -> tuple[Any | None, BookingSettings | None]:
    owner = await primary_super_admin(db)
    if owner is None:
        return None, None
    booking = (
        await db.execute(
            select(BookingSettings).where(
                BookingSettings.user_id == owner.id,
                BookingSettings.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    return owner, booking


async def _dealer_call_slots(db: AsyncSession) -> tuple[Any | None, BookingSettings | None, list[dict[str, str]]]:
    owner, booking = await _booking_settings_for_primary_admin(db)
    if owner is None or booking is None:
        return owner, booking, []
    slots = await _available_booking_slots(db, owner, booking)
    now = _now()
    next_day = now + timedelta(hours=24)
    preferred = [slot for slot in slots if slot.starts_at <= next_day]
    chosen = (preferred or slots)[:3]
    return owner, booking, [
        {
            "starts_at": slot.starts_at.isoformat(),
            "label": slot.label,
            "date_label": slot.date_label,
        }
        for slot in chosen
    ]


async def _decorate_widget(db: AsyncSession, intake: PublicUnderwritingIntake, widget: dict[str, Any] | None) -> dict[str, Any] | None:
    if widget is None or widget.get("type") != "book_call":
        return widget
    owner, booking, slots = await _dealer_call_slots(db)
    decorated = dict(widget)
    decorated["slots"] = slots
    decorated["host_name"] = owner.name or owner.email if owner is not None else "Qualified Commercial"
    decorated["duration_min"] = booking.duration_min if booking is not None else 30
    if not slots:
        decorated["disabled_reason"] = "No call times are available right now. Qualified Commercial will follow up directly."
    return decorated


async def _response(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    token: str | None,
    assistant_message: str | None = None,
    messages: list[Any] | None = None,
    forced_widget_type: str | None = None,
) -> DealerIntakeResponse:
    review = intake.latest_review if intake.latest_review else None
    widget = _widget_for_type(intake, forced_widget_type, source="user_intent", reason="User asked for this tool") if forced_widget_type else None
    widget = widget or _next_widget(intake)
    widget = await _decorate_widget(db, intake, widget)
    files = sorted(_active_files(intake.bucket), key=lambda file: file.created_at, reverse=True)
    summary = upload_link_visible_summary(review, intake.bucket)
    return DealerIntakeResponse(
        intake=DealerIntakeRead.model_validate(intake),
        token=token,
        resume_url=_public_url(f"/dealer-ai-underwriter?token={token}") if token else None,
        upload_url=_public_url(f"/buckets/request/{intake.bucket_upload_link.token}") if intake.bucket_upload_link else None,
        assistant_message=assistant_message or _message_for_widget(widget, intake),
        widget=widget,
        requested_documents=[BucketRequestedDocumentRead.model_validate(doc) for doc in intake.bucket.requested_documents],
        files=[BucketRequestUploadedFileRead.model_validate(file) for file in files],
        ai_summary=summary,
        latest_review=BucketAIReviewRead.model_validate(review) if review else None,
        messages=[BucketAIMessageRead.model_validate(message) for message in (messages or [])],
    )


async def _start_upload(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerFileUploadInit,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
) -> BucketFileUploadInitResponse:
    req = None
    if payload.requested_document_id:
        req = await db.get(BucketRequestedDocument, payload.requested_document_id)
        if req is None or req.bucket_id != intake.bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Requested document does not belong to this intake")
    existing_conditions = [
        BucketFile.bucket_id == intake.bucket_id,
        BucketFile.upload_link_id == intake.bucket_upload_link_id,
        BucketFile.file_name == payload.file_name,
        BucketFile.size_bytes == payload.size_bytes,
        BucketFile.status.in_(("uploading", "uploaded")),
        BucketFile.deleted_at.is_(None),
    ]
    if payload.requested_document_id:
        existing_conditions.append(BucketFile.requested_document_id == payload.requested_document_id)
    else:
        existing_conditions.append(BucketFile.requested_document_id.is_(None))
    existing = (await db.execute(select(BucketFile).where(*existing_conditions).order_by(BucketFile.created_at.desc()))).scalars().first()
    if existing:
        upload_url, headers = _upload_url(existing.s3_key, payload.content_type)
        return BucketFileUploadInitResponse(file_id=existing.id, upload_url=upload_url, s3_key=existing.s3_key, required_headers=headers)
    if req is not None and not req.allow_multiple_files:
        existing_for_doc = (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.bucket_id == intake.bucket_id,
                    BucketFile.requested_document_id == req.id,
                    BucketFile.status.in_(("uploading", "uploaded")),
                    BucketFile.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existing_for_doc is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This requested document only allows one file")
    _, prefix, _ = _bucket_storage_config()
    file_id = uuid4()
    safe = _safe_filename(payload.file_name)
    s3_key = f"{prefix}/uploads/{intake.bucket_id}/{file_id}-{safe}"
    file = BucketFile(
        id=file_id,
        bucket_id=intake.bucket_id,
        requested_document_id=payload.requested_document_id,
        upload_link_id=intake.bucket_upload_link_id,
        file_name=payload.file_name,
        s3_key=s3_key,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        uploaded_by_name=actor_name,
        uploaded_by_email=actor_email,
        status="uploading",
    )
    db.add(file)
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_file_upload_started",
        request=request,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role="public_lead",
        target_type="file",
        target_id=str(file.id),
        detail=payload.file_name,
    )
    await db.commit()
    upload_url, headers = _upload_url(s3_key, payload.content_type)
    return BucketFileUploadInitResponse(file_id=file.id, upload_url=upload_url, s3_key=s3_key, required_headers=headers)


async def _complete_upload(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerUploadComplete,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
) -> BucketFile:
    file = await db.get(BucketFile, payload.file_id)
    if file is None or file.bucket_id != intake.bucket_id or file.upload_link_id != intake.bucket_upload_link_id or file.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if file.status != "uploaded":
        file.status = "uploaded"
        if file.requested_document_id:
            req = await db.get(BucketRequestedDocument, file.requested_document_id)
            if req is not None:
                req.status = "uploaded"
        if intake.bucket_upload_link is not None:
            intake.bucket_upload_link.completed_at = _now()
    if payload.note:
        db.add(
            BucketNote(
                bucket_id=intake.bucket_id,
                author_name=actor_name,
                author_role="public_lead",
                visibility="shared",
                content=payload.note,
            )
        )
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_file_uploaded",
        request=request,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role="public_lead",
        target_type="file",
        target_id=str(file.id),
        detail=file.file_name,
    )
    await db.commit()
    await db.refresh(file)
    return file


@router.post("/start", response_model=DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def start_dealer_intake(
    payload: DealerIntakeStart,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    if not payload.terms_accepted or not payload.privacy_accepted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Terms and Privacy Policy acceptance is required.")
    client = await _find_or_create_client(db, payload)
    bucket, link = await _create_bucket_for_intake(db, client, payload, request)
    token = _new_public_token()
    intake = PublicUnderwritingIntake(
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=_hash_token(token),
        full_name=payload.full_name.strip(),
        email=client.email or _normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        intake_state={
            "messages": [],
            "source": "dealer_ai_intake",
            "legal_acceptance": {
                "terms_accepted": payload.terms_accepted,
                "privacy_accepted": payload.privacy_accepted,
                "terms_version": payload.terms_version or TERMS_VERSION,
                "privacy_version": payload.privacy_version or PRIVACY_VERSION,
                **_request_audit(request),
            },
        },
    )
    db.add(intake)
    await db.commit()
    intake = await _load_public_intake(db, token)
    email_record = _record_resume_email(intake, token=token, request=request, reason="intake_created")
    await db.commit()
    intake = await _load_public_intake(db, token)
    email_note = (
        " I also emailed you a secure resume link so you can come back later."
        if email_record.get("ok")
        else " Use the copy resume link option as a backup if email delivery is unavailable."
    )
    return await _response(
        db,
        intake,
        token=token,
        assistant_message=(
            "I opened your secure dealer funding file. I am going to screen this like a bank underwriter: tax returns, current P&L, "
            "bank statements, real estate collateral, and any floorplan/MCA exposure that applies. Upload what you have now, and I will "
            "only ask follow-up questions when the LLC/account structure or collateral values are not clear enough to make a preliminary call."
            + email_note
        ),
    )


@router.post("/resume-link", response_model=DealerResumeLinkResponse)
async def send_dealer_resume_link(
    payload: DealerResumeLinkRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerResumeLinkResponse:
    email = _normalize_email(str(payload.email))
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.email == email)
            .options(selectinload(PublicUnderwritingIntake.bucket))
            .order_by(PublicUnderwritingIntake.updated_at.desc())
        )
    ).scalars().first()
    if intake is not None and intake.bucket is not None and intake.bucket.archived_at is None:
        token = _new_public_token()
        intake.token_hash = _hash_token(token)
        _record_resume_email(intake, token=token, request=request, reason="resume_link_requested")
        await _log(
            db,
            intake.bucket_id,
            "dealer_ai_resume_link_requested",
            request=request,
            actor_name=intake.full_name,
            actor_email=intake.email,
            actor_role="public_lead",
            target_type="dealer_ai_intake",
            target_id=str(intake.id),
            detail="Dealer AI resume link requested by email",
        )
        await db.commit()
    return DealerResumeLinkResponse()


@router.get("/{token}", response_model=DealerIntakeResponse)
async def get_dealer_intake(token: str, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    return await _response(db, intake, token=token)


@router.patch("/{token}", response_model=DealerIntakeResponse)
async def update_dealer_intake(
    token: str,
    payload: DealerIntakePatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    update_data = payload.model_dump(exclude_unset=True)
    _apply_updates(intake, payload)
    await _log_dealer_update_events(
        db,
        intake,
        update_data,
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(db, intake, token=token)


@router.post("/{token}/chat", response_model=DealerIntakeResponse)
async def dealer_intake_chat(
    token: str,
    payload: DealerChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    update_data = payload.updates.model_dump(exclude_unset=True) if payload.updates else {}
    _apply_updates(intake, payload.updates)
    forced_widget_type = _widget_intent_from_message(payload.message, intake)
    await _log_dealer_update_events(
        db,
        intake,
        update_data,
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
    )
    intake.last_message_at = _now()
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        chat_messages, _ = await create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="uploader",
            message=payload.message.strip(),
            actor_name=intake.full_name,
            upload_link=intake.bucket_upload_link,
        )
        messages = chat_messages
        if chat_messages:
            assistant_message = chat_messages[-1].content
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(db, intake, token=token, assistant_message=assistant_message, messages=messages, forced_widget_type=forced_widget_type)


@router.post("/{token}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def dealer_upload_init(
    token: str,
    payload: DealerFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    intake = await _load_public_intake(db, token)
    return await _start_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@router.post("/{token}/files/complete", response_model=BucketFileRead)
async def dealer_upload_complete(
    token: str,
    payload: DealerUploadComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    return await _complete_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@router.post("/{token}/run-review", response_model=DealerIntakeResponse)
async def run_dealer_review(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **_dealer_context(intake)}
    review = BucketAIReview(
        bucket_id=intake.bucket_id,
        requested_by_user_id=None,
        status="queued",
        context_snapshot=intake.bucket.ai_context or {},
        file_ids=[str(file.id) for file in _active_files(intake.bucket)],
        provider="bedrock",
    )
    db.add(review)
    await db.flush()
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_review_queued",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="ai_review",
        target_id=str(review.id),
        detail="Public dealer AI screen",
    )
    await run_bucket_ai_review(db, review.id)
    fresh_review = await latest_review(db, intake.bucket_id)
    intake.latest_review_id = fresh_review.id if fresh_review else review.id
    if fresh_review and isinstance(fresh_review.result, dict):
        intake.result_snapshot = fresh_review.result
        intake.status = "reviewed"
        intake.completed_at = _now()
        _, _, slots = await _dealer_call_slots(db)
        state = _intake_state(intake)
        if slots and not state.get("call_options_shown_at"):
            state["call_options_shown_at"] = _now().isoformat()
            intake.intake_state = state
            await _log(
                db,
                intake.bucket_id,
                "dealer_ai_call_options_shown",
                request=request,
                actor_name=intake.full_name,
                actor_email=intake.email,
                actor_role="public_lead",
                target_type="dealer_ai_intake",
                target_id=str(intake.id),
                detail="Dealer AI call options shown",
            )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(db, intake, token=token)


@router.post("/{token}/book-call", response_model=DealerIntakeResponse)
async def book_dealer_call(
    token: str,
    payload: DealerBookCallRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    if _call_booked(intake):
        return await _response(
            db,
            intake,
            token=token,
            assistant_message="Your call is already booked. Keep uploading baseline documents here if anything is still missing before the meeting.",
        )
    starts_at = _to_utc_minute(payload.starts_at)
    owner, booking, slots = await _dealer_call_slots(db)
    if owner is None or booking is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Call scheduling is not available right now.")
    if not any(abs((datetime.fromisoformat(slot["starts_at"]) - starts_at).total_seconds()) < 1 for slot in slots):
        raise HTTPException(status.HTTP_409_CONFLICT, "That call time is no longer available. Choose another time.")

    who = f"{intake.full_name} <{intake.email}>"
    description = (
        "Booked from Dealer AI Underwriter.\n"
        f"Dealer intake: {intake.id}\n"
        f"Bucket: {intake.bucket_id}\n"
        f"Business: {intake.business_name or '(not provided)'}\n"
        f"Name: {intake.full_name}\n"
        f"Email: {intake.email}\n"
        f"Phone: {intake.phone or '(not provided)'}\n"
        f"Requested amount: {intake.requested_loan_amount or '(not provided)'}\n"
        f"Use of funds: {intake.loan_purpose or '(not provided)'}\n"
    )
    ev = CalendarEvent(
        loan_id=None,
        kind=CalendarEventKind.CALL,
        title=f"Dealer AI call: {intake.business_name or intake.full_name}",
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=booking.duration_min,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.AUTO,
        owner_user_id=owner.id,
        external_ref_kind="dealer_ai_intake",
        external_ref_id=str(intake.id),
    )
    db.add(ev)
    await db.flush()

    state = _intake_state(intake)
    state["call_booking"] = {
        "event_id": str(ev.id),
        "starts_at": starts_at.isoformat(),
        "booked_at": _now().isoformat(),
        "host_user_id": str(owner.id),
        "host_email": owner.email,
    }
    intake.intake_state = state
    db.add(
        Activity(
            client_id=intake.client_id,
            actor_id=None,
            actor_label="public",
            kind="calendar.dealer_ai_call_booked",
            summary=f"Dealer AI call booked for {intake.business_name or intake.full_name}",
            payload={
                "event_id": str(ev.id),
                "intake_id": str(intake.id),
                "bucket_id": str(intake.bucket_id),
                "host_user_id": str(owner.id),
                "starts_at": starts_at.isoformat(),
            },
        )
    )
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_call_booked",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="calendar_event",
        target_id=str(ev.id),
        detail=f"Dealer AI call booked for {starts_at.isoformat()}",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(
        db,
        intake,
        token=token,
        assistant_message="Your call is booked. Keep uploading baseline documents here if anything is still missing before the meeting.",
    )


@client_router.get("", response_model=list[DealerIntakeRead])
async def list_my_dealer_intakes(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[DealerIntakeRead]:
    if user.role != Role.CLIENT or user.client is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client account required")
    rows = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.client_id == user.client.id)
            .order_by(PublicUnderwritingIntake.updated_at.desc())
        )
    ).scalars().all()
    return [DealerIntakeRead.model_validate(row) for row in rows]


@client_router.get("/{intake_id}", response_model=DealerIntakeResponse)
async def get_my_dealer_intake(intake_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake = await _load_client_intake(db, user, intake_id)
    return await _response(db, intake, token=None)


@client_router.post("/{intake_id}/chat", response_model=DealerIntakeResponse)
async def my_dealer_intake_chat(
    intake_id: UUID,
    payload: DealerChatRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_client_intake(db, user, intake_id)
    update_data = payload.updates.model_dump(exclude_unset=True) if payload.updates else {}
    _apply_updates(intake, payload.updates)
    forced_widget_type = _widget_intent_from_message(payload.message, intake)
    await _log_dealer_update_events(db, intake, update_data, request=request, user=user)
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        chat_messages, _ = await create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="uploader",
            message=payload.message.strip(),
            actor_name=user.name or intake.full_name,
            user=user,
            upload_link=intake.bucket_upload_link,
        )
        messages = chat_messages
        if chat_messages:
            assistant_message = chat_messages[-1].content
    await db.commit()
    intake = await _load_client_intake(db, user, intake_id)
    return await _response(db, intake, token=None, assistant_message=assistant_message, messages=messages, forced_widget_type=forced_widget_type)


@client_router.post("/{intake_id}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def my_dealer_upload_init(
    intake_id: UUID,
    payload: DealerFileUploadInit,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    intake = await _load_client_intake(db, user, intake_id)
    return await _start_upload(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)


@client_router.post("/{intake_id}/files/complete", response_model=BucketFileRead)
async def my_dealer_upload_complete(
    intake_id: UUID,
    payload: DealerUploadComplete,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_client_intake(db, user, intake_id)
    return await _complete_upload(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)
