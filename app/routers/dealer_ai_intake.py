from __future__ import annotations

import hashlib
import asyncio
import html
import json
import logging
import mimetypes
import re
import secrets
import time
import zipfile
from io import BytesIO
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, with_loader_criteria

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import CalendarEventKind, CalendarEventSource, CalendarEventStatus, Role
from app.models.activity import Activity
from app.models.booking_settings import BookingSettings
from app.models.bucket import Bucket, BucketAIMessage, BucketAIReview, BucketFile, BucketFileAnalysis, BucketNote, BucketRequestedDocument, BucketUploadLink, BucketVendorAccess
from app.models.client import Client
from app.models.event import CalendarEvent
from app.models.dealer_intake_login import DealerIntakeLoginChallenge
from app.models.public_underwriting_intake import PublicUnderwritingIntake, PublicUnderwritingIntakeArtifact, PublicUnderwritingIntakeEmailSend
from app.models.user import User
from app.routers.public import _available_booking_slots, _to_utc_minute
from app.routers.buckets import (
    _bucket_storage_config,
    _generate_passcode,
    _hash_passcode,
    _log,
    _public_url,
    _s3_client,
    _safe_filename,
    _sanitize_upload_content_type,
    _upload_url,
    _vendor_user_from_payload,
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
from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION, create_chat_reply, latest_review, run_bucket_ai_review, upload_link_visible_summary
from app.services.ai.bedrock_client import get_client, model_light
from app.services.ai.usage import json_safe_metadata, tracked_messages_create
from app.services.dealer_ai_intelligence_pdf import render_dealer_intelligence_pdf
from app.services.email.ses_client import send_email, send_raw_email
from app.services.email.user_mailer import send_as_user
from app.services.payment_authorization import primary_super_admin
from app.services.public_underwriting_packet_pdf import render_underwriting_packet_pdf


router = APIRouter(prefix="/public/dealer-ai-intake", tags=["dealer-ai-intake"])
funding_router = APIRouter(prefix="/public/funding-review", tags=["public-funding-review"])
client_router = APIRouter(prefix="/buckets/client/intakes", tags=["client-bucket-intakes"])
admin_router = APIRouter(prefix="/admin/ai-underwriter-leads", tags=["admin-ai-underwriter-leads"])
log = logging.getLogger(__name__)

TERMS_VERSION = "2026-05-19"
PRIVACY_VERSION = "2026-05-19"
DEALER_LOGIN_CODE_TTL_MINUTES = 10
DEALER_LOGIN_SESSION_TTL_HOURS = 12
DEALER_LOGIN_MAX_ATTEMPTS = 5
DEALER_LOGIN_RATE_LIMIT_WINDOW_MINUTES = 15
DEALER_LOGIN_RATE_LIMIT_MAX = 5
ZIP_MAX_ENTRIES = 60
ZIP_MAX_ENTRY_BYTES = 40 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 80 * 1024 * 1024

# Abuse guards for the fully-public POST endpoints (single-instance in-memory,
# same assumption as the public.py throttle). /start creates a Client + Bucket +
# upload link + intake per call, and /run-review runs a heavy Bedrock pass, so
# both are rate-limited to stop scripted mass-creation / LLM-cost amplification.
_START_MIN_INTERVAL_SECONDS = 15.0
_START_LAST_BY_IP: dict[str, float] = {}
_REVIEW_MIN_INTERVAL_SECONDS = 45.0
_REVIEW_LAST_BY_TOKEN: dict[str, float] = {}
# Admin-initiated re-run cooldown, keyed by intake id. Each re-run is a heavy
# Bedrock pass over up to 8 files; a short cooldown blocks accidental repeats.
_ADMIN_REVIEW_MIN_INTERVAL_SECONDS = 60.0
_ADMIN_REVIEW_LAST_BY_INTAKE: dict[str, float] = {}


def _throttle_or_429(store: dict[str, float], key: str, min_interval: float, message: str) -> None:
    now = time.monotonic()
    last = store.get(key)
    if last is not None and (now - last) < min_interval:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, message)
    store[key] = now
ZIP_SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".csv",
    ".xlsx",
    ".txt",
    ".md",
    ".log",
}


REQUIRED_DOCUMENTS = [
    {
        "name": "Last 2 years business tax returns",
        "category": "Financials",
        "description": "Upload the dealership/business tax returns for the last two years.",
        "allow_multiple_files": True,
    },
    {
        "name": "Current year P&L",
        "category": "Financials",
        "description": "Upload the current year profit and loss statement for the dealership/business.",
        "allow_multiple_files": True,
    },
    {
        "name": "Last 6 months bank statements",
        "category": "Bank Statements",
        "description": "Upload the last six months of the main operating business bank statements.",
        "allow_multiple_files": True,
    },
]

REAL_ESTATE_REQUIRED_DOCUMENTS = [
    {
        "name": "Lease, rent roll, or rent support",
        "category": "Property income",
        "description": "Upload lease agreements, rent roll, or other support for current and expected rent.",
        "allow_multiple_files": True,
    },
    {
        "name": "Purchase contract, payoff, or mortgage statement",
        "category": "Transaction and debt",
        "description": "Upload the purchase contract for acquisitions or payoff/mortgage statements for refinance and cash-out.",
        "allow_multiple_files": True,
    },
    {
        "name": "Taxes, insurance, HOA, or PITIA support",
        "category": "Property expenses",
        "description": "Upload property tax, insurance, HOA, or payment evidence needed to estimate PITIA and DSCR.",
        "allow_multiple_files": True,
    },
    {
        "name": "Entity or vesting documents",
        "category": "Ownership",
        "description": "Upload entity, vesting, or ownership documents when available.",
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


class FundingReviewStart(BaseModel):
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=48)
    investor_name: str | None = Field(default=None, max_length=180)
    target_property_address: str | None = Field(default=None, max_length=320)
    transaction_type: str | None = Field(default=None, max_length=64)
    requested_amount: float | None = Field(default=None, ge=0)
    estimated_value_or_purchase_price: float | None = Field(default=None, ge=0)
    monthly_rent: float | None = Field(default=None, ge=0)
    estimated_credit_tier: str | None = Field(default=None, max_length=64)
    terms_accepted: bool = False
    privacy_accepted: bool = False
    terms_version: str = Field(default=TERMS_VERSION, max_length=32)
    privacy_version: str = Field(default=PRIVACY_VERSION, max_length=32)

    @field_validator("phone", "investor_name", "target_property_address", "transaction_type", "estimated_credit_tier", mode="before")
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


class AdminLeadCreate(BaseModel):
    """Super-admin creates an AI-underwriter lead on behalf of a client. Mirrors the
    public start schemas but with no terms/throttle and an explicit variant selector.
    The client can later log in with this email exactly like a self-serve lead."""

    variant: str = Field(default="dealer")  # "dealer" | "real_estate"
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=48)
    business_name: str | None = Field(default=None, max_length=180)
    # Real-estate basics (optional; mirror FundingReviewStart).
    investor_name: str | None = Field(default=None, max_length=180)
    target_property_address: str | None = Field(default=None, max_length=320)
    transaction_type: str | None = Field(default=None, max_length=64)
    requested_amount: float | None = Field(default=None, ge=0)
    estimated_value_or_purchase_price: float | None = Field(default=None, ge=0)
    monthly_rent: float | None = Field(default=None, ge=0)
    estimated_credit_tier: str | None = Field(default=None, max_length=64)
    notify_client: bool = False  # email the client a secure resume/login link now
    force_new: bool = False  # create a second lead even if one already exists for this email

    @field_validator("variant", mode="before")
    @classmethod
    def normalize_variant(cls, value: object) -> object:
        return (str(value).strip().lower() if value else "dealer")

    @field_validator(
        "phone", "business_name", "investor_name", "target_property_address",
        "transaction_type", "estimated_credit_tier", mode="before",
    )
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


class ReviewRunStartResponse(BaseModel):
    review_id: UUID
    status: str


class ReviewProgressResponse(BaseModel):
    review_id: UUID
    status: str
    stage: str
    label: str
    percent: int
    files_total: int
    files_done: int
    error: str | None = None


class DealerResumeLinkRequest(BaseModel):
    email: EmailStr


class DealerResumeLinkResponse(BaseModel):
    ok: bool = True
    message: str = "If a matching secure intake exists, a resume link has been sent."


class DealerLoginStartRequest(BaseModel):
    email: EmailStr


class DealerLoginStartResponse(BaseModel):
    ok: bool = True
    login_required: bool = False
    message: str = "If a secure dealer file exists for this email, a short access code has been sent."


class DealerLoginVerifyRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=16)


class DealerLogoutRequest(BaseModel):
    session_token: str | None = Field(default=None, max_length=255)


class DealerLogoutResponse(BaseModel):
    ok: bool = True


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


class PublicUnderwritingArtifactRead(ORMModel):
    id: UUID
    intake_id: UUID
    artifact_type: str
    title: str
    body_text: str | None = None
    body_json: dict[str, Any] | None = None
    s3_key: str | None = None
    download_url: str | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class PublicUnderwritingEmailSendRead(ORMModel):
    id: UUID
    intake_id: UUID
    executive_summary_artifact_id: UUID | None = None
    lender_packet_artifact_id: UUID | None = None
    to_emails: list[str]
    cc_emails: list[str] | None = None
    subject: str
    body: str
    vendor_access_ids: list[str] | None = None
    ses_status: str
    ses_message_ids: list[str] | None = None
    ses_error: str | None = None
    sent_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class VendorEmailPreviewRequest(BaseModel):
    to_emails: list[EmailStr] = Field(default_factory=list)
    cc_emails: list[EmailStr] = Field(default_factory=list)
    include_lender_packet: bool = True
    subject: str | None = Field(default=None, max_length=512)
    body: str | None = Field(default=None, max_length=8000)


class VendorEmailSendRequest(VendorEmailPreviewRequest):
    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=12000)
    # Optional Google Drive files to attach (file ids from the sender's Drive
    # picker). Downloaded via the sender's OAuth grant at send time.
    drive_file_ids: list[str] = Field(default_factory=list)
    can_preview: bool = True
    can_download: bool = True
    can_add_notes: bool = True
    can_view_ai_summary: bool = True
    can_use_ai_chat: bool = False
    can_view_ai_tasks: bool = False
    can_propose_tasks: bool = False


class VendorEmailPreviewResponse(BaseModel):
    subject: str
    body: str
    to_emails: list[str]
    cc_emails: list[str]
    executive_summary: PublicUnderwritingArtifactRead | None = None
    lender_packet: PublicUnderwritingArtifactRead | None = None


class VendorEmailSendResponse(BaseModel):
    email_sends: list[PublicUnderwritingEmailSendRead]
    vendor_access_ids: list[UUID]


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
    session_token: str | None = None
    resume_url: str | None = None
    upload_url: str | None = None
    assistant_message: str
    widget: dict[str, Any] | None
    requested_documents: list[BucketRequestedDocumentRead]
    files: list[BucketRequestUploadedFileRead]
    ai_summary: dict[str, Any] | None = None
    latest_review: BucketAIReviewRead | None = None
    messages: list[BucketAIMessageRead] = []
    artifacts: list[PublicUnderwritingArtifactRead] = []
    email_sends: list[PublicUnderwritingEmailSendRead] = []


class DealerAILeadRow(BaseModel):
    id: UUID
    variant: str
    client_id: UUID | None = None
    bucket_id: UUID
    bucket_name: str
    full_name: str
    email: str
    phone: str | None = None
    business_name: str | None = None
    status: str
    probability_status: str | None = None
    confidence: str | None = None
    one_next_step: str | None = None
    latest_review_status: str | None = None
    booking_recommended: bool = False
    call_booked: bool = False
    file_count: int = 0
    missing_required_count: int = 0
    requested_loan_amount: float | None = None
    estimated_credit_score: int | None = None
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None


class DealerAILeadListResponse(BaseModel):
    items: list[DealerAILeadRow]
    total: int
    limit: int
    offset: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_public_token() -> str:
    return secrets.token_urlsafe(32)


def _new_login_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _dealer_session_from_request(request: Request) -> str | None:
    raw = request.headers.get("x-dealer-session") or request.headers.get("authorization")
    if not raw:
        return None
    if raw.lower().startswith("bearer "):
        return raw.split(" ", 1)[1].strip()
    return raw.strip()


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
    public_path: str = "/dealer-ai-underwriter",
    review_label: str = "dealer funding review",
    room_label: str = "dealer financing file",
) -> dict[str, Any]:
    resume_url = _public_url(f"{public_path}?token={token}")
    subject = f"Your Qualified Commercial {review_label} link"
    body_text = (
        f"Hi {intake.full_name},\n\n"
        f"Use this secure link to resume your Qualified Commercial {review_label}:\n"
        f"{resume_url}\n\n"
        f"This link opens your encrypted AI underwriting room for the {room_label}. "
        "If you did not request this link, you can ignore this email.\n\n"
        "Qualified Commercial LLC"
    )
    body_html = (
        f"<p>Hi {intake.full_name},</p>"
        f"<p>Use this secure link to resume your Qualified Commercial {review_label}:</p>"
        f'<p><a href="{resume_url}">Resume {html.escape(review_label)}</a></p>'
        f"<p>This link opens your encrypted AI underwriting room for the {html.escape(room_label)}. "
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


def _record_login_code_email(
    intake: PublicUnderwritingIntake,
    *,
    code: str,
    request: Request,
    reason: str,
    review_label: str = "dealer funding review",
) -> dict[str, Any]:
    subject = f"Your Qualified Commercial {review_label} access code"
    body_text = (
        f"Hi {intake.full_name},\n\n"
        f"Your {review_label} access code is {code}.\n\n"
        f"This code expires in {DEALER_LOGIN_CODE_TTL_MINUTES} minutes. "
        "If you did not request this code, you can ignore this email.\n\n"
        "Qualified Commercial LLC"
    )
    body_html = (
        f"<p>Hi {intake.full_name},</p>"
        f"<p>Your {html.escape(review_label)} access code is:</p>"
        f'<p style="font-size:22px;font-weight:700;letter-spacing:2px">{code}</p>'
        f"<p>This code expires in {DEALER_LOGIN_CODE_TTL_MINUTES} minutes. "
        "If you did not request this code, you can ignore this email.</p>"
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
    deliveries = state.get("dealer_login_email_deliveries")
    if not isinstance(deliveries, list):
        deliveries = []
    deliveries.append(record)
    state["dealer_login_email"] = record
    state["dealer_login_email_deliveries"] = deliveries[-10:]
    intake.intake_state = state
    return record


def _admin_lead_url(intake: PublicUnderwritingIntake) -> str:
    return _public_url(f"/admin/ai-underwriter-leads?lead={intake.id}")


def _admin_bucket_url(intake: PublicUnderwritingIntake) -> str:
    return _public_url(f"/buckets?bucket={intake.bucket_id}")


def _dealer_label(intake: PublicUnderwritingIntake) -> str:
    return intake.business_name or intake.full_name or intake.email or "Dealer AI lead"


async def _super_admin_emails(db: AsyncSession) -> list[str]:
    rows = (
        await db.execute(
            select(User.email)
            .where(User.role == Role.SUPER_ADMIN)
            .where(User.deleted_at.is_(None))
            .order_by(User.email.asc())
        )
    ).scalars().all()
    recipients = [email.strip().lower() for email in rows if email and "@" in email]
    if not recipients:
        fallback = get_settings().primary_super_admin_email.strip().lower()
        if fallback:
            recipients.append(fallback)
    return sorted(set(recipients))


def _email_line(value: Any) -> str:
    text = str(value or "").strip()
    return text or "-"


def _dealer_decision_from_result(result: dict[str, Any]) -> str | None:
    assessment = result.get("bankability_assessment") if isinstance(result.get("bankability_assessment"), dict) else {}
    fragments = [
        result.get("probability_status"),
        result.get("fundability_status"),
        result.get("status"),
        result.get("screen_status"),
        assessment.get("status") if isinstance(assessment, dict) else None,
        assessment.get("reason") if isinstance(assessment, dict) else None,
    ]
    joined = " ".join(str(fragment or "") for fragment in fragments).lower()
    if result.get("booking_recommended") is True or "good probability" in joined or "book call" in joined:
        return "approved"
    if any(term in joined for term in ("poor probability", "not fundable", "not bankable", "do not call", "declined", "denied")):
        return "denied"
    return None


async def _send_super_admin_email(
    db: AsyncSession,
    *,
    subject: str,
    body_text: str,
    body_html: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for recipient in await _super_admin_emails(db):
        result = send_email(to_email=recipient, subject=subject, body_text=body_text, body_html=body_html)
        records.append(
            {
                "recipient": recipient,
                "ok": result.ok,
                "status": result.detail,
                "message_id": result.message_id,
                "sent_at": _now().isoformat(),
            }
        )
    return records


async def _record_super_admin_intake_notification(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    request: Request,
) -> None:
    state = _intake_state(intake)
    if state.get("super_admin_intake_started_email"):
        return
    lead_url = _admin_lead_url(intake)
    bucket_url = _admin_bucket_url(intake)
    label = _dealer_label(intake)
    subject = f"Dealer AI intake started: {label}"
    body_text = (
        "A dealer started a Qualified Commercial AI funding review.\n\n"
        f"Dealer/business: {_email_line(intake.business_name)}\n"
        f"Contact: {_email_line(intake.full_name)}\n"
        f"Email: {_email_line(intake.email)}\n"
        f"Phone: {_email_line(intake.phone)}\n"
        f"Bucket: {_email_line(intake.bucket.name if intake.bucket else intake.bucket_id)}\n\n"
        f"Review lead: {lead_url}\n"
        f"Open bucket: {bucket_url}\n"
    )
    body_html = (
        "<p>A dealer started a Qualified Commercial AI funding review.</p>"
        "<ul>"
        f"<li><strong>Dealer/business:</strong> {html.escape(_email_line(intake.business_name))}</li>"
        f"<li><strong>Contact:</strong> {html.escape(_email_line(intake.full_name))}</li>"
        f"<li><strong>Email:</strong> {html.escape(_email_line(intake.email))}</li>"
        f"<li><strong>Phone:</strong> {html.escape(_email_line(intake.phone))}</li>"
        f"<li><strong>Bucket:</strong> {html.escape(_email_line(intake.bucket.name if intake.bucket else intake.bucket_id))}</li>"
        "</ul>"
        f'<p><a href="{html.escape(lead_url)}">Review dealer AI lead</a></p>'
        f'<p><a href="{html.escape(bucket_url)}">Open bucket</a></p>'
    )
    try:
        state["super_admin_intake_started_email"] = {
            "type": "dealer_ai_intake_started",
            "sent_at": _now().isoformat(),
            "deliveries": await _send_super_admin_email(db, subject=subject, body_text=body_text, body_html=body_html),
            **_request_audit(request),
        }
        intake.intake_state = state
    except Exception as exc:  # noqa: BLE001
        log.warning("dealer_ai_intake: super-admin start notification failed intake=%s: %s", intake.id, exc)


async def _record_super_admin_decision_notification(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    result: dict[str, Any],
    *,
    request: Request,
    review_id: UUID | None = None,
) -> None:
    decision = _dealer_decision_from_result(result)
    if decision is None:
        return
    state = _intake_state(intake)
    notifications = state.get("super_admin_decision_emails")
    if not isinstance(notifications, dict):
        notifications = {}
    if notifications.get(decision):
        return
    lead_url = _admin_lead_url(intake)
    bucket_url = _admin_bucket_url(intake)
    label = _dealer_label(intake)
    probability = _email_line(result.get("probability_status"))
    confidence = _email_line(result.get("confidence"))
    next_step = _email_line(result.get("one_next_step") or _next_step_text(result))
    if decision == "approved":
        subject = f"Dealer AI approved screen: {label}"
        headline = "The Dealer AI screen returned a positive result."
    else:
        subject = f"Dealer AI denied screen: {label}"
        headline = "The Dealer AI screen returned a negative result."
    body_text = (
        f"{headline}\n\n"
        "This is an AI preliminary screen, not a final commitment to lend.\n\n"
        f"Dealer/business: {_email_line(intake.business_name)}\n"
        f"Contact: {_email_line(intake.full_name)}\n"
        f"Email: {_email_line(intake.email)}\n"
        f"Probability status: {probability}\n"
        f"Confidence: {confidence}\n"
        f"Next step: {next_step}\n\n"
        f"Review lead: {lead_url}\n"
        f"Open bucket: {bucket_url}\n"
    )
    body_html = (
        f"<p>{html.escape(headline)}</p>"
        "<p><strong>This is an AI preliminary screen, not a final commitment to lend.</strong></p>"
        "<ul>"
        f"<li><strong>Dealer/business:</strong> {html.escape(_email_line(intake.business_name))}</li>"
        f"<li><strong>Contact:</strong> {html.escape(_email_line(intake.full_name))}</li>"
        f"<li><strong>Email:</strong> {html.escape(_email_line(intake.email))}</li>"
        f"<li><strong>Probability status:</strong> {html.escape(probability)}</li>"
        f"<li><strong>Confidence:</strong> {html.escape(confidence)}</li>"
        f"<li><strong>Next step:</strong> {html.escape(next_step)}</li>"
        "</ul>"
        f'<p><a href="{html.escape(lead_url)}">Review dealer AI lead</a></p>'
        f'<p><a href="{html.escape(bucket_url)}">Open bucket</a></p>'
    )
    try:
        notifications[decision] = {
            "type": f"dealer_ai_{decision}",
            "review_id": str(review_id) if review_id else None,
            "probability_status": result.get("probability_status"),
            "sent_at": _now().isoformat(),
            "deliveries": await _send_super_admin_email(db, subject=subject, body_text=body_text, body_html=body_html),
            **_request_audit(request),
        }
        state["super_admin_decision_emails"] = notifications
        intake.intake_state = state
    except Exception as exc:  # noqa: BLE001
        log.warning("dealer_ai_intake: super-admin decision notification failed intake=%s decision=%s: %s", intake.id, decision, exc)


async def _latest_active_intake_by_email(
    db: AsyncSession,
    email: str,
    *,
    variant: str | None = None,
) -> PublicUnderwritingIntake | None:
    query = (
        select(PublicUnderwritingIntake)
        .where(PublicUnderwritingIntake.email == _normalize_email(email))
        .join(Bucket, PublicUnderwritingIntake.bucket_id == Bucket.id)
        .where(Bucket.archived_at.is_(None))
        .options(
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
            selectinload(PublicUnderwritingIntake.bucket_upload_link),
            selectinload(PublicUnderwritingIntake.latest_review),
            with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
        )
        .order_by(PublicUnderwritingIntake.updated_at.desc())
    )
    if variant:
        query = query.where(PublicUnderwritingIntake.variant == variant)
    return (await db.execute(query)).scalars().first()


async def _start_login_challenge(
    db: AsyncSession,
    *,
    email: str,
    request: Request,
    reason: str,
    variant: str | None = None,
    review_label: str = "dealer funding review",
    event_prefix: str = "dealer_ai",
    target_type: str = "dealer_ai_intake",
) -> bool:
    normalized = _normalize_email(email)
    intake = await _latest_active_intake_by_email(db, normalized, variant=variant)
    if intake is None or intake.bucket is None or intake.bucket.archived_at is not None:
        return False
    email_hash = _hash_token(normalized)
    window_start = _now() - timedelta(minutes=DEALER_LOGIN_RATE_LIMIT_WINDOW_MINUTES)
    recent_count = (
        await db.execute(
            select(func.count(DealerIntakeLoginChallenge.id)).where(
                DealerIntakeLoginChallenge.email_hash == email_hash,
                DealerIntakeLoginChallenge.created_at >= window_start,
            )
        )
    ).scalar_one()
    if recent_count >= DEALER_LOGIN_RATE_LIMIT_MAX:
        return True
    code = _new_login_code()
    challenge = DealerIntakeLoginChallenge(
        intake_id=intake.id,
        email_hash=email_hash,
        code_hash=_hash_token(code),
        expires_at=_now() + timedelta(minutes=DEALER_LOGIN_CODE_TTL_MINUTES),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(challenge)
    _record_login_code_email(intake, code=code, request=request, reason=reason, review_label=review_label)
    await _log(
        db,
        intake.bucket_id,
        f"{event_prefix}_login_code_sent",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type=target_type,
        target_id=str(intake.id),
        detail=f"{review_label.title()} continuation code sent",
    )
    return True


async def _load_intake_by_dealer_session(db: AsyncSession, session_token: str) -> tuple[PublicUnderwritingIntake, DealerIntakeLoginChallenge]:
    session_hash = _hash_token(session_token)
    challenge = (
        await db.execute(
            select(DealerIntakeLoginChallenge)
            .where(
                DealerIntakeLoginChallenge.session_hash == session_hash,
                DealerIntakeLoginChallenge.revoked_at.is_(None),
                DealerIntakeLoginChallenge.session_expires_at > _now(),
            )
            .options(
                selectinload(DealerIntakeLoginChallenge.intake)
                .selectinload(PublicUnderwritingIntake.bucket)
                .selectinload(Bucket.requested_documents),
                selectinload(DealerIntakeLoginChallenge.intake)
                .selectinload(PublicUnderwritingIntake.bucket)
                .selectinload(Bucket.files),
                selectinload(DealerIntakeLoginChallenge.intake)
                .selectinload(PublicUnderwritingIntake.bucket)
                .selectinload(Bucket.notes),
                selectinload(DealerIntakeLoginChallenge.intake).selectinload(PublicUnderwritingIntake.bucket_upload_link),
                selectinload(DealerIntakeLoginChallenge.intake).selectinload(PublicUnderwritingIntake.latest_review),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if challenge is None or challenge.intake is None or challenge.intake.bucket.archived_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Dealer session is expired or invalid")
    return challenge.intake, challenge


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
            "title": "Upload Stage 1 cash-flow documents",
            "description": (
                "Upload last 2 years business tax returns, YTD P&L, and last 6 months from the main operating bank account first."
            ),
            "missing_document_ids": [str(doc.id) for doc in missing],
        }
    return None


def _widget_for_type(intake: PublicUnderwritingIntake, kind: str, *, source: str = "system_next_step", reason: str | None = None) -> dict[str, Any] | None:
    missing = _missing_required_docs(intake.bucket)
    widgets: dict[str, dict[str, Any]] = {
        "upload_files": {
            "type": "upload_files",
            "title": "Upload Stage 1 cash-flow documents",
            "description": (
                "Upload last 2 years business tax returns, YTD P&L, and last 6 months from the main operating bank account first."
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
            "description": "Answer only what you know: requested amount, detailed use of funds, and estimated credit score. Break down payoff, working capital, inventory, taxes, repairs, acquisitions, or other uses.",
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
    # The upload/entity/deal/real-estate widgets are car-dealer-worded (floorplan,
    # dealer LLCs, Stage 1 cash-flow docs). Suppress them for non-dealer intakes so
    # a real-estate file never surfaces dealer widgets; book_call / bankability_result
    # / run_review are product-neutral and stay available to both.
    review_type = (intake.bucket.ai_context or {}).get("review_type")
    dealer_only_widgets = {"upload_files", "entity_structure", "deal_profile", "real_estate_schedule", "referral"}
    if review_type != "dealer_gatekeeper_v1" and kind in dealer_only_widgets:
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
    rerun_terms = ("reanalyze", "re-analyze", "rerun", "re-run", "run again", "review again", "refresh review", "refresh screen")
    if any(term in text for term in rerun_terms):
        return "run_review"
    review_terms = ("review", "underwrite", "screen", "fundable", "bankable", "preliminary")
    if any(term in text for term in review_terms):
        return "bankability_result" if intake.result_snapshot else "run_review"
    return None


def _message_for_widget(widget: dict[str, Any] | None, intake: PublicUnderwritingIntake) -> str:
    if not widget:
        if isinstance(intake.result_snapshot, dict):
            return _format_review_update(intake.result_snapshot)
        # No widget and no review yet: give a product-appropriate opening so a
        # real-estate file never sees dealer-flavored fallback text.
        if intake.variant == FUNDING_VARIANT:
            return _funding_empty_message()
        if not _active_files(intake.bucket):
            return (
                "Your secure underwriter chat is open. Attach PDFs, images, ZIP files, spreadsheets, or bank/tax documents here, "
                "and I will screen what they prove before asking the next underwriting question."
            )
        return (
            "I am reading the uploaded file set like a banking underwriter. I will classify the documents by what they actually are, "
            "then tell you what they support and what baseline items are still missing."
        )
    kind = widget.get("type")
    if kind == "upload_files":
        return (
            "Your secure file room is open. Stage 1 starts with the cash-flow package only: last 2 years business tax returns, "
            "YTD P&L, and the last 6 months from the main operating bank account. I will ask for one clarification at a time after that."
        )
    if kind == "entity_structure":
        return (
            "Next I need to understand the dealership structure: the main operating LLC, the main operating bank account, "
            "any related LLCs, and how those accounts/entities work together."
        )
    if kind == "deal_profile":
        return (
            "I have files to review. Next, give me the rough requested amount, estimated credit score, and a detailed use of funds. "
            "Break down what the money is for, such as debt payoff, working capital, inventory, taxes, repairs, acquisition, or cash-out reserves. "
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


def _short_text(value: Any, *, max_len: int = 260) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _review_item_text(item: Any) -> str | None:
    if not isinstance(item, dict):
        return _short_text(item, max_len=190) if item else None
    title = _short_text(item.get("title") or item.get("document_type") or item.get("file_name") or item.get("category"), max_len=88)
    detail = _short_text(item.get("detail") or item.get("summary") or item.get("gap") or item.get("instructions") or item.get("reason"), max_len=180)
    if title and detail:
        return f"{title}: {detail}"
    return title or detail or None


def _evidence_items(result: dict[str, Any]) -> list[str]:
    evidence_map = result.get("document_evidence_map") if isinstance(result.get("document_evidence_map"), dict) else {}
    files = evidence_map.get("files") if isinstance(evidence_map, dict) else []
    items: list[str] = []
    if isinstance(files, list):
        for file_item in files:
            if not isinstance(file_item, dict):
                continue
            file_name = _short_text(file_item.get("file_name"), max_len=70)
            supports = file_item.get("supports") if isinstance(file_item.get("supports"), list) else []
            support_text = _short_text("; ".join(str(item) for item in supports[:2] if item), max_len=190)
            classification = _short_text(file_item.get("ai_classification"), max_len=70)
            if file_name and support_text:
                items.append(f"{file_name}: {support_text}")
            elif file_name and classification:
                items.append(f"{file_name}: classified as {classification}")
            if len(items) >= 4:
                break
    if not items:
        available = result.get("available_documents")
        if isinstance(available, list):
            items = [text for text in (_review_item_text(item) for item in available[:4]) if text]
    return items[:4]


def _blocking_items(result: dict[str, Any]) -> list[str]:
    raw_items: list[Any] = []
    missing = result.get("missing_or_incomplete_items")
    gaps = result.get("proof_of_funds_financial_collateral_gaps")
    if isinstance(missing, list):
        raw_items.extend(missing)
    if isinstance(gaps, list):
        raw_items.extend(gaps)
    seen: set[str] = set()
    items: list[str] = []
    for item in raw_items:
        text = _review_item_text(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)
        if len(items) >= 4:
            break
    return items


def _next_step_text(result: dict[str, Any]) -> str:
    next_best = result.get("next_best_action") if isinstance(result.get("next_best_action"), dict) else {}
    next_type = str(next_best.get("type") or "").lower()
    one_next_step = _short_text(result.get("one_next_step"), max_len=300)
    if one_next_step:
        return one_next_step
    detail = _short_text(next_best.get("detail") or next_best.get("title") or "", max_len=300)
    questions = result.get("underwriter_questions") if isinstance(result.get("underwriter_questions"), list) else []
    entity_question = None
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_text = str(question.get("question") or "")
        lower = question_text.lower()
        if any(term in lower for term in ("llc", "entity", "entities", "account", "bank account", "transfer")):
            entity_question = _short_text(question_text, max_len=260)
            break
    if next_type == "entity_structure" or (entity_question and not detail):
        return (
            "Reply with the primary operating LLC and the main operating bank account first. "
            "After that, I will ask how the related property LLCs move money with the dealership."
        )
    if detail:
        return detail
    if entity_question:
        return (
            "Reply with the primary operating LLC and main bank account first. "
            "We will handle the related LLC money-flow explanation after that."
        )
    return "Send the next baseline item or clarification requested above, and I will update the screen from there."


def _format_review_update(result: dict[str, Any]) -> str:
    assessment = result.get("bankability_assessment") if isinstance(result.get("bankability_assessment"), dict) else {}
    probability = _short_text(result.get("probability_status"), max_len=90)
    status_label = probability or _short_text(assessment.get("status") if isinstance(assessment, dict) else None, max_len=90) or "Updated"
    reason = _short_text(
        (assessment.get("reason") if isinstance(assessment, dict) else None)
        or result.get("executive_summary")
        or "Review the current evidence and missing baseline items.",
        max_len=420,
    )
    lines = [
        "Preliminary screen updated",
        "",
        "Status",
        f"- {status_label}",
    ]
    if reason:
        lines.append(f"- {reason}")
    evidence = _evidence_items(result)
    if evidence:
        lines.extend(["", "What the files prove", *[f"- {item}" for item in evidence]])
    blockers = _blocking_items(result)
    if blockers:
        lines.extend(["", "Still blocking a decision", *[f"- {item}" for item in blockers]])
    strengths = result.get("strengths") if isinstance(result.get("strengths"), list) else []
    risks = result.get("risks") if isinstance(result.get("risks"), list) else []
    if strengths:
        lines.extend(["", "Strengths", *[f"- {_short_text(item, max_len=180)}" for item in strengths[:3] if _short_text(item)]])
    if risks and not blockers:
        lines.extend(["", "Risks", *[f"- {_short_text(item, max_len=180)}" for item in risks[:3] if _short_text(item)]])
    lines.extend(
        [
            "",
            "Next step",
            f"- {_next_step_text(result)}",
            "- I will handle the rest in sequence after this answer, so you are not hit with every clarification at once.",
        ]
    )
    if result.get("booking_recommended") is True:
        lines.extend(["", "Booking", "- This looks strong enough to offer an underwriting call. Choose one of the available times below."])
    return "\n".join(lines)


def _dealer_context(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    state = _intake_state(intake)
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
        "chat_facts": state.get("chat_facts") if isinstance(state.get("chat_facts"), list) else [],
        "baseline_document_policy": {
            "stage": "stage_1_bankability",
            "allowed_document_categories": [
                "last 2 years business tax returns",
                "current year/YTD P&L",
                "last 6 months main operating bank statements",
                "requested amount",
                "detailed use of funds with amount breakdown",
                "stated current monthly debt payments",
                "estimated credit tier/score",
            ],
            "do_not_request_other_document_categories": True,
            "stage_2_after_good_probability_only": [
                "personal tax returns",
                "personal financial statement",
                "dealer license",
                "debt schedule",
                "mortgage statements",
                "property tax/insurance",
                "entity docs",
                "KYC/credit authorization",
                "appraisal/BPO/title items when needed",
            ],
        },
        "underwriting_focus": (
            "Strictly screen Stage 1 bankability for dealer capital without asking the client to choose a loan product. "
            "Infer likely paths such as real-estate-backed full doc, DSCR/collateral support, cash-out working capital, "
            "portfolio-backed funding, high-cost debt refinance, or floorplan support from the documents and answers. "
            "Stage 1 focuses on business tax returns, YTD P&L, main operating bank statements, requested amount, detailed use of funds, "
            "stated monthly debt, and estimated credit. Do not ask for the full Stage 2 package until Stage 1 shows good probability. "
            "Treat real estate and related LLC/account structure as targeted follow-up clarifications after cash-flow context supports a path."
        ),
        "custom_instructions": (
            "This is a public lead-magnet strict Stage 1 underwriter for car dealers. Ask first for last 2 years business tax returns, "
            "YTD P&L, last 6 months main operating bank statements, requested amount, a detailed use-of-funds breakdown, stated monthly debt payments, "
            "and estimated credit. The user may not know which lending product fits. Return one of these probability statuses: "
            "Good probability - book call, Promising but needs one clarification, Not enough evidence yet, or Poor probability based on current file. "
            "Set booking_recommended true only for Good probability - book call. Return a preliminary screen, not a commitment to lend."
        ),
    }


def _funding_review_context(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    state = _intake_state(intake)
    basics = state.get("funding_review_basics") if isinstance(state.get("funding_review_basics"), dict) else {}
    return {
        "review_type": "real_estate_dscr_v1",
        "deal_type": "real estate investor funding review",
        "documentation_level": "preliminary DSCR / investor review",
        "collateral_type": "income-producing real estate",
        "loan_purpose": intake.loan_purpose or basics.get("transaction_type"),
        "requested_loan_amount": float(intake.requested_loan_amount) if intake.requested_loan_amount is not None else basics.get("requested_amount"),
        "estimated_credit_tier": basics.get("estimated_credit_tier"),
        "investor_name": intake.business_name,
        "target_property_address": basics.get("target_property_address"),
        "transaction_type": basics.get("transaction_type"),
        "estimated_value_or_purchase_price": basics.get("estimated_value_or_purchase_price"),
        "monthly_rent": basics.get("monthly_rent"),
        "chat_facts": state.get("chat_facts") if isinstance(state.get("chat_facts"), list) else [],
        "baseline_document_policy": {
            "stage": "stage_1_dscr_property_screen",
            "allowed_document_categories": [
                "lease, rent roll, or rent support",
                "purchase contract for acquisition",
                "payoff or mortgage statement for refinance/cash-out",
                "property tax, insurance, HOA, or PITIA support",
                "entity or vesting documents when available",
                "requested amount",
                "estimated value or purchase price",
                "monthly rent",
                "transaction type",
                "estimated credit tier",
            ],
            "do_not_request_dealer_documents": True,
            "stage_1_metrics": [
                "DSCR",
                "LTV",
                "estimated property value",
                "loan amount",
                "equity",
                "monthly rent",
                "PITIA",
                "NOI",
                "cash to close",
                "max supportable loan",
                "reserve/cash gap",
                "credit-tier impact",
            ],
        },
        "underwriting_focus": (
            "Screen a real-estate investor file for DSCR and investor lending. Infer purchase, refinance, or cash-out path from the intake, chat, "
            "and documents. Focus on rent support, PITIA, DSCR, LTV, equity, cash to close, property condition, occupancy, lease/rent roll, "
            "purchase contract or payoff, taxes, insurance, HOA, entity/vesting, and estimated credit tier. Ask one next question or upload request at a time."
        ),
        "custom_instructions": (
            "This is not a car dealer review. Do not ask about dealership name, floorplan, MCA, inventory, gross receipts, or dealership LLC workflow. "
            "Return a preliminary funding screen for DSCR/investor real estate only. Use the probability statuses: Good probability - book call, "
            "Promising but needs one clarification, Not enough evidence yet, or Poor probability based on current file. Never invent DSCR, LTV, PITIA, "
            "cash-to-close, or rent metrics; use null or unavailable when evidence is missing."
        ),
    }


def _record_chat_fact(intake: PublicUnderwritingIntake, message: str | None, *, source: str = "client_chat") -> None:
    text = (message or "").strip()
    if not text:
        return
    state = _intake_state(intake)
    facts = state.get("chat_facts")
    if not isinstance(facts, list):
        facts = []
    facts.append({"at": _now().isoformat(), "source": source, "text": text[:1200]})
    state["chat_facts"] = facts[-30:]
    intake.intake_state = state


async def _recent_dealer_chat(db: AsyncSession, intake: PublicUnderwritingIntake) -> list[dict[str, str]]:
    # Include BOTH the client (uploader) thread and the internal admin thread. The
    # operator often corrects or supplements facts in the admin chat (e.g. a
    # restated credit score), and those corrections are authoritative — excluding
    # them made the summary/email report a stale client-stated value.
    rows = (
        await db.execute(
            select(BucketAIMessage)
            .where(
                BucketAIMessage.bucket_id == intake.bucket_id,
                BucketAIMessage.audience.in_(["uploader", "admin"]),
            )
            .order_by(BucketAIMessage.created_at.desc())
            .limit(32)
        )
    ).scalars().all()
    return [
        {
            "role": row.role,
            "author": row.author_name or row.role,
            "audience": row.audience,
            "content": row.content[:1600],
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }
        for row in reversed(rows)
    ]


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


async def _find_or_create_funding_client(db: AsyncSession, payload: FundingReviewStart) -> Client:
    email = _normalize_email(str(payload.email))
    client = (await db.execute(select(Client).where(Client.email == email).order_by(Client.created_at.desc()))).scalars().first()
    owner = await primary_super_admin(db)
    lead_payload = {
        "source": "funding_review",
        "investor_name": payload.investor_name,
        "target_property_address": payload.target_property_address,
        "transaction_type": payload.transaction_type,
        "requested_amount": payload.requested_amount,
        "estimated_value_or_purchase_price": payload.estimated_value_or_purchase_price,
        "monthly_rent": payload.monthly_rent,
        "estimated_credit_tier": payload.estimated_credit_tier,
    }
    if client is None:
        client = Client(
            name=payload.full_name.strip(),
            email=email,
            phone=payload.phone,
            referral_source="funding_review",
            originating_agent_id=owner.id if owner else None,
            current_agent_id=owner.id if owner else None,
            source_channel="funding_review",
            lead_source="other",
            lead_temperature="warm",
            financing_support_needed="yes",
            relationship_context="new_lead",
            client_experience_mode="self_directed",
            client_experience_mode_reason="funding_review",
            client_experience_mode_locked_by="firm",
            lead_intake=lead_payload,
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
    intake.update({key: value for key, value in lead_payload.items() if value is not None})
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
            "screening_stage": "stage_1_bankability",
            "deal_type": "dealer financing with real estate collateral",
            "documentation_level": "full doc",
            "collateral_type": "real estate collateral and business assets",
            "client_email": client.email,
            "stage_1_required_items": [
                "last 2 years business tax returns",
                "current year/YTD P&L",
                "last 6 months main operating bank statements",
                "requested amount",
                "detailed use of funds with amount breakdown",
                "stated current monthly debt payments",
                "estimated credit tier/score",
            ],
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


async def _create_bucket_for_funding_review(db: AsyncSession, client: Client, payload: FundingReviewStart, request: Request) -> tuple[Bucket, BucketUploadLink]:
    owner = await primary_super_admin(db)
    investor_name = payload.investor_name or payload.full_name
    bucket = Bucket(
        name=f"{investor_name} Funding Review",
        bucket_type="real_estate_ai_intake",
        client_name=investor_name,
        purpose="Real estate investor funding AI intake",
        description="Public DSCR and investor lending preliminary review.",
        ai_context={
            "review_type": "real_estate_dscr_v1",
            "screening_stage": "stage_1_dscr_property_screen",
            "deal_type": "real estate investor funding review",
            "documentation_level": "preliminary DSCR / investor review",
            "collateral_type": "income-producing residential or commercial real estate",
            "client_email": client.email,
            "target_property_address": payload.target_property_address,
            "transaction_type": payload.transaction_type,
            "requested_amount": payload.requested_amount,
            "estimated_value_or_purchase_price": payload.estimated_value_or_purchase_price,
            "monthly_rent": payload.monthly_rent,
            "estimated_credit_tier": payload.estimated_credit_tier,
            "stage_1_required_items": [
                "lease, rent roll, or rent support",
                "purchase contract, payoff, or mortgage statement",
                "property tax, insurance, HOA, or PITIA support",
                "estimated value or purchase price",
                "requested amount",
                "monthly rent",
                "transaction type",
                "estimated credit tier",
            ],
        },
        created_by_id=owner.id if owner else None,
    )
    db.add(bucket)
    await db.flush()
    for doc in REAL_ESTATE_REQUIRED_DOCUMENTS:
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
        "funding_review_intake_created",
        request=request,
        actor_name=payload.full_name,
        actor_email=client.email,
        actor_role="public_lead",
        target_type="bucket",
        target_id=str(bucket.id),
        detail="Public real estate funding review created",
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
    # Stamp the AI context for THIS intake's product — never force dealer context
    # onto a real-estate intake (this is reached from dealer chat/patch, the
    # client portal, and funding chat). Keys off the RE variant, which is stable
    # across the variant-normalization migration.
    context_fn = _funding_review_context if intake.variant == FUNDING_VARIANT else _dealer_context
    intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **context_fn(intake)}


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


def _artifact_download_url(artifact: PublicUnderwritingIntakeArtifact) -> str | None:
    if not artifact.s3_key:
        return None
    try:
        bucket, _prefix, _kms = _bucket_storage_config()
        filename = _safe_filename(f"{artifact.title or artifact.artifact_type}.pdf")
        return _s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": artifact.s3_key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=900,
        )
    except Exception:
        log.exception("Unable to build public underwriting artifact URL")
        return None


def _artifact_read(artifact: PublicUnderwritingIntakeArtifact) -> PublicUnderwritingArtifactRead:
    return PublicUnderwritingArtifactRead.model_validate(artifact).model_copy(
        update={"download_url": _artifact_download_url(artifact)}
    )


def _email_send_read(row: PublicUnderwritingIntakeEmailSend) -> PublicUnderwritingEmailSendRead:
    return PublicUnderwritingEmailSendRead.model_validate(row)


async def _management_artifacts(db: AsyncSession, intake_id: UUID) -> list[PublicUnderwritingIntakeArtifact]:
    return (
        await db.execute(
            select(PublicUnderwritingIntakeArtifact)
            .where(PublicUnderwritingIntakeArtifact.intake_id == intake_id)
            .order_by(PublicUnderwritingIntakeArtifact.created_at.desc())
        )
    ).scalars().all()


async def _management_email_sends(db: AsyncSession, intake_id: UUID) -> list[PublicUnderwritingIntakeEmailSend]:
    return (
        await db.execute(
            select(PublicUnderwritingIntakeEmailSend)
            .where(PublicUnderwritingIntakeEmailSend.intake_id == intake_id)
            .order_by(PublicUnderwritingIntakeEmailSend.created_at.desc())
            .limit(40)
        )
    ).scalars().all()


async def _latest_artifact(db: AsyncSession, intake_id: UUID, artifact_type: str) -> PublicUnderwritingIntakeArtifact | None:
    return (
        await db.execute(
            select(PublicUnderwritingIntakeArtifact)
            .where(
                PublicUnderwritingIntakeArtifact.intake_id == intake_id,
                PublicUnderwritingIntakeArtifact.artifact_type == artifact_type,
            )
            .order_by(PublicUnderwritingIntakeArtifact.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _ai_response_text(resp: Any) -> str:
    blocks = getattr(resp, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _strip_code_fence(text: str) -> str:
    """Remove a leading/trailing markdown code fence (```json ... ```), if present.

    The heavy/light models frequently wrap their JSON in a fenced block. Without
    stripping the fence, json.loads fails and the caller falls back to treating
    the whole fenced string as prose — which is exactly why the executive summary
    was rendering as raw JSON in the UI.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the closing fence.
        newline = stripped.find("\n")
        if newline != -1:
            stripped = stripped[newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -3]
    return stripped.strip()


def _repair_truncated_json(text: str) -> dict[str, Any] | None:
    """Recover a JSON object cut off by max_tokens: close an open string + any
    unbalanced brackets and drop a dangling key, then retry. Keeps a long
    exec-summary/email response from collapsing to a raw {"body": ...} blob when
    the model runs out of output budget mid-object. Returns the parsed dict or None."""
    cleaned = _strip_code_fence(text)
    if not cleaned.startswith("{"):
        return None
    in_string = False
    escape = False
    stack: list[str] = []
    for ch in cleaned:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    repaired = cleaned
    if in_string:
        repaired += '"'
    repaired = repaired.rstrip()
    # Truncation left a dangling object key with no value (`"key` just closed, or
    # `"key":` with nothing after) — drop it so the object closes validly.
    if in_string and stack and stack[-1] == "}":
        last_key_quote = repaired.rfind('"', 0, len(repaired) - 1)
        if last_key_quote > 0:
            repaired = repaired[:last_key_quote].rstrip().rstrip(",").rstrip()
    repaired = repaired.rstrip()
    while repaired and repaired[-1] in ":,":
        if repaired[-1] == ":":
            repaired = repaired[:-1].rstrip()
            key_q = repaired.rfind('"')
            key_q2 = repaired.rfind('"', 0, key_q) if key_q > 0 else -1
            if key_q2 >= 0:
                repaired = repaired[:key_q2].rstrip().rstrip(",").rstrip()
        else:
            repaired = repaired[:-1].rstrip()
    for closer in reversed(stack):
        repaired += closer
    try:
        parsed = json.loads(repaired)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _json_from_ai_text(text: str) -> dict[str, Any]:
    stripped = _strip_code_fence(text)
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {"body": stripped}
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(stripped[start:end + 1])
                return parsed if isinstance(parsed, dict) else {"body": stripped}
            except json.JSONDecodeError:
                pass
    # Recover a truncated object so structured fields (title, key_metrics, …)
    # survive instead of collapsing into a raw {"body": raw-json-string} fallback
    # that renders as JSON in the UI.
    repaired = _repair_truncated_json(stripped)
    if isinstance(repaired, dict) and repaired:
        return repaired
    return {"body": stripped}


def _latest_result_for_intake(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    review = intake.latest_review if intake.latest_review else None
    if review and isinstance(review.result, dict):
        return review.result
    if isinstance(intake.result_snapshot, dict):
        return intake.result_snapshot
    return {}


async def _lead_management_context(db: AsyncSession, intake: PublicUnderwritingIntake) -> dict[str, Any]:
    docs_by_id = {str(doc.id): doc for doc in intake.bucket.requested_documents}
    files = []
    for file in sorted(_active_files(intake.bucket), key=lambda item: item.created_at, reverse=True):
        doc = docs_by_id.get(str(file.requested_document_id)) if file.requested_document_id else None
        files.append(
            {
                "id": str(file.id),
                "file_name": file.file_name,
                "zip_entry_path": file.zip_entry_path,
                "content_type": file.content_type,
                "size_bytes": file.size_bytes,
                "requested_document": doc.name if doc else None,
                "created_at": file.created_at.isoformat() if file.created_at else None,
            }
        )
    missing_docs = [
        {"id": str(doc.id), "name": doc.name, "category": doc.category, "description": doc.description}
        for doc in _missing_required_docs(intake.bucket)
    ]
    chat_history = await _recent_dealer_chat(db, intake)
    return {
        "variant": intake.variant,
        "intake": {
            "id": str(intake.id),
            "full_name": intake.full_name,
            "email": intake.email,
            "phone": intake.phone,
            "business_name": intake.business_name,
            "loan_purpose": intake.loan_purpose,
            "requested_loan_amount": float(intake.requested_loan_amount) if intake.requested_loan_amount is not None else None,
            "estimated_credit_score": intake.estimated_credit_score,
            "referral_source": intake.referral_source,
            "asset_rows": intake.asset_rows,
            "intake_state": intake.intake_state,
        },
        "bucket": {
            "id": str(intake.bucket_id),
            "name": intake.bucket.name,
            "purpose": intake.bucket.purpose,
            "description": intake.bucket.description,
        },
        "files": files,
        "missing_documents": missing_docs,
        "latest_review": _latest_result_for_intake(intake),
        "chat_history": chat_history,
        # Deterministically resolved most-recent borrower-stated credit score, so the
        # model does not have to reconcile conflicting chat/prior-review values (it
        # was picking a stale earlier value). Authoritative when present.
        "authoritative_facts": _authoritative_facts_from_chat(chat_history, intake),
    }


_CREDIT_RE = re.compile(r"(\d{3})\s*\+?\s*(?:credit|fico)|(?:credit|fico)[^\d]{0,20}(\d{3})", re.IGNORECASE)


def _authoritative_facts_from_chat(
    chat_history: list[dict[str, str]], intake: PublicUnderwritingIntake
) -> dict[str, Any]:
    """Resolve facts the operator/borrower stated in chat that must override any
    stale figure in a prior review — currently the credit score. Scans newest-first
    and returns the most recent stated value so the summary/email never reports an
    outdated number."""
    facts: dict[str, Any] = {}
    # chat_history is oldest→newest; walk newest-first for the latest statement.
    # ONLY trust user/borrower/operator messages — an assistant reply may echo a
    # stale value, so counting assistant text would defeat the correction.
    for msg in reversed(chat_history):
        if str(msg.get("role") or "").lower() != "user":
            continue
        content = str(msg.get("content") or "")
        if not content:
            continue
        m = _CREDIT_RE.search(content)
        if m:
            score = m.group(1) or m.group(2)
            if score and 300 <= int(score) <= 850:
                plus = "+" if "+" in content else ""
                facts["credit_score"] = f"{score}{plus}"
                facts["credit_score_source"] = f"most recent borrower statement in chat ({msg.get('created_at') or 'chat'})"
                break
    if "credit_score" not in facts and intake.estimated_credit_score:
        facts["credit_score"] = str(intake.estimated_credit_score)
        facts["credit_score_source"] = "intake form"
    return facts


async def _collect_packet_financials(
    db: AsyncSession, intake: PublicUnderwritingIntake
) -> dict[str, Any]:
    """Pull the structured per-file facts the lender packet visualizes: month-over-month
    bank activity (last 6 months) and 2-year tax-return figures. Reads the durable
    per-file analysis cache (no new AI calls). Returns raw facts; the PDF renderer
    handles charting and redaction so this stays a thin data-loader."""
    active_ids = {file.id for file in _active_files(intake.bucket)}
    if not active_ids:
        return {"bank_months": [], "tax_years": []}
    rows = (
        await db.execute(
            select(BucketFileAnalysis)
            .where(
                BucketFileAnalysis.bucket_id == intake.bucket_id,
                BucketFileAnalysis.analysis_version == CURRENT_FILE_ANALYSIS_VERSION,
                BucketFileAnalysis.status == "completed",
            )
            .order_by(BucketFileAnalysis.created_at.desc())
        )
    ).scalars().all()

    from app.services.public_underwriting_packet_pdf import (
        extract_bank_months,
        extract_tax_years,
    )

    analyses = [
        {
            "file_id": str(row.bucket_file_id),
            "classification": row.classification,
            "key_facts": row.analysis.get("key_facts") if isinstance(row.analysis, dict) else {},
        }
        for row in rows
        if row.bucket_file_id in active_ids
    ]
    return {
        "bank_months": extract_bank_months(analyses),
        "tax_years": extract_tax_years(analyses),
    }


def _summary_title(intake: PublicUnderwritingIntake) -> str:
    label = intake.business_name or intake.full_name or "Underwriting lead"
    return f"{label} executive summary"


async def _generate_management_json(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
    *,
    purpose: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = await _lead_management_context(db, intake)
    variant_label = "real estate DSCR/investor" if intake.variant.startswith("real_estate") else "dealer capital"
    if purpose == "executive_summary":
        schema = {
            "title": "short title (borrower name + deal in a few words)",
            "executive_summary": "2-4 FLOWING PARAGRAPHS of underwriter prose — no bullet fragments, no key:value lines. Written like a credit officer's memo.",
            "recommended_approach": "1-2 sentences: the best loan approach/structure",
            "suggested_application_types": ["product/application paths that make sense"],
            "borrower_profile": "1-2 sentence narrative of the borrower/guarantor",
            "entity_vesting_notes": "1-2 sentence narrative of entity/ownership/vesting",
            "property_collateral": "1-2 sentence narrative of property/collateral",
            "requested_terms": "1-2 sentences: requested amount and purpose",
            "key_metrics": [
                {"label": "human label e.g. 2024 Gross Revenue", "value": "scalar value e.g. $31.2M", "note": "optional one-line context"}
            ],
            "documents_reviewed": ["file — what it proves"],
            "missing_confirmations": ["missing item or unsupported field"],
            "risks": ["risk item"],
            "mitigants": ["mitigant"],
            "vendor_submission_angle": "1-2 sentences: how to position this to vendors/lenders",
            "next_best_action": "one next operator action",
            "disclaimer": "preliminary review only",
        }
        instruction = (
            "Create an operator-facing executive summary for a Qualified Commercial AI underwriting lead. "
            "Use the uploaded evidence, chat answers, latest review, and intake data only. Do not invent values. "
            "If a field is unsupported, write 'Awaiting evidence'. "
            "When sources conflict, the MOST RECENT chat statement is authoritative and overrides any earlier chat "
            "message AND any figure in the prior 'latest_review' text (that prior review may be stale). "
            "If context.authoritative_facts.credit_score is present, you MUST use that exact credit score value "
            "everywhere and ignore any other credit number in the evidence or prior review."
        )
        system = (
            "You are a senior commercial credit officer writing an internal executive summary that a human underwriter "
            "will read and forward to lenders. Write in clear, confident underwriter prose — NOT machine output. "
            "The 'executive_summary' field MUST be 2-4 flowing narrative paragraphs (no bullet points, no 'key: value' "
            "fragments, no JSON-looking text inside it). Every narrative field is prose a person would write. "
            "'key_metrics' MUST be a flat list of {label, value, note?} objects where value is a SHORT scalar string "
            "(format examples only: a dollar figure, a ratio like 1.35x, or a percentage) — never a nested object or array. "
            "Be specific and cite figures from the evidence; never copy a number from these instructions. "
            "For any credit score/tier, use ONLY the value the borrower most recently stated in the chat (a later message "
            "overrides an earlier one) or a value present in an uploaded document — never estimate, round, or invent a "
            "credit score. If no credit score has been provided, write 'Not provided' rather than guessing. "
            "Return STRICT JSON only, matching the given shape exactly."
        )
        feature = "loan_summary"
    else:
        schema = {"subject": "email subject", "body": "editable outreach email body"}
        instruction = (
            "Prepare a lender/vendor outreach email that WE are sending TO an external lending party about this borrower. "
            "It must be professional, concise, and editable. Include the strongest evidence, suggested submission angle, "
            "missing confirmations, and say that a secure bucket login link and underwriting packet are included. Do not "
            "include unsecured file links. "
            "The email MUST open with the greeting 'Dear Lending Party,' — do NOT address it to Qualified Commercial and "
            "do NOT invent a specific recipient name or company. "
            "The email MUST close with this exact signature block on its own lines:\n"
            "Best regards,\nJonathan Franco"
        )
        system = (
            "You are writing, on behalf of the sender Jonathan Franco, a concise professional outreach email that will be "
            "sent to an outside lender/vendor. The sender is presenting a borrower's file to that lender — the lender is the "
            "recipient, NOT Qualified Commercial. Open with 'Dear Lending Party,' and sign off as 'Jonathan Franco'. Warm but "
            "businesslike; short paragraphs; no placeholders, no bracketed tokens, no raw field dumps. Return STRICT JSON "
            "only, matching the given shape exactly."
        )
        feature = "lender_send"
    prompt = {
        "purpose": purpose,
        "variant": variant_label,
        "instruction": instruction,
        "required_json_shape": schema,
        "context": context,
        "extra": extra or {},
    }
    resp = await tracked_messages_create(
        db,
        feature=feature,
        client=get_client(),
        model=model_light(),
        user_id=user.id,
        client_id=intake.client_id,
        metadata={"intake_id": intake.id, "bucket_id": intake.bucket_id, "purpose": purpose},
        # Generous budget so a full exec summary (many key_metrics + documents_reviewed)
        # is not truncated mid-JSON — truncation is the main cause of the summary
        # rendering as raw JSON.
        max_tokens=4096,
        temperature=0.3,
        system=system,
        messages=[{"role": "user", "content": json.dumps(json_safe_metadata(prompt), ensure_ascii=False)}],
    )
    parsed = _json_from_ai_text(_ai_response_text(resp))
    if purpose == "executive_summary" and not parsed.get("executive_summary") and parsed.get("body"):
        parsed["executive_summary"] = parsed["body"]
    return json_safe_metadata(parsed)


def _format_executive_summary_markdown(summary: dict[str, Any]) -> str:
    """Render the structured executive-summary JSON into clean, human-readable
    markdown so the UI and the exported package show prose, not raw key/values."""

    def _clean(value: Any) -> str:
        return str(value or "").strip()

    def _lines(value: Any) -> list[str]:
        if isinstance(value, list):
            return [_clean(item) for item in value if _clean(item)]
        text = _clean(value)
        return [text] if text else []

    parts: list[str] = []
    title = _clean(summary.get("title"))
    if title:
        parts.append(f"# {title}")

    body = _clean(summary.get("executive_summary")) or _clean(summary.get("body"))
    if body:
        parts.append(body)

    narrative_sections = [
        ("Recommended approach", summary.get("recommended_approach")),
        ("Borrower profile", summary.get("borrower_profile")),
        ("Entity & vesting", summary.get("entity_vesting_notes")),
        ("Property / collateral", summary.get("property_collateral")),
        ("Requested terms", summary.get("requested_terms")),
        ("Vendor submission angle", summary.get("vendor_submission_angle")),
    ]
    for label, value in narrative_sections:
        text = _clean(value)
        if text and text.lower() != "awaiting evidence":
            parts.append(f"## {label}\n{text}")

    metrics = summary.get("key_metrics")
    if isinstance(metrics, list) and metrics:
        rows = []
        for m in metrics:
            if isinstance(m, dict):
                label = _clean(m.get("label"))
                value = _clean(m.get("value"))
                note = _clean(m.get("note"))
                if label or value:
                    rows.append(f"- **{label or 'Metric'}:** {value}" + (f" — {note}" if note else ""))
        if rows:
            parts.append("## Key metrics\n" + "\n".join(rows))
    elif isinstance(metrics, dict) and metrics:
        # Back-compat: older records stored key_metrics as a dict.
        rows = [f"- **{str(k).replace('_', ' ').title()}:** {_clean(v)}" for k, v in metrics.items() if _clean(v)]
        if rows:
            parts.append("## Key metrics\n" + "\n".join(rows))

    list_sections = [
        ("Applications suggested", summary.get("suggested_application_types")),
        ("Documents reviewed", summary.get("documents_reviewed")),
        ("Strengths / mitigants", summary.get("mitigants")),
        ("Risks", summary.get("risks")),
        ("Missing confirmations", summary.get("missing_confirmations")),
    ]
    for label, value in list_sections:
        items = _lines(value)
        if items:
            parts.append(f"## {label}\n" + "\n".join(f"- {item}" for item in items))

    nba = _clean(summary.get("next_best_action"))
    if nba:
        parts.append(f"## Next best action\n{nba}")
    disclaimer = _clean(summary.get("disclaimer"))
    if disclaimer:
        parts.append(f"_{disclaimer}_")
    return "\n\n".join(parts).strip()


async def _create_executive_summary_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
) -> PublicUnderwritingIntakeArtifact:
    summary = await _generate_management_json(db, intake, user, purpose="executive_summary")
    title = str(summary.get("title") or _summary_title(intake))[:240]
    body_text = _format_executive_summary_markdown(summary)
    if not body_text:
        # Never surface a raw JSON blob: prefer the narrative field, and if only a
        # raw {"body": "...json..."} survived, pull the executive_summary string out.
        candidate = str(summary.get("executive_summary") or summary.get("body") or "").strip()
        if candidate.lstrip().startswith("{") or candidate.lstrip().startswith("```"):
            recovered = _repair_truncated_json(candidate) or {}
            candidate = str(recovered.get("executive_summary") or "").strip()
        body_text = candidate
    artifact = PublicUnderwritingIntakeArtifact(
        intake_id=intake.id,
        artifact_type="executive_summary",
        title=title,
        body_text=body_text,
        body_json=summary,
        created_by_user_id=user.id,
    )
    db.add(artifact)
    await db.flush()
    await _log(
        db,
        intake.bucket_id,
        "underwriting_executive_summary_generated",
        user=user,
        actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=title,
    )
    return artifact


async def _ensure_executive_summary_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
) -> PublicUnderwritingIntakeArtifact:
    existing = await _latest_artifact(db, intake.id, "executive_summary")
    return existing or await _create_executive_summary_artifact(db, intake, user)


async def _store_lender_packet_pdf(
    intake: PublicUnderwritingIntake,
    pdf_bytes: bytes,
    title: str,
) -> str:
    bucket, prefix, kms_key_id = _bucket_storage_config()
    key_prefix = f"{prefix}/public-underwriting/{intake.id}/artifacts" if prefix else f"public-underwriting/{intake.id}/artifacts"
    key = f"{key_prefix}/{uuid4()}-{_safe_filename(title)}.pdf"
    await asyncio.to_thread(
        _s3_client().put_object,
        Bucket=bucket,
        Key=key,
        Body=pdf_bytes,
        ContentType="application/pdf",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=kms_key_id,
    )
    return key


async def _create_lender_packet_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
    executive_summary: PublicUnderwritingIntakeArtifact | None = None,
) -> PublicUnderwritingIntakeArtifact:
    summary_artifact = executive_summary or await _ensure_executive_summary_artifact(db, intake, user)
    files = sorted(_active_files(intake.bucket), key=lambda file: file.created_at, reverse=True)
    missing_docs = _missing_required_docs(intake.bucket)
    financials = await _collect_packet_financials(db, intake)
    title = f"{intake.business_name or intake.full_name or 'Lead'} lender packet"
    pdf_bytes = await asyncio.to_thread(
        render_underwriting_packet_pdf,
        intake=intake,
        files=files,
        missing_docs=missing_docs,
        result=_latest_result_for_intake(intake),
        executive_summary=summary_artifact.body_json if isinstance(summary_artifact.body_json, dict) else None,
        financials=financials,
    )
    s3_key = await _store_lender_packet_pdf(intake, pdf_bytes, title)
    artifact = PublicUnderwritingIntakeArtifact(
        intake_id=intake.id,
        artifact_type="lender_packet",
        title=title,
        body_text="Qualified Commercial underwriting packet PDF generated for lender/vendor review.",
        body_json={"source_summary_artifact_id": str(summary_artifact.id), "size_bytes": len(pdf_bytes)},
        s3_key=s3_key,
        created_by_user_id=user.id,
    )
    db.add(artifact)
    await db.flush()
    await _log(
        db,
        intake.bucket_id,
        "underwriting_lender_packet_generated",
        user=user,
        actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=title,
    )
    return artifact


async def _ensure_lender_packet_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
) -> PublicUnderwritingIntakeArtifact:
    existing = await _latest_artifact(db, intake.id, "lender_packet")
    return existing or await _create_lender_packet_artifact(db, intake, user)


async def _s3_bytes(s3_key: str) -> bytes:
    bucket, _prefix, _kms = _bucket_storage_config()

    def _read() -> bytes:
        response = _s3_client().get_object(Bucket=bucket, Key=s3_key)
        return response["Body"].read()

    return await asyncio.to_thread(_read)


async def _prepare_vendor_access(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    email: str,
    payload: VendorEmailSendRequest,
) -> BucketVendorAccess:
    vendor = await _vendor_user_from_payload(
        db,
        vendor_user_id=None,
        vendor_name=email.split("@", 1)[0],
        vendor_email=email,
        send_invite=True,
    )
    access = (
        await db.execute(
            select(BucketVendorAccess).where(
                BucketVendorAccess.bucket_id == intake.bucket_id,
                BucketVendorAccess.vendor_user_id == vendor.id,
            )
        )
    ).scalar_one_or_none()
    if access is None:
        access = BucketVendorAccess(bucket_id=intake.bucket_id, vendor_user_id=vendor.id)
        db.add(access)
        await db.flush()
        event_name = "vendor_access_created"
    else:
        event_name = "vendor_access_updated"
    access.status = "active"
    access.file_scope = "all_active"
    access.files = []
    access.can_preview = payload.can_preview
    access.can_download = payload.can_download
    access.can_add_notes = payload.can_add_notes
    access.can_see_internal_notes = False
    access.can_view_ai_summary = payload.can_view_ai_summary
    access.can_use_ai_chat = payload.can_use_ai_chat
    access.can_view_ai_tasks = payload.can_view_ai_tasks
    access.can_propose_tasks = payload.can_propose_tasks
    await _log(
        db,
        intake.bucket_id,
        event_name,
        actor_name="Qualified Commercial",
        actor_role="system",
        target_type="vendor_access",
        target_id=str(access.id),
        detail=f"Vendor package access prepared for {email}",
    )
    return access


# Keys of intake_state that are safe to expose to the public/uploader token
# holder. Everything else (super-admin notification emails + message-ids,
# login/resume email delivery records with reviewer IPs/user-agents, retained
# chat_facts) is internal audit data and must never reach the dealer.
_CLIENT_SAFE_INTAKE_STATE_KEYS = ("entity_structure", "call_booking", "deal_profile")


def _client_safe_intake_state(state: Any) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    safe = {key: state[key] for key in _CLIENT_SAFE_INTAKE_STATE_KEYS if key in state}
    return safe or None


def _client_safe_result(result: Any) -> Any:
    """Strip internal underwriter-only fields from an AI review result before
    it reaches a public/uploader response. The dealer intelligence panel shows
    bankability / key_metrics / strengths / risks; it never reads the fields
    removed here (raw AI context, per-file red flags, question routing)."""
    if not isinstance(result, dict):
        return result
    safe = {key: value for key, value in result.items() if key != "context_snapshot"}
    questions = safe.get("underwriter_questions")
    if isinstance(questions, list):
        safe["underwriter_questions"] = [
            {k: v for k, v in item.items() if k not in ("route", "reason")} if isinstance(item, dict) else item
            for item in questions
        ]
    per_file = safe.get("per_file_summaries")
    if isinstance(per_file, list):
        safe["per_file_summaries"] = [
            {k: v for k, v in item.items() if k != "red_flags"} if isinstance(item, dict) else item
            for item in per_file
        ]
    return safe


async def _response(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    token: str | None,
    session_token: str | None = None,
    assistant_message: str | None = None,
    messages: list[Any] | None = None,
    forced_widget_type: str | None = None,
    forced_widget_source: str = "user_intent",
    forced_widget_reason: str | None = None,
    public_path: str = "/dealer-ai-underwriter",
    empty_message: str | None = None,
    include_management: bool = False,
    admin_thread: bool = False,
) -> DealerIntakeResponse:
    review = intake.latest_review if intake.latest_review else None
    latest_result = review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
    widget = None
    if latest_result and latest_result.get("booking_recommended") is True and not _call_booked(intake):
        widget = await _decorate_widget(
            db,
            intake,
            _widget_for_type(
                intake,
                "book_call",
                source="ai_recommended",
                reason="Stage 1 screen shows good probability.",
            ),
        )
    files = sorted(_active_files(intake.bucket), key=lambda file: file.created_at, reverse=True)
    summary = upload_link_visible_summary(review, intake.bucket)
    if messages is None:
        # The admin cockpit reads the PRIVATE internal thread (audience='admin');
        # the public/uploader surfaces read the client-visible 'uploader' thread.
        if admin_thread:
            filters = [
                BucketAIMessage.bucket_id == intake.bucket_id,
                BucketAIMessage.audience == "admin",
            ]
        else:
            filters = [
                BucketAIMessage.bucket_id == intake.bucket_id,
                BucketAIMessage.audience == "uploader",
                BucketAIMessage.upload_link_id == intake.bucket_upload_link_id,
            ]
        recent = (
            await db.execute(
                select(BucketAIMessage)
                .where(*filters)
                .order_by(BucketAIMessage.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        messages = list(reversed(recent))
    # Management artifacts + email sends are populated only for the super-admin
    # dealer-lead endpoint (include_management=True); every public/uploader/
    # funding caller gets empty lists.
    artifacts = await _management_artifacts(db, intake.id) if include_management else []
    email_sends = await _management_email_sends(db, intake.id) if include_management else []
    intake_read = DealerIntakeRead.model_validate(intake)
    # Redact internal-only data from the public/uploader payload. The dealer
    # sees only whitelisted intake_state keys and a sanitized review result.
    intake_read.intake_state = _client_safe_intake_state(intake.intake_state)
    intake_read.result_snapshot = _client_safe_result(intake.result_snapshot)
    review_read = None
    if review:
        review_read = BucketAIReviewRead.model_validate(review)
        review_read.result = _client_safe_result(review_read.result)
        review_read.context_snapshot = None
    return DealerIntakeResponse(
        intake=intake_read,
        token=token,
        session_token=session_token,
        resume_url=_public_url(f"{public_path}?token={token}") if token else None,
        upload_url=_public_url(f"/buckets/request/{intake.bucket_upload_link.token}") if intake.bucket_upload_link else None,
        assistant_message=assistant_message or (_format_review_update(latest_result) if latest_result else empty_message or _message_for_widget(widget, intake)),
        widget=widget,
        requested_documents=[BucketRequestedDocumentRead.model_validate(doc) for doc in intake.bucket.requested_documents],
        files=[BucketRequestUploadedFileRead.model_validate(file) for file in files],
        ai_summary=summary,
        latest_review=review_read,
        messages=[BucketAIMessageRead.model_validate(message) for message in (messages or [])],
        artifacts=[_artifact_read(artifact) for artifact in artifacts],
        email_sends=[_email_send_read(row) for row in email_sends],
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
        content_type=_sanitize_upload_content_type(payload.content_type),
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


def _is_zip_upload(file: BucketFile) -> bool:
    media = (file.content_type or "").lower()
    return file.file_name.lower().endswith(".zip") or "application/zip" in media or "application/x-zip-compressed" in media


def _safe_zip_entry_path(name: str) -> str | None:
    normalized = name.replace("\\", "/").strip("/")
    if not normalized or normalized.endswith("/"):
        return None
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    if not parts or len(parts) != len(normalized.split("/")):
        return None
    return "/".join(parts)[:700]


def _zip_entry_supported(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in ZIP_SUPPORTED_EXTENSIONS)


def _guess_entry_content_type(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    if guessed:
        return guessed
    lower = name.lower()
    if lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if lower.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


def _read_bucket_object(s3_key: str) -> bytes | None:
    bucket, _, _ = _bucket_storage_config()
    try:
        obj = _s3_client().get_object(Bucket=bucket, Key=s3_key)
        return obj["Body"].read()
    except Exception:  # noqa: BLE001
        return None


def _put_bucket_object(s3_key: str, content_type: str, data: bytes) -> None:
    bucket, _, kms_key_id = _bucket_storage_config()
    _s3_client().put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=data,
        ContentType=content_type,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=kms_key_id,
    )


async def _extract_zip_bucket_files(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    file: BucketFile,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
) -> None:
    if not _is_zip_upload(file):
        return
    if file.extraction_status in {"extracted", "partial", "skipped"}:
        return
    raw = _read_bucket_object(file.s3_key)
    if raw is None:
        file.extraction_status = "skipped"
        file.extraction_reason = json.dumps([{"entry": file.file_name, "reason": "zip_fetch_failed"}])
        return
    skipped: list[dict[str, str]] = []
    extracted = 0
    total_bytes = 0
    _, prefix, _ = _bucket_storage_config()
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            for index, member in enumerate(archive.infolist()):
                if index >= ZIP_MAX_ENTRIES:
                    skipped.append({"entry": member.filename, "reason": "zip_entry_limit"})
                    continue
                entry_path = _safe_zip_entry_path(member.filename)
                if not entry_path:
                    skipped.append({"entry": member.filename, "reason": "zip_unsafe_path"})
                    continue
                if entry_path.lower().endswith(".zip"):
                    skipped.append({"entry": entry_path, "reason": "nested_zip_unsupported"})
                    continue
                if member.flag_bits & 0x1:
                    skipped.append({"entry": entry_path, "reason": "zip_entry_encrypted"})
                    continue
                if member.file_size > ZIP_MAX_ENTRY_BYTES:
                    skipped.append({"entry": entry_path, "reason": "zip_entry_too_large"})
                    continue
                if total_bytes + member.file_size > ZIP_MAX_TOTAL_BYTES:
                    skipped.append({"entry": entry_path, "reason": "zip_total_too_large"})
                    continue
                if not _zip_entry_supported(entry_path):
                    skipped.append({"entry": entry_path, "reason": "unsupported_file_type"})
                    continue
                duplicate = (
                    await db.execute(
                        select(BucketFile).where(
                            BucketFile.parent_zip_file_id == file.id,
                            BucketFile.zip_entry_path == entry_path,
                            BucketFile.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if duplicate is not None:
                    continue
                try:
                    data = archive.read(member)
                except RuntimeError:
                    skipped.append({"entry": entry_path, "reason": "zip_entry_encrypted"})
                    continue
                total_bytes += len(data)
                content_type = _sanitize_upload_content_type(_guess_entry_content_type(entry_path))
                child_id = uuid4()
                safe = _safe_filename(entry_path.split("/")[-1])
                child_key = f"{prefix}/uploads/{intake.bucket_id}/{child_id}-zip-{safe}"
                _put_bucket_object(child_key, content_type, data)
                child = BucketFile(
                    id=child_id,
                    bucket_id=intake.bucket_id,
                    requested_document_id=None,
                    upload_link_id=intake.bucket_upload_link_id,
                    file_name=entry_path,
                    s3_key=child_key,
                    content_type=content_type,
                    size_bytes=len(data),
                    uploaded_by_name=actor_name,
                    uploaded_by_email=actor_email,
                    status="uploaded",
                    parent_zip_file_id=file.id,
                    zip_entry_path=entry_path,
                    extraction_status="extracted",
                )
                db.add(child)
                extracted += 1
    except zipfile.BadZipFile:
        skipped.append({"entry": file.file_name, "reason": "zip_parse_failed"})
    file.extraction_status = "extracted" if extracted and not skipped else "partial" if extracted else "skipped"
    file.extraction_reason = json.dumps(skipped[-80:])
    if extracted:
        await _log(
            db,
            intake.bucket_id,
            "dealer_ai_zip_extracted",
            request=request,
            actor_name=actor_name,
            actor_email=actor_email,
            actor_role="public_lead",
            target_type="file",
            target_id=str(file.id),
            detail=f"Extracted {extracted} supported file(s) from {file.file_name}",
        )


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
    await _extract_zip_bucket_files(db, intake, file, request, actor_name=actor_name, actor_email=actor_email)
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
    # Queue this file (and any files extracted from it as a zip) for background
    # analysis so the review composes from a warm per-file cache.
    try:
        from app.services.bucket_ai import enqueue_file_analysis

        await db.flush()
        children = (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.parent_zip_file_id == file.id,
                    BucketFile.deleted_at.is_(None),
                    BucketFile.status == "uploaded",
                )
            )
        ).scalars().all()
        for target in [file, *children]:
            await enqueue_file_analysis(db, target)
    except Exception:  # noqa: BLE001
        log.exception("enqueue file analysis failed intake=%s file=%s", intake.id, file.id)
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
    _throttle_or_429(
        _START_LAST_BY_IP,
        (request.client.host if request.client else "?") or "?",
        _START_MIN_INTERVAL_SECONDS,
        "Please wait a moment before starting another review.",
    )
    existing = await _latest_active_intake_by_email(db, str(payload.email))
    if existing is not None:
        await _start_login_challenge(db, email=str(payload.email), request=request, reason="existing_intake_start")
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A secure dealer file already exists for this email. We sent a short access code so you can continue that file.",
        )
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
    await _record_super_admin_intake_notification(db, intake, request=request)
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


@router.post("/login/start", response_model=DealerLoginStartResponse)
async def start_dealer_login(
    payload: DealerLoginStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerLoginStartResponse:
    login_required = await _start_login_challenge(db, email=str(payload.email), request=request, reason="dealer_login_requested")
    await db.commit()
    return DealerLoginStartResponse(
        login_required=login_required,
        message=(
            "We found an existing secure dealer file for this email. Enter the code we sent to continue."
            if login_required
            else "No existing secure dealer file was found. Complete Step 1 to start a new review."
        ),
    )


@router.post("/login/verify", response_model=DealerIntakeResponse)
async def verify_dealer_login(
    payload: DealerLoginVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    email = _normalize_email(str(payload.email))
    email_hash = _hash_token(email)
    challenge = (
        await db.execute(
            select(DealerIntakeLoginChallenge)
            .where(
                DealerIntakeLoginChallenge.email_hash == email_hash,
                DealerIntakeLoginChallenge.used_at.is_(None),
                DealerIntakeLoginChallenge.revoked_at.is_(None),
                DealerIntakeLoginChallenge.expires_at > _now(),
            )
            .order_by(DealerIntakeLoginChallenge.created_at.desc())
        )
    ).scalars().first()
    if challenge is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    if challenge.attempt_count >= DEALER_LOGIN_MAX_ATTEMPTS:
        challenge.revoked_at = _now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    if _hash_token(payload.code.strip()) != challenge.code_hash:
        challenge.attempt_count += 1
        if challenge.attempt_count >= DEALER_LOGIN_MAX_ATTEMPTS:
            challenge.revoked_at = _now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    session_token = secrets.token_urlsafe(40)
    public_token = _new_public_token()
    challenge.used_at = _now()
    challenge.session_hash = _hash_token(session_token)
    challenge.session_expires_at = _now() + timedelta(hours=DEALER_LOGIN_SESSION_TTL_HOURS)
    intake = await db.get(PublicUnderwritingIntake, challenge.intake_id)
    if intake is None:
        challenge.revoked_at = _now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    intake.token_hash = _hash_token(public_token)
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_login_verified",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="dealer_ai_intake",
        target_id=str(intake.id),
        detail="Dealer AI continuation login verified",
    )
    await db.commit()
    intake = await _load_public_intake(db, public_token)
    return await _response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        assistant_message="Welcome back. I restored your secure dealer funding room with your prior uploads and chat context.",
    )


@router.get("/session", response_model=DealerIntakeResponse)
async def get_dealer_session(request: Request, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    session_token = _dealer_session_from_request(request)
    if not session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Dealer session is required")
    intake, _challenge = await _load_intake_by_dealer_session(db, session_token)
    public_token = _new_public_token()
    intake.token_hash = _hash_token(public_token)
    await db.commit()
    intake = await _load_public_intake(db, public_token)
    return await _response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        assistant_message="Welcome back. I restored your secure dealer funding room with your prior uploads and chat context.",
    )


@router.post("/logout", response_model=DealerLogoutResponse)
async def logout_dealer_session(
    payload: DealerLogoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerLogoutResponse:
    session_token = payload.session_token or _dealer_session_from_request(request)
    if session_token:
        challenge = (
            await db.execute(
                select(DealerIntakeLoginChallenge).where(
                    DealerIntakeLoginChallenge.session_hash == _hash_token(session_token),
                    DealerIntakeLoginChallenge.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if challenge is not None:
            challenge.revoked_at = _now()
            await db.commit()
    return DealerLogoutResponse()


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


@router.get("/intelligence.pdf")
async def download_dealer_intelligence_pdf(
    request: Request,
    token: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    if token:
        intake = await _load_public_intake(db, token)
    else:
        session_token = _dealer_session_from_request(request)
        if not session_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Dealer session or resume token required")
        intake, _challenge = await _load_intake_by_dealer_session(db, session_token)
    _require_dealer_intake(intake)
    review = intake.latest_review if intake.latest_review else None
    latest_result = review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
    files = sorted(_active_files(intake.bucket), key=lambda file: file.created_at, reverse=True)
    missing_docs = _missing_required_docs(intake.bucket)
    pdf_bytes = await asyncio.to_thread(
        render_dealer_intelligence_pdf,
        intake=intake,
        files=files,
        missing_docs=missing_docs,
        result=latest_result,
    )
    filename = _safe_filename(f"dealer-intelligence-{intake.business_name or intake.full_name or 'review'}.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _require_super_admin(user: CurrentUser) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


def _lead_result(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    review = intake.latest_review if intake.latest_review else None
    if review and isinstance(review.result, dict):
        return review.result
    return intake.result_snapshot if isinstance(intake.result_snapshot, dict) else {}


def _lead_row(intake: PublicUnderwritingIntake) -> DealerAILeadRow:
    result = _lead_result(intake)
    active_files = _active_files(intake.bucket)
    missing_docs = _missing_required_docs(intake.bucket)
    return DealerAILeadRow(
        id=intake.id,
        variant=intake.variant,
        client_id=intake.client_id,
        bucket_id=intake.bucket_id,
        bucket_name=intake.bucket.name,
        full_name=intake.full_name,
        email=intake.email,
        phone=intake.phone,
        business_name=intake.business_name,
        status=intake.status,
        probability_status=str(result.get("probability_status") or "") or None,
        confidence=str(result.get("confidence") or "") or None,
        one_next_step=str(result.get("one_next_step") or "") or None,
        latest_review_status=intake.latest_review.status if intake.latest_review else None,
        booking_recommended=result.get("booking_recommended") is True,
        call_booked=_call_booked(intake),
        file_count=len(active_files),
        missing_required_count=len(missing_docs),
        requested_loan_amount=float(intake.requested_loan_amount) if intake.requested_loan_amount is not None else None,
        estimated_credit_score=int(intake.estimated_credit_score) if intake.estimated_credit_score is not None else None,
        created_at=intake.created_at,
        updated_at=intake.updated_at,
        last_message_at=intake.last_message_at,
    )


async def _load_admin_dealer_lead(db: AsyncSession, intake_id: UUID) -> PublicUnderwritingIntake:
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == intake_id)
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI lead not found")
    return intake


async def _execute_intake_review(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    request: Request,
    actor_name: str,
    actor_email: str | None,
    actor_role: str,
    log_event: str,
    detail: str,
    requested_by_user_id: UUID | None = None,
) -> BucketAIReview | None:
    """Run an AI review INLINE over the intake's current bucket uploads and
    snapshot the result back onto the intake. Shared by the public dealer/funding
    run-review endpoints and the admin re-run endpoint.

    Picks the AI context by variant so a real-estate lead is never re-run with
    dealer context (and vice-versa). Returns the fresh review (or None)."""
    is_funding = intake.variant == FUNDING_VARIANT
    context_fn = _funding_review_context if is_funding else _dealer_context
    recent_key = "recent_funding_review_chat" if is_funding else "recent_dealer_chat"
    recent_chat = await _recent_dealer_chat(db, intake)
    review_context = {
        **(intake.bucket.ai_context or {}),
        **context_fn(intake),
        recent_key: recent_chat,
        # Deterministically resolved borrower-stated facts (e.g. latest credit
        # score) so the review synthesis reports the corrected value, not a stale
        # one echoed in a prior review.
        "authoritative_facts": _authoritative_facts_from_chat(recent_chat, intake),
    }
    intake.bucket.ai_context = review_context
    review = BucketAIReview(
        bucket_id=intake.bucket_id,
        requested_by_user_id=requested_by_user_id,
        status="queued",
        context_snapshot=review_context,
        file_ids=[str(file.id) for file in _active_files(intake.bucket)],
        provider="bedrock",
    )
    review.progress = {"stage": "queued", "label": "Preparing the review…", "percent": 0, "files_total": 0, "files_done": 0}
    db.add(review)
    await db.flush()
    await _log(
        db,
        intake.bucket_id,
        log_event,
        request=request,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role=actor_role,
        target_type="ai_review",
        target_id=str(review.id),
        detail=detail,
    )
    await run_bucket_ai_review(db, review.id)
    fresh_review = await latest_review(db, intake.bucket_id)
    intake.latest_review_id = fresh_review.id if fresh_review else review.id
    if fresh_review and isinstance(fresh_review.result, dict):
        intake.result_snapshot = fresh_review.result
        intake.status = "reviewed"
        intake.completed_at = _now()
    return fresh_review


async def _create_queued_review(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    request: Request,
    actor_name: str,
    actor_email: str | None,
    actor_role: str,
    log_event: str,
    detail: str,
    requested_by_user_id: UUID | None = None,
) -> BucketAIReview:
    """Create a queued review row (committed) for a lead, picking the AI context
    by variant. The heavy pass runs separately (background) so the request
    returns immediately and the UI can poll progress."""
    is_funding = intake.variant == FUNDING_VARIANT
    context_fn = _funding_review_context if is_funding else _dealer_context
    recent_key = "recent_funding_review_chat" if is_funding else "recent_dealer_chat"
    recent_chat = await _recent_dealer_chat(db, intake)
    review_context = {
        **(intake.bucket.ai_context or {}),
        **context_fn(intake),
        recent_key: recent_chat,
        # Deterministically resolved borrower-stated facts (e.g. latest credit
        # score) so the review synthesis reports the corrected value, not a stale
        # one echoed in a prior review.
        "authoritative_facts": _authoritative_facts_from_chat(recent_chat, intake),
    }
    intake.bucket.ai_context = review_context
    review = BucketAIReview(
        bucket_id=intake.bucket_id,
        requested_by_user_id=requested_by_user_id,
        status="queued",
        context_snapshot=review_context,
        file_ids=[str(file.id) for file in _active_files(intake.bucket)],
        provider="bedrock",
        progress={"stage": "queued", "label": "Preparing the review…", "percent": 0, "files_total": 0, "files_done": 0},
    )
    db.add(review)
    await db.flush()
    await _log(
        db,
        intake.bucket_id,
        log_event,
        request=request,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role=actor_role,
        target_type="ai_review",
        target_id=str(review.id),
        detail=detail,
    )
    await db.commit()
    return review


async def _run_review_background(review_id: UUID, intake_id: UUID) -> None:
    """Run a queued review to completion in its own DB session (survives the
    request), then snapshot the result onto the intake. Errors are captured on
    the review row (status/progress='error') by run_bucket_ai_review."""
    from app.db import SessionLocal

    async with SessionLocal() as db:
        try:
            await run_bucket_ai_review(db, review_id)
        except Exception:  # noqa: BLE001
            await db.rollback()
            log.exception("background review failed review=%s", review_id)
            return
        # Snapshot the completed result onto the intake.
        try:
            intake = await db.get(PublicUnderwritingIntake, intake_id)
            review = await db.get(BucketAIReview, review_id)
            if intake is not None and review is not None:
                intake.latest_review_id = review_id
                if isinstance(review.result, dict):
                    intake.result_snapshot = review.result
                    intake.status = "reviewed"
                    intake.completed_at = _now()
                await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            log.exception("background review snapshot failed review=%s", review_id)


@admin_router.get("", response_model=DealerAILeadListResponse)
async def list_dealer_ai_leads(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    status_filter: str | None = None,
    probability_status: str | None = None,
    variant_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DealerAILeadListResponse:
    _require_super_admin(user)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    stmt = (
        select(PublicUnderwritingIntake)
        .join(Bucket, PublicUnderwritingIntake.bucket_id == Bucket.id)
        .where(Bucket.archived_at.is_(None))
        .options(
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
            selectinload(PublicUnderwritingIntake.bucket_upload_link),
            selectinload(PublicUnderwritingIntake.latest_review),
            with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
        )
        .order_by(PublicUnderwritingIntake.updated_at.desc())
    )
    if status_filter and status_filter != "all":
        stmt = stmt.where(PublicUnderwritingIntake.status == status_filter)
    if variant_filter and variant_filter != "all":
        if variant_filter == "dealer":
            # Accept both the canonical and legacy dealer variant so the filter is
            # correct before and after the 0090 normalization migration.
            stmt = stmt.where(PublicUnderwritingIntake.variant.in_(DEALER_VARIANTS))
        elif variant_filter == "real_estate":
            stmt = stmt.where(PublicUnderwritingIntake.variant == FUNDING_VARIANT)
        else:
            stmt = stmt.where(PublicUnderwritingIntake.variant == variant_filter)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(PublicUnderwritingIntake.full_name).like(needle)
            | func.lower(PublicUnderwritingIntake.email).like(needle)
            | func.lower(PublicUnderwritingIntake.business_name).like(needle)
        )
    rows = list((await db.execute(stmt)).scalars().unique().all())
    if probability_status and probability_status != "all":
        rows = [
            row for row in rows
            if str(_lead_result(row).get("probability_status") or "") == probability_status
        ]
    total = len(rows)
    page = rows[offset:offset + limit]
    return DealerAILeadListResponse(
        items=[_lead_row(row) for row in page],
        total=total,
        limit=limit,
        offset=offset,
    )


@admin_router.get("/{intake_id}/intelligence.pdf")
async def download_admin_dealer_intelligence_pdf(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    review = intake.latest_review if intake.latest_review else None
    latest_result = review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
    files = sorted(_active_files(intake.bucket), key=lambda file: file.created_at, reverse=True)
    missing_docs = _missing_required_docs(intake.bucket)
    pdf_bytes = await asyncio.to_thread(
        render_dealer_intelligence_pdf,
        intake=intake,
        files=files,
        missing_docs=missing_docs,
        result=latest_result,
    )
    filename = _safe_filename(f"dealer-intelligence-{intake.business_name or intake.full_name or 'review'}.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@admin_router.get("/{intake_id}", response_model=DealerIntakeResponse)
async def get_dealer_ai_lead(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    return await _response(db, intake, token=None, include_management=True, admin_thread=True)


@admin_router.post("", response_model=DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_ai_lead(
    payload: AdminLeadCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Super-admin creates an AI-underwriter lead ON BEHALF of a client and can
    start underwriting immediately. The client can log in later with this email
    exactly like a self-serve lead (email + code), since login keys off the email
    and the intake carries client_id. Reuses the same creation helpers as the
    public /start flows via lightweight adapter payloads. No terms/throttle."""
    _require_super_admin(user)
    if payload.variant not in ("dealer", "real_estate"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "variant must be 'dealer' or 'real_estate'")
    is_re = payload.variant == "real_estate"
    variant_const = FUNDING_VARIANT if is_re else "dealer_gatekeeper_v1"

    # Duplicate policy: unless force_new, surface the existing active lead so the
    # operator opens it instead of creating a duplicate bucket for the same email.
    if not payload.force_new:
        existing = await _latest_active_intake_by_email(db, str(payload.email), variant=variant_const)
        if existing is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "message": "An active lead already exists for this email.",
                    "intake_id": str(existing.id),
                },
            )

    provenance = {
        "created_by_admin": {
            "user_id": str(user.id),
            "name": user.name,
            "email": user.email,
            "at": _now().isoformat(),
        },
        "on_behalf_of_client": True,
    }

    if is_re:
        adapter = FundingReviewStart(
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            investor_name=payload.investor_name,
            target_property_address=payload.target_property_address,
            transaction_type=payload.transaction_type,
            requested_amount=payload.requested_amount,
            estimated_value_or_purchase_price=payload.estimated_value_or_purchase_price,
            monthly_rent=payload.monthly_rent,
            estimated_credit_tier=payload.estimated_credit_tier,
        )
        client = await _find_or_create_funding_client(db, adapter)
        bucket, link = await _create_bucket_for_funding_review(db, client, adapter, request)
    else:
        adapter = DealerIntakeStart(
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            business_name=payload.business_name,
        )
        client = await _find_or_create_client(db, adapter)
        bucket, link = await _create_bucket_for_intake(db, client, adapter, request)

    # CRM traceability: mark that this client/lead originated from an admin action.
    if isinstance(client.lead_intake, dict):
        client.lead_intake = {**client.lead_intake, "created_by_admin": str(user.id)}

    token = _new_public_token()
    intake_state: dict[str, Any] = {
        "messages": [],
        "source": ("funding_review" if is_re else "dealer_ai_intake"),
        "admin_provenance": provenance,
    }
    if is_re:
        intake_state["funding_review_basics"] = {
            "investor_name": payload.investor_name,
            "target_property_address": payload.target_property_address,
            "transaction_type": payload.transaction_type,
            "requested_amount": payload.requested_amount,
            "estimated_value_or_purchase_price": payload.estimated_value_or_purchase_price,
            "monthly_rent": payload.monthly_rent,
            "estimated_credit_tier": payload.estimated_credit_tier,
        }

    intake = PublicUnderwritingIntake(
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=_hash_token(token),
        variant=variant_const,
        full_name=payload.full_name.strip(),
        email=client.email or _normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=(payload.investor_name if is_re else payload.business_name),
        loan_purpose=(payload.transaction_type if is_re else None),
        requested_loan_amount=(payload.requested_amount if is_re else None),
        asset_rows=(
            [
                {
                    "address": payload.target_property_address,
                    "estimated_property_value": payload.estimated_value_or_purchase_price,
                    "notes": "Target property from admin-created funding review",
                }
            ]
            if is_re and (payload.target_property_address or payload.estimated_value_or_purchase_price)
            else []
        ),
        intake_state=intake_state,
    )
    db.add(intake)
    await _log(
        db,
        bucket.id,
        "dealer_ai_lead_created_by_admin",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Admin created {payload.variant} lead for {intake.email}",
    )
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake.id)

    email_note = ""
    if payload.notify_client:
        if is_re:
            record = _record_resume_email(
                intake,
                token=token,
                request=request,
                reason="admin_created",
                public_path=FUNDING_PUBLIC_PATH,
                review_label="real estate funding review",
                room_label="real estate funding review file",
            )
        else:
            record = _record_resume_email(intake, token=token, request=request, reason="admin_created")
        await db.commit()
        intake = await _load_admin_dealer_lead(db, intake.id)
        email_note = (
            " A secure login link was emailed to the client."
            if record.get("ok")
            else " Email delivery is unavailable; share the resume link manually."
        )

    return await _response(
        db,
        intake,
        token=token,
        public_path=(FUNDING_PUBLIC_PATH if is_re else "/dealer-ai-underwriter"),
        include_management=True,
        admin_thread=True,
        assistant_message=(
            "Lead created on behalf of the client. Upload documents or start the AI screen when ready."
            + email_note
        ),
    )


@admin_router.post("/{intake_id}/run-review", response_model=ReviewRunStartResponse)
async def rerun_dealer_ai_lead_review(
    intake_id: UUID,
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ReviewRunStartResponse:
    """Kick off an admin re-run of the AI review on the lead's latest uploads.
    Returns immediately with a review_id; the heavy pass runs in the background
    and the UI polls GET /{intake_id}/review-progress. Cooldown-throttled."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    _throttle_or_429(
        _ADMIN_REVIEW_LAST_BY_INTAKE,
        str(intake.id),
        _ADMIN_REVIEW_MIN_INTERVAL_SECONDS,
        "A review was just re-run for this lead. Please wait a moment before running another.",
    )
    is_funding = intake.variant == FUNDING_VARIANT
    review = await _create_queued_review(
        db,
        intake,
        request=request,
        actor_name=user.name or "Super admin",
        actor_email=user.email,
        actor_role="super_admin",
        log_event="funding_review_ai_review_rerun" if is_funding else "dealer_ai_review_rerun",
        detail="Admin re-run over latest uploads",
        requested_by_user_id=user.id,
    )
    background.add_task(_run_review_background, review.id, intake.id)
    return ReviewRunStartResponse(review_id=review.id, status="queued")


@admin_router.get("/{intake_id}/review-progress", response_model=ReviewProgressResponse)
async def dealer_ai_lead_review_progress(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    review_id: UUID | None = None,
) -> ReviewProgressResponse:
    """Poll the live progress of a lead's most recent (or a specific) AI review."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    if review_id is not None:
        review = await db.get(BucketAIReview, review_id)
        if review is None or review.bucket_id != intake.bucket_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    else:
        review = (
            await db.execute(
                select(BucketAIReview)
                .where(BucketAIReview.bucket_id == intake.bucket_id)
                .order_by(BucketAIReview.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if review is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No review found")
    progress = review.progress if isinstance(review.progress, dict) else {}
    return ReviewProgressResponse(
        review_id=review.id,
        status=review.status,
        stage=str(progress.get("stage") or review.status),
        label=str(progress.get("label") or ""),
        percent=int(progress.get("percent") or (100 if review.status in {"completed", "failed"} else 0)),
        files_total=int(progress.get("files_total") or 0),
        files_done=int(progress.get("files_done") or 0),
        error=review.error,
    )


@admin_router.post("/{intake_id}/chat", response_model=DealerIntakeResponse)
async def dealer_ai_lead_chat(
    intake_id: UUID,
    payload: DealerChatRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Admin chat with the underwriting AI on a lead, in a PRIVATE internal
    thread (audience='admin') the client never sees."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    assistant_message = None
    if payload.message and payload.message.strip():
        chat_messages, _, _ = await create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="admin",
            message=payload.message.strip(),
            actor_name=user.name or "Super admin",
            user=user,
        )
        if chat_messages:
            assistant_message = chat_messages[-1].content
        # Persist the operator's stated facts (requested amount, credit tier, use
        # of funds, …) so the next review/summary sees them — the client path
        # already records into chat_facts; the admin path did not until now.
        _record_chat_fact(intake, payload.message, source="admin_chat")
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake_id)
    return await _response(
        db,
        intake,
        token=None,
        include_management=True,
        assistant_message=assistant_message,
        admin_thread=True,
    )


async def _client_thread_messages(db: AsyncSession, intake: PublicUnderwritingIntake) -> list[BucketAIMessage]:
    """The client-visible (uploader) thread for this lead, oldest→newest."""
    rows = (
        await db.execute(
            select(BucketAIMessage)
            .where(
                BucketAIMessage.bucket_id == intake.bucket_id,
                BucketAIMessage.audience == "uploader",
                BucketAIMessage.upload_link_id == intake.bucket_upload_link_id,
            )
            .order_by(BucketAIMessage.created_at.desc())
            .limit(80)
        )
    ).scalars().all()
    return list(reversed(rows))


class ClientThreadResponse(BaseModel):
    messages: list[BucketAIMessageRead] = []


@admin_router.get("/{intake_id}/client-thread", response_model=ClientThreadResponse)
async def get_dealer_ai_client_thread(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientThreadResponse:
    """Return the CLIENT-visible (uploader) conversation for this lead so the
    super-admin can see what the borrower and their AI have exchanged. This is a
    different thread from the private admin cockpit chat (audience='admin')."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    messages = await _client_thread_messages(db, intake)
    return ClientThreadResponse(messages=[BucketAIMessageRead.model_validate(m) for m in messages])


@admin_router.post("/{intake_id}/client-thread/reply", response_model=ClientThreadResponse)
async def reply_dealer_ai_client_thread(
    intake_id: UUID,
    payload: DealerChatRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientThreadResponse:
    """Post a message ON BEHALF of the operator INTO the client's (uploader)
    thread, attributed as the underwriter, so the borrower sees a human reply and
    their AI advances the funnel. The message IS visible to the client (unlike the
    private admin thread). An audit-log entry records the admin who authored it."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    if not (payload.message and payload.message.strip()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A message is required")
    if intake.bucket_upload_link is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This lead has no client upload link to reply into")
    attribution = f"Underwriter — {user.name}" if user.name else "Underwriter"
    await create_chat_reply(
        db,
        bucket=intake.bucket,
        audience="uploader",
        message=payload.message.strip(),
        actor_name=attribution,
        user=user,
        upload_link=intake.bucket_upload_link,
    )
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_admin_replied_to_client",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"On-behalf reply into client thread by {user.name or user.email}",
    )
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake_id)
    messages = await _client_thread_messages(db, intake)
    return ClientThreadResponse(messages=[BucketAIMessageRead.model_validate(m) for m in messages])


@admin_router.post("/{intake_id}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def dealer_ai_lead_upload_init(
    intake_id: UUID,
    payload: DealerFileUploadInit,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    """Admin file upload into the lead's bucket (attaches to the upload link and
    runs zip extraction on complete, like the client path)."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    return await _start_upload(db, intake, payload, request, actor_name=user.name or "Super admin", actor_email=user.email)


@admin_router.post("/{intake_id}/files/complete", response_model=BucketFileRead)
async def dealer_ai_lead_upload_complete(
    intake_id: UUID,
    payload: DealerUploadComplete,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    return await _complete_upload(db, intake, payload, request, actor_name=user.name or "Super admin", actor_email=user.email)


@admin_router.post("/{intake_id}/executive-summary", response_model=PublicUnderwritingArtifactRead)
async def create_dealer_ai_executive_summary(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PublicUnderwritingArtifactRead:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    artifact = await _create_executive_summary_artifact(db, intake, user)
    await db.commit()
    artifact = await _latest_artifact(db, intake_id, "executive_summary") or artifact
    return _artifact_read(artifact)


@admin_router.post("/{intake_id}/lender-packet", response_model=PublicUnderwritingArtifactRead)
async def create_dealer_ai_lender_packet(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PublicUnderwritingArtifactRead:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    artifact = await _create_lender_packet_artifact(db, intake, user)
    await db.commit()
    artifact = await _latest_artifact(db, intake_id, "lender_packet") or artifact
    return _artifact_read(artifact)


@admin_router.get("/{intake_id}/package.zip")
async def download_dealer_ai_package_zip(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Bundle the full shipping package as a single ZIP: every uploaded document,
    the lender-packet PDF, the executive summary (markdown), a ready-to-edit
    vendor email template, and a README manifest — so the operator can ship the
    whole file anywhere (attach, upload, or archive) in one download."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    summary_artifact = await _ensure_executive_summary_artifact(db, intake, user)
    packet_artifact = await _ensure_lender_packet_artifact(db, intake, user)
    email_draft = await _generate_management_json(
        db,
        intake,
        user,
        purpose="vendor_email",
        extra={"executive_summary": summary_artifact.body_json, "lender_packet_title": packet_artifact.title},
    )
    await db.commit()

    label = _safe_filename(intake.business_name or intake.full_name or "lead")
    files = sorted(_active_files(intake.bucket), key=lambda f: f.file_name.lower())
    manifest_lines = [
        f"Qualified Commercial — Underwriting Package",
        f"Borrower/entity: {intake.business_name or intake.full_name or '-'}",
        f"Contact: {intake.full_name or '-'} <{intake.email or '-'}>",
        f"Generated: {_now().strftime('%b %d, %Y %I:%M %p UTC')}",
        "",
        "Contents:",
        "  executive-summary.md   — AI executive summary",
        "  lender-packet.pdf       — formatted lender/vendor packet",
        "  email-template.txt      — ready-to-edit vendor outreach email",
        f"  documents/              — {len(files)} uploaded file(s)",
        "",
        "Uploaded documents:",
    ]

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Executive summary (markdown prose).
        zf.writestr("executive-summary.md", summary_artifact.body_text or "Awaiting AI executive summary.")
        # Lender packet PDF.
        if packet_artifact.s3_key:
            try:
                zf.writestr("lender-packet.pdf", await _s3_bytes(packet_artifact.s3_key))
            except Exception:  # noqa: BLE001
                log.exception("package.zip: lender packet fetch failed intake=%s", intake_id)
        # Vendor email template.
        subject = str(email_draft.get("subject") or f"Qualified Commercial review: {intake.business_name or intake.full_name}")
        body = str(email_draft.get("body") or summary_artifact.body_text or "")
        zf.writestr("email-template.txt", f"Subject: {subject}\n\n{body}\n")
        # All uploaded documents under documents/.
        used_names: set[str] = set()
        for f in files:
            entry_name = _safe_filename(f.file_name) or f"file-{str(f.id)[:8]}"
            candidate = entry_name
            n = 2
            while candidate in used_names:
                candidate = f"{entry_name}-{n}"
                n += 1
            used_names.add(candidate)
            manifest_lines.append(f"  - {f.file_name} ({round((f.size_bytes or 0) / 1024)} KB)")
            try:
                zf.writestr(f"documents/{candidate}", await _s3_bytes(f.s3_key))
            except Exception:  # noqa: BLE001
                log.exception("package.zip: doc fetch failed file=%s", f.id)
        zf.writestr("README.txt", "\n".join(manifest_lines) + "\n")

    payload = buf.getvalue()
    filename = f"{label}-package.zip"
    await _log(
        db,
        intake.bucket_id,
        "underwriting_package_zip_downloaded",
        user=user,
        actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"{filename} ({round(len(payload) / 1024)} KB, {len(files)} docs)",
    )
    await db.commit()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@admin_router.post("/{intake_id}/vendor-email/preview", response_model=VendorEmailPreviewResponse)
async def preview_dealer_ai_vendor_email(
    intake_id: UUID,
    payload: VendorEmailPreviewRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> VendorEmailPreviewResponse:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    summary_artifact = await _ensure_executive_summary_artifact(db, intake, user)
    packet_artifact = await _ensure_lender_packet_artifact(db, intake, user) if payload.include_lender_packet else None
    draft = await _generate_management_json(
        db,
        intake,
        user,
        purpose="vendor_email",
        extra={
            "requested_recipients": [str(email) for email in payload.to_emails],
            "cc_emails": [str(email) for email in payload.cc_emails],
            "executive_summary": summary_artifact.body_json,
            "lender_packet_title": packet_artifact.title if packet_artifact else None,
        },
    )
    subject = payload.subject or str(draft.get("subject") or f"Qualified Commercial review: {intake.business_name or intake.full_name}")
    body = payload.body or str(draft.get("body") or summary_artifact.body_text or "")
    await db.commit()
    return VendorEmailPreviewResponse(
        subject=subject[:512],
        body=body,
        to_emails=[str(email) for email in payload.to_emails],
        cc_emails=[str(email) for email in payload.cc_emails],
        executive_summary=_artifact_read(summary_artifact),
        lender_packet=_artifact_read(packet_artifact) if packet_artifact else None,
    )


@admin_router.post("/{intake_id}/vendor-email/send", response_model=VendorEmailSendResponse)
async def send_dealer_ai_vendor_email(
    intake_id: UUID,
    payload: VendorEmailSendRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> VendorEmailSendResponse:
    _require_super_admin(user)
    if not payload.to_emails:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one vendor email is required")
    intake = await _load_admin_dealer_lead(db, intake_id)
    summary_artifact = await _ensure_executive_summary_artifact(db, intake, user)
    packet_artifact = await _ensure_lender_packet_artifact(db, intake, user) if payload.include_lender_packet else None
    cc_emails = [str(email).lower().strip() for email in payload.cc_emails if str(email).strip()]
    sends: list[PublicUnderwritingIntakeEmailSend] = []
    access_ids: list[UUID] = []
    _MAX_ATTACH = 8 * 1024 * 1024  # per-file cap
    # Aggregate cap for the whole message. Kept comfortably under Gmail's ~25MB
    # limit (attachments inflate ~1.37x under base64) so a stack of sub-8MB files
    # can't build one oversized message that the provider rejects for EVERY
    # recipient. Files that would push past the total are noted, not attached.
    _MAX_TOTAL_ATTACH = 18 * 1024 * 1024
    attachments: list[tuple[str, bytes, str]] = []
    attachment_note = ""
    total_attach_bytes = 0
    if packet_artifact and packet_artifact.s3_key:
        try:
            packet_bytes = await _s3_bytes(packet_artifact.s3_key)
            if len(packet_bytes) <= _MAX_ATTACH:
                attachments.append((f"{_safe_filename(packet_artifact.title)}.pdf", packet_bytes, "application/pdf"))
                total_attach_bytes += len(packet_bytes)
            else:
                attachment_note = "\n\nThe underwriting packet is available through the secure vendor bucket because the PDF is too large for email."
        except Exception as exc:
            attachment_note = f"\n\nThe underwriting packet is available through the secure vendor bucket. Attachment fallback reason: {exc}"
    # Google Drive attachments (downloaded via the sender's OAuth grant). Skip any
    # over the per-file OR aggregate size cap and note them rather than failing the
    # whole send. max_bytes is enforced inside download_file_bytes so oversized
    # files are never fully buffered in memory.
    if payload.drive_file_ids:
        from app.services.google.drive_client import download_file_bytes

        for file_id in payload.drive_file_ids[:10]:
            try:
                got = await download_file_bytes(db, user.id, file_id, max_bytes=_MAX_ATTACH)
            except Exception:  # noqa: BLE001 — not connected / revoked
                got = None
            if got is None:
                attachment_note += f"\n\nA Google Drive file ({file_id}) could not be attached (unavailable or too large)."
                continue
            fname, data, ctype = got
            if total_attach_bytes + len(data) > _MAX_TOTAL_ATTACH:
                attachment_note += f"\n\n'{fname}' was not attached to keep the email under the size limit; it's in the secure vendor bucket."
                continue
            attachments.append((fname, data, ctype))
            total_attach_bytes += len(data)
    for raw_email in payload.to_emails:
        email = str(raw_email).lower().strip()
        access = await _prepare_vendor_access(db, intake, email, payload)
        access_ids.append(access.id)
        vendor_link = _public_url(f"/vendor/buckets?bucket={intake.bucket_id}")
        body = (
            payload.body.strip()
            + "\n\nSecure bucket access:\n"
            + vendor_link
            + "\n\nQualified Commercial has enabled vendor access for this bucket. Please log in with the invited vendor email to view the file package."
            + attachment_note
        )
        html_body = "<br>".join(html.escape(line) for line in body.splitlines())
        # Send from the operator's connected Gmail when available, else firm SES.
        result = await send_as_user(
            db,
            user.id,
            to_emails=[email],
            cc_emails=cc_emails,
            subject=payload.subject.strip(),
            body_text=body,
            body_html=f"<p>{html_body}</p>",
            attachments=attachments or None,
        )
        send_row = PublicUnderwritingIntakeEmailSend(
            intake_id=intake.id,
            executive_summary_artifact_id=summary_artifact.id,
            lender_packet_artifact_id=packet_artifact.id if packet_artifact else None,
            to_emails=[email],
            cc_emails=cc_emails,
            subject=payload.subject.strip(),
            body=body,
            vendor_access_ids=[str(access.id)],
            ses_status=result.detail,
            ses_message_ids=[result.message_id] if result.message_id else None,
            ses_error=result.error,
            sent_by_user_id=user.id,
        )
        db.add(send_row)
        sends.append(send_row)
        await _log(
            db,
            intake.bucket_id,
            "underwriting_vendor_email_sent" if result.ok else "underwriting_vendor_email_failed",
            user=user,
            actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
            target_type="public_underwriting_email",
            target_id=email,
            detail=result.detail if result.ok else result.error or result.detail,
        )
    await db.commit()
    for row in sends:
        await db.refresh(row)
    return VendorEmailSendResponse(email_sends=[_email_send_read(row) for row in sends], vendor_access_ids=access_ids)


@router.get("/{token}", response_model=DealerIntakeResponse)
async def get_dealer_intake(token: str, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_dealer_intake(intake)
    return await _response(db, intake, token=token)


@router.patch("/{token}", response_model=DealerIntakeResponse)
async def update_dealer_intake(
    token: str,
    payload: DealerIntakePatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_dealer_intake(intake)
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
    _require_dealer_intake(intake)
    update_data = payload.updates.model_dump(exclude_unset=True) if payload.updates else {}
    _apply_updates(intake, payload.updates)
    _record_chat_fact(intake, payload.message)
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
        chat_messages, _, _ = await create_chat_reply(
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
    return await _response(
        db,
        intake,
        token=token,
        assistant_message=assistant_message,
        messages=messages,
    )


@router.post("/{token}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def dealer_upload_init(
    token: str,
    payload: DealerFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    intake = await _load_public_intake(db, token)
    _require_dealer_intake(intake)
    return await _start_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@router.post("/{token}/files/complete", response_model=BucketFileRead)
async def dealer_upload_complete(
    token: str,
    payload: DealerUploadComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    _require_dealer_intake(intake)
    return await _complete_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@router.post("/{token}/run-review", response_model=DealerIntakeResponse)
async def run_dealer_review(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_dealer_intake(intake)
    # Per-token cooldown: run-review triggers a heavy Bedrock pass over up to 8
    # files; without this a token holder can replay it to amplify LLM cost.
    _throttle_or_429(
        _REVIEW_LAST_BY_TOKEN,
        _hash_token(token),
        _REVIEW_MIN_INTERVAL_SECONDS,
        "A review was just started. Please wait a moment before running another.",
    )
    fresh_review = await _execute_intake_review(
        db,
        intake,
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        log_event="dealer_ai_review_queued",
        detail="Public dealer AI screen",
    )
    if fresh_review and isinstance(fresh_review.result, dict):
        await _record_super_admin_decision_notification(
            db,
            intake,
            fresh_review.result,
            request=request,
            review_id=fresh_review.id,
        )
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
    _require_dealer_intake(intake)
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
    _record_chat_fact(intake, payload.message)
    await _log_dealer_update_events(db, intake, update_data, request=request, user=user)
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        chat_messages, _, _ = await create_chat_reply(
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
    return await _response(
        db,
        intake,
        token=None,
        assistant_message=assistant_message,
        messages=messages,
    )


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


FUNDING_PUBLIC_PATH = "/funding-review"
FUNDING_VARIANT = "real_estate_dscr_v1"


def _funding_empty_message() -> str:
    return (
        "I opened your secure real estate funding review. I will screen this like an investor-loan underwriter: rent support, PITIA, "
        "DSCR, LTV, purchase or payoff evidence, property value, entity/vesting, and credit tier. Attach what you have and I will ask one "
        "targeted question or upload request at a time."
    )


def _require_funding_intake(intake: PublicUnderwritingIntake) -> None:
    if intake.variant != FUNDING_VARIANT:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Funding review not found")


# Dealer variants: the canonical "dealer_gatekeeper_v1" plus the legacy
# "dealer_financing_v1" default, so the guard is correct both before and after
# the 0090 variant-normalization migration.
DEALER_VARIANTS = {"dealer_gatekeeper_v1", "dealer_financing_v1"}


def _require_dealer_intake(intake: PublicUnderwritingIntake) -> None:
    """Reject a non-dealer intake on the dealer public routes, so a real-estate
    token can never be driven through car-dealer logic. Mirrors
    _require_funding_intake; 404 (not 403) to match the funding convention."""
    if intake.variant not in DEALER_VARIANTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI intake not found")


async def _load_funding_intake_by_session(db: AsyncSession, request: Request) -> tuple[PublicUnderwritingIntake, DealerIntakeLoginChallenge, str]:
    session_token = request.headers.get("x-funding-session") or request.headers.get("X-Funding-Session")
    if not session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Funding review session is required")
    intake, challenge = await _load_intake_by_dealer_session(db, session_token)
    _require_funding_intake(intake)
    return intake, challenge, session_token


@funding_router.post("/start", response_model=DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def start_funding_review(
    payload: FundingReviewStart,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    if not payload.terms_accepted or not payload.privacy_accepted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Terms and Privacy Policy acceptance is required.")
    existing = await _latest_active_intake_by_email(db, str(payload.email), variant=FUNDING_VARIANT)
    if existing is not None:
        await _start_login_challenge(
            db,
            email=str(payload.email),
            request=request,
            reason="existing_funding_review_start",
            variant=FUNDING_VARIANT,
            review_label="real estate funding review",
            event_prefix="funding_review",
            target_type="funding_review_intake",
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A secure funding review already exists for this email. We sent a short access code so you can continue that file.",
        )
    client = await _find_or_create_funding_client(db, payload)
    bucket, link = await _create_bucket_for_funding_review(db, client, payload, request)
    token = _new_public_token()
    basics = {
        "investor_name": payload.investor_name,
        "target_property_address": payload.target_property_address,
        "transaction_type": payload.transaction_type,
        "requested_amount": payload.requested_amount,
        "estimated_value_or_purchase_price": payload.estimated_value_or_purchase_price,
        "monthly_rent": payload.monthly_rent,
        "estimated_credit_tier": payload.estimated_credit_tier,
    }
    intake = PublicUnderwritingIntake(
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=_hash_token(token),
        variant=FUNDING_VARIANT,
        full_name=payload.full_name.strip(),
        email=client.email or _normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.investor_name,
        loan_purpose=payload.transaction_type,
        requested_loan_amount=payload.requested_amount,
        asset_rows=[
            {
                "address": payload.target_property_address,
                "estimated_property_value": payload.estimated_value_or_purchase_price,
                "notes": "Target property from funding review intake",
            }
        ]
        if payload.target_property_address or payload.estimated_value_or_purchase_price
        else [],
        intake_state={
            "messages": [],
            "source": "funding_review",
            "funding_review_basics": basics,
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
    _record_resume_email(
        intake,
        token=token,
        request=request,
        reason="funding_review_created",
        public_path=FUNDING_PUBLIC_PATH,
        review_label="real estate funding review",
        room_label="real estate funding review file",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(db, intake, token=token, public_path=FUNDING_PUBLIC_PATH, assistant_message=_funding_empty_message())


@funding_router.post("/login/start", response_model=DealerLoginStartResponse)
async def start_funding_review_login(
    payload: DealerLoginStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerLoginStartResponse:
    login_required = await _start_login_challenge(
        db,
        email=str(payload.email),
        request=request,
        reason="funding_review_login_requested",
        variant=FUNDING_VARIANT,
        review_label="real estate funding review",
        event_prefix="funding_review",
        target_type="funding_review_intake",
    )
    await db.commit()
    return DealerLoginStartResponse(
        login_required=login_required,
        message=(
            "We found an existing funding review for this email. Enter the code we sent to continue."
            if login_required
            else "No existing funding review was found. Complete Step 1 to start a new review."
        ),
    )


@funding_router.post("/login/verify", response_model=DealerIntakeResponse)
async def verify_funding_review_login(
    payload: DealerLoginVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    email_hash = _hash_token(_normalize_email(str(payload.email)))
    challenge = (
        await db.execute(
            select(DealerIntakeLoginChallenge)
            .join(PublicUnderwritingIntake, DealerIntakeLoginChallenge.intake_id == PublicUnderwritingIntake.id)
            .where(
                PublicUnderwritingIntake.variant == FUNDING_VARIANT,
                DealerIntakeLoginChallenge.email_hash == email_hash,
                DealerIntakeLoginChallenge.used_at.is_(None),
                DealerIntakeLoginChallenge.revoked_at.is_(None),
                DealerIntakeLoginChallenge.expires_at > _now(),
            )
            .order_by(DealerIntakeLoginChallenge.created_at.desc())
        )
    ).scalars().first()
    if challenge is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    if challenge.attempt_count >= DEALER_LOGIN_MAX_ATTEMPTS:
        challenge.revoked_at = _now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    if _hash_token(payload.code.strip()) != challenge.code_hash:
        challenge.attempt_count += 1
        if challenge.attempt_count >= DEALER_LOGIN_MAX_ATTEMPTS:
            challenge.revoked_at = _now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    session_token = secrets.token_urlsafe(40)
    public_token = _new_public_token()
    challenge.used_at = _now()
    challenge.session_hash = _hash_token(session_token)
    challenge.session_expires_at = _now() + timedelta(hours=DEALER_LOGIN_SESSION_TTL_HOURS)
    intake = await db.get(PublicUnderwritingIntake, challenge.intake_id)
    if intake is None or intake.variant != FUNDING_VARIANT:
        challenge.revoked_at = _now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    intake.token_hash = _hash_token(public_token)
    await _log(
        db,
        intake.bucket_id,
        "funding_review_login_verified",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="funding_review_intake",
        target_id=str(intake.id),
        detail="Funding review continuation login verified",
    )
    await db.commit()
    intake = await _load_public_intake(db, public_token)
    return await _response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        public_path=FUNDING_PUBLIC_PATH,
        assistant_message="Welcome back. I restored your secure real estate funding review with your prior uploads and chat context.",
    )


@funding_router.get("/session", response_model=DealerIntakeResponse)
async def get_funding_review_session(request: Request, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake, _challenge, session_token = await _load_funding_intake_by_session(db, request)
    public_token = _new_public_token()
    intake.token_hash = _hash_token(public_token)
    await db.commit()
    intake = await _load_public_intake(db, public_token)
    return await _response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        public_path=FUNDING_PUBLIC_PATH,
        assistant_message="Welcome back. I restored your secure real estate funding review with your prior uploads and chat context.",
    )


@funding_router.post("/logout", response_model=DealerLogoutResponse)
async def logout_funding_review_session(
    payload: DealerLogoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerLogoutResponse:
    session_token = payload.session_token or request.headers.get("x-funding-session") or request.headers.get("X-Funding-Session")
    if session_token:
        challenge = (
            await db.execute(
                select(DealerIntakeLoginChallenge).where(
                    DealerIntakeLoginChallenge.session_hash == _hash_token(session_token),
                    DealerIntakeLoginChallenge.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if challenge is not None:
            challenge.revoked_at = _now()
            await db.commit()
    return DealerLogoutResponse()


@funding_router.get("/intelligence.pdf")
async def download_funding_review_intelligence_pdf(
    request: Request,
    token: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    if token:
        intake = await _load_public_intake(db, token)
        _require_funding_intake(intake)
    else:
        intake, _challenge, _session_token = await _load_funding_intake_by_session(db, request)
    review = intake.latest_review if intake.latest_review else None
    latest_result = review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
    pdf_bytes = await asyncio.to_thread(
        render_dealer_intelligence_pdf,
        intake=intake,
        files=sorted(_active_files(intake.bucket), key=lambda file: file.created_at, reverse=True),
        missing_docs=_missing_required_docs(intake.bucket),
        result=latest_result,
    )
    filename = _safe_filename(f"funding-intelligence-{intake.business_name or intake.full_name or 'review'}.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@funding_router.get("/{token}", response_model=DealerIntakeResponse)
async def get_funding_review(token: str, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_funding_intake(intake)
    return await _response(db, intake, token=token, public_path=FUNDING_PUBLIC_PATH, empty_message=_funding_empty_message())


@funding_router.post("/{token}/chat", response_model=DealerIntakeResponse)
async def funding_review_chat(
    token: str,
    payload: DealerChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_funding_intake(intake)
    _apply_updates(intake, payload.updates)
    _record_chat_fact(intake, payload.message)
    intake.last_message_at = _now()
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **_funding_review_context(intake)}
        chat_messages, _, _ = await create_chat_reply(
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
    return await _response(db, intake, token=token, public_path=FUNDING_PUBLIC_PATH, assistant_message=assistant_message, messages=messages)


@funding_router.post("/{token}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def funding_review_upload_init(
    token: str,
    payload: DealerFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    intake = await _load_public_intake(db, token)
    _require_funding_intake(intake)
    return await _start_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@funding_router.post("/{token}/files/complete", response_model=BucketFileRead)
async def funding_review_upload_complete(
    token: str,
    payload: DealerUploadComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    _require_funding_intake(intake)
    return await _complete_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@funding_router.post("/{token}/run-review", response_model=DealerIntakeResponse)
async def run_funding_review(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_funding_intake(intake)
    await _execute_intake_review(
        db,
        intake,
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        log_event="funding_review_ai_review_queued",
        detail="Public real estate funding screen",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(db, intake, token=token, public_path=FUNDING_PUBLIC_PATH)


@funding_router.post("/{token}/book-call", response_model=DealerIntakeResponse)
async def book_funding_review_call(
    token: str,
    payload: DealerBookCallRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_funding_intake(intake)
    if _call_booked(intake):
        return await _response(
            db,
            intake,
            token=token,
            public_path=FUNDING_PUBLIC_PATH,
            assistant_message="Your call is already booked. Keep uploading property and rent evidence here if anything is still missing before the meeting.",
        )
    starts_at = _to_utc_minute(payload.starts_at)
    owner, booking, slots = await _dealer_call_slots(db)
    if owner is None or booking is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Call scheduling is not available right now.")
    if not any(abs((datetime.fromisoformat(slot["starts_at"]) - starts_at).total_seconds()) < 1 for slot in slots):
        raise HTTPException(status.HTTP_409_CONFLICT, "That call time is no longer available. Choose another time.")
    who = f"{intake.full_name} <{intake.email}>"
    description = (
        "Booked from Real Estate Funding Review.\n"
        f"Funding review intake: {intake.id}\n"
        f"Bucket: {intake.bucket_id}\n"
        f"Investor/entity: {intake.business_name or '(not provided)'}\n"
        f"Name: {intake.full_name}\n"
        f"Email: {intake.email}\n"
        f"Phone: {intake.phone or '(not provided)'}\n"
        f"Requested amount: {intake.requested_loan_amount or '(not provided)'}\n"
        f"Transaction type: {intake.loan_purpose or '(not provided)'}\n"
    )
    ev = CalendarEvent(
        loan_id=None,
        kind=CalendarEventKind.CALL,
        title=f"Funding review call: {intake.business_name or intake.full_name}",
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=booking.duration_min,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.AUTO,
        owner_user_id=owner.id,
        external_ref_kind="funding_review_intake",
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
            kind="calendar.funding_review_call_booked",
            summary=f"Funding review call booked for {intake.business_name or intake.full_name}",
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
        "funding_review_call_booked",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="calendar_event",
        target_id=str(ev.id),
        detail=f"Funding review call booked for {starts_at.isoformat()}",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(
        db,
        intake,
        token=token,
        public_path=FUNDING_PUBLIC_PATH,
        assistant_message="Your call is booked. Keep uploading property, rent, and PITIA evidence here if anything is still missing before the meeting.",
    )
