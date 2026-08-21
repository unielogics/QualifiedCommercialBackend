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
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, with_loader_criteria

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import CalendarEventKind, CalendarEventSource, CalendarEventStatus, ContractType, Language, Role
from app.models.activity import Activity
from app.models.booking_settings import BookingSettings
from app.models.bucket import Bucket, BucketAIMessage, BucketAIReview, BucketDocumentSignature, BucketFile, BucketFileAnalysis, BucketNote, BucketRequestedDocument, BucketShare, BucketUploadLink, BucketVendorAccess
from app.models.client import Client
from app.models.event import CalendarEvent
from app.models.dealer_intake_login import DealerIntakeLoginChallenge
from app.models.public_underwriting_intake import PublicUnderwritingIntake, PublicUnderwritingIntakeArtifact, PublicUnderwritingIntakeEmailSend
from app.models.user import User
from app.routers.public import _available_booking_slots, _to_utc_minute
from app.routers.buckets import (
    _bucket_storage_config,
    _client_ip,
    _delete_s3_object,
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
    BucketNoteRead,
    BucketRequestUploadedFileRead,
    BucketRequestedDocumentRead,
)
from app.schemas.common import ORMModel
from app.services.bucket_ai import CHAT_TURN_ORDER, CURRENT_FILE_ANALYSIS_VERSION, create_chat_reply, latest_review, run_bucket_ai_review, upload_link_visible_summary
from app.services.ai.bedrock_client import get_client, model_light
from app.services.ai.usage import json_safe_metadata, tracked_messages_create
from app.services.dealer_ai_intelligence_pdf import render_dealer_intelligence_pdf
from app.services.main_street_programs import TERM_3_5_MIN_DSCR, TERM_3_5_MIN_REVENUE, TERM_10YR_MIN_DSCR
from app.services.email.ses_client import send_email, send_raw_email
from app.services.email.user_mailer import send_as_user
from app.services.payment_authorization import primary_super_admin
from app.services.public_underwriting_packet_pdf import render_underwriting_packet_pdf


router = APIRouter(prefix="/public/dealer-ai-intake", tags=["dealer-ai-intake"])
funding_router = APIRouter(prefix="/public/funding-review", tags=["public-funding-review"])
client_router = APIRouter(prefix="/buckets/client/intakes", tags=["client-bucket-intakes"])
admin_router = APIRouter(prefix="/admin/ai-underwriter-leads", tags=["admin-ai-underwriter-leads"])
broker_router = APIRouter(prefix="/broker/ai-underwriter-leads", tags=["broker-ai-underwriter-leads"])
# Slim public MCA-refinance intake (mca_refi_v1) — endpoints live at the end of
# this module, mirroring funding_router's subset.
mca_router = APIRouter(prefix="/public/mca-refinance", tags=["public-mca-refinance"])
log = logging.getLogger(__name__)

TERMS_VERSION = "2026-05-19"
PRIVACY_VERSION = "2026-05-19"

# Client-facing welcome copy in English/Spanish, keyed by Language. Spanish
# strings: AI-translated, not yet native-speaker reviewed.
_DEALER_WELCOME = {
    Language.EN: (
        "I opened your secure dealer funding file. I am going to screen this like a bank underwriter: tax returns, current P&L, "
        "bank statements, real estate collateral, and any floorplan/MCA exposure that applies. Upload what you have now, and I will "
        "only ask follow-up questions when the LLC/account structure or collateral values are not clear enough to make a preliminary call."
    ),
    Language.ES: (
        "Abrí tu expediente seguro de financiamiento para concesionario. Voy a evaluar este archivo como lo haría un suscriptor bancario: "
        "declaraciones de impuestos, P&L actual, estados de cuenta bancarios, garantía inmobiliaria y cualquier exposición de floorplan/MCA "
        "que aplique. Sube lo que tengas ahora, y solo haré preguntas de seguimiento cuando la estructura de la LLC/cuenta o los valores de "
        "la garantía no sean lo suficientemente claros para hacer una evaluación preliminar."
    ),
}

_DEALER_WELCOME_BACK = {
    Language.EN: "Welcome back. I restored your secure dealer funding room with your prior uploads and chat context.",
    Language.ES: "Bienvenido de nuevo. Restauré tu sala segura de financiamiento con tus archivos y el contexto del chat anteriores.",
}

_RE_WELCOME_BACK = {
    Language.EN: "Welcome back. I restored your secure real estate funding review with your prior uploads and chat context.",
    Language.ES: "Bienvenido de nuevo. Restauré tu revisión segura de financiamiento inmobiliario con tus archivos y el contexto del chat anteriores.",
}


def _dealer_welcome(lang: str, *, email_note: str = "") -> str:
    return _DEALER_WELCOME.get(lang, _DEALER_WELCOME[Language.EN]) + email_note


def _dealer_welcome_back(lang: str) -> str:
    return _DEALER_WELCOME_BACK.get(lang, _DEALER_WELCOME_BACK[Language.EN])


def _re_welcome_back(lang: str) -> str:
    return _RE_WELCOME_BACK.get(lang, _RE_WELCOME_BACK[Language.EN])


def _admin_created_welcome(variant: str | bool) -> str:
    """Admin/broker-created-lead welcome — itemizes the baseline checklist by
    name (REQUIRED_DOCUMENTS / REAL_ESTATE_REQUIRED_DOCUMENTS) so whoever
    opens the lead first (the dealer partner, or the client once they log in)
    knows exactly what to gather right away, instead of a generic placeholder.
    English only — these leads are created by an internal admin/broker, not
    the client, so there is no preferred_language context to honor here the
    way _dealer_welcome() does for the self-serve flow."""
    # Accepts the old boolean for call sites not yet migrated: True meant
    # real estate, False meant dealer.
    if variant is True:
        variant = FUNDING_VARIANT
    elif variant is False:
        variant = DEALER_VARIANT
    if variant == FUNDING_VARIANT:
        docs = REAL_ESTATE_REQUIRED_DOCUMENTS
    elif variant == MAIN_STREET_VARIANT:
        from app.services.main_street_programs import MAIN_STREET_REQUIRED_DOCUMENTS

        docs = MAIN_STREET_REQUIRED_DOCUMENTS
    elif variant == MCA_VARIANT:
        docs = MCA_REQUIRED_DOCUMENTS
    else:
        docs = REQUIRED_DOCUMENTS
    bullets = "\n".join(f"- {doc['name']}" for doc in docs)
    return (
        "Lead created on behalf of the client. To get a preliminary screen, gather:\n"
        f"{bullets}\n"
        "Upload what you have now, or start the AI screen once ready."
    )


def _persist_admin_welcome_message(bucket_id: UUID, content: str) -> BucketAIMessage:
    """Writes the create-time welcome text as a real BucketAIMessage (audience
    'admin', role 'assistant') instead of only returning it in the one-time
    HTTP response. Without this, _response()'s message reload (which always
    reads from the DB, never from the just-generated assistant_message text)
    finds nothing on the next GET/reopen, and the welcome — including the
    itemized document checklist — silently disappears the moment the admin
    or broker closes and reopens the lead. Caller must db.add() the intake's
    own changes and db.commit() as usual; this only stages the row."""
    return BucketAIMessage(
        bucket_id=bucket_id,
        audience="admin",
        role="assistant",
        author_name="Bucket AI",
        content=content,
    )


_DEALER_START_EMAIL_NOTE_OK = {
    Language.EN: " I also emailed you a secure resume link so you can come back later.",
    Language.ES: " También te envié por correo electrónico un enlace seguro para reanudar, para que puedas volver más tarde.",
}
_DEALER_START_EMAIL_NOTE_FAILED = {
    Language.EN: " Use the copy resume link option as a backup if email delivery is unavailable.",
    Language.ES: " Usa la opción de copiar el enlace para reanudar como respaldo si el envío de correo electrónico no está disponible.",
}


def _dealer_start_email_note(lang: str, *, ok: bool) -> str:
    table = _DEALER_START_EMAIL_NOTE_OK if ok else _DEALER_START_EMAIL_NOTE_FAILED
    return table.get(lang, table[Language.EN])


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
    {
        "name": "Debt schedule",
        "category": "Debts",
        "description": "Upload a schedule of all outstanding business debt: lender, balance, and monthly payment for each.",
        "allow_multiple_files": False,
    },
    {
        "name": "Personal financial statement",
        "category": "Personal Financials",
        "description": "Upload a completed personal financial statement (PFS) for each owner. Use the blank form below if you need one.",
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


# The MCA-refinance intake collects exactly three things and stops. The
# slimness IS the product — a borrower drowning in daily debits abandons long
# checklists, and everything else can be gathered after the desk engages.
#
# Naming is load-bearing twice over: "bank"+"statement" in the first row keys
# _baseline_key in bucket_ai so the >=6-months readiness engine works
# unchanged; "mca" in the third row maps uploads to floorplan_mca_inventory in
# _CATEGORY_CLASSIFICATIONS. The credit authorization is seeded at CREATION
# with its signature_kind — no other public flow does this today (the admin
# endpoint mints it on demand for other variants); seeding it is what lets the
# borrower sign in-room without an admin touching the file first.
MCA_REQUIRED_DOCUMENTS = [
    {
        "name": "Last 6 months bank statements",
        "category": "Bank Statements",
        "description": "Upload the last six months of the main operating business bank statements — the account your advances debit from.",
        "allow_multiple_files": True,
    },
    {
        "name": "Credit Report Authorization",
        "category": "Compliance",
        "description": "Authorize one soft credit check. It does not affect your score and is required to price a structured payoff.",
        "allow_multiple_files": False,
        "requires_signature": True,
        "signature_kind": "credit_authorization",
    },
    {
        "name": "Current MCA / advance terms",
        "category": "Debts",
        "description": "Your current advance agreements or payoff letters — or type the terms in with the form and skip the paperwork.",
        "allow_multiple_files": True,
    },
]


class DealerAssetRow(BaseModel):
    id: str | None = None
    address: str = Field(default="", max_length=320)
    estimated_loan_amount: float | None = Field(default=None, ge=0)
    estimated_property_value: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)


class DealerOwner(BaseModel):
    name: str = Field(max_length=180)
    ownership_percent: float | None = Field(default=None, ge=0, le=100)


class DealerEntityStructure(BaseModel):
    primary_operating_entity: str | None = Field(default=None, max_length=180)
    main_operating_bank_account: str | None = Field(default=None, max_length=180)
    related_entities: str | None = Field(default=None, max_length=1200)
    relationship_explanation: str | None = Field(default=None, max_length=1600)
    # Captured conversationally via proposed_borrower_facts (see
    # _merge_dealer_details) — each named owner gets its own dynamically
    # created "Identification — {name}" requested document.
    owners: list[DealerOwner] = Field(default_factory=list)

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
    preferred_language: Language = Language.EN

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
    preferred_language: Language = Language.EN

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
    preferred_language: Language = Language.EN
    # Optionally assign the file to a dealer partner at creation, so the team's
    # first message on it reaches that partner's channel. Must be a dealer_partner.
    broker_user_id: UUID | None = None

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


class BrokerLeadCreate(BaseModel):
    """Dealer partner (Role.DEALER_PARTNER) creates an AI-underwriter lead on
    behalf of their own client. Dealer-variant only — no variant selector, no
    real-estate fields. The client can later log in with this email exactly
    like a self-serve lead."""

    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=48)
    business_name: str | None = Field(default=None, max_length=180)
    notify_client: bool = False
    force_new: bool = False
    preferred_language: Language = Language.EN

    @field_validator("phone", "business_name", mode="before")
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


class OutcomeStatusUpdate(BaseModel):
    outcome_status: Literal["submitted", "closed", "denied"]


class LanguageUpdate(BaseModel):
    preferred_language: Language


class DealerLeadNoteCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class AdminLeadFromBucketCreate(BaseModel):
    """Super-admin converts an EXISTING Bucket into an AI-underwriter lead — the
    admin already has a folder of files (collected some other way) and wants the
    AI audit + package-build against it, without a second, empty bucket being
    created. Mirrors AdminLeadCreate's client fields but has NO bucket-creation
    fields (no target_property_address / requested_amount / etc.) since the
    bucket, and whatever files/requested-docs are already on it, stay as-is."""

    variant: str = Field(default="dealer")  # "dealer" | "real_estate"
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=48)
    business_name: str | None = Field(default=None, max_length=180)  # or investor name, real-estate
    notify_client: bool = False  # default OFF — this is an admin audit flow, not client self-service
    force_new: bool = False  # create a second lead even if one already exists for this bucket
    preferred_language: Language = Language.EN

    @field_validator("variant", mode="before")
    @classmethod
    def normalize_variant(cls, value: object) -> object:
        return (str(value).strip().lower() if value else "dealer")

    @field_validator("phone", "business_name", mode="before")
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


class AdminContractRequest(BaseModel):
    """Admin requests one of the 3 client-facing contract types (SBA
    Engagement, Client Engagement, Consulting Addendum) be signed on this
    lead. Admin supplies the handful of deal-specific blanks not already on
    the lead record (client legal name/entity/state auto-fill from the lead
    where available); render_contract_document() fills the rest from each
    field's own default (same pattern as the Referral Protection portal),
    and the flattened text is stored exactly like the existing
    credit-authorization requested-document — the sign-time path is
    completely unchanged."""

    contract_type: ContractType
    field_values: dict[str, str] = Field(default_factory=dict)


class AdminContractRequestStatus(BaseModel):
    contract_type: ContractType
    requested: bool
    signed: bool
    requested_document_id: UUID | None = None


class AdminCreditAuthorizationRequest(BaseModel):
    """Admin requests a credit-authorization signature from the client on a
    lead. Same request/response shape for dealer AND real-estate leads — the
    only thing that varies per vertical is which template/default text gets
    attached, which is admin-supplied data, not branching logic."""

    template_file_id: UUID | None = None  # an admin-uploaded blank form (e.g. dealer-specific doc)
    document_text: str | None = None  # overrides the built-in default disclosure when no template


class RequestPfsOrDebtScheduleRequest(BaseModel):
    """Admin/broker requests a Personal Financial Statement or Debt Schedule
    on a dealer lead. Optional owner_name lets admin/broker request a SECOND
    (or later) owner's PFS beyond the single baseline row every dealer lead
    already gets at creation — _ensure_requested_document is idempotent on
    the exact document name, so re-requesting the baseline document (no
    owner_name) is always a safe no-op, never a duplicate."""

    owner_name: str | None = Field(default=None, max_length=180)

    @field_validator("owner_name", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return None if value == "" else value


class RequestLeadDeletionRequest(BaseModel):
    """Flags a lead for deletion — sets delete_requested_at/by only, destroys
    nothing. The requesting broker's own list filters this lead out
    immediately; the admin list never does, so admin always sees pending
    requests and must separately confirm before anything is destroyed."""

    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return None if value == "" else value


class ConfirmLeadDeletionRequest(BaseModel):
    """Super-admin's confirmation for an irreversible hard delete. The
    frontend gates this behind a themed danger confirm dialog, so the API no
    longer requires a prior deletion-request flag or a typed-name speed bump —
    a super admin can delete in one action. confirm_name is accepted but
    optional (kept for backward compatibility / audit)."""

    confirm_name: str | None = Field(default=None, max_length=180)


class AdminCreditPullRequest(BaseModel):
    ssn: str | None = Field(default=None, description="9 digits, no dashes; optional retry after no-hit")

    @field_validator("ssn")
    @classmethod
    def _ssn_digits_only(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not v.isdigit() or len(v) != 9:
            raise ValueError("SSN must be exactly 9 digits, no dashes")
        return v


class BankerSensitiveIdentifiers(BaseModel):
    """Transient identifiers collected only at final banker-submission time
    -- never persisted to intake_state or any DB column, mirroring
    AdminCreditPullRequest.ssn's never-persisted convention. Forwarded
    in-memory into build_banker_payload and returned once in the response;
    the caller must never log or store the response."""

    ssn: str | None = Field(default=None, description="9 digits, no dashes")
    personal_tax_id: str | None = Field(default=None, description="9 digits, no dashes (ITIN)")

    @field_validator("ssn", "personal_tax_id")
    @classmethod
    def _digits_only(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not v.isdigit() or len(v) != 9:
            raise ValueError("Must be exactly 9 digits, no dashes")
        return v


class PrepareBankerSubmissionRequest(BaseModel):
    identifiers: BankerSensitiveIdentifiers = Field(default_factory=BankerSensitiveIdentifiers)


class PrepareBankerSubmissionResponse(BaseModel):
    payload: dict[str, Any]


class LeadCreditStatusResponse(BaseModel):
    authorization_requested: bool
    authorization_signed: bool
    requested_document_id: UUID | None = None
    pull_id: UUID | None = None
    fico: int | None = None
    pulled_at: datetime | None = None
    expires_at: datetime | None = None


# Human-facing labels for every _compute_loan_program_fit key — the single
# source of truth consumed by the admin panel, the lender-packet PDF section,
# and the executive-summary "Eligible programs" metric splice, so a new
# program only needs a label added here rather than 3 separate hardcoded
# lists staying in sync by hand.
PROGRAM_LABELS: dict[str, str] = {
    "sba": "SBA",
    "real_estate_backed": "Real-estate-backed",
    "reinsurance_backed": "Reinsurance-backed",
    "jumbo_dscr": "Jumbo / DSCR",
    "term_loan_10_year": "10-Year Term Loan",
    "term_loan_3_5_year": "3-5 Year Term Loan",
    "term_loan_loc_hybrid": "Term Loan / LOC Hybrid",
    "line_of_credit": "Line of Credit",
    "equipment_financing": "Equipment Financing",
    "merchant_processing": "Merchant Processing",
    "transportation_factoring": "Transportation Factoring",
    "debt_consulting": "Debt Consulting",
}


class LeadProgramFitResponse(BaseModel):
    """Admin-only read of the deterministic program-fit screen — mirrors
    LeadCreditStatusResponse's shape (a plain read-only status endpoint, not
    routed through the redacted intake_state payload). `programs` is keyed by
    the same program keys as PROGRAM_LABELS/_compute_loan_program_fit."""
    computed: bool
    programs: dict[str, dict[str, Any]] = Field(default_factory=dict)


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


class DealerDocumentSignRequest(BaseModel):
    """Generic e-sign submission for a requires_signature BucketRequestedDocument.
    applicant_* fields are only required when the requested document's
    signature_kind is "credit_authorization" (mirrors CreditPullRequest's
    identity fields minus SSN, which is never collected/persisted here)."""

    requested_document_id: UUID
    typed_name: str = Field(min_length=1, max_length=160)
    esign_consent: bool
    signature_data_url: str = Field(min_length=1)
    applicant_legal_first_name: str | None = Field(default=None, max_length=120)
    applicant_legal_last_name: str | None = Field(default=None, max_length=120)
    applicant_dob: str | None = Field(default=None, max_length=32)
    applicant_street: str | None = Field(default=None, max_length=240)
    applicant_city: str | None = Field(default=None, max_length=120)
    applicant_state: str | None = Field(default=None, max_length=2)
    applicant_zip: str | None = Field(default=None, max_length=10)


class DealerPfsAssetRow(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0)


class DealerPfsLiabilityRow(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0)


class DealerPfsSubmission(BaseModel):
    """On-screen Personal Financial Statement submission — the fallback when a
    borrower doesn't have or doesn't understand a real PFS to upload. No SSN
    field: not needed for the AI's net-worth/liquidity math, and a hard credit
    pull (if ever needed) goes through the separate credit-authorization
    e-sign flow, which also never collects SSN."""

    owner_full_name: str = Field(min_length=1, max_length=180)
    statement_date: str = Field(min_length=1, max_length=32)
    assets: list[DealerPfsAssetRow] = Field(min_length=1, max_length=8)
    liabilities: list[DealerPfsLiabilityRow] = Field(min_length=0, max_length=6)
    acknowledgment: bool


class DealerDebtScheduleRow(BaseModel):
    lender: str = Field(min_length=1, max_length=180)
    balance: float = Field(ge=0)
    monthly_payment: float = Field(ge=0)


class DealerDebtScheduleSubmission(BaseModel):
    """On-screen Debt Schedule submission — same fallback rationale as
    DealerPfsSubmission above."""

    business_name: str = Field(min_length=1, max_length=180)
    debts: list[DealerDebtScheduleRow] = Field(min_length=1, max_length=30)
    acknowledgment: bool


class DealerBookCallRequest(BaseModel):
    starts_at: datetime


class DriveIngestRequest(BaseModel):
    # Google Drive file ids the operator selected in the Drive picker. Under the
    # drive.file scope these resolve only to files the app can see (picker-/app-
    # granted), downloaded via the operator's OAuth grant at ingest time.
    drive_file_ids: list[str] = Field(min_length=1, max_length=50)


class DriveIngestItemResult(BaseModel):
    drive_file_id: str
    file_name: str | None = None
    status: str  # "ingested" | "skipped"
    reason: str | None = None


class DriveIngestResponse(BaseModel):
    ingested: int
    skipped: int
    items: list[DriveIngestItemResult]


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
    # Attachment toggles. Defaults preserve prior behavior (lender packet on;
    # summary PDF + ZIP off). `include_lender_packet` (inherited) still gates the
    # packet for backward-compat; attach_lender_packet mirrors it when provided.
    attach_lender_packet: bool | None = None  # None → fall back to include_lender_packet
    attach_executive_summary: bool = False  # summary is markdown → attached as .txt
    attach_package_zip: bool = False
    # How the recipient reaches the secure bucket:
    #   "login"    → Clerk-invited vendor login link (default, prior behavior)
    #   "passcode" → a no-login BucketShare link + one-time passcode embedded in the body
    #   "none"     → no bucket access blurb
    bucket_access: Literal["login", "passcode", "none"] = "login"
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
    preferred_language: str = "en"
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
    # Internal admin <-> dealer-partner notes thread. Populated only for the
    # admin_thread=True audience (admin cockpit + broker portal) — never sent
    # to the public/uploader client-facing response.
    notes: list[BucketNoteRead] = []


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
    outcome_status: str = "submitted"
    preferred_language: str = "en"
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
    # Two-step delete state — never null-filtered out of the admin list, so
    # admin always sees pending requests and must separately confirm.
    delete_requested_at: datetime | None = None
    delete_requested_by: str | None = None
    # Client/broker activity this admin hasn't seen yet (admin_activity_seen).
    unseen_activity_count: int = 0
    # Unread messages in the team<->partner communication channel for THIS
    # viewer (dealer_lead_channel_seen). Distinct from unseen_activity_count,
    # which counts all bucket activity, not just channel messages.
    channel_unread_count: int = 0


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


async def _record_resume_email(
    intake: PublicUnderwritingIntake,
    *,
    token: str,
    request: Request,
    reason: str,
    public_path: str = "/dealer-ai-underwriter",
    review_label: str = "dealer funding review",
    room_label: str = "dealer financing file",
    db: AsyncSession | None = None,
    sender_user_id: UUID | None = None,
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
    # When an authenticated admin created the lead, send the resume link FROM their
    # connected Gmail (send_as_user, SES fallback). Public/self-serve callers have no
    # acting user, so they stay on firm SES.
    if db is not None and sender_user_id is not None:
        from app.services.email.user_mailer import send_as_user

        result = await send_as_user(
            db, sender_user_id, to_emails=[intake.email], subject=subject,
            body_text=body_text, body_html=body_html,
        )
    else:
        import asyncio as _asyncio

        result = await _asyncio.to_thread(
            send_email, to_email=intake.email, subject=subject, body_text=body_text, body_html=body_html,
        )
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
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents).selectinload(BucketRequestedDocument.template_file),
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
                .selectinload(Bucket.requested_documents)
                .selectinload(BucketRequestedDocument.template_file),
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
    """Required checklist items not yet satisfied. A doc is satisfied either by
    a directly-linked upload OR by the review-time reconciliation that flips
    status to "uploaded" when an analyzed file's classification matches the
    category (chat-room drag-drops rarely link a requested_document_id, so
    status is the only signal for them)."""
    uploaded = _uploaded_doc_ids(bucket)
    return [
        doc
        for doc in bucket.requested_documents
        if doc.required and doc.status != "uploaded" and doc.id not in uploaded
    ]


# Which of the frontend's 8 fixed PFS asset-row labels count toward
# liquid_assets (cash/savings/marketable securities only, per
# FILE_ANALYSIS_PREAMBLE's own definition — excludes retirement, real estate,
# vehicles, business equity, other). Must stay in sync with the matching
# PFS_ASSET_LABELS liquid flags in qcdesktop's DraftFinancialFormModal.tsx.
_PFS_LIQUID_LABELS = frozenset({
    "Cash on hand and in banks",
    "Savings accounts",
    "Stocks and bonds / other marketable securities",
})


def _pfs_key_facts(payload: DealerPfsSubmission) -> dict[str, Any]:
    total_assets = sum(row.amount for row in payload.assets)
    total_liabilities = sum(row.amount for row in payload.liabilities)
    liquid_assets = sum(row.amount for row in payload.assets if row.label in _PFS_LIQUID_LABELS)
    return {
        "statement_date": payload.statement_date,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities,
        "liquid_assets": liquid_assets,
    }


def _debt_schedule_key_facts(payload: DealerDebtScheduleSubmission) -> dict[str, Any]:
    debts = [
        {
            "lender": row.lender,
            "original_amount": None,
            "current_balance": row.balance,
            "monthly_payment": row.monthly_payment,
            "maturity_date": None,
        }
        for row in payload.debts
    ]
    return {
        "debts": debts,
        "total_monthly_debt_service": sum(row.monthly_payment for row in payload.debts),
        "total_outstanding_balance": sum(row.balance for row in payload.debts),
    }


async def _ensure_requested_document(
    db: AsyncSession,
    bucket: Bucket,
    *,
    name: str,
    category: str,
    description: str | None = None,
    allow_multiple_files: bool = False,
) -> BucketRequestedDocument:
    """Idempotent get-or-create by (bucket_id, name) — lets the dealer chat
    add a new baseline document mid-conversation (e.g. a newly-named owner's
    ID, a tax extension filing, reinsurance statements) without duplicating
    the row on a later turn. Mirrors the initial-bucket-creation loop in
    _create_bucket_for_intake, just callable outside that one-time path."""
    existing = next((doc for doc in bucket.requested_documents if doc.name == name), None)
    if existing is not None:
        return existing
    doc = BucketRequestedDocument(
        bucket_id=bucket.id,
        name=name,
        category=category,
        description=description,
        required=True,
        allow_multiple_files=allow_multiple_files,
        status="requested",
        is_custom=True,
    )
    db.add(doc)
    await db.flush()
    bucket.requested_documents.append(doc)
    return doc


def _asset_rows(intake: PublicUnderwritingIntake) -> list[dict[str, Any]]:
    rows = intake.asset_rows if isinstance(intake.asset_rows, list) else []
    return [row for row in rows if isinstance(row, dict)]


def _intake_state(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    return dict(intake.intake_state or {})


def _entity_structure(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    raw = _intake_state(intake).get("entity_structure")
    return raw if isinstance(raw, dict) else {}


def _credit_pull_state(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """The compact credit-pull cross-reference written by
    run_lead_credit_pull: {pull_id, fico, pulled_at, expires_at}. Same
    intake_state sub-object pattern as entity_structure/funding_review_basics.
    Deliberately NOT in _CLIENT_SAFE_INTAKE_STATE_KEYS — bureau data stays
    admin/AI-only, for both dealer and real-estate leads."""
    raw = _intake_state(intake).get("credit_pull")
    return raw if isinstance(raw, dict) else {}


def _key_metrics(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """The latest computed key_metrics dict, sourced from the latest review
    result if present, else the intake's result_snapshot. Both carry the same
    shape (see REVIEW_PREAMBLE/_compute_key_metrics_from_cache)."""
    review = intake.latest_review if intake.latest_review else None
    result = review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
    if not isinstance(result, dict):
        return {}
    km = result.get("key_metrics")
    return km if isinstance(km, dict) else {}


def _loan_program_fit(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """The deterministically-computed program-fit signal written by
    _compute_loan_program_fit — same intake_state sub-object pattern as
    credit_pull. Admin/AI-only, like credit_pull: never surfaced to the
    dealer directly (no widget, no chat announcement of program names or
    pricing) — the AI only uses it to avoid re-asking a resolved question."""
    raw = _intake_state(intake).get("loan_program_fit")
    return raw if isinstance(raw, dict) else {}


# Reinsurance-backed program pricing, as supplied by the lending desk. Rate
# steps downward with loan size; anything at/above $5MM is custom-priced by
# the desk, not looked up here. One-year maturity; a 3-year maturity is not
# yet approved by management. Loans under $3MM require only a Personal
# Financial Statement (PFS); at/above $3MM the full underwriting package
# (the enriched dealer baseline) is required regardless.
_REINSURANCE_RATE_STEPS = (
    (1_500_000, 7.87),
    (2_000_000, 7.62),
    (3_000_000, 7.32),
)
_REINSURANCE_MIN_REVENUE = 500_000
_REINSURANCE_MIN_LIQUID_ASSETS = 2_500_000
_REINSURANCE_DOC_TIER_THRESHOLD = 3_000_000
_REINSURANCE_CUSTOM_PRICING_THRESHOLD = 5_000_000
_JUMBO_MIN_REVENUE = 750_000
_JUMBO_MIN_DSCR = 1.25

# Screening thresholds for the 10 programs added alongside the original 4
# (sba/real_estate_backed/reinsurance_backed/jumbo_dscr above). Deterministic,
# non-AI, computed from the same _key_metrics()/_dealer_details()/document-
# checklist inputs the original 4 already use — a qualification-potential
# screen, not a field-completeness form. Every threshold here is a
# provisional screening cutoff pending lending-desk sign-off, same status as
# the reinsurance/jumbo constants above when they were first added.
#
# The term-band numbers are aliased from the Main Street module rather than
# restated. They were copied here once and then drifted: when the lender's sheet
# was reconciled, Main Street moved and this file did not, so the same business
# could screen differently depending on which funnel it arrived through. Binding
# to the source makes that particular drift impossible.
#
# The predicate *shapes* still differ — the dealer screen has no minimum-profile,
# FICO or time-in-business gate — and unifying those means refactoring this
# router, which is a separate job.
_TERM_3_5_MIN_REVENUE = TERM_3_5_MIN_REVENUE
_TERM_3_5_MIN_DSCR = TERM_3_5_MIN_DSCR
_TERM_10YR_MIN_DSCR = TERM_10YR_MIN_DSCR

_LOC_MIN_REVENUE = 100_000
_TERM_HYBRID_MIN_REVENUE = 200_000
_TERM_HYBRID_MIN_DSCR = 1.1

# KNOWN DIVERGENCE, deliberately not reconciled here. On the Main Street sheet
# the 10-year band has no revenue minimum at all — what bounds it is a cap of
# 50% of annualized sales, so revenue sizes the loan instead of gating it. That
# rule needs a requested amount to mean anything, and applying it on this path
# would change which programs dealer files surface, which is a desk decision
# rather than a cleanup. Until then this keeps its own revenue proxy.
#
# Worth knowing when that decision gets made: the real program excludes auto,
# RV and boat dealerships outright, so on a dealer file it is very likely the
# honest answer is "not eligible" rather than a different threshold.
_TERM_10YR_MIN_REVENUE = 300_000
_MERCHANT_PROCESSING_MIN_ANNUALIZED_DEPOSITS = 120_000
_FACTORING_MIN_REVENUE = 100_000
_DEBT_CONSULTING_MAX_DSCR = 1.0


def _reinsurance_rate_for_amount(amount: float | None) -> tuple[float | None, bool]:
    """Returns (rate_percent, is_custom_priced). rate_percent is None until an
    amount is known; is_custom_priced is True at/above $5MM, where the desk
    prices individually rather than off this step table."""
    if amount is None:
        return None, False
    if amount >= _REINSURANCE_CUSTOM_PRICING_THRESHOLD:
        return None, True
    rate = None
    for threshold, step_rate in _REINSURANCE_RATE_STEPS:
        if amount >= threshold:
            rate = step_rate
    return rate, False


def _compute_loan_program_fit(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """Deterministic (non-AI) computation of which financing programs this
    dealer file likely qualifies for, mirroring the credit_pull mechanism:
    a plain computed dict written to intake_state, read back by an accessor,
    and injected into the AI context / packet financials / PDF / executive
    summary — never surfaced to the dealer directly. Recomputed cheaply on
    every chat turn (no AI call, no DB round trip beyond what's already
    loaded), so it never goes stale.

    All 14 programs are screened from the SAME inputs — _key_metrics()
    (AI-derived from uploaded documents: revenue, DSCR, cash flow, debt
    burden, deposit velocity, PFS liquid assets), the document checklist, and
    _dealer_details() (the handful of borrower-stated facts with no document
    source). This is a qualification-potential screen over metrics the
    document-analysis pipeline already computes, not a field-completeness
    form — no program below requires a new borrower-facing field.

    - SBA: the default path. Eligible once the full enriched baseline is
      complete (tax returns/extension, current-year P&L, bank statements,
      debt schedule, PFS, and one ID per declared owner) — no size/DSCR
      threshold of its own.
    - real_estate_backed: flagged when real-estate collateral has been
      declared with a stated value. No pricing rules were supplied for this
      program, so it carries eligibility only.
    - reinsurance_backed: eligible when the dealer/owner has confirmed a
      reinsurance account, both reinsurance statements are uploaded, and
      either revenue or PFS liquid assets clear the stated thresholds.
      Carries the desk-supplied pricing table and doc-tier flag.
    - jumbo_dscr: eligible when revenue and the real (debt-schedule-derived)
      DSCR both clear their thresholds.
    - term_loan_10_year / term_loan_3_5_year / term_loan_loc_hybrid: revenue
      + DSCR bands, same shape as jumbo_dscr — longer terms carry a higher
      bar on both.
    - line_of_credit: a revenue/cash-flow floor with a lighter documentation
      bar than the term programs (bank-derived deposit activity alone, not
      the full enriched baseline SBA requires).
    - equipment_financing: eligible once the borrower has stated equipment-
      or-vehicle financing intent (no document source for pure intent) AND
      cash flow is positive enough to support a payment.
    - merchant_processing: a deposit-velocity floor from bank-statement
      analysis — not a loan, so no DSCR requirement.
    - transportation_factoring: a revenue floor, same cash-flow-based signal
      as merchant processing (receivables-specific extraction not yet
      available — revenue is the best current proxy).
    - debt_consulting: triggered by a distress signal (DSCR at/below the
      consulting threshold once a real debt schedule exists) — a "this file
      would benefit from consolidation" flag, not a funding-amount rule.
    """
    km = _key_metrics(intake)
    revenue = km.get("ytd_annualized_revenue")
    dscr = km.get("estimated_dscr")
    cash_flow = km.get("estimated_ebitda_or_cash_flow")
    annualized_deposits = km.get("annualized_adjusted_deposits")
    liquid_assets = km.get("pfs_total_liquid_assets")
    details = _dealer_details(intake)
    requested_amount = float(intake.requested_loan_amount) if intake.requested_loan_amount is not None else None

    def _meets(value: Any, threshold: float) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= threshold

    def _clears(value: Any, threshold: float) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > threshold

    sba = {"eligible": not _missing_required_docs(intake.bucket)}

    real_estate_backed = {
        "eligible": _has_real_estate_schedule(intake),
        "note": "Advance rate and pricing pending admin review — no rules configured for this program yet.",
    }

    reinsurance_present = details.get("reinsurance_account_present") is True
    reinsurance_docs_uploaded = reinsurance_present and not any(
        "reinsurance" in (doc.category or "").lower() and doc.id not in _uploaded_doc_ids(intake.bucket)
        for doc in intake.bucket.requested_documents
    )
    reinsurance_liquidity_met = _meets(revenue, _REINSURANCE_MIN_REVENUE) or _meets(
        liquid_assets, _REINSURANCE_MIN_LIQUID_ASSETS
    )
    reinsurance_eligible = reinsurance_present and reinsurance_docs_uploaded and reinsurance_liquidity_met
    rate, custom_priced = _reinsurance_rate_for_amount(requested_amount)
    reinsurance_backed = {
        "eligible": reinsurance_eligible,
        "trading_platform": details.get("reinsurance_trading_platform"),
        "requested_amount": requested_amount,
        "rate_percent": rate,
        "custom_priced_5mm_plus": custom_priced,
        "maturity_years": 1,
        "doc_tier": (
            "full_underwriting"
            if requested_amount is not None and requested_amount >= _REINSURANCE_DOC_TIER_THRESHOLD
            else "pfs_only"
        ),
    }

    jumbo_eligible = _clears(revenue, _JUMBO_MIN_REVENUE) and _clears(dscr, _JUMBO_MIN_DSCR)
    jumbo_dscr = {"eligible": bool(jumbo_eligible), "revenue": revenue, "dscr": dscr}

    term_loan_10_year = {
        "eligible": bool(_meets(revenue, _TERM_10YR_MIN_REVENUE) and _meets(dscr, _TERM_10YR_MIN_DSCR)),
        "revenue": revenue,
        "dscr": dscr,
    }
    term_loan_3_5_year = {
        "eligible": bool(_meets(revenue, _TERM_3_5_MIN_REVENUE) and _meets(dscr, _TERM_3_5_MIN_DSCR)),
        "revenue": revenue,
        "dscr": dscr,
    }
    term_loan_loc_hybrid = {
        "eligible": bool(_meets(revenue, _TERM_HYBRID_MIN_REVENUE) and _meets(dscr, _TERM_HYBRID_MIN_DSCR)),
        "revenue": revenue,
        "dscr": dscr,
    }
    line_of_credit = {
        "eligible": bool(_meets(revenue, _LOC_MIN_REVENUE) or _meets(annualized_deposits, _LOC_MIN_REVENUE)),
        "revenue": revenue,
        "annualized_deposits": annualized_deposits,
    }
    equipment_financing_intent = details.get("financing_equipment_or_vehicle") is True
    equipment_financing = {
        "eligible": bool(equipment_financing_intent and _clears(cash_flow, 0)),
        "cash_flow": cash_flow,
    }
    merchant_processing = {
        "eligible": bool(_meets(annualized_deposits, _MERCHANT_PROCESSING_MIN_ANNUALIZED_DEPOSITS)),
        "annualized_deposits": annualized_deposits,
    }
    transportation_factoring = {
        "eligible": bool(_meets(revenue, _FACTORING_MIN_REVENUE)),
        "revenue": revenue,
    }
    debt_consulting_eligible = km.get("estimated_debt_burden") is not None and isinstance(
        dscr, (int, float)
    ) and not isinstance(dscr, bool) and dscr <= _DEBT_CONSULTING_MAX_DSCR
    debt_consulting = {
        "eligible": bool(debt_consulting_eligible),
        "dscr": dscr,
        "estimated_debt_burden": km.get("estimated_debt_burden"),
    }

    return {
        "sba": sba,
        "real_estate_backed": real_estate_backed,
        "reinsurance_backed": reinsurance_backed,
        "jumbo_dscr": jumbo_dscr,
        "term_loan_10_year": term_loan_10_year,
        "term_loan_3_5_year": term_loan_3_5_year,
        "term_loan_loc_hybrid": term_loan_loc_hybrid,
        "line_of_credit": line_of_credit,
        "equipment_financing": equipment_financing,
        "merchant_processing": merchant_processing,
        "transportation_factoring": transportation_factoring,
        "debt_consulting": debt_consulting,
    }


def _apply_loan_program_fit(intake: PublicUnderwritingIntake) -> None:
    """Recomputes and writes intake_state["loan_program_fit"]. Cheap and
    idempotent — safe to call on every dealer chat turn."""
    state = _intake_state(intake)
    state["loan_program_fit"] = _compute_loan_program_fit(intake)
    intake.intake_state = state


# ---------------------------------------------------------------------------
# DSCR potential — deterministic math for real_estate_dscr_v1 leads. The AI
# review/chat can reason about DSCR, but the numbers themselves come from this
# arithmetic, never from the model. Assumption constants are surfaced verbatim
# in the output so every consumer (admin panel, AI context, packets) shows the
# same math. Mirrors the program-fit pattern: cheap, idempotent, recomputed
# live rather than trusted from a snapshot.
_DSCR_RATE_BANDS = (0.0675, 0.075, 0.0825)  # fallback 30-yr fixed DSCR note rates
_DSCR_AMORT_MONTHS = 360
_DSCR_TARGETS = (1.0, 1.10, 1.25)
# When no tax/insurance/HOA evidence is uploaded yet, estimate carrying costs
# as an annual percentage of property value (≈1.1% taxes + 0.5% insurance).
_DSCR_TAX_INS_ANNUAL_PCT_OF_VALUE = 0.016

# Admin-configurable DSCR pricing (AppSettings.data["dscr_pricing"], see
# DscrPricingSettings). Cached at module level with a short TTL so the sync
# context builders can price without a DB round trip; async endpoints refresh
# it opportunistically. Falls back to the constants above when never loaded.
_DSCR_PRICING_TTL_SECONDS = 60.0
_dscr_pricing_cache: dict[str, Any] = {}
_dscr_pricing_cache_at: float = 0.0


async def _refresh_dscr_pricing(db: AsyncSession) -> None:
    global _dscr_pricing_cache, _dscr_pricing_cache_at
    now = time.monotonic()
    if _dscr_pricing_cache and now - _dscr_pricing_cache_at < _DSCR_PRICING_TTL_SECONDS:
        return
    from app.models.app_settings import AppSettings
    from app.schemas.settings import DscrPricingSettings

    row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    raw = (row.data or {}).get("dscr_pricing") if row else None
    try:
        parsed = DscrPricingSettings.model_validate(raw) if isinstance(raw, dict) else DscrPricingSettings()
    except Exception:  # noqa: BLE001 — a malformed blob must never break pricing
        parsed = DscrPricingSettings()
    _dscr_pricing_cache = parsed.model_dump()
    _dscr_pricing_cache_at = now


def _dscr_fico_estimate(intake: PublicUnderwritingIntake) -> tuple[int | None, str]:
    """Best available credit signal: soft-pull FICO first, then the stated
    credit tier text ("720-759", "Mid Credit", "excellent", a bare score)."""
    pulled = _credit_pull_state(intake).get("fico")
    if isinstance(pulled, (int, float)) and not isinstance(pulled, bool) and 300 <= pulled <= 850:
        return int(pulled), "soft credit pull"
    basics_raw = _intake_state(intake).get("funding_review_basics")
    basics = basics_raw if isinstance(basics_raw, dict) else {}
    tier_text = str(basics.get("estimated_credit_tier") or getattr(intake, "estimated_credit_score", None) or "").strip().lower()
    if not tier_text:
        return None, "no credit signal"
    match = re.search(r"\d{3}", tier_text)
    if match:
        score = int(match.group())
        if 300 <= score <= 850:
            return score, f"stated tier '{tier_text}'"
    for keyword, score in (("excellent", 780), ("prime", 760), ("good", 720), ("mid", 700), ("fair", 660), ("low", 620), ("poor", 580)):
        if keyword in tier_text:
            return score, f"stated tier '{tier_text}'"
    return None, "no credit signal"


def _dscr_rate_bands_for(intake: PublicUnderwritingIntake) -> tuple[tuple[float, ...], int, float, str]:
    """(rate_bands, amortization_months, tax_ins_pct, credit_note) from the
    cached admin pricing config and the file's credit signal."""
    pricing = _dscr_pricing_cache
    if not pricing:
        return _DSCR_RATE_BANDS, _DSCR_AMORT_MONTHS, _DSCR_TAX_INS_ANNUAL_PCT_OF_VALUE, "default pricing (settings not loaded)"
    fico, fico_source = _dscr_fico_estimate(intake)
    tiers = sorted(pricing.get("rate_tiers") or [], key=lambda tier: -int(tier.get("min_fico", 0)))
    base = None
    tier_note = ""
    if tiers:
        chosen = None
        if fico is not None:
            for tier in tiers:
                if fico >= int(tier.get("min_fico", 0)):
                    chosen = tier
                    break
        if chosen is None:
            chosen = tiers[len(tiers) // 2]
            tier_note = f"mid tier assumed ({fico_source})"
        else:
            tier_note = f"tier ≥{chosen.get('min_fico')} FICO via {fico_source}"
        base = float(chosen.get("annual_rate", _DSCR_RATE_BANDS[1]))
    if base is None:
        return _DSCR_RATE_BANDS, _DSCR_AMORT_MONTHS, _DSCR_TAX_INS_ANNUAL_PCT_OF_VALUE, "default pricing (no tiers configured)"
    spread = float(pricing.get("band_spread") or 0.0075)
    bands = tuple(rate for rate in (base - spread, base, base + spread) if rate > 0)
    months = int(pricing.get("amortization_months") or _DSCR_AMORT_MONTHS)
    tax_pct = float(pricing.get("tax_insurance_annual_pct_of_value") or _DSCR_TAX_INS_ANNUAL_PCT_OF_VALUE)
    return bands, months, tax_pct, tier_note


def _dscr_monthly_payment(principal: float, annual_rate: float, months: int = _DSCR_AMORT_MONTHS) -> float:
    """Amortizing monthly P&I for a fixed-rate note."""
    monthly_rate = annual_rate / 12.0
    factor = (monthly_rate * (1 + monthly_rate) ** months) / ((1 + monthly_rate) ** months - 1)
    return principal * factor


def _dscr_principal_for_payment(payment: float, annual_rate: float, months: int = _DSCR_AMORT_MONTHS) -> float:
    """Inverse of _dscr_monthly_payment: the loan size a monthly P&I supports."""
    monthly_rate = annual_rate / 12.0
    factor = (monthly_rate * (1 + monthly_rate) ** months) / ((1 + monthly_rate) ** months - 1)
    return payment / factor


def _km_number(km: dict[str, Any], *keys: str) -> float | None:
    """First numeric value among the given key_metrics keys (bool excluded)."""
    for key in keys:
        value = km.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


async def _merge_thread_borrower_facts(db: AsyncSession, intake: PublicUnderwritingIntake, assistant_message: BucketAIMessage) -> None:
    """Persist model-proposed borrower facts from ANY thread's assistant reply
    (client, admin cockpit, or broker portal) into the intake — the operator
    states deal-shaping facts ("client wants $1.5M") in the internal thread as
    often as the borrower does in theirs. Variant-routed to the correct
    validator; refreshes the deterministic program-fit snapshot for dealers."""
    raw = assistant_message.metadata_json.get("raw") if isinstance(assistant_message.metadata_json, dict) else None
    proposed = raw.get("proposed_borrower_facts") if isinstance(raw, dict) else None
    if not proposed:
        return
    if intake.variant == FUNDING_VARIANT:
        _merge_funding_review_details(intake, proposed)
    else:
        newly_accepted = _merge_dealer_details(intake, proposed)
        await _apply_dealer_detail_documents(db, intake, newly_accepted)
        _apply_loan_program_fit(intake)


def _compute_dscr_potential(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """Deterministic DSCR-potential screen for a real-estate lead.

    Combines the intake basics (borrower-stated rent/value/amount), the
    conversationally-gathered funding_review_details, and any document-derived
    key_metrics from the latest review, preferring extracted evidence over
    stated values. Produces, with the math shown:
      - LTV at the requested amount
      - projected PITIA and DSCR at each candidate rate band
      - max supportable loan (and implied LTV) at DSCR targets 1.00/1.10/1.25
      - the monthly rent required to carry the requested amount at each target
    """
    basics_raw = _intake_state(intake).get("funding_review_basics")
    basics = basics_raw if isinstance(basics_raw, dict) else {}
    details = _funding_review_details(intake)
    km = _key_metrics(intake)

    def _stated(key: str) -> float | None:
        value = basics.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return None

    extracted_rent = _km_number(km, "monthly_rent", "in_place_monthly_rent", "market_monthly_rent", "gross_monthly_rent")
    monthly_rent = extracted_rent or _stated("monthly_rent")
    rent_source = "documents" if extracted_rent else ("stated" if monthly_rent else None)

    extracted_value = _km_number(km, "estimated_property_value", "property_value", "purchase_price", "appraised_value")
    property_value = extracted_value or _stated("estimated_value_or_purchase_price")
    value_source = "documents" if extracted_value else ("stated" if property_value else None)

    requested = float(intake.requested_loan_amount) if intake.requested_loan_amount is not None else _stated("requested_amount")

    extracted_carry = _km_number(km, "monthly_tax_insurance_hoa", "monthly_taxes_insurance", "monthly_pitia_taxes_insurance")
    extracted_pitia = _km_number(km, "monthly_pitia", "estimated_pitia", "pitia")

    missing = [
        label
        for label, value in (
            ("monthly rent (lease, rent roll, or stated)", monthly_rent),
            ("property value or purchase price", property_value),
            ("requested loan amount", requested),
        )
        if value is None
    ]
    if missing:
        return {"computed": False, "missing": missing}

    rate_bands, amort_months, tax_ins_pct, credit_note = _dscr_rate_bands_for(intake)

    if extracted_carry is not None:
        monthly_tax_ins = extracted_carry
        carry_source = "documents"
    else:
        monthly_tax_ins = property_value * tax_ins_pct / 12.0
        carry_source = f"assumed {tax_ins_pct:.1%} of value per year"

    ltv = requested / property_value if property_value else None
    mid_rate = rate_bands[len(rate_bands) // 2]

    scenarios = []
    for rate in rate_bands:
        pi = _dscr_monthly_payment(requested, rate, amort_months)
        pitia = pi + monthly_tax_ins
        scenarios.append(
            {
                "annual_rate": rate,
                "monthly_principal_interest": round(pi, 2),
                "monthly_pitia": round(pitia, 2),
                "dscr": round(monthly_rent / pitia, 3) if pitia else None,
            }
        )

    max_loans = {}
    required_rents = {}
    requested_pitia_mid = _dscr_monthly_payment(requested, mid_rate, amort_months) + monthly_tax_ins
    for target in _DSCR_TARGETS:
        supportable_pi = monthly_rent / target - monthly_tax_ins
        max_loan = _dscr_principal_for_payment(supportable_pi, mid_rate, amort_months) if supportable_pi > 0 else 0.0
        max_loans[f"{target:.2f}"] = {
            "max_loan": round(max_loan, 0),
            "implied_ltv": round(max_loan / property_value, 3) if property_value else None,
            "at_annual_rate": mid_rate,
        }
        required_rents[f"{target:.2f}"] = round(requested_pitia_mid * target, 2)

    dscr_mid = next(s["dscr"] for s in scenarios if s["annual_rate"] == mid_rate)
    return {
        "computed": True,
        "inputs": {
            "monthly_rent": monthly_rent,
            "monthly_rent_source": rent_source,
            "property_value": property_value,
            "property_value_source": value_source,
            "requested_loan_amount": requested,
            "monthly_tax_insurance_hoa": round(monthly_tax_ins, 2),
            "tax_insurance_source": carry_source,
            "extracted_monthly_pitia": extracted_pitia,
            "transaction_type": basics.get("transaction_type"),
            "estimated_credit_tier": basics.get("estimated_credit_tier"),
            "down_payment_amount": details.get("down_payment_amount"),
        },
        "assumptions": {
            "amortization_months": amort_months,
            "rate_bands": list(rate_bands),
            "benchmark_rate": mid_rate,
            "credit_pricing": credit_note,
            "note": "Fixed-rate fully-amortizing P&I; rate band from the file's credit signal and admin DSCR pricing settings; taxes/insurance from documents when uploaded, otherwise estimated from property value. Deterministic screen, not a quote.",
        },
        "ltv": round(ltv, 3) if ltv is not None else None,
        "dscr_at_requested": dscr_mid,
        "scenarios": scenarios,
        "max_loan_at_target_dscr": max_loans,
        "required_monthly_rent_at_requested": required_rents,
    }


# Fields the real-estate chat may populate on funding_review_details.
_FUNDING_REVIEW_DETAIL_KEYS = (
    "down_payment_amount",
    "prior_property_ownership",
    "is_commercial_property",
    "property_type",
    "requested_amount",
)


def _funding_review_details(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """Conversationally-gathered real-estate detail (down payment, prior
    ownership, residential-vs-commercial intent) — same intake_state
    sub-object pattern as funding_review_basics/credit_pull. Populated via
    proposed_borrower_facts in funding_review_chat, never by the dealer flow."""
    raw = _intake_state(intake).get("funding_review_details")
    return raw if isinstance(raw, dict) else {}


def _merge_funding_review_details(intake: PublicUnderwritingIntake, proposed: Any) -> None:
    """Validates and merges an AI-proposed proposed_borrower_facts object into
    intake_state["funding_review_details"]. Never trusts the model's shape
    blindly — any key not on the allowlist, or with the wrong type, is
    dropped rather than persisted."""
    if not isinstance(proposed, dict):
        return
    accepted: dict[str, Any] = {}
    for key in _FUNDING_REVIEW_DETAIL_KEYS:
        if key not in proposed:
            continue
        value = proposed[key]
        if key == "prior_property_ownership" or key == "is_commercial_property":
            if isinstance(value, bool):
                accepted[key] = value
            continue
        if key == "down_payment_amount":
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                accepted[key] = float(value)
            continue
        if key == "requested_amount":
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 500_000_000:
                accepted[key] = float(value)
            continue
        if key == "property_type":
            text = str(value).strip()
            if text:
                accepted[key] = text[:64]
            continue
    if not accepted:
        return
    state = _intake_state(intake)
    existing = state.get("funding_review_details")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(accepted)
    state["funding_review_details"] = merged
    # A restated amount supersedes the intake-form figure everywhere (DSCR
    # potential, PDF, packets) — keep the column and the basics in sync.
    if "requested_amount" in accepted:
        intake.requested_loan_amount = accepted["requested_amount"]
        basics = state.get("funding_review_basics")
        if isinstance(basics, dict):
            basics["requested_amount"] = accepted["requested_amount"]
            state["funding_review_basics"] = basics
    intake.intake_state = state


def _re_prequal_ready(intake: PublicUnderwritingIntake) -> bool:
    """True once a real-estate lead has enough to auto-surface a
    prequalification in chat: a completed soft credit pull, every RE baseline
    document category uploaded, and every conversational detail (down
    payment, prior ownership, commercial-vs-residential) answered — on top
    of the deal basics already required to start the intake. Distinct from
    _apply_lending_readiness (bucket_ai.py), which is dealer-shaped
    (bank-statement/tax-return baseline) and does not fit RE evidence
    categories or factor in credit-pull status. Dealer leads never call this."""
    if intake.variant != FUNDING_VARIANT:
        return False
    if _credit_pull_state(intake).get("fico") is None:
        return False
    if _missing_required_docs(intake.bucket):
        return False
    basics = _intake_state(intake).get("funding_review_basics")
    basics = basics if isinstance(basics, dict) else {}
    if not all(
        basics.get(key) not in (None, "")
        for key in ("target_property_address", "transaction_type", "requested_amount", "estimated_value_or_purchase_price")
    ):
        return False
    details = _funding_review_details(intake)
    return all(details.get(key) is not None for key in _FUNDING_REVIEW_DETAIL_KEYS if key != "property_type")


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


# Deal-shaping facts stated conversationally (any thread) that must land on
# the intake columns themselves — requested amount, monthly debt, use of funds
# — so the PDF, program fit, packets, and lists stop showing "—" for numbers
# the AI already knows.
_DEALER_DETAIL_KEYS = (
    "owners",
    "current_year_tax_filed",
    "reinsurance_account_present",
    "reinsurance_trading_platform",
    # Equipment-financing intent has no natural document source (nothing to
    # extract until an invoice/quote is uploaded) — same category as the
    # boolean facts above: a borrower-stated signal used only to gate the
    # equipment_financing program-fit rule, never a field-collection form.
    "financing_equipment_or_vehicle",
    "requested_loan_amount",
    "stated_monthly_debt_payments",
    "use_of_funds",
)


def _dealer_details(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """Conversationally-gathered dealer facts (owners, whether current-year
    taxes are filed, reinsurance-account status) — same intake_state
    sub-object pattern as funding_review_details/credit_pull. Populated via
    proposed_borrower_facts in dealer_intake_chat, never by the RE flow."""
    raw = _intake_state(intake).get("dealer_details")
    return raw if isinstance(raw, dict) else {}


def _merge_dealer_details(intake: PublicUnderwritingIntake, proposed: Any) -> dict[str, Any]:
    """Validates and merges an AI-proposed proposed_borrower_facts object into
    intake_state["dealer_details"]. Never trusts the model's shape blindly —
    any key not on the allowlist, or with the wrong type, is dropped rather
    than persisted. Returns the newly-accepted keys only (not the merged
    whole), so the caller can react to what's NEW this turn (e.g. create a
    requested document for a newly-named owner)."""
    if not isinstance(proposed, dict):
        return {}
    accepted: dict[str, Any] = {}
    for key in _DEALER_DETAIL_KEYS:
        if key not in proposed:
            continue
        value = proposed[key]
        if key == "owners":
            if not isinstance(value, list):
                continue
            owners: list[dict[str, Any]] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                percent = item.get("ownership_percent")
                owner: dict[str, Any] = {"name": name[:180]}
                if isinstance(percent, (int, float)) and not isinstance(percent, bool) and 0 <= percent <= 100:
                    owner["ownership_percent"] = float(percent)
                owners.append(owner)
            if owners:
                accepted[key] = owners
            continue
        if key in ("current_year_tax_filed", "reinsurance_account_present", "financing_equipment_or_vehicle"):
            if isinstance(value, bool):
                accepted[key] = value
            continue
        if key == "reinsurance_trading_platform":
            text = str(value).strip()
            if text:
                accepted[key] = text[:180]
            continue
        if key in ("requested_loan_amount", "stated_monthly_debt_payments"):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 500_000_000:
                accepted[key] = float(value)
            continue
        if key == "use_of_funds":
            text = str(value).strip()
            if text:
                accepted[key] = text[:500]
            continue
    if not accepted:
        return {}
    state = _intake_state(intake)
    existing = state.get("dealer_details")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(accepted)
    state["dealer_details"] = merged
    intake.intake_state = state
    # Deal-shaping facts land on the intake columns too, so every consumer
    # (PDF, program fit, packets, lead list) sees them without digging into
    # intake_state. Stated facts are authoritative over blank columns; a
    # newly-stated amount also supersedes an older stated amount.
    if "requested_loan_amount" in accepted:
        intake.requested_loan_amount = accepted["requested_loan_amount"]
    if "use_of_funds" in accepted and not (intake.loan_purpose or "").strip():
        intake.loan_purpose = accepted["use_of_funds"][:255]
    return accepted


async def _apply_dealer_detail_documents(db: AsyncSession, intake: PublicUnderwritingIntake, newly_accepted: dict[str, Any]) -> None:
    """Turns newly-learned dealer_details facts into dynamically-created
    requested documents, so the baseline checklist grows with the
    conversation instead of asking everything upfront. Idempotent via
    _ensure_requested_document — safe to call every turn, only acts on keys
    present in newly_accepted this turn."""
    if not newly_accepted:
        return
    if "owners" in newly_accepted:
        for owner in newly_accepted["owners"]:
            name = owner.get("name")
            if not name:
                continue
            await _ensure_requested_document(
                db,
                intake.bucket,
                name=f"Identification — {name}",
                category="Identity",
                description=f"Upload a government-issued photo ID for {name}.",
            )
    if newly_accepted.get("current_year_tax_filed") is False:
        await _ensure_requested_document(
            db,
            intake.bucket,
            name="Current-year tax extension filing",
            category="Financials",
            description="Upload the current-year tax extension filing since the return has not yet been filed.",
        )
    if newly_accepted.get("reinsurance_account_present") is True:
        await _ensure_requested_document(
            db,
            intake.bucket,
            name="Reinsurance account bank statement (last 2 months)",
            category="Reinsurance",
            description="Upload the last two months of bank statements for the reinsurance account.",
        )
        await _ensure_requested_document(
            db,
            intake.bucket,
            name="Reinsurance account administrator statement (last 2 months)",
            category="Reinsurance",
            description="Upload the last two months of administrator statements for the reinsurance account.",
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
        "book_call": {
            "type": "book_call",
            "title": "Book the next underwriting call",
            "description": "Choose one of the next available times with Qualified Commercial to validate the preliminary screen.",
        },
    }
    widget = widgets.get(kind)
    if widget is None:
        return None
    # upload_files is car-dealer-worded (floorplan, Stage 1 cash-flow docs);
    # suppress it for non-dealer intakes so a real-estate file never sees it.
    # book_call is product-neutral and stays available to both.
    review_type = (intake.bucket.ai_context or {}).get("review_type")
    if review_type != "dealer_gatekeeper_v1" and kind == "upload_files":
        return None
    return {**widget, "source": source, "reason": reason or source}


def _prequalification_widget(artifact: PublicUnderwritingIntakeArtifact) -> dict[str, Any]:
    """Builds the in-chat prequalification_result widget from a generated
    prequalification artifact's body_json, so the borrower sees the outcome
    as a card in the same turn it becomes ready — no separate round-trip."""
    body = artifact.body_json if isinstance(artifact.body_json, dict) else {}
    return {
        "type": "prequalification_result",
        "title": "You're prequalified",
        "description": str(body.get("prequalification_summary") or "Your preliminary prequalification is ready.")[:600],
        "source": "system_next_step",
        "reason": "prequalification_ready",
        "suggested_program": body.get("suggested_program"),
        "sizing": body.get("sizing") if isinstance(body.get("sizing"), dict) else None,
        "next_step": body.get("next_step"),
        "disclaimer": body.get("disclaimer"),
    }


def _message_for_widget(widget: dict[str, Any] | None, intake: PublicUnderwritingIntake) -> str:
    if not widget:
        if isinstance(intake.result_snapshot, dict):
            return _format_review_update(intake.result_snapshot)
        # No widget and no review yet: give a product-appropriate opening so a
        # real-estate file never sees dealer-flavored fallback text.
        if intake.variant == FUNDING_VARIANT:
            return _funding_empty_message(intake.preferred_language)
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
    if kind == "book_call":
        return "The preliminary screen is ready. Choose one of the available call times so Qualified Commercial can validate the file and next steps with you."
    if kind == "prequalification_result":
        return str(widget.get("description") or "Your preliminary prequalification is ready — review it below.")
    return "How can I help with this file?"


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
        "credit_pull": _credit_pull_state(intake) or None,
        "business_name": intake.business_name,
        "referral_source": intake.referral_source,
        "entity_structure": _entity_structure(intake),
        "asset_rows": _asset_rows(intake),
        "dealer_details": _dealer_details(intake) or None,
        "loan_program_fit": _loan_program_fit(intake) or None,
        "chat_facts": state.get("chat_facts") if isinstance(state.get("chat_facts"), list) else [],
        "baseline_document_policy": {
            "stage": "stage_1_bankability",
            "allowed_document_categories": [
                "last 2 years business tax returns (or current-year extension filing if not yet filed)",
                "current year/YTD P&L",
                "last 6 months main operating bank statements",
                "debt schedule",
                "personal financial statement for each owner",
                "identification for each owner",
                "requested amount",
                "detailed use of funds with amount breakdown",
                "stated current monthly debt payments",
                "estimated credit tier/score",
            ],
            "do_not_request_other_document_categories": True,
            "stage_2_after_good_probability_only": [
                "personal tax returns",
                "dealer license",
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


def _main_street_details(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """Borrower-stated facts with no document source, for the Main Street
    vertical. Mirrors _dealer_details but keyed to an operating business."""
    state = _intake_state(intake)
    raw = state.get("main_street_details")
    return raw if isinstance(raw, dict) else {}


def _main_street_context(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """AI context for an operating-business file.

    Deliberately does NOT carry the dealer's floorplan/MCA framing or the
    real-estate file's rent/PITIA framing. Industry and intent are first-class
    here because they decide which documents are even appropriate to ask for —
    see app/services/main_street_programs.py, which owns that mapping.
    """
    from app.services.main_street_programs import (
        MAIN_STREET_INDUSTRIES,
        intent_kind,
        normalize_industry,
        normalize_intent,
    )

    state = _intake_state(intake)
    details = _main_street_details(intake)
    intent = normalize_intent(details.get("intent"))
    industry = normalize_industry(details.get("industry"))
    kind = intent_kind(intent)

    context: dict[str, Any] = {
        "review_type": "main_street_v1",
        "deal_type": "operating business funding review",
        "documentation_level": "preliminary operating-business screen",
        "collateral_type": "business cash flow",
        "intent": intent,
        "intent_kind": kind,
        "industry": industry,
        "industry_label": MAIN_STREET_INDUSTRIES[industry]["en"],
        "loan_purpose": intake.loan_purpose,
        "requested_loan_amount": float(intake.requested_loan_amount)
        if intake.requested_loan_amount is not None
        else None,
        "estimated_credit_score": intake.estimated_credit_score,
        "credit_pull": _credit_pull_state(intake) or None,
        "business_name": intake.business_name,
        "referral_source": intake.referral_source,
        "entity_structure": _entity_structure(intake),
        "main_street_details": details or None,
        "chat_facts": state.get("chat_facts") if isinstance(state.get("chat_facts"), list) else [],
    }

    if kind == "non_lending":
        # No lending package, no program fit, no fundability verdict. Asking a
        # point-of-sale enquiry for two years of tax returns loses the lead, and
        # scoring it for fundability would be meaningless.
        context["baseline_document_policy"] = {
            "stage": "non_lending_enquiry",
            "allowed_document_categories": (
                ["merchant processing statements — last 3 months"]
                if intent == "merchant_services"
                else []
            ),
            "do_not_request_other_document_categories": True,
        }
        context["underwriting_focus"] = (
            "This borrower did not come for a loan. Assess fit for the product they "
            "asked about, then move toward booking a call. Do not run a lending "
            "screen, do not request an underwriting package, and do not state or "
            "imply a fundability verdict."
        )
        context["custom_instructions"] = (
            "Non-lending enquiry. For merchant services, ask only for the last three "
            "monthly processing statements and read the current effective rate and "
            "monthly volume off them. For a business-systems enquiry, ask for nothing "
            "at all — it is a qualification conversation. Booking a call is the goal "
            "state, not a review."
        )
        return context

    context["loan_program_fit"] = _loan_program_fit(intake) or None
    context["baseline_document_policy"] = {
        "stage": "stage_1_operating_business",
        "allowed_document_categories": [
            "last 6 months business bank statements",
            "last 2 years business tax returns (or the current-year extension filing)",
            "year-to-date P&L and balance sheet",
            "business debt schedule",
        ],
        "do_not_request_other_document_categories": True,
        "conditional_after_industry_known": [
            "merchant processing statements",
            "operating authority, IFTA filings and a fleet schedule (transportation only)",
            "equipment schedules and vendor quotes",
            "licences and permits for the trade",
            "lease or deed for the operating location",
            "accounts receivable aging",
        ],
        "max_new_conditional_documents_per_turn": 2,
        "stage_2_after_good_probability_only": [
            "personal financial statement for each 20%+ owner",
            "owner resume",
            "use-of-proceeds breakdown",
            "identification for each owner",
            "KYC/credit authorization",
        ],
    }
    context["underwriting_focus"] = (
        "Screen Stage 1 capacity for an ordinary operating business without asking the "
        "borrower to choose a loan product. Deposits and their consistency, filed tax "
        "years, and the debt schedule carry the screen. Time in business, industry, and "
        "whether the operating location is owned or leased are decisive and frequently "
        "missing — name them as the blocking gap rather than requesting more documents. "
        "Industry-specific documents are conditional and come only after the industry is "
        "known and the baseline is at least half satisfied."
    )
    context["custom_instructions"] = (
        "Public lead-magnet Stage 1 screener for operating businesses. Ask first for the "
        "four baseline items: six months of business bank statements, two years of business "
        "tax returns or the current-year extension, YTD P&L and balance sheet, and a "
        "business debt schedule. The borrower may not know which product fits. Return one "
        "of: Good probability - book call, Promising but needs one clarification, Not "
        "enough evidence yet, or Poor probability based on current file. Set "
        "booking_recommended true only for Good probability - book call. This is a "
        "preliminary screen, not a commitment to lend."
    )
    return context


def _context_fn_for(intake: PublicUnderwritingIntake):
    """Pick the AI context builder for an intake's variant.

    Replaces five separate `_funding_review_context if is_funding else
    _dealer_context` ternaries. Each of those silently defaulted anything that
    was not the real-estate variant to the DEALER context — so a Main Street
    file would have been screened with floorplan and MCA framing. Route through
    here so a new vertical cannot inherit dealer's context by omission.
    """
    if intake.variant == FUNDING_VARIANT:
        return _funding_review_context
    if intake.variant == MAIN_STREET_VARIANT:
        return _main_street_context
    if intake.variant == MCA_VARIANT:
        return _mca_context
    return _dealer_context


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
        "credit_pull": _credit_pull_state(intake) or None,
        "investor_name": intake.business_name,
        "target_property_address": basics.get("target_property_address"),
        "transaction_type": basics.get("transaction_type"),
        "estimated_value_or_purchase_price": basics.get("estimated_value_or_purchase_price"),
        "monthly_rent": basics.get("monthly_rent"),
        "funding_review_details": _funding_review_details(intake) or None,
        "dscr_potential": _compute_dscr_potential(intake),
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
            .order_by(BucketAIMessage.created_at.desc(), CHAT_TURN_ORDER.desc())
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
    # Email match is inherently fragile (same email can belong to more than one
    # client record). Harden the selection: among same-email candidates, prefer
    # one already originated from a dealer AI intake so a new intake reuses the
    # dealer-intake client instead of hijacking an unrelated agent-book client;
    # otherwise fall back to the most recent.
    candidates = (
        await db.execute(select(Client).where(Client.email == email).order_by(Client.created_at.desc()))
    ).scalars().all()
    client = next((c for c in candidates if c.source_channel == "dealer_ai_intake"), None) or (
        candidates[0] if candidates else None
    )
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
    # Non-destructively tag a reused client so it's identifiable as dealer-AI
    # sourced — only when empty, so an existing agent/referral attribution is
    # never overwritten.
    if not client.source_channel:
        client.source_channel = "dealer_ai_intake"
    if not client.referral_source:
        client.referral_source = "dealer_ai_intake"
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
                "last 2 years business tax returns (or current-year extension filing if not yet filed)",
                "current year/YTD P&L",
                "last 6 months main operating bank statements",
                "debt schedule",
                "personal financial statement for each owner",
                "identification for each owner",
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
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents).selectinload(BucketRequestedDocument.template_file),
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
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents).selectinload(BucketRequestedDocument.template_file),
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
    context_fn = _context_fn_for(intake)
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


def _requested_document_read(doc: BucketRequestedDocument) -> BucketRequestedDocumentRead:
    """Adds a signed download URL for an admin-uploaded blank-form template
    (e.g. a fillable PFS), independent of requires_signature — this is a plain
    "download the blank form" affordance, not part of the e-sign flow."""
    data = BucketRequestedDocumentRead.model_validate(doc)
    if doc.template_file_id and doc.template_file is not None:
        bucket, _prefix, _kms = _bucket_storage_config()
        filename = _safe_filename(doc.template_file.file_name)
        data.template_download_url = _s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": doc.template_file.s3_key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=900,
        )
    return data


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
        # Surfaced as explicit top-level keys (rather than left buried inside
        # intake.intake_state above) so the executive-summary/prequalification
        # prompt can reference eligible programs and program-specific facts by
        # name without having to reach into a raw JSONB dump. Dealer-only —
        # None for real-estate leads, same gate _prepend_program_fit_key_metric
        # already uses. This is an admin/AI-internal artifact (the executive
        # summary a human underwriter reads), not the borrower-facing chat —
        # the "never disclose a program name to the borrower" rule governs the
        # chat only, per this session's existing loan_program_fit convention.
        "program_fit": _loan_program_fit(intake) if intake.variant != FUNDING_VARIANT else None,
        "program_labels": PROGRAM_LABELS if intake.variant != FUNDING_VARIANT else None,
        "dealer_details": _dealer_details(intake) if intake.variant != FUNDING_VARIANT else None,
    }


_CREDIT_RE = re.compile(r"(\d{3})\s*\+?\s*(?:credit|fico)|(?:credit|fico)[^\d]{0,20}(\d{3})", re.IGNORECASE)


def _authoritative_facts_from_chat(
    chat_history: list[dict[str, str]], intake: PublicUnderwritingIntake
) -> dict[str, Any]:
    """Resolve facts the operator/borrower stated in chat that must override any
    stale figure in a prior review — currently the credit score. Scans newest-first
    and returns the most recent stated value so the summary/email never reports an
    outdated number.

    Priority: a real bureau soft pull (credit_pull_state) always wins — it's
    verified, not self-reported — followed by the most recent chat statement,
    then the plain intake-form estimate. Same priority order for dealer AND
    real-estate leads."""
    facts: dict[str, Any] = {}
    credit_state = _credit_pull_state(intake)
    if credit_state.get("fico") is not None:
        facts["credit_score"] = str(credit_state["fico"])
        facts["credit_score_source"] = "verified credit bureau soft pull"
        return facts
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


async def _credit_financials_section(db: AsyncSession, intake: PublicUnderwritingIntake) -> dict[str, Any] | None:
    """The lender-packet credit sub-dict: FICO + tier + a few key bullets from
    a completed bureau pull. None when no pull has run yet on this lead —
    same lookup for dealer AND real-estate leads."""
    credit_state = _credit_pull_state(intake)
    pull_id = credit_state.get("pull_id")
    if not pull_id:
        return None
    from app.models.credit_pull import CreditPull

    pull = await db.get(CreditPull, UUID(pull_id))
    if pull is None:
        return None
    result: dict[str, Any] = {
        "fico": pull.fico,
        "pulled_at": pull.pulled_at.isoformat() if pull.pulled_at else None,
        "expires_at": pull.expires_at.isoformat() if pull.expires_at else None,
        "bullets": [],
    }
    from app.routers.credit import _scraped_from_pull
    from app.services.credit_summary import summarize as summarize_credit

    scraped = _scraped_from_pull(pull)
    if scraped is not None:
        summary = summarize_credit(scraped)
        result["tier"] = summary.tier
        result["bullets"] = [b.label for b in summary.bullets if b.label][:4]
    return result


async def _collect_packet_financials(
    db: AsyncSession, intake: PublicUnderwritingIntake
) -> dict[str, Any]:
    """Pull the structured per-file facts the lender packet visualizes: month-over-month
    bank activity (last 6 months), 2-year tax-return figures, and (when a soft pull has
    run) a credit summary. Reads the durable per-file analysis cache (no new AI calls).
    Returns raw facts; the PDF renderer handles charting and redaction so this stays a
    thin data-loader."""
    credit = await _credit_financials_section(db, intake)
    # program_fit is a dealer-only signal — never computed/rendered for a
    # real-estate lead's packet.
    program_fit = _loan_program_fit(intake) if intake.variant != FUNDING_VARIANT else None
    active_ids = {file.id for file in _active_files(intake.bucket)}
    if not active_ids:
        return {"bank_months": [], "tax_years": [], "credit": credit, "program_fit": program_fit}
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
        "credit": credit,
        "program_fit": program_fit,
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
    variant_label = _variant_label(intake.variant)
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
            "everywhere and ignore any other credit number in the evidence or prior review. "
            "If context.program_fit is present (dealer leads only), use context.program_labels to name every "
            "program where program_fit[key].eligible is true, and weave the eligible programs and, where "
            "requested_loan_amount or the program's own sizing fields support it, an estimate of total addressable "
            "capital across those programs into 'recommended_approach' and/or the closing paragraph of "
            "'executive_summary' — this executive summary is an internal document read by a human underwriter, "
            "not the borrower-facing chat, so naming programs here is expected and required when eligible. Never "
            "state a specific interest rate unless context.program_fit itself already contains one (e.g. "
            "reinsurance_backed.rate_percent)."
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
    elif purpose == "prequalification":
        schema = {
            "title": "short title (borrower name + property/deal in a few words)",
            "prequalification_summary": "2-3 FLOWING PARAGRAPHS in underwriter prose stating the preliminary prequalification outcome, the program fit, and the reasoning — not bullet fragments.",
            "suggested_program": "the single best-fit product/program name",
            "alternate_programs": ["other product paths worth mentioning, if any"],
            "sizing": {
                "requested_amount": "scalar value e.g. $500,000",
                "estimated_value_or_purchase_price": "scalar value e.g. $750,000",
                "down_payment": "scalar value stated by the borrower, or 'Not provided'",
                "estimated_ltv": "percentage e.g. 66.7%",
                "credit_tier": "the verified/stated credit tier or FICO",
            },
            "key_strengths": ["short factor supporting a positive prequalification"],
            "conditions_or_watchpoints": ["short item the lender/underwriter would still confirm"],
            "next_step": "one clear next action for the borrower, e.g. book a call",
            "disclaimer": "This is a preliminary prequalification based on the information and evidence provided so far. It is not a commitment to lend and is subject to full underwriting, appraisal, and verification.",
        }
        instruction = (
            "Draft a borrower-facing preliminary real-estate investor/DSCR prequalification. Use ONLY the intake basics, "
            "the conversationally-gathered funding_review_details (down payment, prior property ownership, "
            "residential-vs-commercial intent), the verified credit pull, uploaded evidence, and chat history. Do not "
            "invent values — if something is unsupported, say so plainly rather than guessing. "
            "If context.authoritative_facts.credit_score is present, you MUST use that exact credit score value "
            "everywhere and ignore any other credit number in the evidence or prior review. "
            "Size the deal using the same 60%-75% LTV guidance already shown to the borrower on the intake form."
        )
        system = (
            "You are a senior real-estate investor / DSCR underwriter delivering a preliminary prequalification "
            "directly to the borrower in plain, confident, warm-but-professional prose — this is a moment the borrower "
            "should feel good about, while staying strictly accurate to the evidence on file. Never state a firm rate, "
            "a guaranteed approval, or a closing timeline. Always include the disclaimer verbatim as given in the "
            "schema. Return STRICT JSON only, matching the given shape exactly."
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


def _prepend_credit_key_metric(summary: dict[str, Any], intake: PublicUnderwritingIntake) -> None:
    """Injects a deterministic "Credit (verified)" row into summary["key_metrics"]
    from the real bureau pull — real data, not AI-guessed, so this bypasses the
    model entirely. Same behavior for dealer AND real-estate leads. Renders via
    both _format_executive_summary_markdown and the packet PDF's generic
    key_metrics passthrough with no further wiring. No-op when no pull has run."""
    credit_state = _credit_pull_state(intake)
    fico = credit_state.get("fico")
    if fico is None:
        return
    row = {"label": "Credit score (verified)", "value": str(fico), "note": "Bureau soft pull"}
    metrics = summary.get("key_metrics")
    if isinstance(metrics, list):
        metrics.insert(0, row)
    else:
        summary["key_metrics"] = [row]


def _prepend_program_fit_key_metric(summary: dict[str, Any], intake: PublicUnderwritingIntake) -> None:
    """Injects an "Eligible programs" row from the deterministic program-fit
    screen — dealer-only, real data (not AI-guessed). No-op for real-estate
    leads or when no program is eligible yet."""
    if intake.variant == FUNDING_VARIANT:
        return
    fit = _loan_program_fit(intake)
    if not fit:
        return
    eligible = [label for key, label in PROGRAM_LABELS.items() if (fit.get(key) or {}).get("eligible")]
    if not eligible:
        return
    row = {"label": "Eligible programs", "value": ", ".join(eligible), "note": "Deterministic screen — confirm with underwriter"}
    metrics = summary.get("key_metrics")
    if isinstance(metrics, list):
        metrics.insert(0, row)
    else:
        summary["key_metrics"] = [row]


async def _create_executive_summary_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
) -> PublicUnderwritingIntakeArtifact:
    summary = await _generate_management_json(db, intake, user, purpose="executive_summary")
    _prepend_credit_key_metric(summary, intake)
    _prepend_program_fit_key_metric(summary, intake)
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


def _format_prequalification_markdown(summary: dict[str, Any]) -> str:
    """Render the structured prequalification JSON into clean, human-readable
    markdown — same shape/approach as _format_executive_summary_markdown, kept
    separate since the sections differ (sizing, program fit, borrower-facing
    disclaimer) and this text is shown directly to the borrower, not just an
    internal operator."""

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

    body = _clean(summary.get("prequalification_summary"))
    if body:
        parts.append(body)

    program = _clean(summary.get("suggested_program"))
    if program:
        parts.append(f"## Suggested program\n{program}")
    alternates = _lines(summary.get("alternate_programs"))
    if alternates:
        parts.append("## Other paths worth considering\n" + "\n".join(f"- {item}" for item in alternates))

    sizing = summary.get("sizing")
    if isinstance(sizing, dict):
        rows = [f"- **{str(k).replace('_', ' ').title()}:** {_clean(v)}" for k, v in sizing.items() if _clean(v)]
        if rows:
            parts.append("## Sizing\n" + "\n".join(rows))

    for label, value in [
        ("Key strengths", summary.get("key_strengths")),
        ("Conditions / watchpoints", summary.get("conditions_or_watchpoints")),
    ]:
        items = _lines(value)
        if items:
            parts.append(f"## {label}\n" + "\n".join(f"- {item}" for item in items))

    next_step = _clean(summary.get("next_step"))
    if next_step:
        parts.append(f"## Next step\n{next_step}")
    disclaimer = _clean(summary.get("disclaimer"))
    if disclaimer:
        parts.append(f"_{disclaimer}_")
    return "\n\n".join(parts).strip()


async def _create_prequalification_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
) -> PublicUnderwritingIntakeArtifact:
    summary = await _generate_management_json(db, intake, user, purpose="prequalification")
    title = str(summary.get("title") or f"{intake.business_name or intake.full_name or 'Investor'} prequalification")[:240]
    body_text = _format_prequalification_markdown(summary)
    if not body_text:
        candidate = str(summary.get("prequalification_summary") or "").strip()
        if candidate.lstrip().startswith("{") or candidate.lstrip().startswith("```"):
            recovered = _repair_truncated_json(candidate) or {}
            candidate = str(recovered.get("prequalification_summary") or "").strip()
        body_text = candidate
    artifact = PublicUnderwritingIntakeArtifact(
        intake_id=intake.id,
        artifact_type="prequalification",
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
        "underwriting_prequalification_generated",
        user=user,
        actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=title,
    )
    return artifact


async def _ensure_prequalification_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
) -> PublicUnderwritingIntakeArtifact:
    existing = await _latest_artifact(db, intake.id, "prequalification")
    return existing or await _create_prequalification_artifact(db, intake, user)


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
    public_path: str = "/dealer-ai-underwriter",
    empty_message: str | None = None,
    include_management: bool = False,
    admin_thread: bool = False,
    thread_user: User | None = None,
    prequalification_widget: dict[str, Any] | None = None,
) -> DealerIntakeResponse:
    review = intake.latest_review if intake.latest_review else None
    latest_result = review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
    widget = prequalification_widget
    if widget is None and latest_result and latest_result.get("booking_recommended") is True and not _call_booked(intake):
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
            if thread_user is not None:
                # Each internal viewer (super admin, dealer partner) keeps a
                # PRIVATE thread with the AI on this lead — threads are never
                # shared between users. NULL-user rows are system welcomes,
                # visible to every internal viewer.
                filters.append(or_(BucketAIMessage.user_id == thread_user.id, BucketAIMessage.user_id.is_(None)))
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
                .order_by(BucketAIMessage.created_at.desc(), CHAT_TURN_ORDER.desc())
                .limit(50)
            )
        ).scalars().all()
        messages = list(reversed(recent))
    # Management artifacts + email sends are populated only for the super-admin
    # dealer-lead endpoint (include_management=True); every public/uploader/
    # funding caller gets empty lists.
    artifacts = await _management_artifacts(db, intake.id) if include_management else []
    email_sends = await _management_email_sends(db, intake.id) if include_management else []
    # Internal notes thread — admin/dealer-partner only, never the client.
    notes = (
        sorted(
            (n for n in intake.bucket.notes if n.visibility == "admin"),
            key=lambda n: n.created_at,
        )
        if admin_thread
        else []
    )
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
        requested_documents=[_requested_document_read(doc) for doc in intake.bucket.requested_documents],
        files=[BucketRequestUploadedFileRead.model_validate(file) for file in files],
        ai_summary=summary,
        latest_review=review_read,
        messages=[BucketAIMessageRead.model_validate(message) for message in (messages or [])],
        artifacts=[_artifact_read(artifact) for artifact in artifacts],
        email_sends=[_email_send_read(row) for row in email_sends],
        notes=[BucketNoteRead.model_validate(n) for n in notes],
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


async def _sign_requested_document(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerDocumentSignRequest,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
) -> BucketFile:
    """Generic e-sign fulfillment for a requires_signature BucketRequestedDocument.
    Shared by BOTH the dealer and funding-review public routers — identical
    mechanism for both verticals; only the requested document's template/text
    (admin-supplied) differs. Renders the certificate PDF, stores it as a
    normal BucketFile linked via requested_document_id (which alone satisfies
    the checklist via the existing upload-driven status recalculation)."""
    from app.services import document_signature as sig_service
    from app.services.payment_authorization import client_ip

    req = await db.get(BucketRequestedDocument, payload.requested_document_id)
    if req is None or req.bucket_id != intake.bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requested document not found")
    if not req.requires_signature:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This requested document does not require a signature")
    if req.status == "uploaded":
        raise HTTPException(status.HTTP_409_CONFLICT, "This document has already been signed")
    if not payload.esign_consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "E-SIGN consent is required")

    is_credit_auth = req.signature_kind == "credit_authorization"
    document_text = req.signature_document_text or (
        sig_service.credit_authorization_document_text() if is_credit_auth else ""
    )
    if not document_text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This requested document has no signable text configured")
    doc_version = (
        sig_service.CREDIT_AUTHORIZATION_DOCUMENT_VERSION if is_credit_auth else "custom-1"
    )
    doc_hash = sig_service.document_hash(document_text)

    applicant_data = None
    extra_rows: list[tuple[str, str]] = []
    if is_credit_auth:
        required = [
            payload.applicant_legal_first_name,
            payload.applicant_legal_last_name,
            payload.applicant_dob,
            payload.applicant_street,
            payload.applicant_city,
            payload.applicant_state,
            payload.applicant_zip,
        ]
        if any(v is None or not str(v).strip() for v in required):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "All applicant identity fields are required to sign this form")
        applicant_data = {
            "legal_first_name": payload.applicant_legal_first_name,
            "legal_last_name": payload.applicant_legal_last_name,
            "dob": payload.applicant_dob,
            "street": payload.applicant_street,
            "city": payload.applicant_city,
            "state": payload.applicant_state,
            "zip": payload.applicant_zip,
        }
        extra_rows = [
            ("Applicant name", f"{payload.applicant_legal_first_name} {payload.applicant_legal_last_name}"),
            ("Date of birth", payload.applicant_dob or ""),
            (
                "Address",
                ", ".join(
                    x for x in [payload.applicant_street, payload.applicant_city, payload.applicant_state, payload.applicant_zip] if x
                ),
            ),
        ]

    sig_bytes, sig_hash, sig_content_type = sig_service.decode_signature_data_url(payload.signature_data_url)
    if not sig_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A drawn signature is required")

    now = _now()
    signature = BucketDocumentSignature(
        requested_document_id=req.id,
        document_version=doc_version,
        document_hash=doc_hash,
        typed_name=payload.typed_name.strip(),
        esign_consent=True,
        applicant_data=applicant_data,
        ip_address=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:512],
        signed_at=now,
    )
    db.add(signature)
    await db.flush()

    _, prefix, kms_key_id = _bucket_storage_config()
    sig_ext = "png" if "png" in sig_content_type else "bin"
    sig_key = f"{prefix}/signatures/{intake.bucket_id}/{signature.id}/signature.{sig_ext}"
    _put_bucket_object(sig_key, sig_content_type, sig_bytes)
    signature.signature_s3_key = sig_key
    signature.signature_hash = sig_hash

    title = "Credit Report Authorization Certificate" if is_credit_auth else f"{req.name} — Signed Certificate"
    pdf_bytes = sig_service.render_signature_certificate_pdf(
        signature=signature, title=title, document_text=document_text, extra_rows=extra_rows
    )
    cert_key = f"{prefix}/signatures/{intake.bucket_id}/{signature.id}/certificate.pdf"
    _put_bucket_object(cert_key, "application/pdf", pdf_bytes)
    signature.certificate_s3_key = cert_key
    signature.certificate_hash = hashlib.sha256(pdf_bytes).hexdigest()

    result_file = BucketFile(
        bucket_id=intake.bucket_id,
        requested_document_id=req.id,
        upload_link_id=intake.bucket_upload_link_id,
        file_name=f"{req.name} - Signed Certificate.pdf"[:255],
        s3_key=cert_key,
        content_type="application/pdf",
        size_bytes=len(pdf_bytes),
        uploaded_by_name=actor_name,
        uploaded_by_email=actor_email,
        status="uploaded",
    )
    db.add(result_file)
    await db.flush()
    signature.result_file_id = result_file.id
    req.status = "uploaded"

    await _log(
        db,
        intake.bucket_id,
        "requested_document_signed",
        request=request,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role="public_lead",
        target_type="requested_document",
        target_id=str(req.id),
        detail=req.name,
    )
    await db.commit()
    await db.refresh(result_file)

    if actor_email:
        _send_signed_document_copy_email(
            to_email=actor_email,
            typed_name=actor_name,
            document_title=req.name,
            pdf_bytes=pdf_bytes,
        )

    return result_file


def _send_signed_document_copy_email(*, to_email: str, typed_name: str, document_title: str, pdf_bytes: bytes) -> None:
    """E-SIGN-compliant delivery of the signer's own copy for the requested-
    document/chat sign flow (credit authorization + the 3 client-facing
    contract types) — same pattern as app/routers/contracts.py's
    _send_signed_copy_email, kept separate since this path's certificate PDF
    is rendered by document_signature.py, not contract_templates.py."""
    from app.services.email.ses_client import send_raw_email

    subject = f"Signed: {document_title}"
    body_text = (
        f"Hello {typed_name},\n\n"
        f"Attached is your signed copy of {document_title}, executed electronically under the U.S. "
        "E-SIGN Act and UETA.\n\n"
        "You may request a paper copy of this signed document at any time, or withdraw your consent to "
        "electronic records prospectively, by contacting support@qualifiedcommercial.com.\n\n"
        "Qualified Commercial LLC"
    )
    send_raw_email(
        to_emails=[to_email],
        subject=subject,
        body_text=body_text,
        attachments=[(f"{document_title}.pdf"[:255], pdf_bytes, "application/pdf")],
    )


async def _store_drafted_form_pdf(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    req: BucketRequestedDocument,
    pdf_bytes: bytes,
    request: Request,
    *,
    file_label: str,
    classification: str,
    key_facts: dict[str, Any],
    actor_name: str,
    actor_email: str,
) -> BucketFile:
    """Store an on-screen-drafted PFS/debt-schedule PDF exactly like a real
    upload: a normal BucketFile linked via requested_document_id (which alone
    satisfies the checklist), SSE-KMS encrypted like every other bucket file.

    The BucketFileAnalysis row is written directly from the trusted structured
    input the borrower just typed — bypassing analyze_bucket_file/Bedrock
    entirely, since there is nothing for the AI to re-derive from a picture of
    numbers we ourselves just wrote into the PDF. This makes the drafted
    form's key_facts available to _compute_key_metrics_from_cache /
    extract_debt_schedule / extract_personal_financial_statements identically
    to a real AI-analyzed upload — those readers only look at `classification`
    and `key_facts`, never at provenance."""
    _, prefix, _ = _bucket_storage_config()
    file_id = uuid4()
    s3_key = f"{prefix}/drafted-forms/{intake.bucket_id}/{file_id}.pdf"
    _put_bucket_object(s3_key, "application/pdf", pdf_bytes)
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()

    result_file = BucketFile(
        id=file_id,
        bucket_id=intake.bucket_id,
        requested_document_id=req.id,
        upload_link_id=intake.bucket_upload_link_id,
        file_name=f"{file_label}.pdf"[:255],
        s3_key=s3_key,
        content_type="application/pdf",
        size_bytes=len(pdf_bytes),
        uploaded_by_name=actor_name,
        uploaded_by_email=actor_email,
        status="uploaded",
    )
    db.add(result_file)
    req.status = "uploaded"
    await db.flush()

    db.add(
        BucketFileAnalysis(
            bucket_file_id=result_file.id,
            bucket_id=intake.bucket_id,
            content_hash=content_hash,
            analysis_version=CURRENT_FILE_ANALYSIS_VERSION,
            provider="drafted_form",
            status="completed",
            classification=classification,
            confidence="high",
            summary=f"{file_label} submitted via the on-screen drafting form.",
            analysis={"key_facts": key_facts},
            analyzed_at=_now(),
        )
    )

    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_drafted_form_submitted",
        request=request,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role="public_lead",
        target_type="requested_document",
        target_id=str(req.id),
        detail=file_label,
    )
    await db.commit()
    await db.refresh(result_file)
    return result_file


async def _submit_pfs_form(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerPfsSubmission,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
) -> BucketFile:
    if not payload.acknowledgment:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You must acknowledge the disclaimer to submit this form")
    req = next((doc for doc in intake.bucket.requested_documents if doc.category == "Personal Financials"), None)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Personal financial statement is not requested on this intake")

    from app.services.dealer_forms_pdf import render_pfs_pdf

    key_facts = _pfs_key_facts(payload)
    pdf_bytes = render_pfs_pdf(
        owner_full_name=payload.owner_full_name,
        statement_date=payload.statement_date,
        assets=[(row.label, row.amount) for row in payload.assets],
        liabilities=[(row.label, row.amount) for row in payload.liabilities],
        total_assets=key_facts["total_assets"],
        total_liabilities=key_facts["total_liabilities"],
        net_worth=key_facts["net_worth"],
    )
    return await _store_drafted_form_pdf(
        db,
        intake,
        req,
        pdf_bytes,
        request,
        file_label=f"Personal Financial Statement — {payload.owner_full_name}",
        classification="personal_financial_statement",
        key_facts=key_facts,
        actor_name=actor_name,
        actor_email=actor_email,
    )


async def _submit_debt_schedule_form(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerDebtScheduleSubmission,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
) -> BucketFile:
    if not payload.acknowledgment:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You must acknowledge the disclaimer to submit this form")
    req = next((doc for doc in intake.bucket.requested_documents if doc.category == "Debts"), None)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Debt schedule is not requested on this intake")
    if req.status == "uploaded":
        raise HTTPException(status.HTTP_409_CONFLICT, "A debt schedule has already been submitted for this file")

    from app.services.dealer_forms_pdf import render_debt_schedule_pdf

    key_facts = _debt_schedule_key_facts(payload)
    pdf_bytes = render_debt_schedule_pdf(
        business_name=payload.business_name,
        debts=[(row.lender, row.balance, row.monthly_payment) for row in payload.debts],
        total_balance=key_facts["total_outstanding_balance"],
        total_monthly=key_facts["total_monthly_debt_service"],
    )
    return await _store_drafted_form_pdf(
        db,
        intake,
        req,
        pdf_bytes,
        request,
        file_label="Business Debt Schedule",
        classification="debt_schedule",
        key_facts=key_facts,
        actor_name=actor_name,
        actor_email=actor_email,
    )


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
        preferred_language=payload.preferred_language,
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
    email_record = await _record_resume_email(intake, token=token, request=request, reason="intake_created")
    await _record_super_admin_intake_notification(db, intake, request=request)
    await db.commit()
    intake = await _load_public_intake(db, token)
    email_note = _dealer_start_email_note(intake.preferred_language, ok=email_record.get("ok") is True)
    return await _response(
        db,
        intake,
        token=token,
        assistant_message=_dealer_welcome(intake.preferred_language, email_note=email_note),
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
        assistant_message=_dealer_welcome_back(intake.preferred_language),
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
        assistant_message=_dealer_welcome_back(intake.preferred_language),
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
        await _record_resume_email(intake, token=token, request=request, reason="resume_link_requested")
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
        outcome_status=intake.outcome_status,
        preferred_language=intake.preferred_language,
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
        delete_requested_at=intake.delete_requested_at,
        delete_requested_by=intake.delete_requested_by.name if intake.delete_requested_by else None,
    )


async def _load_admin_dealer_lead(db: AsyncSession, intake_id: UUID) -> PublicUnderwritingIntake:
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == intake_id)
            .options(
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents).selectinload(BucketRequestedDocument.template_file),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
                selectinload(PublicUnderwritingIntake.bucket_upload_link),
                selectinload(PublicUnderwritingIntake.latest_review),
                selectinload(PublicUnderwritingIntake.delete_requested_by),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if intake is None or intake.bucket.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI lead not found")
    return intake


async def _require_dealer_partner(user: CurrentUser, db: AsyncSession) -> None:
    if user.role != Role.DEALER_PARTNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dealer partner role required")
    # Hard-block every broker endpoint until BOTH the individual (Platform
    # Access Agreement) and their company (Referral Protection Agreement)
    # have a signed ContractAgreement on file — see app/routers/contracts.py
    # and app/services/contract_templates.py. This is the real enforcement
    # point; the frontend gate in AppShell.tsx is UX on top of it.
    from app.enums import ContractSubjectType, ContractType
    from app.models.contract_agreement import ContractAgreement

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
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You must sign the Platform Access Agreement before using the platform",
        )
    company_signed = None
    if user.referral_partner_company_id is not None:
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
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your company must have a signed Referral Protection Agreement on file before using the platform",
        )


async def _load_broker_dealer_lead(db: AsyncSession, user: User, intake_id: UUID) -> PublicUnderwritingIntake:
    """Same eager-load shape as _load_admin_dealer_lead, scoped to broker_id ==
    user.id so a partner can never reach another partner's (or the house's)
    lead by guessing an id. 404 (not 403) on ownership mismatch — matches this
    codebase's existing convention of not revealing whether an id exists to a
    caller who doesn't own it."""
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(
                PublicUnderwritingIntake.id == intake_id,
                PublicUnderwritingIntake.broker_id == user.id,
            )
            .options(
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents).selectinload(BucketRequestedDocument.template_file),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
                selectinload(PublicUnderwritingIntake.bucket_upload_link),
                selectinload(PublicUnderwritingIntake.latest_review),
                selectinload(PublicUnderwritingIntake.delete_requested_by),
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
    context_fn = _context_fn_for(intake)
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
        # Program-fit reads key_metrics, which only just became available on
        # the fresh review — recompute now that the real numbers exist.
        if not is_funding:
            _apply_loan_program_fit(intake)
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
    context_fn = _context_fn_for(intake)
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
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents).selectinload(BucketRequestedDocument.template_file),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
            selectinload(PublicUnderwritingIntake.bucket_upload_link),
            selectinload(PublicUnderwritingIntake.latest_review),
            selectinload(PublicUnderwritingIntake.delete_requested_by),
            with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
        )
        .order_by(PublicUnderwritingIntake.updated_at.desc())
    )
    if status_filter and status_filter != "all":
        stmt = stmt.where(PublicUnderwritingIntake.status == status_filter)
    if variant_filter and variant_filter != "all":
        if variant_filter == "dealer":
            stmt = stmt.where(PublicUnderwritingIntake.variant == DEALER_VARIANT)
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
    items = [_lead_row(row) for row in page]
    # NEW badges: unseen client/broker activity per lead for THIS admin,
    # against their per-lead seen cursors (default window: last 7 days for
    # leads never opened).
    from app.models.admin_activity import AdminActivitySeen
    from app.services.admin_activity import unseen_counts_by_bucket

    seen_rows = (
        await db.execute(
            select(AdminActivitySeen).where(
                AdminActivitySeen.user_id == user.id,
                AdminActivitySeen.intake_id.in_([row.id for row in page] or [UUID(int=0)]),
            )
        )
    ).scalars().all()
    seen_by_intake = {row.intake_id: row.seen_at for row in seen_rows if row.intake_id}
    counts = await unseen_counts_by_bucket(
        db,
        seen_by_intake=seen_by_intake,
        bucket_to_intake={row.bucket_id: row.id for row in page},
        default_since=_now() - timedelta(days=7),
    )
    channel_unread = await _channel_unread_by_intake(db, user=user, intakes=page)
    for item in items:
        item.unseen_activity_count = counts.get(item.bucket_id, 0)
        item.channel_unread_count = channel_unread.get(item.id, 0)
    return DealerAILeadListResponse(
        items=items,
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
    is_re = intake.variant == FUNDING_VARIANT
    if is_re:
        await _refresh_dscr_pricing(db)
    pdf_bytes = await asyncio.to_thread(
        render_dealer_intelligence_pdf,
        intake=intake,
        files=files,
        missing_docs=missing_docs,
        result=latest_result,
        # Internal-only sections — never passed on the public/borrower exports.
        program_fit=None if is_re else _compute_loan_program_fit(intake),
        dscr_potential=_compute_dscr_potential(intake) if is_re else None,
        credit=_credit_pull_state(intake) or None,
        internal=True,
    )
    filename = _safe_filename(f"dealer-intelligence-{intake.business_name or intake.full_name or 'review'}.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _mark_lead_seen(db: AsyncSession, user: User, intake_id: UUID) -> None:
    """Upsert this admin's per-lead seen cursor — opening a lead clears its
    NEW badge. Commits its own tiny write so read-only GETs stay side-effect
    safe for the caller."""
    from app.models.admin_activity import AdminActivitySeen

    row = (
        await db.execute(
            select(AdminActivitySeen).where(
                AdminActivitySeen.user_id == user.id,
                AdminActivitySeen.intake_id == intake_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(AdminActivitySeen(user_id=user.id, intake_id=intake_id, seen_at=_now()))
    else:
        row.seen_at = _now()
    await db.commit()


# ---------------------------------------------------------------------------
# Dealer-lead communication channel (the shared BucketNote(visibility="admin")
# thread between the internal team and the lead's dealer partner). Read-state
# and inbox live here; see models/dealer_lead_channel_seen.py.
# ---------------------------------------------------------------------------


class DealerLeadMessagePreview(BaseModel):
    content: str
    author_name: str | None = None
    author_role: str | None = None
    created_at: datetime


class DealerLeadInboxItem(BaseModel):
    intake_id: UUID
    name: str
    business_name: str | None = None
    full_name: str
    status: str
    outcome_status: str = "submitted"
    unread_count: int = 0
    last_message: DealerLeadMessagePreview | None = None
    last_message_at: datetime | None = None


class DealerLeadInboxResponse(BaseModel):
    items: list[DealerLeadInboxItem] = []
    total_unread: int = 0


class ChannelSeenResponse(BaseModel):
    intake_id: UUID
    seen_at: datetime


def _channel_side(role) -> str:
    """The two sides of a dealer-lead channel: the lead's partner vs the
    internal team. Unread = messages authored by the OTHER side. Accepts a Role
    enum or a raw role string (BucketNote.author_role stores the string)."""
    role_str = getattr(role, "value", role)
    return "partner" if role_str == Role.DEALER_PARTNER.value else "team"


def _channel_last_message(intake: PublicUnderwritingIntake) -> DealerLeadMessagePreview | None:
    notes = sorted(
        (n for n in intake.bucket.notes if n.visibility == "admin"),
        key=lambda n: n.created_at,
    )
    if not notes:
        return None
    last = notes[-1]
    return DealerLeadMessagePreview(
        content=last.content,
        author_name=last.author_name,
        author_role=last.author_role,
        created_at=last.created_at,
    )


async def _channel_unread_by_intake(
    db: AsyncSession,
    *,
    user: User,
    intakes: list[PublicUnderwritingIntake],
) -> dict[UUID, int]:
    """Per-lead count of channel messages from the OTHER side that `user` has
    not seen. Mirrors admin_activity.unseen_counts_by_bucket, but over the
    BucketNote(visibility='admin') thread and the dealer_lead_channel_seen
    cursor (which — unlike admin_activity_seen — also covers dealer partners).
    A lead never opened counts every other-side message as unread."""
    from app.models.dealer_lead_channel_seen import DealerLeadChannelSeen

    if not intakes:
        return {}
    viewer_side = _channel_side(user.role)
    bucket_to_intake = {i.bucket_id: i.id for i in intakes}

    seen_rows = (
        await db.execute(
            select(DealerLeadChannelSeen).where(
                DealerLeadChannelSeen.user_id == user.id,
                DealerLeadChannelSeen.intake_id.in_([i.id for i in intakes]),
            )
        )
    ).scalars().all()
    seen_by_intake = {r.intake_id: r.seen_at for r in seen_rows}

    note_rows = (
        await db.execute(
            select(BucketNote.bucket_id, BucketNote.author_role, BucketNote.created_at).where(
                BucketNote.bucket_id.in_(list(bucket_to_intake.keys())),
                BucketNote.visibility == "admin",
            )
        )
    ).all()
    counts: dict[UUID, int] = {}
    for bucket_id, author_role, created_at in note_rows:
        intake_id = bucket_to_intake.get(bucket_id)
        if intake_id is None or _channel_side(author_role) == viewer_side:
            continue  # your own side's messages are never "unread" to you
        seen_at = seen_by_intake.get(intake_id)
        if seen_at is None or created_at > seen_at:
            counts[intake_id] = counts.get(intake_id, 0) + 1
    return counts


async def _mark_channel_seen(db: AsyncSession, user: User, intake_id: UUID) -> datetime:
    """Upsert the caller's channel read cursor for one lead. Commits its own
    tiny write so it's safe to call from a GET."""
    from app.models.dealer_lead_channel_seen import DealerLeadChannelSeen

    row = (
        await db.execute(
            select(DealerLeadChannelSeen).where(
                DealerLeadChannelSeen.intake_id == intake_id,
                DealerLeadChannelSeen.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    now = _now()
    if row is None:
        db.add(DealerLeadChannelSeen(intake_id=intake_id, user_id=user.id, seen_at=now))
    else:
        row.seen_at = now
    await db.commit()
    return now


def _inbox_item(intake: PublicUnderwritingIntake, unread: int) -> DealerLeadInboxItem:
    return DealerLeadInboxItem(
        intake_id=intake.id,
        name=intake.business_name or intake.full_name,
        business_name=intake.business_name,
        full_name=intake.full_name,
        status=intake.status,
        outcome_status=intake.outcome_status,
        unread_count=unread,
        last_message=_channel_last_message(intake),
        last_message_at=intake.last_message_at,
    )


class WhatsNewItem(BaseModel):
    event_id: str
    intake_id: UUID | None = None
    lead_name: str | None = None
    variant: str | None = None
    action: str
    label: str
    actor_name: str | None = None
    actor_role: str | None = None
    detail: str | None = None
    created_at: datetime


class WhatsNewResponse(BaseModel):
    items: list[WhatsNewItem] = []
    unseen_count: int = 0
    feed_seen_at: datetime | None = None


@admin_router.get("/whats-new", response_model=WhatsNewResponse)
async def get_whats_new(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WhatsNewResponse:
    """Recent client/broker platform activity (uploads, messages, forms,
    bookings, new intakes) across all leads, with this admin's unseen count
    against their feed cursor."""
    _require_super_admin(user)
    from app.models.admin_activity import AdminActivitySeen
    from app.services.admin_activity import client_activity_rows

    since = _now() - timedelta(days=7)
    items = await client_activity_rows(db, since=since, limit=60)
    cursor = (
        await db.execute(
            select(AdminActivitySeen).where(
                AdminActivitySeen.user_id == user.id,
                AdminActivitySeen.intake_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    feed_seen_at = cursor.seen_at if cursor else None
    unseen = sum(1 for item in items if feed_seen_at is None or item["created_at"] > feed_seen_at)
    return WhatsNewResponse(
        items=[WhatsNewItem(**{key: value for key, value in item.items() if key != "bucket_id"}) for item in items],
        unseen_count=unseen,
        feed_seen_at=feed_seen_at,
    )


@admin_router.post("/whats-new/seen", response_model=WhatsNewResponse)
async def mark_whats_new_seen(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WhatsNewResponse:
    """Advance this admin's feed cursor to now (Mark all seen)."""
    _require_super_admin(user)
    from app.models.admin_activity import AdminActivitySeen

    cursor = (
        await db.execute(
            select(AdminActivitySeen).where(
                AdminActivitySeen.user_id == user.id,
                AdminActivitySeen.intake_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if cursor is None:
        db.add(AdminActivitySeen(user_id=user.id, intake_id=None, seen_at=_now()))
    else:
        cursor.seen_at = _now()
    await db.commit()
    return await get_whats_new(user, db)


class DealerPartnerOption(BaseModel):
    id: UUID
    name: str
    email: str


class AssignPartnerRequest(BaseModel):
    broker_user_id: UUID | None = None


async def _load_dealer_partner_user(db: AsyncSession, user_id: UUID) -> User:
    partner = await db.get(User, user_id)
    if partner is None or partner.role != Role.DEALER_PARTNER or partner.deleted_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected user is not an active dealer partner.")
    return partner


@admin_router.get("/dealer-partners", response_model=list[DealerPartnerOption])
async def list_dealer_partners(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[DealerPartnerOption]:
    """Dealer partners the team can assign a file to / start a conversation with.
    Registered before GET /{intake_id} so the literal path isn't captured."""
    _require_super_admin(user)
    rows = (
        await db.execute(
            select(User)
            .where(User.role == Role.DEALER_PARTNER, User.deleted_at.is_(None))
            .order_by(User.name)
        )
    ).scalars().all()
    return [DealerPartnerOption(id=r.id, name=r.name or r.email, email=r.email) for r in rows]


@admin_router.patch("/{intake_id}/assign-partner", response_model=DealerIntakeResponse)
async def assign_lead_partner(
    intake_id: UUID,
    payload: AssignPartnerRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Assign (or clear) the dealer partner on a file so the team's messages on
    it reach that partner's channel. Setting None detaches the partner."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    if payload.broker_user_id is None:
        intake.broker_id = None
    else:
        partner = await _load_dealer_partner_user(db, payload.broker_user_id)
        intake.broker_id = partner.id
    await _log(
        db, intake.bucket_id, "dealer_ai_lead_partner_assigned", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id),
        detail=str(payload.broker_user_id) if payload.broker_user_id else "detached",
    )
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake.id)
    return await _response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)


@admin_router.get("/messages", response_model=DealerLeadInboxResponse)
async def admin_dealer_channel_inbox(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerLeadInboxResponse:
    """Internal team's dealer-lead message inbox across every lead. Registered
    before GET /{intake_id} so the literal path isn't captured as an intake_id."""
    _require_super_admin(user)
    stmt = (
        select(PublicUnderwritingIntake)
        .join(Bucket, PublicUnderwritingIntake.bucket_id == Bucket.id)
        .where(Bucket.archived_at.is_(None))
        .options(selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes))
    )
    intakes = list((await db.execute(stmt)).scalars().unique().all())
    return await _build_channel_inbox(db, user, intakes)


@admin_router.get("/{intake_id}", response_model=DealerIntakeResponse)
async def get_dealer_ai_lead(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    await _mark_lead_seen(db, user, intake.id)
    return await _response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)


@admin_router.post("/{intake_id}/request-deletion", response_model=DealerIntakeResponse)
async def admin_request_lead_deletion(
    intake_id: UUID,
    payload: RequestLeadDeletionRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Admin flags a lead for deletion — same flag broker-side "request
    deletion" sets, needed here for admin-created/self-serve leads that have
    no broker to defer to. Destroys nothing; only gates the separate
    confirm-deletion endpoint open."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    intake.delete_requested_at = _now()
    intake.delete_requested_by_user_id = user.id
    await _log(
        db, intake.bucket_id, "dealer_ai_lead_deletion_requested_by_admin", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id), detail=payload.reason,
    )
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake.id)
    return await _response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)


@admin_router.post("/{intake_id}/cancel-deletion-request", response_model=DealerIntakeResponse)
async def admin_cancel_lead_deletion(
    intake_id: UUID,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Admin retracts a pending deletion request (their own, or a broker's)
    without destroying anything — fully reversible."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    intake.delete_requested_at = None
    intake.delete_requested_by_user_id = None
    await _log(
        db, intake.bucket_id, "dealer_ai_lead_deletion_request_cancelled_by_admin", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id),
    )
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake.id)
    return await _response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)


@admin_router.post("/{intake_id}/confirm-deletion", status_code=status.HTTP_204_NO_CONTENT)
async def admin_confirm_lead_deletion(
    intake_id: UUID,
    payload: ConfirmLeadDeletionRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Irreversible hard delete — only reachable on a lead a broker/admin has
    already flagged via request-deletion (409 otherwise), and only with the
    lead's exact name typed as a deliberate speed bump (400 on mismatch).

    Deletes every BucketFile/artifact object from S3 (best-effort — a failed
    object delete is logged but never blocks the DB delete, matching
    _delete_s3_object's own internal exception-swallowing), then deletes the
    Bucket row. public_underwriting_intakes.bucket_id is itself
    ondelete="CASCADE" FROM buckets (the intake is the dependent side), so
    deleting the bucket cascades to the intake and every other
    bucket-scoped table (requested_documents, files, notes, activity_logs,
    ai_reviews, ai_messages, upload_links, shares, vendor_access,
    public_shares, file_analyses) in one statement — see
    app/models/bucket.py's Bucket relationships, all already
    cascade="all, delete-orphan" to match.

    Never touches client_id/broker_id (already ON DELETE SET NULL on the
    intake) or CreditPull rows (keyed to client_id, not intake_id) — a
    deleted lead never takes its client or bureau history down with it.

    The bucket's own BucketActivityLog is about to be destroyed by
    definition, so this action is logged to the application logger instead
    of _log(...) — there is no durable admin-wide audit trail in this
    codebase to write a cross-bucket entry to."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    # One-click super-admin delete: no prior request flag and no typed-name
    # required — the frontend danger dialog is the safeguard. (Brokers still
    # can only request; only a super admin reaches this hard-delete.)
    confirm_target = (intake.business_name or intake.full_name or "").strip().lower()

    for file in intake.bucket.files:
        status_result = _delete_s3_object(file.s3_key)
        if status_result != "deleted":
            log.warning("hard-delete: S3 object delete failed for BucketFile id=%s key=%s", file.id, file.s3_key)
    artifacts = await _management_artifacts(db, intake.id)
    for artifact in artifacts:
        if artifact.s3_key:
            status_result = _delete_s3_object(artifact.s3_key)
            if status_result != "deleted":
                log.warning("hard-delete: S3 object delete failed for artifact id=%s key=%s", artifact.id, artifact.s3_key)

    log.warning(
        "hard-delete: super_admin=%s (%s) permanently deleted dealer AI lead id=%s bucket_id=%s email=%s name=%s requested_by=%s at=%s ip=%s",
        user.id, user.email, intake.id, intake.bucket_id, intake.email, confirm_target,
        intake.delete_requested_by_user_id, intake.delete_requested_at, _client_ip(request),
    )
    await db.delete(intake.bucket)
    await db.commit()


@admin_router.get("/{intake_id}/notes", response_model=list[BucketNoteRead])
async def list_admin_lead_notes(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[BucketNote]:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    return sorted((n for n in intake.bucket.notes if n.visibility == "admin"), key=lambda n: n.created_at)


@admin_router.post("/{intake_id}/notes", response_model=BucketNoteRead, status_code=status.HTTP_201_CREATED)
async def create_admin_lead_note(
    intake_id: UUID,
    payload: DealerLeadNoteCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketNote:
    """Internal note on the shared admin <-> dealer-partner thread for this
    lead. visibility="admin" keeps it invisible to the client and to any
    bucket vendor/share viewer — confirmed no other surface reads visibility
    == "admin" notes."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    note = BucketNote(
        bucket_id=intake.bucket_id,
        author_name=user.name,
        author_role=user.role,
        visibility="admin",
        content=payload.content,
    )
    db.add(note)
    intake.last_message_at = _now()
    await db.flush()
    await _log(db, intake.bucket_id, "dealer_ai_lead_note_created", request=request, user=user, target_type="note", target_id=str(note.id))
    await db.commit()
    await db.refresh(note)
    return note


@admin_router.patch("/{intake_id}/outcome-status", response_model=DealerIntakeResponse)
async def update_lead_outcome_status(
    intake_id: UUID,
    payload: OutcomeStatusUpdate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Admin-only: sets the firm's loan decision on this lead (submitted /
    closed / denied). No broker-accessible write path exists to this field —
    the loan outcome is the firm's call, not the referring partner's."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    intake.outcome_status = payload.outcome_status
    await _log(
        db, intake.bucket_id, "dealer_ai_lead_outcome_status_changed", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id), detail=payload.outcome_status,
    )
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake.id)
    return await _response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)


@admin_router.patch("/{intake_id}/language", response_model=DealerIntakeResponse)
async def update_lead_language(
    intake_id: UUID,
    payload: LanguageUpdate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Admin-only: corrects the client's language preference after the fact.
    Mirrors update_lead_outcome_status — once set (by the client's own pick,
    or an admin/broker's pick at lead-creation time), only an admin can
    change it. No broker-accessible write path exists to this field."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    intake.preferred_language = payload.preferred_language
    await _log(
        db, intake.bucket_id, "dealer_ai_lead_language_changed", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id), detail=payload.preferred_language,
    )
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake.id)
    return await _response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)


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
    if payload.variant not in _ADMIN_VARIANT_CONSTANTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"variant must be one of {', '.join(sorted(_ADMIN_VARIANT_CONSTANTS))}",
        )
    is_re = payload.variant == "real_estate"
    variant_const = _ADMIN_VARIANT_CONSTANTS[payload.variant]

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
        preferred_language=payload.preferred_language,
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
    if payload.broker_user_id is not None:
        partner = await _load_dealer_partner_user(db, payload.broker_user_id)
        intake.broker_id = partner.id
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
    welcome_text = _admin_created_welcome(variant_const)
    db.add(_persist_admin_welcome_message(bucket.id, welcome_text))
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake.id)

    email_note = ""
    if payload.notify_client:
        if is_re:
            record = await _record_resume_email(
                intake,
                token=token,
                request=request,
                reason="admin_created",
                public_path=FUNDING_PUBLIC_PATH,
                review_label="real estate funding review",
                room_label="real estate funding review file",
                db=db,
                sender_user_id=user.id,
            )
        else:
            record = await _record_resume_email(
                intake, token=token, request=request, reason="admin_created", db=db, sender_user_id=user.id,
            )
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
        thread_user=user,
        assistant_message=welcome_text + email_note,
    )


# ── Dealer-partner (broker) endpoints ───────────────────────────────────
# Curated subset for Role.DEALER_PARTNER: create/view/chat/upload/run-review
# on their OWN leads only (broker_id == user.id). No credit-pull, program-fit,
# vendor-email, exports, client-thread, or drive-ingest routes exist here —
# those stay admin-only by design (see plan doc for the full rationale).


@broker_router.get("", response_model=DealerAILeadListResponse)
async def list_broker_dealer_leads(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    status_filter: str | None = None,
    probability_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DealerAILeadListResponse:
    await _require_dealer_partner(user, db)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    stmt = (
        select(PublicUnderwritingIntake)
        .join(Bucket, PublicUnderwritingIntake.bucket_id == Bucket.id)
        .where(
            Bucket.archived_at.is_(None),
            PublicUnderwritingIntake.broker_id == user.id,
            # A broker's own deletion request hides the lead from their own
            # board immediately — the lead stays fully intact and visible to
            # admin (with a pending badge) until admin separately confirms.
            PublicUnderwritingIntake.delete_requested_at.is_(None),
        )
        .options(
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents).selectinload(BucketRequestedDocument.template_file),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
            selectinload(PublicUnderwritingIntake.bucket_upload_link),
            selectinload(PublicUnderwritingIntake.latest_review),
            selectinload(PublicUnderwritingIntake.delete_requested_by),
            with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
        )
        .order_by(PublicUnderwritingIntake.updated_at.desc())
    )
    if status_filter and status_filter != "all":
        stmt = stmt.where(PublicUnderwritingIntake.status == status_filter)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(PublicUnderwritingIntake.full_name).like(needle)
            | func.lower(PublicUnderwritingIntake.email).like(needle)
            | func.lower(PublicUnderwritingIntake.business_name).like(needle)
        )
    rows = list((await db.execute(stmt)).scalars().unique().all())
    if probability_status and probability_status != "all":
        rows = [row for row in rows if str(_lead_result(row).get("probability_status") or "") == probability_status]
    total = len(rows)
    page = rows[offset:offset + limit]
    items = [_lead_row(row) for row in page]
    channel_unread = await _channel_unread_by_intake(db, user=user, intakes=page)
    for item in items:
        item.channel_unread_count = channel_unread.get(item.id, 0)
    return DealerAILeadListResponse(items=items, total=total, limit=limit, offset=offset)


@broker_router.get("/messages", response_model=DealerLeadInboxResponse)
async def broker_dealer_channel_inbox(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerLeadInboxResponse:
    """Dealer partner's whole communication inbox: one row per owned lead that
    has a team<->partner thread, newest activity first, with this partner's
    unread count. Each row clicks through to that lead's Messages tab in the UI."""
    await _require_dealer_partner(user, db)
    stmt = (
        select(PublicUnderwritingIntake)
        .join(Bucket, PublicUnderwritingIntake.bucket_id == Bucket.id)
        .where(
            Bucket.archived_at.is_(None),
            PublicUnderwritingIntake.broker_id == user.id,
            PublicUnderwritingIntake.delete_requested_at.is_(None),
        )
        .options(selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes))
    )
    intakes = list((await db.execute(stmt)).scalars().unique().all())
    return await _build_channel_inbox(db, user, intakes)


async def _build_channel_inbox(
    db: AsyncSession, user: User, intakes: list[PublicUnderwritingIntake]
) -> DealerLeadInboxResponse:
    # Only leads that actually have a team<->partner thread belong in the inbox.
    threaded = [i for i in intakes if any(n.visibility == "admin" for n in i.bucket.notes)]
    unread = await _channel_unread_by_intake(db, user=user, intakes=threaded)
    items = [_inbox_item(i, unread.get(i.id, 0)) for i in threaded]
    items.sort(key=lambda it: it.last_message_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return DealerLeadInboxResponse(items=items, total_unread=sum(it.unread_count for it in items))


@broker_router.post("/{intake_id}/messages/seen", response_model=ChannelSeenResponse)
async def broker_mark_channel_seen(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChannelSeenResponse:
    """Dealer partner opened the Messages tab on this lead — clear its unread."""
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    seen_at = await _mark_channel_seen(db, user, intake.id)
    return ChannelSeenResponse(intake_id=intake.id, seen_at=seen_at)


@admin_router.post("/{intake_id}/messages/seen", response_model=ChannelSeenResponse)
async def admin_mark_channel_seen(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChannelSeenResponse:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    seen_at = await _mark_channel_seen(db, user, intake.id)
    return ChannelSeenResponse(intake_id=intake.id, seen_at=seen_at)


@broker_router.post("", response_model=DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def create_broker_ai_lead(
    payload: BrokerLeadCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Dealer partner creates an AI-underwriter lead ON BEHALF of their own
    client. Dealer-variant only. Reuses the same creation helpers as the
    public /start and admin-create flows. No terms/throttle."""
    await _require_dealer_partner(user, db)

    if not payload.force_new:
        existing = await _latest_active_intake_by_email(db, str(payload.email), variant=DEALER_VARIANT)
        if existing is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"message": "An active lead already exists for this email.", "intake_id": str(existing.id)},
            )

    provenance = {
        "created_by_broker": {
            "user_id": str(user.id),
            "name": user.name,
            "email": user.email,
            "at": _now().isoformat(),
        },
        "on_behalf_of_client": True,
    }

    adapter = DealerIntakeStart(
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        business_name=payload.business_name,
    )
    client = await _find_or_create_client(db, adapter)
    bucket, link = await _create_bucket_for_intake(db, client, adapter, request)

    if isinstance(client.lead_intake, dict):
        client.lead_intake = {**client.lead_intake, "created_by_broker": str(user.id)}

    token = _new_public_token()
    intake_state: dict[str, Any] = {
        "messages": [],
        "source": "dealer_ai_intake",
        "broker_provenance": provenance,
    }

    intake = PublicUnderwritingIntake(
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        broker_id=user.id,
        token_hash=_hash_token(token),
        variant=DEALER_VARIANT,
        full_name=payload.full_name.strip(),
        email=client.email or _normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        preferred_language=payload.preferred_language,
        asset_rows=[],
        intake_state=intake_state,
    )
    db.add(intake)
    await _log(
        db,
        bucket.id,
        "dealer_ai_lead_created_by_broker",
        request=request,
        user=user,
        actor_role="dealer_partner",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Broker {user.email} created dealer lead for {intake.email}",
    )
    welcome_text = _admin_created_welcome(DEALER_VARIANT)
    db.add(_persist_admin_welcome_message(bucket.id, welcome_text))
    await db.commit()
    intake = await _load_broker_dealer_lead(db, user, intake.id)

    email_note = ""
    if payload.notify_client:
        record = await _record_resume_email(
            intake, token=token, request=request, reason="broker_created", db=db, sender_user_id=user.id,
        )
        await db.commit()
        intake = await _load_broker_dealer_lead(db, user, intake.id)
        email_note = (
            " A secure login link was emailed to the client."
            if record.get("ok")
            else " Email delivery is unavailable; share the resume link manually."
        )

    return await _response(
        db,
        intake,
        token=token,
        public_path="/dealer-ai-underwriter",
        include_management=False,
        admin_thread=True,
        thread_user=user,
        assistant_message=welcome_text + email_note,
    )


@broker_router.get("/{intake_id}", response_model=DealerIntakeResponse)
async def get_broker_dealer_lead(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return await _response(db, intake, token=None, include_management=False, admin_thread=True, thread_user=user)


@broker_router.post("/{intake_id}/request-deletion", response_model=DealerIntakeResponse)
async def broker_request_lead_deletion(
    intake_id: UUID,
    payload: RequestLeadDeletionRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Flags the broker's own lead for deletion. Destroys nothing — only
    hides it from THIS broker's own list (list_broker_dealer_leads filters
    delete_requested_at IS NULL); admin still sees the full lead, with a
    pending-deletion badge, until admin separately confirms."""
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    intake.delete_requested_at = _now()
    intake.delete_requested_by_user_id = user.id
    await _log(
        db, intake.bucket_id, "dealer_ai_lead_deletion_requested_by_broker", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id), detail=payload.reason,
    )
    await db.commit()
    intake = await _load_broker_dealer_lead(db, user, intake.id)
    return await _response(db, intake, token=None, include_management=False, admin_thread=True, thread_user=user)


@broker_router.post("/{intake_id}/cancel-deletion-request", response_model=DealerIntakeResponse)
async def broker_cancel_lead_deletion(
    intake_id: UUID,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Lets a broker undo their own accidental deletion request before admin
    acts on it — fully reversible, no confirmation needed."""
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    intake.delete_requested_at = None
    intake.delete_requested_by_user_id = None
    await _log(
        db, intake.bucket_id, "dealer_ai_lead_deletion_request_cancelled_by_broker", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id),
    )
    await db.commit()
    intake = await _load_broker_dealer_lead(db, user, intake.id)
    return await _response(db, intake, token=None, include_management=False, admin_thread=True, thread_user=user)


@broker_router.get("/{intake_id}/notes", response_model=list[BucketNoteRead])
async def list_broker_lead_notes(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[BucketNote]:
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return sorted((n for n in intake.bucket.notes if n.visibility == "admin"), key=lambda n: n.created_at)


@broker_router.post("/{intake_id}/notes", response_model=BucketNoteRead, status_code=status.HTTP_201_CREATED)
async def create_broker_lead_note(
    intake_id: UUID,
    payload: DealerLeadNoteCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketNote:
    """Same shared thread as create_admin_lead_note — both routers write/read
    the identical BucketNote rows (visibility='admin'), which is what makes
    this a shared admin<->broker thread with no separate join table."""
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    note = BucketNote(
        bucket_id=intake.bucket_id,
        author_name=user.name,
        author_role=user.role,
        visibility="admin",
        content=payload.content,
    )
    db.add(note)
    intake.last_message_at = _now()
    await db.flush()
    await _log(db, intake.bucket_id, "dealer_ai_lead_note_created", request=request, user=user, target_type="note", target_id=str(note.id))
    await db.commit()
    await db.refresh(note)
    return note


@broker_router.post("/{intake_id}/chat", response_model=DealerIntakeResponse)
async def broker_dealer_lead_chat(
    intake_id: UUID,
    payload: DealerChatRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Dealer-partner chat with the underwriting AI on their own lead, in the
    same private internal thread (audience='admin') the admin cockpit uses —
    the client never sees this thread either way."""
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    assistant_message = None
    if payload.message and payload.message.strip():
        if intake.variant == FUNDING_VARIANT:
            await _refresh_dscr_pricing(db)
        context_fn = _context_fn_for(intake)
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **context_fn(intake)}
        chat_messages, _, _ = await create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="admin",
            message=payload.message.strip(),
            actor_name=user.name or "Dealer partner",
            user=user,
        )
        if chat_messages:
            assistant_message = chat_messages[-1].content
            await _merge_thread_borrower_facts(db, intake, chat_messages[-1])
        _record_chat_fact(intake, payload.message, source="broker_chat")
    await db.commit()
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return await _response(
        db,
        intake,
        token=None,
        include_management=False,
        assistant_message=assistant_message,
        admin_thread=True,
        thread_user=user,
    )


@broker_router.post("/{intake_id}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def broker_dealer_lead_upload_init(
    intake_id: UUID,
    payload: DealerFileUploadInit,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return await _start_upload(db, intake, payload, request, actor_name=user.name or "Dealer partner", actor_email=user.email)


@broker_router.post("/{intake_id}/files/complete", response_model=BucketFileRead)
async def broker_dealer_lead_upload_complete(
    intake_id: UUID,
    payload: DealerUploadComplete,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return await _complete_upload(db, intake, payload, request, actor_name=user.name or "Dealer partner", actor_email=user.email)


@broker_router.post("/{intake_id}/requested-documents/pfs", response_model=BucketFileRead)
async def broker_submit_lead_pfs(
    intake_id: UUID,
    payload: DealerPfsSubmission,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    """Dealer partner fills out the on-screen PFS on behalf of their client —
    same fallback available to the client themselves, usable here so a
    broker can close out the checklist without waiting on the client."""
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return await _submit_pfs_form(db, intake, payload, request, actor_name=user.name or "Dealer partner", actor_email=user.email)


@broker_router.post("/{intake_id}/requested-documents/debt-schedule", response_model=BucketFileRead)
async def broker_submit_lead_debt_schedule(
    intake_id: UUID,
    payload: DealerDebtScheduleSubmission,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return await _submit_debt_schedule_form(db, intake, payload, request, actor_name=user.name or "Dealer partner", actor_email=user.email)


@broker_router.post("/{intake_id}/run-review", response_model=ReviewRunStartResponse)
async def rerun_broker_dealer_lead_review(
    intake_id: UUID,
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ReviewRunStartResponse:
    """Same async run-review + polling pattern as the admin endpoint, scoped
    to the broker's own lead."""
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    _throttle_or_429(
        _ADMIN_REVIEW_LAST_BY_INTAKE,
        str(intake.id),
        _ADMIN_REVIEW_MIN_INTERVAL_SECONDS,
        "A review was just re-run for this lead. Please wait a moment before running another.",
    )
    review = await _create_queued_review(
        db,
        intake,
        request=request,
        actor_name=user.name or "Dealer partner",
        actor_email=user.email,
        actor_role="dealer_partner",
        log_event="dealer_ai_review_rerun_by_broker",
        detail="Broker re-run over latest uploads",
        requested_by_user_id=user.id,
    )
    background.add_task(_run_review_background, review.id, intake.id)
    return ReviewRunStartResponse(review_id=review.id, status="queued")


@broker_router.get("/{intake_id}/review-progress", response_model=ReviewProgressResponse)
async def broker_dealer_lead_review_progress(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    review_id: UUID | None = None,
) -> ReviewProgressResponse:
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
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


@broker_router.post("/{intake_id}/request-pfs", response_model=BucketRequestedDocumentRead)
async def broker_request_lead_pfs(
    intake_id: UUID,
    payload: RequestPfsOrDebtScheduleRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketRequestedDocument:
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return await _request_pfs(db, intake, payload.owner_name)


@broker_router.post("/{intake_id}/request-debt-schedule", response_model=BucketRequestedDocumentRead)
async def broker_request_lead_debt_schedule(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketRequestedDocument:
    await _require_dealer_partner(user, db)
    intake = await _load_broker_dealer_lead(db, user, intake_id)
    return await _request_debt_schedule(db, intake)


@admin_router.post("/from-bucket/{bucket_id}", response_model=DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_ai_lead_from_bucket(
    bucket_id: UUID,
    payload: AdminLeadFromBucketCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """Convert an EXISTING Bucket (already collecting files some other way) into an
    AI-underwriter lead, so the admin can audit those files with the AI review and
    build a lender package — without a second, empty bucket being created and
    without re-uploading anything. Reuses the same client find-or-create + intake
    construction as create_admin_ai_lead, but skips _create_bucket_for_intake /
    _create_bucket_for_funding_review entirely: the bucket, its BucketFile rows,
    and any BucketRequestedDocument checklist it already has stay exactly as-is.
    File association is automatic (_active_files just filters bucket.files)."""
    _require_super_admin(user)
    if payload.variant not in _ADMIN_VARIANT_CONSTANTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"variant must be one of {', '.join(sorted(_ADMIN_VARIANT_CONSTANTS))}",
        )
    is_re = payload.variant == "real_estate"
    variant_const = _ADMIN_VARIANT_CONSTANTS[payload.variant]

    bucket = (
        await db.execute(
            select(Bucket)
            .where(Bucket.id == bucket_id)
            .options(selectinload(Bucket.upload_links))
        )
    ).scalar_one_or_none()
    if bucket is None or bucket.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket not found")

    # Duplicate policy: unless force_new, surface the existing lead for this bucket
    # rather than creating a second intake on top of the same files.
    if not payload.force_new:
        existing = (
            await db.execute(
                select(PublicUnderwritingIntake).where(PublicUnderwritingIntake.bucket_id == bucket_id)
            )
        ).scalars().first()
        if existing is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "message": "An AI-underwriter lead already exists for this bucket.",
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
        "converted_from_bucket_id": str(bucket.id),
    }

    if is_re:
        adapter = FundingReviewStart(
            full_name=payload.full_name, email=payload.email, phone=payload.phone,
            investor_name=payload.business_name,
        )
        client = await _find_or_create_funding_client(db, adapter)
    else:
        adapter = DealerIntakeStart(
            full_name=payload.full_name, email=payload.email, phone=payload.phone,
            business_name=payload.business_name,
        )
        client = await _find_or_create_client(db, adapter)

    if isinstance(client.lead_intake, dict):
        client.lead_intake = {**client.lead_intake, "created_by_admin": str(user.id)}

    # Reuse the bucket's existing active upload link (if any) rather than minting a
    # new one — bucket_upload_link_id is nullable, so a bucket with no client-facing
    # link at all (e.g. a purely admin-populated audit bucket) is fine too.
    link = next((l for l in bucket.upload_links if l.status == "active"), None)

    token = _new_public_token()
    intake = PublicUnderwritingIntake(
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id if link else None,
        token_hash=_hash_token(token),
        variant=variant_const,
        full_name=payload.full_name.strip(),
        email=client.email or _normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        preferred_language=payload.preferred_language,
        intake_state={
            "messages": [],
            "source": "bucket_conversion",
            "admin_provenance": provenance,
        },
    )
    db.add(intake)
    await db.flush()

    # Warm the per-file analysis cache cheaply (placeholder inserts, no model calls)
    # so the scheduler drain starts analyzing before the admin even opens the lead.
    from app.services.bucket_ai import enqueue_file_analysis

    for file in _active_files(bucket):
        await enqueue_file_analysis(db, file)

    await _log(
        db,
        bucket.id,
        "ai_lead_created_from_bucket",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Admin converted bucket '{bucket.name}' into a {payload.variant} lead for {intake.email}",
    )
    await db.commit()
    intake = await _load_admin_dealer_lead(db, intake.id)

    email_note = ""
    if payload.notify_client:
        if is_re:
            record = await _record_resume_email(
                intake, token=token, request=request, reason="admin_created",
                public_path=FUNDING_PUBLIC_PATH, review_label="real estate funding review",
                room_label="real estate funding review file", db=db, sender_user_id=user.id,
            )
        else:
            record = await _record_resume_email(
                intake, token=token, request=request, reason="admin_created", db=db, sender_user_id=user.id,
            )
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
        thread_user=user,
        assistant_message=(
            "Lead created from the existing bucket — its files are already attached. "
            "Run the AI review when ready." + email_note
        ),
    )


_CREDIT_AUTH_DOC_NAME = "Credit Report Authorization"

# The 3 client-facing contract types deliverable through this existing
# requested-document/chat sign flow (see app/routers/contracts.py's module
# docstring — PLATFORM_ACCESS and REFERRAL_PROTECTION are NOT routed here).
_CONTRACT_SIGNATURE_KIND: dict[ContractType, str] = {
    ContractType.SBA_ENGAGEMENT: "contract_sba_engagement",
    ContractType.CLIENT_ENGAGEMENT: "contract_client_engagement",
    ContractType.CONSULTING_ADDENDUM: "contract_consulting_addendum",
}


def _credit_authorization_doc(intake: PublicUnderwritingIntake) -> BucketRequestedDocument | None:
    """The lead's credit_authorization requested-document, if one has been
    requested — same lookup for dealer AND real-estate leads."""
    for doc in intake.bucket.requested_documents:
        if doc.signature_kind == "credit_authorization":
            return doc
    return None


def _contract_doc(intake: PublicUnderwritingIntake, contract_type: ContractType) -> BucketRequestedDocument | None:
    signature_kind = _CONTRACT_SIGNATURE_KIND[contract_type]
    for doc in intake.bucket.requested_documents:
        if doc.signature_kind == signature_kind:
            return doc
    return None


def _contract_field_values_from_lead(intake: PublicUnderwritingIntake, contract_type: ContractType) -> dict[str, str]:
    """Auto-fill the handful of identity fields every client-facing contract
    template shares (client legal name) from the lead record, so the admin
    only has to type what isn't already known. Every other in-scope field
    (notice contacts, fee amounts) is left for the admin's explicit
    field_values to override — see render_contract_document()'s own
    per-field default fallback for anything neither supplies."""
    values: dict[str, str] = {}
    name = (intake.business_name or intake.full_name or "").strip()
    if name:
        for field_name in ("client_legal_name",):
            values[field_name] = name
    return values


@admin_router.post("/{intake_id}/credit-authorization", response_model=BucketRequestedDocumentRead)
async def request_lead_credit_authorization(
    intake_id: UUID,
    payload: AdminCreditAuthorizationRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketRequestedDocument:
    """Admin requests the client sign a credit-authorization form on this
    lead — identical code path for dealer and real-estate leads. Idempotent:
    if one is already requested on this bucket, returns it (updating the
    template/text in place if the client hasn't signed yet — once signed,
    the request is immutable so an in-flight signature can't be silently
    invalidated by a wording change) rather than creating a duplicate
    checklist item."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)

    if payload.template_file_id is not None:
        template_file = await db.get(BucketFile, payload.template_file_id)
        if template_file is None or template_file.bucket_id != intake.bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Template file does not belong to this lead's bucket")

    existing = _credit_authorization_doc(intake)
    if existing is not None:
        if existing.status != "uploaded" and (payload.template_file_id is not None or payload.document_text is not None):
            existing.template_file_id = payload.template_file_id
            existing.signature_document_text = payload.document_text
            await db.commit()
            await db.refresh(existing)
        return existing

    doc = BucketRequestedDocument(
        bucket_id=intake.bucket_id,
        name=_CREDIT_AUTH_DOC_NAME,
        category="compliance",
        description="Sign to authorize a soft credit inquiry for this file.",
        required=True,
        is_custom=True,
        requires_signature=True,
        signature_kind="credit_authorization",
        template_file_id=payload.template_file_id,
        signature_document_text=payload.document_text,
    )
    db.add(doc)
    await _log(
        db,
        intake.bucket_id,
        "credit_authorization_requested",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Admin requested credit authorization for {intake.email}",
    )
    await db.commit()
    await db.refresh(doc)
    return doc


async def _request_pfs(db: AsyncSession, intake: PublicUnderwritingIntake, owner_name: str | None) -> BucketRequestedDocument:
    """Shared by admin+broker request-pfs endpoints. Idempotent via
    _ensure_requested_document — requesting the baseline PFS again (no
    owner_name) returns the existing row rather than duplicating; a distinct
    owner_name creates a separate per-owner PFS request, same naming
    convention as the per-owner ID documents in _apply_dealer_detail_documents."""
    name = f"Personal financial statement — {owner_name}" if owner_name else "Personal financial statement"
    description = (
        f"Upload a completed personal financial statement (PFS) for {owner_name}. Use the blank form below if you need one."
        if owner_name
        else "Upload a completed personal financial statement (PFS) for each owner. Use the blank form below if you need one."
    )
    doc = await _ensure_requested_document(db, intake.bucket, name=name, category="Personal Financials", description=description, allow_multiple_files=True)
    await db.commit()
    return doc


async def _request_debt_schedule(db: AsyncSession, intake: PublicUnderwritingIntake) -> BucketRequestedDocument:
    """Shared by admin+broker request-debt-schedule endpoints. Idempotent via
    _ensure_requested_document — a lead only ever has one debt schedule, so
    there is no owner_name variant here (unlike PFS)."""
    doc = await _ensure_requested_document(
        db,
        intake.bucket,
        name="Debt schedule",
        category="Debts",
        description="Upload a schedule of all outstanding business debt: lender, balance, and monthly payment for each.",
    )
    await db.commit()
    return doc


@admin_router.post("/{intake_id}/request-pfs", response_model=BucketRequestedDocumentRead)
async def admin_request_lead_pfs(
    intake_id: UUID,
    payload: RequestPfsOrDebtScheduleRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketRequestedDocument:
    """Admin requests a PFS on this lead — either the baseline PFS (already
    auto-created for most leads) or a second/later owner's PFS. Unlike
    credit-authorization, there is no signature/template step: this just
    ensures the requested-document exists so the client can upload a real
    file or use the on-screen fill-in-online form. Dealer leads only —
    PFS/debt-schedule are not a real-estate concept on this platform."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    _require_dealer_intake(intake)
    return await _request_pfs(db, intake, payload.owner_name)


@admin_router.post("/{intake_id}/request-debt-schedule", response_model=BucketRequestedDocumentRead)
async def admin_request_lead_debt_schedule(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketRequestedDocument:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    _require_dealer_intake(intake)
    return await _request_debt_schedule(db, intake)


_CONTRACT_DOC_NAME: dict[ContractType, str] = {
    ContractType.SBA_ENGAGEMENT: "SBA Advisory and Packaging Engagement Agreement",
    ContractType.CLIENT_ENGAGEMENT: "Capital Advisory and Placement Engagement Agreement",
    ContractType.CONSULTING_ADDENDUM: "Consulting and Fee Schedule Addendum",
}


@admin_router.post("/{intake_id}/contracts", response_model=BucketRequestedDocumentRead)
async def request_lead_contract(
    intake_id: UUID,
    payload: AdminContractRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketRequestedDocument:
    """Admin requests one of the 3 client-facing contract types be signed on
    this lead, via the existing requested-document/chat sign flow — see
    app/routers/contracts.py's module docstring for why PLATFORM_ACCESS and
    REFERRAL_PROTECTION are NOT routed here. Idempotent per contract_type,
    same immutable-once-signed semantics as credit-authorization."""
    _require_super_admin(user)
    if payload.contract_type not in _CONTRACT_SIGNATURE_KIND:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This contract type is not requestable on a lead")
    intake = await _load_admin_dealer_lead(db, intake_id)

    from app.services import contract_templates as tpl

    field_values = {**_contract_field_values_from_lead(intake, payload.contract_type), **payload.field_values}
    rendered = tpl.render_contract_document(payload.contract_type, field_values)
    document_text = rendered.plain_text

    existing = _contract_doc(intake, payload.contract_type)
    if existing is not None:
        if existing.status != "uploaded":
            existing.signature_document_text = document_text
            await db.commit()
            await db.refresh(existing)
        return existing

    doc = BucketRequestedDocument(
        bucket_id=intake.bucket_id,
        name=_CONTRACT_DOC_NAME[payload.contract_type],
        category="compliance",
        description="Sign to execute this engagement agreement.",
        required=True,
        is_custom=True,
        requires_signature=True,
        signature_kind=_CONTRACT_SIGNATURE_KIND[payload.contract_type],
        signature_document_text=document_text,
    )
    db.add(doc)
    await _log(
        db,
        intake.bucket_id,
        "contract_requested",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Admin requested {payload.contract_type.value} for {intake.email}",
    )
    await db.commit()
    await db.refresh(doc)
    return doc


@admin_router.get("/{intake_id}/contracts", response_model=list[AdminContractRequestStatus])
async def get_lead_contract_status(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[AdminContractRequestStatus]:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    results: list[AdminContractRequestStatus] = []
    for contract_type in _CONTRACT_SIGNATURE_KIND:
        doc = _contract_doc(intake, contract_type)
        results.append(
            AdminContractRequestStatus(
                contract_type=contract_type,
                requested=doc is not None,
                signed=bool(doc and doc.status == "uploaded"),
                requested_document_id=doc.id if doc else None,
            )
        )
    return results


@admin_router.get("/{intake_id}/credit-status", response_model=LeadCreditStatusResponse)
async def get_lead_credit_status(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LeadCreditStatusResponse:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    doc = _credit_authorization_doc(intake)
    if doc is None:
        return LeadCreditStatusResponse(authorization_requested=False, authorization_signed=False)

    credit_state = _credit_pull_state(intake)
    return LeadCreditStatusResponse(
        authorization_requested=True,
        authorization_signed=doc.status == "uploaded",
        requested_document_id=doc.id,
        pull_id=UUID(credit_state["pull_id"]) if credit_state and credit_state.get("pull_id") else None,
        fico=credit_state.get("fico") if credit_state else None,
        pulled_at=datetime.fromisoformat(credit_state["pulled_at"]) if credit_state and credit_state.get("pulled_at") else None,
        expires_at=datetime.fromisoformat(credit_state["expires_at"]) if credit_state and credit_state.get("expires_at") else None,
    )


@admin_router.get("/{intake_id}/program-fit", response_model=LeadProgramFitResponse)
async def get_lead_program_fit(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LeadProgramFitResponse:
    """Admin-only read of the deterministic program-fit screen (all 14
    programs in PROGRAM_LABELS) — dealer leads only, recomputed live from the
    current key_metrics/dealer_details rather than relying on the possibly-
    stale intake_state snapshot, so the admin always sees the latest figures
    without needing a chat turn or re-run first."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    if intake.variant == FUNDING_VARIANT:
        return LeadProgramFitResponse(computed=False)
    fit = _compute_loan_program_fit(intake)
    return LeadProgramFitResponse(computed=True, programs=fit)


class LeadDscrPotentialResponse(BaseModel):
    """Admin-only read of the deterministic DSCR-potential screen for a
    real-estate lead — same live-recompute pattern as LeadProgramFitResponse
    so the admin always sees figures derived from the current facts."""
    computed: bool
    potential: dict[str, Any] = Field(default_factory=dict)


@admin_router.get("/{intake_id}/dscr-potential", response_model=LeadDscrPotentialResponse)
async def get_lead_dscr_potential(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LeadDscrPotentialResponse:
    """Deterministic DSCR/LTV/max-loan math for real-estate leads only —
    dealer leads have the program-fit screen instead."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    if intake.variant != FUNDING_VARIANT:
        return LeadDscrPotentialResponse(computed=False)
    await _refresh_dscr_pricing(db)
    potential = _compute_dscr_potential(intake)
    return LeadDscrPotentialResponse(computed=bool(potential.get("computed")), potential=potential)


@admin_router.post("/{intake_id}/credit-pull", response_model=LeadCreditStatusResponse)
async def run_lead_credit_pull(
    intake_id: UUID,
    payload: AdminCreditPullRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LeadCreditStatusResponse:
    """Admin runs a soft credit pull on a lead's client, gated on the client
    having SIGNED the credit-authorization requested-document first. Identical
    path for dealer and real-estate leads — the pull is keyed to
    intake.client_id, which every lead has. Deliberately skips the Stripe
    payment-authorization gate the borrower self-serve endpoint requires
    (POST /credit/pull) — this is an admin underwriting-audit action, and
    consent here is the signed authorization document itself."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    if intake.client_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This lead has no linked client")

    doc = _credit_authorization_doc(intake)
    if doc is None or doc.status != "uploaded":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The client must sign the credit authorization before a pull can run.",
        )

    signature = (
        await db.execute(
            select(BucketDocumentSignature)
            .where(BucketDocumentSignature.requested_document_id == doc.id)
            .order_by(BucketDocumentSignature.created_at.desc())
        )
    ).scalars().first()
    applicant_data = signature.applicant_data if signature else None
    if not applicant_data:
        raise HTTPException(status.HTTP_409_CONFLICT, "No applicant identity data on file for this signature")

    client = await db.get(Client, intake.client_id)
    if client is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Linked client not found")

    from datetime import date as _date

    from app.services import credit_pull_core

    try:
        dob = _date.fromisoformat(str(applicant_data.get("dob")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Applicant date of birth on file is invalid") from exc

    try:
        pull = await credit_pull_core.run_soft_pull(
            db,
            client=client,
            applicant=credit_pull_core.SoftPullApplicant(
                legal_first_name=str(applicant_data.get("legal_first_name") or ""),
                legal_last_name=str(applicant_data.get("legal_last_name") or ""),
                dob=dob,
                street=str(applicant_data.get("street") or ""),
                city=str(applicant_data.get("city") or ""),
                state=str(applicant_data.get("state") or ""),
                zip=str(applicant_data.get("zip") or ""),
                ssn=payload.ssn,
            ),
            actor=user,
        )
    except credit_pull_core.SoftPullDenied as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": exc.code, "message": exc.message}) from exc
    except credit_pull_core.SoftPullValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except credit_pull_core.SoftPullRateLimited as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except credit_pull_core.SoftPullUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    # Compact cross-reference on the intake — the AI context functions and
    # package PDF read this instead of re-querying CreditPull, and it's the
    # ONLY place credit data touches PublicUnderwritingIntake (never added to
    # _CLIENT_SAFE_INTAKE_STATE_KEYS — bureau data stays admin/AI-only).
    state = _intake_state(intake)
    state["credit_pull"] = {
        "pull_id": str(pull.id),
        "fico": pull.fico,
        "pulled_at": pull.pulled_at.isoformat() if pull.pulled_at else None,
        "expires_at": pull.expires_at.isoformat() if pull.expires_at else None,
    }
    intake.intake_state = state
    await db.commit()

    return LeadCreditStatusResponse(
        authorization_requested=True,
        authorization_signed=True,
        requested_document_id=doc.id,
        pull_id=pull.id,
        fico=pull.fico,
        pulled_at=pull.pulled_at,
        expires_at=pull.expires_at,
    )


@admin_router.post("/{intake_id}/prepare-banker-submission", response_model=PrepareBankerSubmissionResponse)
async def prepare_banker_submission(
    intake_id: UUID,
    payload: PrepareBankerSubmissionRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrepareBankerSubmissionResponse:
    """Assembles the normalized JSON payload an admin would hand to the
    banker's intake system -- shared borrower/entity fields, computed
    key_metrics/program-fit, and (if supplied) transient sensitive
    identifiers. Stateless: nothing in this request or response is
    persisted -- no DB write, no intake_state mutation, no logging of the
    identifiers. The real outbound POST to the banker's own API is future
    work pending that integration's spec; this returns the assembled
    payload for admin review/download today."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    if intake.variant == FUNDING_VARIANT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Banker submission payloads are dealer leads only")
    from app.services.banker_submission import build_banker_payload

    built = build_banker_payload(
        intake,
        key_metrics=_key_metrics(intake),
        program_fit=_compute_loan_program_fit(intake),
        entity_structure=_entity_structure(intake),
        owners=_dealer_details(intake).get("owners") or [],
        sensitive_identifiers=payload.identifiers.model_dump(),
    )
    return PrepareBankerSubmissionResponse(payload=built)


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
        # Refresh the product AI context first so the internal thread reasons
        # over current deterministic figures (dscr_potential, program fit)
        # rather than the snapshot from the last client-side sync.
        if intake.variant == FUNDING_VARIANT:
            await _refresh_dscr_pricing(db)
        context_fn = _context_fn_for(intake)
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **context_fn(intake)}
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
            await _merge_thread_borrower_facts(db, intake, chat_messages[-1])
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
        thread_user=user,
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
            .order_by(BucketAIMessage.created_at.desc(), CHAT_TURN_ORDER.desc())
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
        preferred_language=intake.preferred_language,
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


@admin_router.post("/{intake_id}/requested-documents/pfs", response_model=BucketFileRead)
async def admin_submit_lead_pfs(
    intake_id: UUID,
    payload: DealerPfsSubmission,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    """Admin fills out the on-screen PFS on behalf of the client — same
    fallback the client sees on the public/logged-in pages, usable here so an
    admin can close out the checklist without waiting on the client."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    return await _submit_pfs_form(db, intake, payload, request, actor_name=user.name or "Super admin", actor_email=user.email)


@admin_router.post("/{intake_id}/requested-documents/debt-schedule", response_model=BucketFileRead)
async def admin_submit_lead_debt_schedule(
    intake_id: UUID,
    payload: DealerDebtScheduleSubmission,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    return await _submit_debt_schedule_form(db, intake, payload, request, actor_name=user.name or "Super admin", actor_email=user.email)


@admin_router.post("/{intake_id}/files/ingest-from-drive", response_model=DriveIngestResponse)
async def dealer_ai_lead_ingest_from_drive(
    intake_id: UUID,
    payload: DriveIngestRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DriveIngestResponse:
    """Ingest operator-selected Google Drive files into the lead's bucket so the
    AI learns from them.

    Each Drive file is downloaded via the operator's own OAuth grant (drive.file
    scope — only picker-/app-granted files are visible), stored into the bucket's
    S3 prefix as a BucketFile(status='uploaded'), and queued for per-file AI
    analysis (enqueue_file_analysis) exactly like a normal upload — so the next
    review composes from a warm cache. Best-effort per file: an unavailable /
    oversized / duplicate file is skipped and reported, never aborting the batch."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)

    from app.services.bucket_ai import enqueue_file_analysis
    from app.services.google import google_oauth_client
    from app.services.google.drive_client import download_file_bytes

    _INGEST_MAX_BYTES = 25 * 1024 * 1024  # per-file cap for AI ingest
    _, prefix, _ = _bucket_storage_config()
    actor_name = user.name or "Super admin"
    actor_email = user.email

    items: list[DriveIngestItemResult] = []
    ingested = 0
    skipped = 0
    # De-dupe against ids already ingested in this request.
    seen_ids: set[str] = set()
    # S3 keys written this request — used to best-effort clean up orphans if the
    # batch fails before/at commit (S3 puts are not transactional with the DB).
    put_keys: list[str] = []
    log = logging.getLogger(__name__)

    def _cleanup_put_objects() -> None:
        bucket, _p, _k = _bucket_storage_config()
        for key in put_keys:
            try:
                _s3_client().delete_object(Bucket=bucket, Key=key)
            except Exception:  # noqa: BLE001
                log.warning("drive ingest: orphan cleanup failed key=%s", key)

    try:
        for raw_id in payload.drive_file_ids:
            file_id = (raw_id or "").strip()
            if not file_id or file_id in seen_ids:
                continue
            seen_ids.add(file_id)
            try:
                got = await download_file_bytes(db, user.id, file_id, max_bytes=_INGEST_MAX_BYTES)
            except (
                google_oauth_client.GoogleNotConnected,
                google_oauth_client.GoogleScopeMissing,
                google_oauth_client.GoogleTokenRevoked,
            ) as exc:
                # A credential failure mid-batch is terminal for the remaining
                # files, but files already stored this request are kept: commit
                # them (below), and if NONE were ingested yet, surface a 409 so
                # the operator knows to reconnect. This never orphans S3 — every
                # put so far has a matching row about to be committed.
                if ingested == 0:
                    _cleanup_put_objects()
                    if isinstance(exc, google_oauth_client.GoogleScopeMissing):
                        raise HTTPException(status.HTTP_409_CONFLICT, "Reconnect Google with Drive access to import files.") from exc
                    raise HTTPException(status.HTTP_409_CONFLICT, "Connect your Google account (Settings → Connections) before importing from Drive.") from exc
                items.append(DriveIngestItemResult(drive_file_id=file_id, status="skipped", reason="google_disconnected"))
                skipped += 1
                break
            if got is None:
                items.append(DriveIngestItemResult(drive_file_id=file_id, status="skipped", reason="unavailable_or_too_large"))
                skipped += 1
                continue
            fname, data, ctype = got
            content_type = _sanitize_upload_content_type(ctype)
            safe = _safe_filename(fname)
            # Content-level idempotency: hash the downloaded bytes and skip if a
            # byte-identical active file is already in the bucket. Using the hash
            # (not name+size) avoids both a false "already there" on a coincidental
            # name+size collision AND the multiple-rows crash a name+size lookup
            # could hit. .first() (not one_or_none) is safe against dupes.
            content_hash = hashlib.sha256(data).hexdigest()
            existing = (
                await db.execute(
                    select(BucketFile.id).where(
                        BucketFile.bucket_id == intake.bucket_id,
                        BucketFile.content_hash == content_hash,
                        BucketFile.deleted_at.is_(None),
                    ).limit(1)
                )
            ).first()
            if existing is not None:
                items.append(DriveIngestItemResult(drive_file_id=file_id, file_name=fname, status="skipped", reason="already_in_bucket"))
                skipped += 1
                continue

            new_id = uuid4()
            s3_key = f"{prefix}/uploads/{intake.bucket_id}/{new_id}-drive-{safe}"
            try:
                _put_bucket_object(s3_key, content_type, data)
            except Exception:  # noqa: BLE001
                log.exception("drive ingest: S3 put failed intake=%s file=%s", intake_id, file_id)
                items.append(DriveIngestItemResult(drive_file_id=file_id, file_name=fname, status="skipped", reason="storage_error"))
                skipped += 1
                continue
            put_keys.append(s3_key)
            bucket_file = BucketFile(
                id=new_id,
                bucket_id=intake.bucket_id,
                requested_document_id=None,
                upload_link_id=intake.bucket_upload_link_id,
                file_name=fname,
                s3_key=s3_key,
                content_type=content_type,
                size_bytes=len(data),
                content_hash=content_hash,
                uploaded_by_name=actor_name,
                uploaded_by_email=actor_email,
                status="uploaded",
            )
            db.add(bucket_file)
            await db.flush()
            # Queue per-file analysis so the review composes from a warm cache.
            try:
                await enqueue_file_analysis(db, bucket_file)
            except Exception:  # noqa: BLE001
                log.exception("drive ingest: enqueue analysis failed file=%s", bucket_file.id)
            items.append(DriveIngestItemResult(drive_file_id=file_id, file_name=fname, status="ingested"))
            ingested += 1

        await _log(
            db,
            intake.bucket_id,
            "underwriting_drive_files_ingested",
            request=request,
            actor_name=actor_name,
            actor_email=actor_email,
            actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
            target_type="bucket",
            target_id=str(intake.bucket_id),
            detail=f"Ingested {ingested} Drive file(s), skipped {skipped}",
        )
        await db.commit()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — any failure after S3 writes: roll back + GC orphans.
        await db.rollback()
        _cleanup_put_objects()
        log.exception("drive ingest: batch failed intake=%s — rolled back + cleaned %d object(s)", intake_id, len(put_keys))
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Drive import failed; no files were imported.")
    return DriveIngestResponse(ingested=ingested, skipped=skipped, items=items)


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


@admin_router.post("/{intake_id}/prequalification", response_model=PublicUnderwritingArtifactRead)
async def create_dealer_ai_prequalification(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PublicUnderwritingArtifactRead:
    """Admin manual draft/redraft of the borrower prequalification — always
    creates a fresh artifact (unlike the chat auto-trigger's idempotent
    get-or-create), since an admin clicking this expects a regenerate.
    Real-estate only: the schema/instructions are DSCR-specific."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    if intake.variant != FUNDING_VARIANT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Prequalification is only available for real estate leads")
    artifact = await _create_prequalification_artifact(db, intake, user)
    await db.commit()
    artifact = await _latest_artifact(db, intake_id, "prequalification") or artifact
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


async def build_package_zip_bytes(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
) -> tuple[str, bytes]:
    """Build the full shipping package ZIP as raw bytes: every uploaded document,
    the lender-packet PDF, the executive summary (markdown), a ready-to-edit
    vendor email template, and a README manifest.

    Pure builder — returns (filename, bytes) with NO Response and NO DB commit, so
    both the download route and the vendor-email send path can attach the same ZIP.
    It DOES ensure the summary/packet artifacts + regenerate the email template
    (side-effects the caller is expected to commit)."""
    summary_artifact = await _ensure_executive_summary_artifact(db, intake, user)
    packet_artifact = await _ensure_lender_packet_artifact(db, intake, user)
    email_draft = await _generate_management_json(
        db,
        intake,
        user,
        purpose="vendor_email",
        extra={"executive_summary": summary_artifact.body_json, "lender_packet_title": packet_artifact.title},
    )

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
                log.exception("package.zip: lender packet fetch failed intake=%s", intake.id)
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

    return f"{label}-package.zip", buf.getvalue()


@admin_router.get("/{intake_id}/package.zip")
async def download_dealer_ai_package_zip(
    intake_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Bundle the full shipping package as a single ZIP — see build_package_zip_bytes."""
    _require_super_admin(user)
    intake = await _load_admin_dealer_lead(db, intake_id)
    filename, payload = await build_package_zip_bytes(db, intake, user)
    files = _active_files(intake.bucket)
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
    # The lender packet is attached when explicitly requested (attach_lender_packet)
    # or, for backward-compat, via the legacy include_lender_packet flag.
    want_packet = payload.attach_lender_packet if payload.attach_lender_packet is not None else payload.include_lender_packet
    packet_artifact = await _ensure_lender_packet_artifact(db, intake, user) if want_packet else None
    cc_emails = [str(email).lower().strip() for email in payload.cc_emails if str(email).strip()]
    sends: list[PublicUnderwritingIntakeEmailSend] = []
    access_ids: list[UUID] = []
    _MAX_ATTACH = 8 * 1024 * 1024  # per-file cap
    # Aggregate RAW cap for the whole message. Base64 inflates attachments ~1.37x,
    # so 15 MiB raw ≈ ~20.5 MB encoded — a real buffer under Gmail's 25MB ceiling
    # once MIME headers, the HTML body, and multipart boundaries are added. A stack
    # of sub-8MB files can't build one oversized message that the provider rejects
    # for EVERY recipient. Files that would push past the total are noted, not attached.
    _MAX_TOTAL_ATTACH = 15 * 1024 * 1024
    attachments: list[tuple[str, bytes, str]] = []
    attachment_note = ""
    total_attach_bytes = 0

    def _try_attach(name: str, data: bytes, ctype: str, *, too_big_note: str) -> None:
        """Append an attachment if it fits both the per-file and running aggregate
        caps; otherwise record a note (never silently drop, never overflow)."""
        nonlocal total_attach_bytes, attachment_note
        if len(data) > _MAX_ATTACH or total_attach_bytes + len(data) > _MAX_TOTAL_ATTACH:
            attachment_note += too_big_note
            return
        attachments.append((name, data, ctype))
        total_attach_bytes += len(data)

    # Lender packet PDF.
    if packet_artifact and packet_artifact.s3_key:
        try:
            _try_attach(
                f"{_safe_filename(packet_artifact.title)}.pdf",
                await _s3_bytes(packet_artifact.s3_key),
                "application/pdf",
                too_big_note="\n\nThe underwriting packet is available through the secure vendor bucket because the PDF is too large for email.",
            )
        except Exception as exc:  # noqa: BLE001
            attachment_note += f"\n\nThe underwriting packet is available through the secure vendor bucket. Attachment fallback reason: {exc}"
    # Executive summary (markdown → .txt).
    if payload.attach_executive_summary and summary_artifact.body_text:
        _try_attach(
            f"{_safe_filename(summary_artifact.title or 'executive-summary')}.txt",
            summary_artifact.body_text.encode("utf-8"),
            "text/plain; charset=utf-8",
            too_big_note="\n\nThe executive summary is available through the secure vendor bucket (too large to attach).",
        )
    # Full shipping package ZIP (built on the fly).
    if payload.attach_package_zip:
        try:
            zip_name, zip_bytes = await build_package_zip_bytes(db, intake, user)
            _try_attach(
                zip_name,
                zip_bytes,
                "application/zip",
                too_big_note="\n\nThe full package ZIP is too large to email — use the secure bucket access below or the Download ZIP button.",
            )
        except Exception:  # noqa: BLE001
            log.exception("vendor-email: package zip build failed intake=%s", intake_id)
            attachment_note += "\n\nThe full package ZIP could not be attached; use the secure bucket access below."
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
            _try_attach(
                fname, data, ctype,
                too_big_note=f"\n\n'{fname}' was not attached to keep the email under the size limit; it's in the secure vendor bucket.",
            )

    # Bucket access blurb. "passcode" creates ONE no-login share for the bucket and
    # embeds the link + one-time passcode in every recipient's body (same access for
    # all). "login" keeps the per-recipient Clerk vendor-login link. "none" omits it.
    passcode_blurb = ""
    passcode_blurb_redacted = ""  # persisted variant with the one-time code masked
    if payload.bucket_access == "passcode":
        share_passcode = _generate_passcode()
        first_recipient = str(payload.to_emails[0]) if payload.to_emails else "Vendor"
        share = BucketShare(
            bucket_id=intake.bucket_id,
            token=secrets.token_urlsafe(32),
            recipient_name=first_recipient[:180],  # column is String(180)
            recipient_email=first_recipient[:320] if payload.to_emails else None,
            passcode_hash=_hash_passcode(share_passcode),
            can_preview=payload.can_preview,
            can_download=payload.can_download,
            can_add_notes=payload.can_add_notes,
            can_view_ai_summary=payload.can_view_ai_summary,
            can_use_ai_chat=payload.can_use_ai_chat,
            can_view_ai_tasks=payload.can_view_ai_tasks,
            can_propose_tasks=payload.can_propose_tasks,
        )
        # Grant the share the bucket's active uploaded files, else the recipient
        # opens the link to an empty package.
        share.files = list(_active_files(intake.bucket))
        db.add(share)
        await db.flush()
        share_url = _public_url(f"/buckets/share/{share.token}")
        passcode_blurb = (
            "\n\nSecure file access (no login required):\n"
            f"{share_url}\nAccess code: {share_passcode}\n"
            "Open the link and enter the access code to view the file package."
        )
        # The one-time passcode must NOT be persisted at rest (parity with the
        # hash-only BucketShare design); the stored send-row body masks it.
        passcode_blurb_redacted = (
            "\n\nSecure file access (no login required):\n"
            f"{share_url}\nAccess code: (sent to recipient; not stored)\n"
            "Open the link and enter the access code to view the file package."
        )

    for idx, raw_email in enumerate(payload.to_emails):
        email = str(raw_email).lower().strip()
        access = None
        if payload.bucket_access == "login":
            access = await _prepare_vendor_access(db, intake, email, payload)
            access_ids.append(access.id)
            vendor_link = _public_url(f"/vendor/buckets?bucket={intake.bucket_id}")
            access_blurb = (
                "\n\nSecure bucket access:\n"
                + vendor_link
                + "\n\nQualified Commercial has enabled vendor access for this bucket. Please log in with the invited vendor email to view the file package."
            )
            access_blurb_for_record = access_blurb
        else:
            access_blurb = passcode_blurb  # passcode share (same for all) or "" for none
            access_blurb_for_record = passcode_blurb_redacted  # masks the one-time code at rest
        body = payload.body.strip() + access_blurb + attachment_note
        # Persisted copy never carries the live one-time passcode.
        body_for_record = payload.body.strip() + access_blurb_for_record + attachment_note
        html_body = "<br>".join(html.escape(line) for line in body.splitlines())
        # Each To recipient gets their own message (separate secure-bucket access
        # link). CC only ONCE — on the first message — so a CC'd colleague isn't
        # copied N times (once per To recipient). The send row still records the
        # intended cc list for audit.
        send_cc = cc_emails if idx == 0 else []
        # Send from the operator's connected Gmail when available, else firm SES.
        result = await send_as_user(
            db,
            user.id,
            to_emails=[email],
            cc_emails=send_cc,
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
            body=body_for_record,
            vendor_access_ids=[str(access.id)] if access is not None else None,
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
            preferred_language=intake.preferred_language,
        )
        messages = chat_messages
        if chat_messages:
            assistant_message = chat_messages[-1].content
            raw = chat_messages[-1].metadata_json.get("raw") if isinstance(chat_messages[-1].metadata_json, dict) else None
            proposed_facts = raw.get("proposed_borrower_facts") if isinstance(raw, dict) else None
            newly_accepted = _merge_dealer_details(intake, proposed_facts)
            await _apply_dealer_detail_documents(db, intake, newly_accepted)
    _apply_loan_program_fit(intake)
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


@router.post("/{token}/requested-documents/sign", response_model=BucketFileRead)
async def dealer_sign_requested_document(
    token: str,
    payload: DealerDocumentSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    _require_dealer_intake(intake)
    return await _sign_requested_document(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@router.post("/{token}/requested-documents/pfs", response_model=BucketFileRead)
async def dealer_submit_pfs(
    token: str,
    payload: DealerPfsSubmission,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    _require_dealer_intake(intake)
    return await _submit_pfs_form(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@router.post("/{token}/requested-documents/debt-schedule", response_model=BucketFileRead)
async def dealer_submit_debt_schedule(
    token: str,
    payload: DealerDebtScheduleSubmission,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    _require_dealer_intake(intake)
    return await _submit_debt_schedule_form(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


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
            preferred_language=intake.preferred_language,
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


@client_router.post("/{intake_id}/requested-documents/pfs", response_model=BucketFileRead)
async def my_dealer_submit_pfs(
    intake_id: UUID,
    payload: DealerPfsSubmission,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_client_intake(db, user, intake_id)
    return await _submit_pfs_form(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)


@client_router.post("/{intake_id}/requested-documents/debt-schedule", response_model=BucketFileRead)
async def my_dealer_submit_debt_schedule(
    intake_id: UUID,
    payload: DealerDebtScheduleSubmission,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_client_intake(db, user, intake_id)
    return await _submit_debt_schedule_form(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)


FUNDING_PUBLIC_PATH = "/funding-review"
FUNDING_VARIANT = "real_estate_dscr_v1"


# Spanish: AI-translated, not yet native-speaker reviewed.
_FUNDING_EMPTY_MESSAGE = {
    Language.EN: (
        "Welcome — let's get you prequalified. I have the property and deal basics you just submitted, and I will screen this like an "
        "investor-loan underwriter: rent support, PITIA, DSCR, LTV, purchase or payoff evidence, property value, entity/vesting, and credit "
        "tier. I'll also ask a few quick questions — down payment, whether you've owned investment property before, and whether this is "
        "residential or commercial — so I can point you at the right program. Attach what you have and I will ask one targeted question at "
        "a time. Once I have enough to go on, I'll let you know where you stand."
    ),
    Language.ES: (
        "Bienvenido — vamos a preprobarte para el préstamo. Tengo los datos básicos de la propiedad y el trato que acabas de enviar, y voy a "
        "evaluar esto como lo haría un suscriptor de préstamos para inversionistas: soporte de renta, PITIA, DSCR, LTV, evidencia de compra o "
        "pago, valor de la propiedad, entidad/titularidad, y nivel de crédito. También te haré algunas preguntas rápidas — el pago inicial, si "
        "has sido propietario de una propiedad de inversión antes, y si esto es residencial o comercial — para poder orientarte al programa "
        "correcto. Adjunta lo que tengas y te haré una pregunta específica a la vez. Una vez que tenga suficiente información, te diré en qué "
        "posición te encuentras."
    ),
}


def _funding_empty_message(lang: str = Language.EN) -> str:
    return _FUNDING_EMPTY_MESSAGE.get(lang, _FUNDING_EMPTY_MESSAGE[Language.EN])


def _require_funding_intake(intake: PublicUnderwritingIntake) -> None:
    if intake.variant != FUNDING_VARIANT:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Funding review not found")


DEALER_VARIANT = "dealer_gatekeeper_v1"
MAIN_STREET_VARIANT = "main_street_v1"
MCA_VARIANT = "mca_refi_v1"
MCA_PUBLIC_PATH = "/mca-refinance-intake"

# Admin/broker lead creation accepts short names; map them explicitly rather
# than with a boolean. The previous `FUNDING_VARIANT if is_re else
# DEALER_VARIANT` meant widening the validator alone would have silently made a
# main_street payload create a DEALER lead.
_ADMIN_VARIANT_CONSTANTS: dict[str, str] = {
    "dealer": DEALER_VARIANT,
    "real_estate": FUNDING_VARIANT,
    "main_street": MAIN_STREET_VARIANT,
    "mca_refinance": MCA_VARIANT,
}

_VARIANT_LABELS: dict[str, str] = {
    DEALER_VARIANT: "dealer capital",
    FUNDING_VARIANT: "real estate DSCR/investor",
    MAIN_STREET_VARIANT: "operating business",
    MCA_VARIANT: "MCA refinance",
}


def _variant_label(variant: str | None) -> str:
    """Human label used in executive summaries and lender emails.

    Was a two-way ternary defaulting to "dealer capital", which mislabelled every
    Main Street lead in customer-visible output.
    """
    return _VARIANT_LABELS.get(variant or "", "commercial capital")



def _require_dealer_intake(intake: PublicUnderwritingIntake) -> None:
    """Reject a non-dealer intake on the dealer public routes, so a real-estate
    token can never be driven through car-dealer logic. Mirrors
    _require_funding_intake; 404 (not 403) to match the funding convention."""
    if intake.variant != DEALER_VARIANT:
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
        preferred_language=payload.preferred_language,
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
    await _record_resume_email(
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
    return await _response(db, intake, token=token, public_path=FUNDING_PUBLIC_PATH, assistant_message=_funding_empty_message(intake.preferred_language))


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
        assistant_message=_re_welcome_back(intake.preferred_language),
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
        assistant_message=_re_welcome_back(intake.preferred_language),
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
    return await _response(db, intake, token=token, public_path=FUNDING_PUBLIC_PATH, empty_message=_funding_empty_message(intake.preferred_language))


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
            preferred_language=intake.preferred_language,
        )
        messages = chat_messages
        if chat_messages:
            assistant_message = chat_messages[-1].content
            raw = chat_messages[-1].metadata_json.get("raw") if isinstance(chat_messages[-1].metadata_json, dict) else None
            proposed_facts = raw.get("proposed_borrower_facts") if isinstance(raw, dict) else None
            _merge_funding_review_details(intake, proposed_facts)
    await db.commit()
    intake = await _load_public_intake(db, token)

    prequal_widget = None
    if _re_prequal_ready(intake) and await _latest_artifact(db, intake.id, "prequalification") is None:
        acting_admin = await primary_super_admin(db)
        if acting_admin is not None:
            artifact = await _create_prequalification_artifact(db, intake, acting_admin)
            await db.commit()
            prequal_widget = _prequalification_widget(artifact)

    return await _response(
        db,
        intake,
        token=token,
        public_path=FUNDING_PUBLIC_PATH,
        assistant_message=assistant_message,
        messages=messages,
        prequalification_widget=prequal_widget,
    )


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


@funding_router.post("/{token}/requested-documents/sign", response_model=BucketFileRead)
async def funding_review_sign_requested_document(
    token: str,
    payload: DealerDocumentSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    _require_funding_intake(intake)
    return await _sign_requested_document(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


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


# ---------------------------------------------------------------------------
# MCA refinance intake (mca_refi_v1)
# ---------------------------------------------------------------------------
#
# The slimmest public variant by design: the ENTIRE file is six months of bank
# statements, a signed credit authorization, and the current advance terms.
# Everything else — tax returns, P&L, PFS — is deliberately absent; a borrower
# being debited daily abandons long checklists, and the desk gathers the rest
# after it engages. Endpoints mirror funding_router's subset, plus two this
# flow uniquely needs: a typed-in advance-terms form, and the first PUBLIC
# token-gated soft credit pull (elsewhere the pull is admin-executed).

_MCA_EMPTY_MESSAGE = {
    Language.EN: (
        "Welcome — let's map a way out of the daily debits. This review needs exactly three things: "
        "your last six months of business bank statements, one signed credit authorization (a soft "
        "check that does not affect your score), and the terms of your current advance — upload the "
        "agreements or just type the numbers into the form. That is the whole file. Start with "
        "whichever is easiest and I'll walk you through the rest."
    ),
    Language.ES: (
        "Bienvenido — vamos a trazar la salida de los débitos diarios. Esta revisión necesita "
        "exactamente tres cosas: sus últimos seis meses de estados de cuenta bancarios del negocio, "
        "una autorización de crédito firmada (una consulta suave que no afecta su puntaje) y los "
        "términos de su adelanto actual — suba los contratos o simplemente escriba los números en el "
        "formulario. Ese es todo el expediente. Empiece por lo más fácil y yo lo guío con el resto."
    ),
}

_MCA_WELCOME_BACK = {
    Language.EN: (
        "Welcome back. Your MCA refinance file is right where you left it — the checklist on this "
        "page shows which of the three items are still open."
    ),
    Language.ES: (
        "Bienvenido de nuevo. Su expediente de refinanciamiento está tal como lo dejó — la lista en "
        "esta página muestra cuáles de los tres puntos siguen pendientes."
    ),
}


def _mca_empty_message(lang: str = Language.EN) -> str:
    return _MCA_EMPTY_MESSAGE.get(lang, _MCA_EMPTY_MESSAGE[Language.EN])


def _mca_welcome_back(lang: str = Language.EN) -> str:
    return _MCA_WELCOME_BACK.get(lang, _MCA_WELCOME_BACK[Language.EN])


def _require_mca_intake(intake: PublicUnderwritingIntake) -> None:
    if intake.variant != MCA_VARIANT:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCA refinance review not found")


async def _load_mca_intake_by_session(db: AsyncSession, request: Request) -> tuple[PublicUnderwritingIntake, DealerIntakeLoginChallenge, str]:
    session_token = request.headers.get("x-mca-session") or request.headers.get("X-Mca-Session")
    if not session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MCA refinance session is required")
    intake, challenge = await _load_intake_by_dealer_session(db, session_token)
    _require_mca_intake(intake)
    return intake, challenge, session_token


class McaRefiStart(BaseModel):
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=48)
    business_name: str | None = Field(default=None, max_length=180)
    # Optional pre-seed from the marketing calculator, so the room opens
    # already knowing what the applicant typed there.
    remaining_payback: float | None = Field(default=None, ge=0)
    months_remaining: float | None = Field(default=None, ge=0, le=60)
    payment_frequency: str | None = Field(default=None, max_length=16)
    terms_accepted: bool = False
    privacy_accepted: bool = False
    terms_version: str = Field(default=TERMS_VERSION, max_length=32)
    privacy_version: str = Field(default=PRIVACY_VERSION, max_length=32)
    preferred_language: Language = Language.EN

    @field_validator("phone", "business_name", "payment_frequency", mode="before")
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


class McaAdvanceRow(BaseModel):
    funder: str = Field(min_length=1, max_length=180)
    remaining_payback: float = Field(ge=0)
    payment_amount: float = Field(ge=0)
    payment_frequency: str = Field(pattern="^(daily|weekly|biweekly|monthly)$")
    payments_remaining: int | None = Field(default=None, ge=0, le=2000)


class McaTermsSubmission(BaseModel):
    """Typed-in advance terms — the no-paperwork path for the third checklist
    item. Same trusted-structured-input rationale as the debt-schedule form."""

    business_name: str = Field(min_length=1, max_length=180)
    advances: list[McaAdvanceRow] = Field(min_length=1, max_length=10)
    acknowledgment: bool


class PublicCreditPullRequest(BaseModel):
    """Borrower-initiated soft pull. The SSN is optional, transient, and never
    persisted by this API — it improves bureau hit rate and handles the
    no_hit_provide_ssn deny path. Consent is the SIGNED authorization document
    already on file; this endpoint refuses to run without it."""

    ssn: str | None = Field(default=None)

    @field_validator("ssn", mode="before")
    @classmethod
    def normalize_ssn(cls, value: object) -> object:
        if value is None or value == "":
            return None
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if len(digits) != 9:
            raise ValueError("SSN must be 9 digits")
        return digits


_MCA_REMITS_PER_MONTH = {"daily": 21.0, "weekly": 4.33, "biweekly": 2.17, "monthly": 1.0}

# Structured facts the MCA chat may propose. Same precedence law as every
# other detail bundle: type-validated, allowlist-dropped, never guessed.
_MCA_DETAIL_KEYS = (
    "funder",
    "remaining_payback",
    "payment_amount",
    "payment_frequency",
    "months_remaining",
    "factor_rate",
    "advance_count",
    "requested_amount",
)


def _mca_details(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    state = _intake_state(intake)
    details = state.get("mca_details")
    return details if isinstance(details, dict) else {}


def _merge_mca_details(intake: PublicUnderwritingIntake, proposed: Any) -> None:
    """Validate and merge AI-proposed advance facts into
    intake_state["mca_details"]. Any key off the allowlist or with the wrong
    type is dropped rather than persisted."""
    if not isinstance(proposed, dict):
        return
    accepted: dict[str, Any] = {}
    for key in _MCA_DETAIL_KEYS:
        if key not in proposed:
            continue
        value = proposed[key]
        if key in ("funder",):
            text = str(value).strip()
            if text:
                accepted[key] = text[:180]
            continue
        if key == "payment_frequency":
            text = str(value).strip().lower()
            if text in _MCA_REMITS_PER_MONTH:
                accepted[key] = text
            continue
        if key == "advance_count":
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 50:
                accepted[key] = int(value)
            continue
        if key == "factor_rate":
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 1.0 <= value <= 3.0:
                accepted[key] = float(value)
            continue
        if key == "months_remaining":
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 60:
                accepted[key] = float(value)
            continue
        # remaining_payback / payment_amount / requested_amount — dollars.
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 500_000_000:
            accepted[key] = float(value)
    if not accepted:
        return
    state = _intake_state(intake)
    merged = dict(state.get("mca_details") or {})
    merged.update(accepted)
    state["mca_details"] = merged
    intake.intake_state = state


def _mca_context(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    state = _intake_state(intake)
    checklist = {
        doc.name: doc.status for doc in (intake.bucket.requested_documents if intake.bucket else [])
    }
    return {
        "review_type": MCA_VARIANT,
        "deal_type": "MCA refinance review",
        "documentation_level": "three-item MCA refinance file",
        "collateral_type": "business cash flow",
        "business_name": intake.business_name,
        "requested_loan_amount": float(intake.requested_loan_amount) if intake.requested_loan_amount is not None else None,
        "mca_details": _mca_details(intake) or None,
        "credit_pull": _credit_pull_state(intake) or None,
        "checklist_status": checklist,
        "chat_facts": state.get("chat_facts") if isinstance(state.get("chat_facts"), list) else [],
        "baseline_document_policy": {
            "stage": "stage_1_mca_refinance",
            "allowed_document_categories": [
                "last 6 months business bank statements",
                "signed credit authorization",
                "current MCA / advance terms (agreements, payoff letters, or the typed form)",
            ],
            "do_not_request_other_document_categories": True,
        },
    }


async def _find_or_create_mca_client(db: AsyncSession, payload: McaRefiStart) -> Client:
    email = _normalize_email(str(payload.email))
    client = (await db.execute(select(Client).where(Client.email == email).order_by(Client.created_at.desc()))).scalars().first()
    owner = await primary_super_admin(db)
    lead_payload = {
        "source": "mca_refinance",
        "business_name": payload.business_name,
        "remaining_payback": payload.remaining_payback,
        "months_remaining": payload.months_remaining,
        "payment_frequency": payload.payment_frequency,
    }
    if client is None:
        client = Client(
            name=payload.full_name.strip(),
            email=email,
            phone=payload.phone,
            referral_source="mca_refinance",
            originating_agent_id=owner.id if owner else None,
            current_agent_id=owner.id if owner else None,
            source_channel="mca_refinance",
            lead_source="other",
            lead_temperature="warm",
            financing_support_needed="yes",
            relationship_context="new_lead",
            client_experience_mode="self_directed",
            client_experience_mode_reason="mca_refinance",
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
    merged = dict(client.lead_intake or {})
    merged.update({key: value for key, value in lead_payload.items() if value is not None})
    client.lead_intake = merged
    await db.flush()
    return client


async def _create_bucket_for_mca_refi(db: AsyncSession, client: Client, payload: McaRefiStart, request: Request) -> tuple[Bucket, BucketUploadLink]:
    owner = await primary_super_admin(db)
    business = payload.business_name or payload.full_name
    bucket = Bucket(
        name=f"{business} MCA Refinance",
        bucket_type="mca_refinance_intake",
        client_name=business,
        purpose="MCA refinance AI intake",
        description="Public MCA refinance preliminary review — statements, credit authorization, advance terms.",
        ai_context={
            "review_type": MCA_VARIANT,
            "screening_stage": "stage_1_mca_refinance",
            "deal_type": "MCA refinance review",
            "documentation_level": "three-item MCA refinance file",
            "collateral_type": "business cash flow",
            "client_email": client.email,
            "stage_1_required_items": [
                "last 6 months business bank statements",
                "signed credit authorization",
                "current MCA / advance terms",
            ],
        },
        created_by_id=owner.id if owner else None,
    )
    db.add(bucket)
    await db.flush()
    for doc in MCA_REQUIRED_DOCUMENTS:
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
                requires_signature=bool(doc.get("requires_signature", False)),
                signature_kind=doc.get("signature_kind"),
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
        "mca_refinance_intake_created",
        request=request,
        actor_name=payload.full_name,
        actor_email=client.email,
        actor_role="public_lead",
        target_type="bucket",
        target_id=str(bucket.id),
        detail="Public MCA refinance review created",
    )
    await db.flush()
    return bucket, link


@mca_router.post("/start", response_model=DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def start_mca_refinance(
    payload: McaRefiStart,
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
    existing = await _latest_active_intake_by_email(db, str(payload.email), variant=MCA_VARIANT)
    if existing is not None:
        await _start_login_challenge(
            db,
            email=str(payload.email),
            request=request,
            reason="existing_mca_refinance_start",
            variant=MCA_VARIANT,
            review_label="MCA refinance review",
            event_prefix="mca_refinance",
            target_type="mca_refinance_intake",
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A secure MCA refinance review already exists for this email. We sent a short access code so you can continue that file.",
        )
    client = await _find_or_create_mca_client(db, payload)
    bucket, link = await _create_bucket_for_mca_refi(db, client, payload, request)
    token = _new_public_token()
    seeded: dict[str, Any] = {}
    if payload.remaining_payback is not None:
        seeded["remaining_payback"] = payload.remaining_payback
    if payload.months_remaining is not None:
        seeded["months_remaining"] = payload.months_remaining
    if payload.payment_frequency in _MCA_REMITS_PER_MONTH:
        seeded["payment_frequency"] = payload.payment_frequency
    intake = PublicUnderwritingIntake(
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=_hash_token(token),
        variant=MCA_VARIANT,
        full_name=payload.full_name.strip(),
        email=client.email or _normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        loan_purpose="mca_refinance",
        preferred_language=payload.preferred_language,
        intake_state={
            "messages": [],
            "source": "mca_refinance",
            **({"mca_details": seeded} if seeded else {}),
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
    await _record_resume_email(
        intake,
        token=token,
        request=request,
        reason="mca_refinance_created",
        public_path=MCA_PUBLIC_PATH,
        review_label="MCA refinance review",
        room_label="MCA refinance file",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(db, intake, token=token, public_path=MCA_PUBLIC_PATH, assistant_message=_mca_empty_message(intake.preferred_language))


@mca_router.post("/login/start", response_model=DealerLoginStartResponse)
async def start_mca_refinance_login(
    payload: DealerLoginStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerLoginStartResponse:
    login_required = await _start_login_challenge(
        db,
        email=str(payload.email),
        request=request,
        reason="mca_refinance_login_requested",
        variant=MCA_VARIANT,
        review_label="MCA refinance review",
        event_prefix="mca_refinance",
        target_type="mca_refinance_intake",
    )
    await db.commit()
    return DealerLoginStartResponse(
        login_required=login_required,
        message=(
            "We found an existing MCA refinance review for this email. Enter the code we sent to continue."
            if login_required
            else "No existing review was found. Complete the form to start a new one."
        ),
    )


@mca_router.post("/login/verify", response_model=DealerIntakeResponse)
async def verify_mca_refinance_login(
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
                PublicUnderwritingIntake.variant == MCA_VARIANT,
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
    if intake is None or intake.variant != MCA_VARIANT:
        challenge.revoked_at = _now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    intake.token_hash = _hash_token(public_token)
    await _log(
        db,
        intake.bucket_id,
        "mca_refinance_login_verified",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="mca_refinance_intake",
        target_id=str(intake.id),
        detail="MCA refinance continuation login verified",
    )
    await db.commit()
    intake = await _load_public_intake(db, public_token)
    return await _response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        public_path=MCA_PUBLIC_PATH,
        assistant_message=_mca_welcome_back(intake.preferred_language),
    )


@mca_router.get("/session", response_model=DealerIntakeResponse)
async def get_mca_refinance_session(request: Request, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake, _challenge, session_token = await _load_mca_intake_by_session(db, request)
    public_token = _new_public_token()
    intake.token_hash = _hash_token(public_token)
    await db.commit()
    intake = await _load_public_intake(db, public_token)
    return await _response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        public_path=MCA_PUBLIC_PATH,
        assistant_message=_mca_welcome_back(intake.preferred_language),
    )


@mca_router.post("/logout", response_model=DealerLogoutResponse)
async def logout_mca_refinance_session(
    payload: DealerLogoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerLogoutResponse:
    session_token = payload.session_token or request.headers.get("x-mca-session") or request.headers.get("X-Mca-Session")
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


@mca_router.get("/{token}", response_model=DealerIntakeResponse)
async def get_mca_refinance(token: str, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    return await _response(db, intake, token=token, public_path=MCA_PUBLIC_PATH, empty_message=_mca_empty_message(intake.preferred_language))


@mca_router.post("/{token}/chat", response_model=DealerIntakeResponse)
async def mca_refinance_chat(
    token: str,
    payload: DealerChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    _apply_updates(intake, payload.updates)
    _record_chat_fact(intake, payload.message)
    intake.last_message_at = _now()
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **_mca_context(intake)}
        chat_messages, _, _ = await create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="uploader",
            message=payload.message.strip(),
            actor_name=intake.full_name,
            upload_link=intake.bucket_upload_link,
            preferred_language=intake.preferred_language,
        )
        messages = chat_messages
        if chat_messages:
            assistant_message = chat_messages[-1].content
            raw = chat_messages[-1].metadata_json.get("raw") if isinstance(chat_messages[-1].metadata_json, dict) else None
            proposed_facts = raw.get("proposed_borrower_facts") if isinstance(raw, dict) else None
            _merge_mca_details(intake, proposed_facts)
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(
        db,
        intake,
        token=token,
        public_path=MCA_PUBLIC_PATH,
        assistant_message=assistant_message,
        messages=messages,
    )


@mca_router.post("/{token}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def mca_refinance_upload_init(
    token: str,
    payload: DealerFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    return await _start_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@mca_router.post("/{token}/files/complete", response_model=BucketFileRead)
async def mca_refinance_upload_complete(
    token: str,
    payload: DealerUploadComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    return await _complete_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@mca_router.post("/{token}/requested-documents/sign", response_model=BucketFileRead)
async def mca_refinance_sign_requested_document(
    token: str,
    payload: DealerDocumentSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    return await _sign_requested_document(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@mca_router.post("/{token}/requested-documents/mca-terms", response_model=BucketFileRead)
async def submit_mca_refinance_terms(
    token: str,
    payload: McaTermsSubmission,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    """The typed-in path for the third checklist item. Renders a PDF and writes
    a TRUSTED BucketFileAnalysis directly from the structured input (the
    _store_drafted_form_pdf trick), so the terms flow into
    extract_debt_schedule and the metrics pipeline identically to an analyzed
    upload — monthly payments are normalized from each advance's remittance
    frequency."""
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    if not payload.acknowledgment:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You must acknowledge the disclaimer to submit this form")
    req = next((doc for doc in intake.bucket.requested_documents if doc.category == "Debts"), None)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Advance terms are not requested on this intake")
    if req.status == "uploaded":
        raise HTTPException(status.HTTP_409_CONFLICT, "Advance terms have already been submitted for this file")

    from app.services.dealer_forms_pdf import render_debt_schedule_pdf

    rows: list[tuple[str, float, float]] = []
    debts: list[dict[str, Any]] = []
    total_balance = 0.0
    total_monthly = 0.0
    for adv in payload.advances:
        monthly = adv.payment_amount * _MCA_REMITS_PER_MONTH[adv.payment_frequency]
        rows.append((adv.funder, adv.remaining_payback, monthly))
        debts.append(
            {
                "lender": adv.funder,
                "original_amount": None,
                "current_balance": adv.remaining_payback,
                "monthly_payment": round(monthly, 2),
                "maturity_date": None,
                "payment_amount": adv.payment_amount,
                "payment_frequency": adv.payment_frequency,
                "payments_remaining": adv.payments_remaining,
            }
        )
        total_balance += adv.remaining_payback
        total_monthly += monthly

    key_facts: dict[str, Any] = {
        "debts": debts,
        "total_outstanding_balance": round(total_balance, 2),
        "total_monthly_debt_service": round(total_monthly, 2),
        "advance_count": len(debts),
    }
    pdf_bytes = render_debt_schedule_pdf(
        business_name=payload.business_name,
        debts=rows,
        total_balance=key_facts["total_outstanding_balance"],
        total_monthly=key_facts["total_monthly_debt_service"],
    )
    stored = await _store_drafted_form_pdf(
        db,
        intake,
        req,
        pdf_bytes,
        request,
        file_label="Current MCA / Advance Terms",
        classification="debt_schedule",
        key_facts=key_facts,
        actor_name=intake.full_name,
        actor_email=intake.email,
    )
    # Mirror the typed terms into the structured detail bundle the AI context
    # reads, so the chat knows the terms without re-deriving them from the PDF.
    first = payload.advances[0]
    _merge_mca_details(
        intake,
        {
            "funder": first.funder,
            "remaining_payback": round(total_balance, 2),
            "payment_amount": first.payment_amount,
            "payment_frequency": first.payment_frequency,
            "advance_count": len(debts),
        },
    )
    await db.commit()
    return stored


# One public pull per intake, throttled per IP — this endpoint spends bureau
# money on an unauthenticated (token-bearing) caller, so it is deliberately
# stingier than /start.
_MCA_PULL_LAST_BY_IP: dict[str, float] = {}


@mca_router.post("/{token}/credit-pull", response_model=DealerIntakeResponse)
async def mca_refinance_credit_pull(
    token: str,
    payload: PublicCreditPullRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    """The first PUBLIC soft-pull endpoint. Consent is the signed
    credit-authorization document on THIS intake — the endpoint refuses to run
    without it, exactly like the admin path (run_lead_credit_pull), and the
    result lands in the same state["credit_pull"] cross-reference. One pull
    per intake: a second call 409s instead of re-billing the bureau."""
    _throttle_or_429(
        _MCA_PULL_LAST_BY_IP,
        (request.client.host if request.client else "?") or "?",
        30.0,
        "Please wait a moment before requesting the credit check again.",
    )
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    if intake.client_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This review has no linked client")
    if _credit_pull_state(intake).get("fico") is not None or _credit_pull_state(intake).get("pull_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The credit check has already been completed for this file.")

    doc = _credit_authorization_doc(intake)
    if doc is None or doc.status != "uploaded":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Sign the credit authorization first — the soft check cannot run without it.",
        )
    signature = (
        await db.execute(
            select(BucketDocumentSignature)
            .where(BucketDocumentSignature.requested_document_id == doc.id)
            .order_by(BucketDocumentSignature.created_at.desc())
        )
    ).scalars().first()
    applicant_data = signature.applicant_data if signature else None
    if not applicant_data:
        raise HTTPException(status.HTTP_409_CONFLICT, "No applicant identity data on file for this signature")

    client = await db.get(Client, intake.client_id)
    if client is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Linked client not found")

    from datetime import date as _date

    from app.services import credit_pull_core

    try:
        dob = _date.fromisoformat(str(applicant_data.get("dob")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Applicant date of birth on file is invalid") from exc

    try:
        pull = await credit_pull_core.run_soft_pull(
            db,
            client=client,
            applicant=credit_pull_core.SoftPullApplicant(
                legal_first_name=str(applicant_data.get("legal_first_name") or ""),
                legal_last_name=str(applicant_data.get("legal_last_name") or ""),
                dob=dob,
                street=str(applicant_data.get("street") or ""),
                city=str(applicant_data.get("city") or ""),
                state=str(applicant_data.get("state") or ""),
                zip=str(applicant_data.get("zip") or ""),
                ssn=payload.ssn,
            ),
            actor=None,
        )
    except credit_pull_core.SoftPullDenied as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": exc.code, "message": exc.message}) from exc
    except credit_pull_core.SoftPullValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except credit_pull_core.SoftPullRateLimited as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except credit_pull_core.SoftPullUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    state = _intake_state(intake)
    state["credit_pull"] = {
        "pull_id": str(pull.id),
        "fico": pull.fico,
        "pulled_at": pull.pulled_at.isoformat() if pull.pulled_at else None,
        "expires_at": pull.expires_at.isoformat() if pull.expires_at else None,
    }
    intake.intake_state = state
    await _log(
        db,
        intake.bucket_id,
        "mca_refinance_credit_pull",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="mca_refinance_intake",
        target_id=str(intake.id),
        detail="Borrower-initiated soft credit pull completed",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(
        db,
        intake,
        token=token,
        public_path=MCA_PUBLIC_PATH,
        assistant_message="The soft credit check is done — it does not affect your score. That closes another of the three items.",
    )


@mca_router.post("/{token}/run-review", response_model=DealerIntakeResponse)
async def run_mca_refinance_review(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    await _execute_intake_review(
        db,
        intake,
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        log_event="mca_refinance_ai_review_queued",
        detail="Public MCA refinance screen",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(db, intake, token=token, public_path=MCA_PUBLIC_PATH)


@mca_router.post("/{token}/book-call", response_model=DealerIntakeResponse)
async def book_mca_refinance_call(
    token: str,
    payload: DealerBookCallRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _require_mca_intake(intake)
    if _call_booked(intake):
        return await _response(
            db,
            intake,
            token=token,
            public_path=MCA_PUBLIC_PATH,
            assistant_message="Your call is already booked. If any of the three items is still open, closing it before the meeting speeds everything up.",
        )
    starts_at = _to_utc_minute(payload.starts_at)
    owner, booking, slots = await _dealer_call_slots(db)
    if owner is None or booking is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Call scheduling is not available right now.")
    if not any(abs((datetime.fromisoformat(slot["starts_at"]) - starts_at).total_seconds()) < 1 for slot in slots):
        raise HTTPException(status.HTTP_409_CONFLICT, "That call time is no longer available. Choose another time.")
    who = f"{intake.full_name} <{intake.email}>"
    description = (
        "Booked from MCA Refinance Review.\n"
        f"MCA refinance intake: {intake.id}\n"
        f"Bucket: {intake.bucket_id}\n"
        f"Business: {intake.business_name or '(not provided)'}\n"
        f"Name: {intake.full_name}\n"
        f"Email: {intake.email}\n"
        f"Phone: {intake.phone or '(not provided)'}\n"
    )
    ev = CalendarEvent(
        loan_id=None,
        kind=CalendarEventKind.CALL,
        title=f"MCA refinance call: {intake.business_name or intake.full_name}",
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=booking.duration_min,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.AUTO,
        owner_user_id=owner.id,
        external_ref_kind="mca_refinance_intake",
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
            kind="calendar.mca_refinance_call_booked",
            summary=f"MCA refinance call booked for {intake.business_name or intake.full_name}",
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
        "mca_refinance_call_booked",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="calendar_event",
        target_id=str(ev.id),
        detail=f"MCA refinance call booked for {starts_at.isoformat()}",
    )
    await db.commit()
    intake = await _load_public_intake(db, token)
    return await _response(
        db,
        intake,
        token=token,
        public_path=MCA_PUBLIC_PATH,
        assistant_message="Your call is booked. Anything still open on the three-item checklist can be finished right here before the meeting.",
    )
