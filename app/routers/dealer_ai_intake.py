# FastAPI dependency injection intentionally uses callable defaults.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import mimetypes
import re
import secrets
import struct
import time
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, with_loader_criteria

from app.config import get_settings
from app.db import get_db  # noqa: F401  (handlers reach this through core)
from app.dealer_os.services import client_room, consent_delivery
from app.deps import CurrentUser  # noqa: F401  (handlers reach this through core)
from app.enums import (
    CalendarEventKind,  # noqa: F401  (handlers reach this through core)
    CalendarEventSource,  # noqa: F401  (handlers reach this through core)
    CalendarEventStatus,  # noqa: F401  (handlers reach this through core)
    ContractType,
    Language,  # noqa: F401  (handlers reach this through core)
    Role,  # noqa: F401  (handlers reach this through core)
)
from app.lead_types import (
    FundingIntent,
)
from app.lead_types import (
    normalize_funding_intent as normalize_lead_funding_intent,
)
from app.models.activity import Activity  # noqa: F401  (handlers reach this through core)
from app.models.ai_audit_event import AIAuditEvent  # noqa: F401  (handlers reach this through core)
from app.models.application_profile import (  # noqa: F401  (handlers reach this through core)
    ApplicationProfile,
    ApplicationRoomDelivery,
)
from app.models.booking_settings import BookingSettings
from app.models.bucket import (
    Bucket,  # noqa: F401  (handlers reach this through core)
    BucketAIChatAction,
    BucketAIMessage,  # noqa: F401  (handlers reach this through core)
    BucketAIReview,  # noqa: F401  (handlers reach this through core)
    BucketDocumentSignature,  # noqa: F401  (handlers reach this through core)
    BucketFile,  # noqa: F401  (handlers reach this through core)
    BucketFileAnalysis,
    BucketNote,  # noqa: F401  (handlers reach this through core)
    BucketRequestedDocument,  # noqa: F401  (handlers reach this through core)
    BucketShare,  # noqa: F401  (handlers reach this through core)
    BucketUploadLink,
    BucketVendorAccess,
)
from app.models.client import Client  # noqa: F401  (handlers reach this through core)
from app.models.dealer_intake_login import (
    DealerIntakeLoginChallenge,  # noqa: F401  (handlers reach this through core)
)
from app.models.event import CalendarEvent  # noqa: F401  (handlers reach this through core)
from app.models.notification import Notification
from app.models.public_underwriting_intake import (
    PublicUnderwritingIntake,  # noqa: F401  (handlers reach this through core)
    PublicUnderwritingIntakeArtifact,
    PublicUnderwritingIntakeEmailSend,  # noqa: F401  (handlers reach this through core)
)
from app.models.sms_message import SmsMessage  # noqa: F401  (handlers reach this through core)
from app.models.user import User  # noqa: F401  (handlers reach this through core)
from app.routers.buckets import (
    _bucket_storage_config,  # noqa: F401  (handlers reach this through core)
    _client_ip,  # noqa: F401  (handlers reach this through core)
    _delete_s3_object,  # noqa: F401  (handlers reach this through core)
    _download_url,
    _generate_passcode,  # noqa: F401  (handlers reach this through core)
    _hash_passcode,  # noqa: F401  (handlers reach this through core)
    _log,  # noqa: F401  (handlers reach this through core)
    _public_requested_document_read,
    _public_url,  # noqa: F401  (handlers reach this through core)
    _reconcile_completed_bucket_file,
    _request_uploaded_file_reads,
    _s3_client,  # noqa: F401  (handlers reach this through core)
    _safe_filename,  # noqa: F401  (handlers reach this through core)
    _sanitize_upload_content_type,  # noqa: F401  (handlers reach this through core)
    _stale_requested_document_error,
    _upload_url,
    _validate_completed_bucket_file,
    _vendor_user_from_payload,
    _verify_files_before_restore,  # noqa: F401  (handlers reach this through core)
)
from app.routers.public import (  # noqa: F401  (handlers reach this through core)
    _available_booking_slots,
    _to_utc_minute,
)
from app.schemas.bucket import (
    BucketAIMessageRead,
    BucketAIReviewRead,
    BucketFileRead,  # noqa: F401  (handlers reach this through core)
    BucketFileUploadInitResponse,  # noqa: F401  (handlers reach this through core)
    BucketNoteRead,  # noqa: F401  (handlers reach this through core)
    BucketRequestedDocumentRead,  # noqa: F401  (handlers reach this through core)
    BucketRequestUploadedFileRead,
    IntakeChatActionRead,
    IntakeChatActionResult,  # noqa: F401  (handlers reach this through core)
)
from app.schemas.capital_readiness import CapitalReadinessRead
from app.schemas.common import ORMModel
from app.schemas.phone import OptionalPhone, RequiredPhone
from app.schemas.retention import (  # noqa: F401  (handlers reach this through core)
    LegalHoldChange,
    RetentionStatusRead,
)
from app.services import (
    application_profiles as profiles_service,  # noqa: F401  (handlers reach this through core)
)
from app.services import (
    booking_notify,
    booking_reminders,
    bucket_archive,  # noqa: F401  (handlers reach this through core)
    equipment_financing,
    file_events,  # noqa: F401  (handlers reach this through core)
    inline_images,  # noqa: F401  (handlers reach this through core)
    intake_lifecycle,
    intake_retention,  # noqa: F401  (handlers reach this through core)
    locked_file_requests,
    mca_refinance_policy,  # noqa: F401  (handlers reach this through core)
    merchant_processing,
    notifications,
    provenance,
    retention_holds,  # noqa: F401  (handlers reach this through core)
    upload_validation,  # noqa: F401  (handlers reach this through core)
)
from app.services import (
    capital_readiness as capital_readiness_service,  # noqa: F401  (handlers reach this through core)
)
from app.services.ai import engagement  # noqa: F401  (handlers reach this through core)
from app.services.ai.bedrock_client import get_client, model_light
from app.services.ai.usage import json_safe_metadata, tracked_messages_create
from app.services.bucket_ai import (
    CHAT_TURN_ORDER,
    CURRENT_FILE_ANALYSIS_VERSION,
    create_chat_reply,  # noqa: F401  (handlers reach this through core)
    create_human_message,  # noqa: F401  (handlers reach this through core)
    latest_review,
    run_bucket_ai_review,
    upload_link_visible_summary,
)
from app.services.bucket_requests import is_open_request
from app.services.dealer_ai_intelligence_pdf import (
    render_dealer_intelligence_pdf,  # noqa: F401  (handlers reach this through core)
)
from app.services.dealer_partner_access import (
    DEALER_INTAKE_VARIANT,
    require_dealer_partner_standing,
)
from app.services.email.ses_client import send_email, send_raw_email
from app.services.email.user_mailer import (
    send_as_user,  # noqa: F401  (handlers reach this through core)
)
from app.services.foreclosure_rescue import (
    FORECLOSURE_RESCUE_VARIANT,  # noqa: F401  (handlers reach this through core)
    is_above_published_program_max,
)
from app.services.intake_chat_actions import (
    actions_for_messages,
    execute_room_action,  # noqa: F401  (handlers reach this through core)
    template_for_action,  # noqa: F401  (handlers reach this through core)
)
from app.services.main_street_programs import (
    TERM_3_5_MIN_DSCR,
    TERM_3_5_MIN_REVENUE,
    TERM_10YR_MIN_DSCR,
    normalize_industry,  # noqa: F401  (handlers reach this through core)
    normalize_intent,  # noqa: F401  (handlers reach this through core)
)
from app.services.payment_authorization import (
    primary_super_admin,  # noqa: F401  (handlers reach this through core)
)
from app.services.public_underwriting_packet_pdf import (
    render_executive_summary_pdf,
    render_underwriting_packet_pdf,
)
from app.services.sms import send_sms_checked  # noqa: F401  (handlers reach this through core)
from app.services.team_calendar import lock_calendar_owner, team_booking_settings

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
EQUIPMENT_PUBLIC_PATH = "/equipment-financing-intake"

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

_EQUIPMENT_WELCOME = {
    Language.EN: (
        "I opened your secure Equipment Financing review. The equipment request is already selected. "
        "I will review the purchase details, vendor quote, useful life, business cash flow, and the evidence needed to match the request responsibly. "
        "Upload the vendor quote and the business documents you have now; if something is missing, I will ask for one clear item at a time."
    ),
    Language.ES: (
        "Abrí tu revisión segura de Financiamiento de Equipos. La solicitud de equipos ya está seleccionada. "
        "Revisaré los datos de la compra, la cotización, la vida útil, el flujo de caja del negocio y la evidencia necesaria para evaluar la solicitud responsablemente. "
        "Sube la cotización y los documentos del negocio que tengas; si falta algo, te pediré un punto claro a la vez."
    ),
}

_EQUIPMENT_WELCOME_BACK = {
    Language.EN: "Welcome back. I restored your secure Equipment Financing review with the purchase details, uploads, and prior chat intact.",
    Language.ES: "Bienvenido de nuevo. Restauré tu revisión segura de Financiamiento de Equipos con los datos de la compra, archivos y chat anteriores.",
}

_RE_WELCOME_BACK = {
    Language.EN: "Welcome back. I restored your secure real estate funding review with your prior uploads and chat context.",
    Language.ES: "Bienvenido de nuevo. Restauré tu revisión segura de financiamiento inmobiliario con tus archivos y el contexto del chat anteriores.",
}


def _dealer_welcome(lang: str, *, email_note: str = "") -> str:
    return _DEALER_WELCOME.get(lang, _DEALER_WELCOME[Language.EN]) + email_note


def _dealer_welcome_back(lang: str) -> str:
    return _DEALER_WELCOME_BACK.get(lang, _DEALER_WELCOME_BACK[Language.EN])


def _equipment_welcome(lang: str, *, email_note: str = "") -> str:
    return _EQUIPMENT_WELCOME.get(lang, _EQUIPMENT_WELCOME[Language.EN]) + email_note


def _equipment_welcome_back(lang: str) -> str:
    return _EQUIPMENT_WELCOME_BACK.get(lang, _EQUIPMENT_WELCOME_BACK[Language.EN])


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
ZIP_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
ZIP_MAX_ENTRY_BYTES = 40 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 80 * 1024 * 1024
ZIP_FETCH_READ_ATTEMPTS = 3
ZIP_FETCH_MAX_RUNS = 5
ZIP_MAX_CENTRAL_DIRECTORY_BYTES = 2 * 1024 * 1024

# Abuse guards for the fully-public POST endpoints (single-instance in-memory,
# same assumption as the public.py throttle). /start creates a Client + Bucket +
# upload link + intake per call, and /run-review runs a heavy Bedrock pass, so
# both are rate-limited to stop scripted mass-creation / LLM-cost amplification.
_START_MIN_INTERVAL_SECONDS = 15.0
_START_LAST_BY_IP: dict[str, float] = {}
_REVIEW_MIN_INTERVAL_SECONDS = 45.0
_REVIEW_LAST_BY_TOKEN: dict[str, float] = {}

#: Handing a client into their room provisions a profile and reads the room PIN,
#: so it is cheap but not free, and there is no reason to tap it in a loop.
_ROOM_HANDOFF_MIN_INTERVAL_SECONDS = 3.0
_ROOM_HANDOFF_LAST_BY_TOKEN: dict[str, float] = {}
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
    ".tsv",
    ".xlsx",
    ".xlsm",
    ".xls",
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


async def _guard_arf_attribution(db: AsyncSession, payload: BaseModel) -> None:
    from app.services.arf_channel import PRODUCT_BY_KEY, require_hosted_permission

    values = [str(getattr(payload, key, "") or "").casefold() for key in ("program", "source", "page")]
    slugs = {str(item["public_slug"]).casefold() for item in PRODUCT_BY_KEY.values()}
    attributed = any(value.startswith("arf_") or value == "arf" or value.strip("/").split("/")[-1] in slugs for value in values)
    if attributed:
        contact_keys = {"full_name", "email", "phone", "business_name", "terms_accepted", "privacy_accepted", "source", "page", "program", "vertical", "campaign", "cta"}
        await require_hosted_permission(db, requested_fields=sorted(payload.model_fields_set - contact_keys))


class PublicIntakeAttribution(BaseModel):
    source: str | None = Field(default=None, max_length=64)
    page: str | None = Field(default=None, max_length=300)
    program: str | None = Field(default=None, max_length=120)
    vertical: str | None = Field(default=None, max_length=64)
    campaign: str | None = Field(default=None, max_length=120)
    cta: str | None = Field(default=None, max_length=120)
    funding_intent: FundingIntent | None = None

    @field_validator(
        "source",
        "page",
        "program",
        "vertical",
        "campaign",
        "cta",
        mode="before",
    )
    @classmethod
    def normalize_attribution(cls, value: object) -> object:
        return str(value).strip() if value is not None and str(value).strip() else None

    @field_validator("funding_intent", mode="before")
    @classmethod
    def normalize_structured_funding_intent(cls, value: object) -> object:
        return normalize_lead_funding_intent(value)


class EquipmentFinancingDetails(BaseModel):
    """Structured asset facts shared by public and staff-created applications."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=1000)
    age_years: float | None = Field(default=None, ge=0, le=150)
    condition: Literal["new", "used", "refurbished", "unknown"] | None = None
    remaining_useful_life_years: float | None = Field(default=None, gt=0, le=150)
    seller_name: str | None = Field(default=None, max_length=240)
    seller_contact: str | None = Field(default=None, max_length=320)
    seller_type: Literal["dealer", "vendor", "private_party", "unknown"] | None = None
    purchase_price: float | None = Field(default=None, gt=0, le=100_000_000)
    delivery_timing: str | None = Field(default=None, max_length=240)
    intended_business_use: str | None = Field(default=None, max_length=1200)

    @field_validator(
        "description",
        "seller_name",
        "seller_contact",
        "delivery_timing",
        "intended_business_use",
        mode="before",
    )
    @classmethod
    def blank_equipment_text_to_none(cls, value: object) -> object:
        return None if value == "" else value


class CapitalReadinessDiagnosticAnswers(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    business_type: Literal["dealer", "main_street", "real_estate", ""] = Field(
        default="", alias="businessType"
    )
    years: Literal["under_1", "one_two", "over_2", ""] = ""
    revenue: str = Field(default="", max_length=40)
    gross_profit: str = Field(default="", alias="grossProfit", max_length=40)
    net_income: str = Field(default="", alias="netIncome", max_length=40)
    revenue_trend: Literal["growing", "stable", "uneven", "declining", ""] = Field(
        default="", alias="revenueTrend"
    )
    debt_burden: Literal[
        "under_10", "ten_twenty", "twenty_thirty", "over_30", ""
    ] = Field(default="", alias="debtBurden")
    cash_runway: Literal["under_1", "one_two", "two_three", "over_3", ""] = Field(
        default="", alias="cashRunway"
    )
    credit: Literal["strong", "fair", "rebuilding", "unknown", ""] = ""
    records: Literal["current", "partial", "behind", ""] = ""
    banking_behavior: Literal["clean", "occasional", "frequent", "unknown", ""] = Field(
        default="", alias="bankingBehavior"
    )
    collateral: Literal["strong", "some", "none", "unknown", ""] = ""
    funding_purpose: Literal[
        "working_capital",
        "equipment",
        "acquisition",
        "real_estate",
        "debt_refinance",
        "mca_refinance",
        "other",
        "",
    ] = Field(default="", alias="fundingPurpose")
    property_debt_service: str = Field(default="", alias="propertyDebtService", max_length=40)
    occupancy: str = Field(default="", max_length=40)

    @field_validator(
        "revenue",
        "gross_profit",
        "net_income",
        "property_debt_service",
        "occupancy",
    )
    @classmethod
    def finite_numeric_string_or_blank(cls, value: str) -> str:
        normalized = str(value).strip().replace(",", "").replace("$", "")
        if not normalized:
            return ""
        try:
            number = Decimal(normalized)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Enter a valid numeric amount") from exc
        if not number.is_finite():
            raise ValueError("Enter a finite numeric amount")
        return normalized


class CapitalReadinessDiagnosticResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    band: Literal["insufficient", "ready_soon", "three_six", "six_twelve", "one_plus"]
    score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    coverage: float = Field(ge=0, le=100, allow_inf_nan=False)
    grossMargin: float | None = Field(default=None, allow_inf_nan=False)
    netMargin: float | None = Field(default=None, allow_inf_nan=False)
    propertyDscr: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    occupancy: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)


class CapitalReadinessDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    source: Literal["capital_readiness_diagnostic"]
    verification_status: Literal["self_reported_unverified"]
    locale: Literal["en", "es"]
    answers: CapitalReadinessDiagnosticAnswers
    result: CapitalReadinessDiagnosticResult


class CapitalReadinessClaimRequest(BaseModel):
    """Consent-bound handoff from the public diagnostic after authentication."""

    idempotency_key: UUID
    capital_readiness_diagnostic: CapitalReadinessDiagnostic
    business_name: str = Field(min_length=1, max_length=180)
    phone: RequiredPhone
    full_name: str | None = Field(default=None, max_length=180)
    industry: str = Field(default="other", max_length=64)
    intent: str | None = Field(default=None, max_length=64)
    terms_accepted: bool
    privacy_accepted: bool
    terms_version: str = Field(default=TERMS_VERSION, max_length=32)
    privacy_version: str = Field(default=PRIVACY_VERSION, max_length=32)


class CapitalReadinessClaimResponse(BaseModel):
    intake_id: UUID
    profile_id: UUID
    created: bool
    communication_locale: Literal["en", "es"]
    readiness: CapitalReadinessRead


class DealerIntakeStart(PublicIntakeAttribution):
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: RequiredPhone
    business_name: str | None = Field(default=None, max_length=180)
    terms_accepted: bool = False
    privacy_accepted: bool = False
    terms_version: str = Field(default=TERMS_VERSION, max_length=32)
    privacy_version: str = Field(default=PRIVACY_VERSION, max_length=32)
    preferred_language: Language = Language.EN
    capital_readiness_diagnostic: CapitalReadinessDiagnostic | None = None
    requested_amount: float | None = Field(default=None, ge=0, le=100_000_000)
    equipment_details: EquipmentFinancingDetails | None = None

    @field_validator("business_name", "phone", mode="before")
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


class FundingReviewStart(PublicIntakeAttribution):
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: RequiredPhone
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
    capital_readiness_diagnostic: CapitalReadinessDiagnostic | None = None

    @field_validator("phone", "investor_name", "target_property_address", "transaction_type", "estimated_credit_tier", mode="before")
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


def _public_intake_attribution(
    payload: PublicIntakeAttribution,
) -> dict[str, str] | None:
    values = {
        key: value
        for key, value in payload.model_dump(
            include={
                "source",
                "page",
                "program",
                "vertical",
                "campaign",
                "cta",
                "funding_intent",
            }
        ).items()
        if value
    }
    return values or None


def _equipment_details_payload(
    details: EquipmentFinancingDetails | None,
) -> dict[str, Any] | None:
    if details is None:
        return None
    values = details.model_dump(exclude_none=True)
    return values or None


def _is_equipment_application(
    *, funding_intent: object | None, details: EquipmentFinancingDetails | None = None
) -> bool:
    return equipment_financing.is_equipment_funding_intent(funding_intent) or details is not None


def _is_business_equipment_application(payload: PublicIntakeAttribution) -> bool:
    """Route the dedicated public handoff without changing dealer equipment files.

    Both signals are required. A dealer can still request equipment and remain a
    dealer intake; only the public operating-business handoff becomes Main Street.
    """

    vertical = (
        str(payload.vertical or "")
        .strip()
        .casefold()
        .replace("-", "_")
        .replace(" ", "_")
    )
    return (
        vertical in {"business", "main_street", "mainstreet", "operating_business"}
        and payload.funding_intent == "equipment"
    )


def _is_main_street_equipment_intake(intake: PublicUnderwritingIntake) -> bool:
    if intake.variant != MAIN_STREET_VARIANT:
        return False
    state = _intake_state(intake)
    details = state.get("main_street_details")
    return (
        state.get("funding_intent") == "equipment"
        or isinstance(details, dict) and details.get("intent") == "equipment"
    )


def _dealer_route_public_path(intake: PublicUnderwritingIntake) -> str:
    return EQUIPMENT_PUBLIC_PATH if _is_main_street_equipment_intake(intake) else "/dealer-ai-underwriter"


def _dealer_route_welcome_back(intake: PublicUnderwritingIntake) -> str:
    if _is_main_street_equipment_intake(intake):
        return _equipment_welcome_back(intake.preferred_language)
    return _dealer_welcome_back(intake.preferred_language)


async def _configure_equipment_bucket(
    db: AsyncSession,
    bucket: Bucket,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    """Apply the canonical equipment context without creating another program."""

    bucket.purpose = "Equipment financing AI intake"
    bucket.ai_context = {
        **(bucket.ai_context or {}),
        "funding_program_key": "equipment_financing",
        "funding_intent": "equipment",
        "published_amount_range": {
            "minimum": equipment_financing.MINIMUM_AMOUNT,
            "maximum": equipment_financing.MAXIMUM_AMOUNT,
        },
        "published_term_range_months": {
            "minimum": equipment_financing.MINIMUM_TERM_MONTHS,
            "maximum": equipment_financing.MAXIMUM_TERM_MONTHS,
        },
        "equipment_detail_fields": list(equipment_financing.EQUIPMENT_DETAIL_FIELDS),
        **({"equipment_financing_details": details} if details else {}),
    }
    await _ensure_requested_document(
        db,
        bucket,
        name="Vendor quote or invoice for the equipment",
        category="Equipment",
        description=(
            "Upload the current vendor quote or invoice showing the seller, "
            "equipment description, purchase price, and expected delivery."
        ),
        allow_multiple_files=True,
    )


class AdminLeadCreate(BaseModel):
    """Super-admin creates an AI-underwriter lead on behalf of a client. Mirrors the
    public start schemas but with no terms/throttle and an explicit variant selector.
    The client can later log in with this email exactly like a self-serve lead."""

    # One of the keys in _ADMIN_VARIANT_CONSTANTS: dealer | real_estate |
    # main_street | mca_refinance.
    variant: str = Field(default="dealer")
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: RequiredPhone
    business_name: str | None = Field(default=None, max_length=180)
    loan_purpose: str | None = Field(default=None, max_length=255)
    # Main Street: an operating business is defined by what it does and what it
    # wants, and the program screen cannot run without both. Optional so the
    # other three variants are unaffected; normalized (never rejected) so an
    # unrecognized value falls back to "other"/"not_sure" rather than 422-ing a
    # lead the operator is trying to create.
    industry: str | None = Field(default=None, max_length=64)
    intent: str | None = Field(default=None, max_length=64)
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
    capital_readiness_diagnostic: CapitalReadinessDiagnostic | None = None
    equipment_details: EquipmentFinancingDetails | None = None
    # Optionally assign the file to a dealer partner at creation, so the team's
    # first message on it reaches that partner's channel. Must be a dealer_partner.
    broker_user_id: UUID | None = None
    secure_room_pin: str = Field(pattern=r"^[0-9]{6}$")

    @field_validator("variant", mode="before")
    @classmethod
    def normalize_variant(cls, value: object) -> object:
        return (str(value).strip().lower() if value else "dealer")

    @field_validator(
        "phone", "business_name", "loan_purpose", "investor_name", "target_property_address",
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
    phone: RequiredPhone
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


# What the file's timeline says when the firm's decision changes — the new
# outcome in plain words, never the reason.
_OUTCOME_EVENT_TITLES: dict[str, str] = {
    "closed": "File closed",
    "denied": "File denied",
    "submitted": "File reopened",
}


class LanguageUpdate(BaseModel):
    preferred_language: Language


class AdminLeadContactUpdate(BaseModel):
    """Editable operator-facing identity and request fields for one intake."""

    full_name: str | None = Field(default=None, min_length=1, max_length=180)
    email: EmailStr | None = None
    phone: OptionalPhone = None
    business_name: str | None = Field(default=None, max_length=180)
    loan_purpose: str | None = Field(default=None, max_length=255)
    requested_loan_amount: float | None = Field(default=None, ge=0)
    estimated_credit_score: int | None = Field(default=None, ge=300, le=850)
    referral_source: str | None = Field(default=None, max_length=180)

    @field_validator("phone", "business_name", "loan_purpose", "referral_source", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        return None if value == "" else value


class DealerLeadNoteCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    #: Uploads from this author to bind to the note once it exists.
    image_ids: list[UUID] = []


class AdminLeadFromBucketCreate(BaseModel):
    """Super-admin converts an EXISTING Bucket into an AI-underwriter lead — the
    admin already has a folder of files (collected some other way) and wants the
    AI audit + package-build against it, without a second, empty bucket being
    created. Mirrors AdminLeadCreate's client fields but has NO bucket-creation
    fields (no target_property_address / requested_amount / etc.) since the
    bucket, and whatever files/requested-docs are already on it, stay as-is."""

    # One of the keys in _ADMIN_VARIANT_CONSTANTS.
    variant: str = Field(default="dealer")
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: RequiredPhone
    business_name: str | None = Field(default=None, max_length=180)  # or investor name, real-estate
    # Main Street: an operating business is defined by what it does and what it
    # wants, and the program screen cannot run without both. Optional so the
    # other three variants are unaffected; normalized (never rejected) so an
    # unrecognized value falls back to "other"/"not_sure" rather than 422-ing a
    # lead the operator is trying to create.
    industry: str | None = Field(default=None, max_length=64)
    intent: str | None = Field(default=None, max_length=64)
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
    """Flags a lead for archival review and destroys nothing.

    The requesting broker's own list filters the lead out immediately; the
    admin list never does, so an operator can approve the separate,
    reversible archive action.
    """

    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return None if value == "" else value


class ConfirmLeadDeletionRequest(BaseModel):
    """The desk's confirmation for a reversible, evidence-retaining archive.

    Only a super admin or underwriter may perform it. The exact lead name
    remains a required server-side speed bump so a stale or scripted client
    cannot archive the wrong file by omitting the value.
    """

    confirm_name: str = Field(min_length=1, max_length=180)

    @field_validator("confirm_name")
    @classmethod
    def _confirmation_name_is_not_blank(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Confirmation name is required")
        return normalized


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
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    readiness: dict[str, Any] | None = None


class DealerIntakePatch(BaseModel):
    business_name: str | None = Field(default=None, max_length=180)
    phone: OptionalPhone = None
    loan_purpose: str | None = Field(default=None, max_length=255)
    requested_loan_amount: float | None = Field(default=None, ge=0)
    estimated_credit_score: int | None = Field(default=None, ge=300, le=850)
    referral_source: str | None = Field(default=None, max_length=180)
    asset_rows: list[DealerAssetRow] | None = None
    entity_structure: DealerEntityStructure | None = None
    funding_intent: FundingIntent | None = None
    equipment_details: EquipmentFinancingDetails | None = None

    @field_validator("business_name", "phone", "loan_purpose", "referral_source", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("funding_intent", mode="before")
    @classmethod
    def normalize_patch_funding_intent(cls, value: object) -> object:
        return normalize_lead_funding_intent(value)


class DealerChatRequest(BaseModel):
    message: str | None = Field(default=None, max_length=4000)
    updates: DealerIntakePatch | None = None
    # Client-thread replies always land in the secure portal. This optional
    # delivery flag mirrors the same text to the consented client number.
    also_sms: bool = False


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


class DealerResumeLinkRequest(PublicIntakeAttribution):
    email: EmailStr


class DealerResumeLinkResponse(BaseModel):
    ok: bool = True
    message: str = "If a matching secure intake exists, a resume link has been sent."


class DealerLoginStartRequest(PublicIntakeAttribution):
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
    # Typed-name + E-SIGN is intentionally allowed only for the deal-specific
    # Success Fee Agreement. Every other signable still requires a drawing.
    signature_data_url: str | None = Field(default=None, min_length=1)
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


class DealerFinancialStatementSave(BaseModel):
    """A Form 413 body from the client's own page.

    `submit` is the difference between keeping progress and finishing: the page
    autosaves without it and passes it when they press Save.
    """

    body: dict
    statement_date: str | None = None
    submit: bool = False


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
    preview_url: str | None = None
    version: int = 1
    status: Literal["current", "superseded"] = "current"
    bucket_file_id: UUID | None = None
    supersedes_artifact_id: UUID | None = None
    superseded_by_artifact_id: UUID | None = None
    sha256: str | None = None
    generation_id: UUID | None = None
    input_fingerprint: str | None = None
    is_fresh: bool | None = None
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


class PackageReadinessBucketFileRead(BaseModel):
    id: UUID
    bucket_id: UUID
    file_name: str
    content_type: str
    size_bytes: int
    source_kind: str | None = None
    source_detail: str | None = None
    status: str
    preview_url: str | None = None
    download_url: str | None = None
    created_at: datetime


class PackageReadinessGenerationRead(BaseModel):
    generation_id: UUID
    generated_at: datetime
    executive_summary: PublicUnderwritingArtifactRead
    lender_packet: PublicUnderwritingArtifactRead
    bucket_files: list[PackageReadinessBucketFileRead]
    superseded_bucket_file_ids: list[UUID] = Field(default_factory=list)
    artifact_history: list[PublicUnderwritingArtifactRead] = Field(default_factory=list)


class DealerIntakeRead(ORMModel):
    id: UUID
    client_id: UUID | None
    bucket_id: UUID
    bucket_name: str | None = None
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
    foreclosure_rescue_status: str | None = None
    foreclosure_sale_date: date | None = None
    client_contact_suppressed: bool = False
    referral_partner_company_id: UUID | None = None
    assigned_underwriter_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    retention_class: str = "ai_intake_record"
    protected_until: datetime | None = None
    retention_state: Literal[
        "legal_hold", "protected", "expired", "unclassified"
    ] = "unclassified"


class SecureRoomHandoffResponse(BaseModel):
    """Everything the browser needs to walk the client into their room.

    The code is returned once, to a caller that already proved it holds this
    intake, and is never emailed, logged or stored anywhere by this response.
    """

    room_url: str
    #: None when the client set their own PIN — theirs to remember, not ours to resend.
    room_code: str | None = None
    #: recovered | minted | client_chosen
    code_source: str
    tab: str = "todo"


async def _secure_room_handoff(
    db: AsyncSession, intake: PublicUnderwritingIntake, request: Request, *, tab: str
) -> SecureRoomHandoffResponse:
    """Hand a client from their intake into their secure room.

    The intake and the room are already the same bucket and the same link, so
    nothing is created here except the application profile the room needs to
    show banking and agreements at all. The room PIN is recovered rather than
    rotated: rotating would break a link staff emailed with the code read out
    separately, and would lock out a client who bookmarked the room.

    The client is already verified on this intake — by the token they hold or an
    emailed code — so handing them the room credential is not a widening of what
    they can reach. It is the same file, with actions attached.
    """
    _throttle_or_429(
        _ROOM_HANDOFF_LAST_BY_TOKEN,
        str(intake.id),
        _ROOM_HANDOFF_MIN_INTERVAL_SECONDS,
        "One moment — your secure room is opening.",
    )
    link = intake.bucket_upload_link
    if link is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This file has no secure room yet")

    await profiles_service.provision_profile_for_intake(db, intake)

    if link.passcode_set_by_client_at is not None:
        code, source = None, "client_chosen"
    else:
        code = client_room.read_passcode(link)
        source = "recovered"
        if code is None:
            code = _generate_passcode()
            client_room._store_passcode(link, code)
            source = "minted"

    await _log(
        db,
        intake.bucket_id,
        "secure_room_handoff",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="upload_link",
        target_id=str(link.id),
        detail=f"Client opened their secure room from the intake · code {source}",
    )
    await db.commit()
    return SecureRoomHandoffResponse(
        room_url=client_room.room_url(link.token), room_code=code, code_source=source, tab=tab
    )


class DealerIntakeResponse(BaseModel):
    intake: DealerIntakeRead
    mca_refinance_policy: dict[str, Any] | None = None
    communication_locale: Literal["en", "es"] = "en"
    capital_readiness: CapitalReadinessRead | None = None
    # Set while the desk has taken the conversation over: the room shows the
    # underwriter is replying instead of rendering assistant_message as an AI turn.
    ai_paused_until: datetime | None = None
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
    chat_actions: list[IntakeChatActionRead] = []
    artifacts: list[PublicUnderwritingArtifactRead] = []
    email_sends: list[PublicUnderwritingEmailSendRead] = []
    # Internal admin <-> dealer-partner notes thread. Populated only for the
    # admin_thread=True audience (admin cockpit + broker portal) — never sent
    # to the public/uploader client-facing response.
    notes: list[BucketNoteRead] = []
    secure_room_pin: str | None = None
    room_delivery_status: str | None = None
    room_delivery_detail: str | None = None
    # Set when the client owes a signature on a Production Package: the room
    # shows the signing screen and nothing else until it is signed.
    signing_gate: dict[str, Any] | None = None


class DealerAILeadRow(BaseModel):
    id: UUID
    mca_refinance_policy: dict[str, Any] | None = None
    variant: str
    client_id: UUID | None = None
    bucket_id: UUID
    bucket_name: str
    full_name: str
    email: str
    phone: str | None = None
    business_name: str | None = None
    referral_source: str | None = None
    opened_by_name: str | None = None
    opened_by_role: str | None = None
    status: str
    outcome_status: str = "submitted"
    # Canonical status from ApplicationProfile when one exists.  The legacy
    # intake ``status`` can describe review completion, so it must not by
    # itself be presented as proof that a deal funded.
    underwriting_status: str | None = None
    # ``closed`` is a lifecycle result and retains evidence access;
    # ``archived`` is an explicit operator archive and may be restored.
    workspace_state: Literal["active", "closed", "archived"] = "active"
    workspace_state_reason: str | None = None
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
    archived_at: datetime | None = None
    protected_until: datetime | None = None
    legal_hold: bool = False
    effective_legal_hold: bool = False
    retention_state: Literal[
        "legal_hold", "protected", "expired", "unclassified"
    ] = "unclassified"
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


INTAKE_NOTIFICATION_EVENT_DEFINITIONS: dict[str, tuple[str, str]] = {
    "intake_started": (
        "Intake started",
        "A new AI Intake file is created.",
    ),
    "file_uploaded": (
        "File uploaded",
        "A new evidence file is accepted into this intake.",
    ),
    "review_approved": (
        "Positive AI review",
        "The preliminary AI screen returns a positive result.",
    ),
    "review_denied": (
        "Negative AI review",
        "The preliminary AI screen returns a negative result.",
    ),
}
INTAKE_NOTIFICATION_DEFAULT_EVENTS = {
    "intake_started",
    "review_approved",
    "review_denied",
}
INTAKE_NOTIFICATION_ROUTING_STATE_KEY = "notification_routing"
INTAKE_NOTIFICATION_AUDIT_STATE_KEY = "notification_delivery_audit"
INTAKE_NOTIFICATION_ROUTING_VERSION = 1


class IntakeNotificationRule(BaseModel):
    enabled: bool = True
    email_enabled: bool = True
    in_app_enabled: bool = False
    to_user_ids: list[UUID] = Field(default_factory=list, max_length=50)
    cc_user_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @field_validator("to_user_ids", "cc_user_ids")
    @classmethod
    def unique_user_ids(cls, values: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_delivery_shape(self) -> IntakeNotificationRule:
        overlap = set(self.to_user_ids) & set(self.cc_user_ids)
        if overlap:
            raise ValueError("A recipient cannot be both a primary recipient and CC.")
        if self.enabled and not (self.email_enabled or self.in_app_enabled):
            raise ValueError("An enabled notification requires Email or In-app delivery.")
        if self.cc_user_ids and not self.email_enabled:
            raise ValueError("CC recipients require email delivery.")
        if self.enabled and self.email_enabled and self.cc_user_ids and not self.to_user_ids:
            raise ValueError("Choose at least one primary recipient before adding CC recipients.")
        return self


class IntakeNotificationRoutingUpdate(BaseModel):
    events: dict[str, IntakeNotificationRule]

    @field_validator("events")
    @classmethod
    def known_events_only(
        cls, value: dict[str, IntakeNotificationRule]
    ) -> dict[str, IntakeNotificationRule]:
        unknown = set(value) - set(INTAKE_NOTIFICATION_EVENT_DEFINITIONS)
        if unknown:
            raise ValueError(f"Unsupported notification event(s): {', '.join(sorted(unknown))}")
        missing = set(INTAKE_NOTIFICATION_EVENT_DEFINITIONS) - set(value)
        if missing:
            raise ValueError(f"Missing notification event(s): {', '.join(sorted(missing))}")
        return value


class IntakeNotificationUserRead(BaseModel):
    user_id: UUID
    name: str
    email: str
    role: str
    relations: list[str] = Field(default_factory=list)


class IntakeNotificationEventRead(BaseModel):
    key: str
    label: str
    description: str
    rule: IntakeNotificationRule


class IntakeNotificationRoutingRead(BaseModel):
    intake_id: UUID
    version: int = INTAKE_NOTIFICATION_ROUTING_VERSION
    uses_default: bool
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None
    default_fallback_email: str | None = None
    candidates: list[IntakeNotificationUserRead]
    events: list[IntakeNotificationEventRead]


def _now() -> datetime:
    return datetime.now(UTC)


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
    # The queue reads these columns; the JSON stays the full delivery audit.
    intake.last_link_sent_at = _now()
    intake.last_link_status = result.detail
    intake.last_link_kind = "resume"
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
    # The queue reads these columns; the JSON stays the full delivery audit.
    intake.last_link_sent_at = _now()
    intake.last_link_status = result.detail
    intake.last_link_kind = "login"
    return record


def _admin_lead_url(intake: PublicUnderwritingIntake) -> str:
    return _public_url(f"/admin/ai-underwriter-leads?lead={intake.id}")


def _admin_bucket_url(intake: PublicUnderwritingIntake) -> str:
    return _public_url(f"/buckets?bucket={intake.bucket_id}")


def _dealer_label(intake: PublicUnderwritingIntake) -> str:
    return intake.business_name or intake.full_name or intake.email or "Dealer AI lead"


async def _intake_notification_candidates(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
) -> list[IntakeNotificationUserRead]:
    """Return only active users who are allowed to receive this file's updates.

    Desk operators can always receive an intake update. External/Field Desk
    users are eligible only when the intake itself links them as the source,
    broker, underwriter, or a member of the linked referral company. This keeps
    the CC picker useful without turning it into an arbitrary-address data leak.
    """

    direct_relations: dict[UUID, set[str]] = {}

    def relate(user_id: UUID | None, relation: str) -> None:
        if user_id is not None:
            direct_relations.setdefault(user_id, set()).add(relation)

    relate(intake.assigned_underwriter_user_id, "Assigned underwriter")
    relate(intake.source_user_id, "Originating agent")
    relate(intake.broker_id, "Broker / dealer partner")

    rows = (
        await db.execute(
            select(User).where(
                User.deleted_at.is_(None),
                or_(User.account_status.is_(None), User.account_status == "active"),
                or_(
                    User.role.in_([Role.SUPER_ADMIN.value, Role.LOAN_EXEC.value]),
                    User.id.in_(set(direct_relations) or {UUID(int=0)}),
                    *(
                        [
                            User.referral_partner_company_id
                            == intake.referral_partner_company_id
                        ]
                        if intake.referral_partner_company_id is not None
                        else []
                    ),
                ),
            )
        )
    ).scalars().all()

    candidates: list[IntakeNotificationUserRead] = []
    for row in rows:
        relations = set(direct_relations.get(row.id, set()))
        role_value = row.role.value if hasattr(row.role, "value") else str(row.role)
        if role_value == Role.SUPER_ADMIN.value:
            relations.add("Super Admin")
        elif role_value == Role.LOAN_EXEC.value:
            relations.add("Loan Executive")
        if (
            intake.referral_partner_company_id is not None
            and row.referral_partner_company_id == intake.referral_partner_company_id
        ):
            relations.add("Referral team")
        # A client/vendor/lender row is never an internal notification target,
        # even if historical data accidentally links it to a partner company.
        if role_value in {Role.CLIENT.value, Role.VENDOR.value, Role.LENDER.value}:
            continue
        email = (row.email or "").strip().lower()
        if not email or "@" not in email:
            continue
        candidates.append(
            IntakeNotificationUserRead(
                user_id=row.id,
                name=(row.name or email).strip(),
                email=email,
                role=role_value,
                relations=sorted(relations),
            )
        )
    return sorted(candidates, key=lambda item: (item.name.casefold(), item.email))


def _default_intake_notification_rules(
    candidates: list[IntakeNotificationUserRead],
) -> dict[str, IntakeNotificationRule]:
    super_admin_ids = [
        candidate.user_id
        for candidate in candidates
        if candidate.role == Role.SUPER_ADMIN.value
    ]
    return {
        event_key: IntakeNotificationRule(
            enabled=event_key in INTAKE_NOTIFICATION_DEFAULT_EVENTS,
            email_enabled=event_key in INTAKE_NOTIFICATION_DEFAULT_EVENTS,
            in_app_enabled=False,
            to_user_ids=super_admin_ids
            if event_key in INTAKE_NOTIFICATION_DEFAULT_EVENTS
            else [],
            cc_user_ids=[],
        )
        for event_key in INTAKE_NOTIFICATION_EVENT_DEFINITIONS
    }


def _stored_intake_notification_rules(
    intake: PublicUnderwritingIntake,
    candidates: list[IntakeNotificationUserRead],
) -> tuple[dict[str, IntakeNotificationRule], bool, datetime | None, UUID | None]:
    state = _intake_state(intake)
    raw = state.get(INTAKE_NOTIFICATION_ROUTING_STATE_KEY)
    if not isinstance(raw, dict) or raw.get("version") != INTAKE_NOTIFICATION_ROUTING_VERSION:
        return _default_intake_notification_rules(candidates), True, None, None
    raw_events = raw.get("events")
    if not isinstance(raw_events, dict):
        return _default_intake_notification_rules(candidates), True, None, None
    defaults = _default_intake_notification_rules(candidates)
    eligible_ids = {candidate.user_id for candidate in candidates}
    rules: dict[str, IntakeNotificationRule] = {}
    for event_key in INTAKE_NOTIFICATION_EVENT_DEFINITIONS:
        try:
            stored_rule = IntakeNotificationRule.model_validate(
                raw_events.get(event_key)
            )
            # A user can be deactivated or detached from the file after the
            # routing was saved. Do not expose an invisible/stale ID that the
            # operator cannot remove and the PUT endpoint must reject.
            rules[event_key] = stored_rule.model_copy(
                update={
                    "to_user_ids": [
                        user_id
                        for user_id in stored_rule.to_user_ids
                        if user_id in eligible_ids
                    ],
                    "cc_user_ids": [
                        user_id
                        for user_id in stored_rule.cc_user_ids
                        if user_id in eligible_ids
                    ],
                }
            )
            # Relationship and account-status changes can remove every
            # previously selected primary recipient.  Return a safe Off rule
            # instead of presenting an enabled route that cannot deliver.
            # The stored audit/config remains intact so the change is still
            # explainable and an operator can select a current recipient.
            if rules[event_key].enabled and not rules[event_key].to_user_ids:
                rules[event_key] = rules[event_key].model_copy(
                    update={"enabled": False, "cc_user_ids": []}
                )
        except Exception:  # noqa: BLE001
            rules[event_key] = defaults[event_key]
    updated_at = None
    if raw.get("updated_at"):
        try:
            updated_at = datetime.fromisoformat(str(raw["updated_at"]))
        except ValueError:
            updated_at = None
    updated_by_user_id = None
    if raw.get("updated_by_user_id"):
        try:
            updated_by_user_id = UUID(str(raw["updated_by_user_id"]))
        except ValueError:
            updated_by_user_id = None
    return rules, False, updated_at, updated_by_user_id


def _notification_routing_read(
    intake: PublicUnderwritingIntake,
    candidates: list[IntakeNotificationUserRead],
) -> IntakeNotificationRoutingRead:
    rules, uses_default, updated_at, updated_by_user_id = (
        _stored_intake_notification_rules(intake, candidates)
    )
    return IntakeNotificationRoutingRead(
        intake_id=intake.id,
        uses_default=uses_default,
        updated_at=updated_at,
        updated_by_user_id=updated_by_user_id,
        default_fallback_email=(
            get_settings().primary_super_admin_email.strip().lower()
            if uses_default
            and not any(
                rule.to_user_ids
                for rule in rules.values()
                if rule.enabled and rule.email_enabled
            )
            and "@" in get_settings().primary_super_admin_email
            else None
        ),
        candidates=candidates,
        events=[
            IntakeNotificationEventRead(
                key=key,
                label=definition[0],
                description=definition[1],
                rule=rules[key],
            )
            for key, definition in INTAKE_NOTIFICATION_EVENT_DEFINITIONS.items()
        ],
    )


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


async def _dispatch_intake_notification(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    event_type: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> list[dict[str, Any]]:
    candidates = await _intake_notification_candidates(db, intake)
    rules, uses_default, _, _ = _stored_intake_notification_rules(intake, candidates)
    rule = rules[event_type]
    if not rule.enabled:
        return [{"status": "disabled", "event_type": event_type}]

    by_id = {candidate.user_id: candidate for candidate in candidates}
    to_users = [by_id[user_id] for user_id in rule.to_user_ids if user_id in by_id]
    cc_users = [by_id[user_id] for user_id in rule.cc_user_ids if user_id in by_id]
    # CC is an email-envelope concept.  In-app notifications go only to TO
    # recipients so a user selected only as an email copy is not silently
    # subscribed to application alerts as well.
    in_app_user_ids = {user.user_id for user in to_users}
    notification_by_user: dict[UUID, Notification] = {}
    if rule.in_app_enabled and in_app_user_ids:
        created = await notifications.notify_users(
            db,
            recipient_ids=in_app_user_ids,
            event_type=f"dealer_ai_{event_type}",
            category="ai_intake",
            priority="high" if event_type in {"review_approved", "review_denied"} else "medium",
            title=subject,
            body=body_text.split("\n", 1)[0],
            target_type="dealer_ai_intake",
            target_id=str(intake.id),
            deep_link=_admin_lead_url(intake),
            meta={"intake_id": str(intake.id), "event_type": event_type},
            email=False,
            push=False,
        )
        notification_by_user = {row.recipient_user_id: row for row in created}

    fallback_to_emails: list[str] = []
    if rule.email_enabled and not to_users and uses_default:
        fallback = get_settings().primary_super_admin_email.strip().lower()
        if fallback and "@" in fallback:
            fallback_to_emails.append(fallback)

    result = None
    if rule.email_enabled and (to_users or fallback_to_emails):
        result = await asyncio.to_thread(
            send_raw_email,
            to_emails=list(
                dict.fromkeys([*(user.email for user in to_users), *fallback_to_emails])
            ),
            cc_emails=list(dict.fromkeys(user.email for user in cc_users)),
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )

    sent_at = _now().isoformat()
    records: list[dict[str, Any]] = []
    for recipient_type, users in (("to", to_users), ("cc", cc_users)):
        for recipient in users:
            notification = notification_by_user.get(recipient.user_id)
            records.append(
                {
                    "user_id": str(recipient.user_id),
                    "recipient": recipient.email,
                    "recipient_type": recipient_type,
                    "role": recipient.role,
                    "in_app_notification_id": str(notification.id)
                    if notification is not None
                    else None,
                    "email_enabled": rule.email_enabled,
                    "email_ok": result.ok if result is not None else None,
                    "email_status": result.detail if result is not None else "not_enabled",
                    "message_id": result.message_id if result is not None else None,
                    "sent_at": sent_at,
                }
            )
    for email in fallback_to_emails:
        records.append(
            {
                "user_id": None,
                "recipient": email,
                "recipient_type": "to",
                "role": "configured_super_admin_fallback",
                "in_app_notification_id": None,
                "email_enabled": True,
                "email_ok": result.ok if result is not None else None,
                "email_status": result.detail if result is not None else "not_enabled",
                "message_id": result.message_id if result is not None else None,
                "sent_at": sent_at,
            }
        )
    if not records:
        records.append({"status": "no_active_recipients", "event_type": event_type})
    return records


async def _record_intake_notification_once(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    event_type: str,
    event_key: str,
    subject: str,
    body_text: str,
    body_html: str,
    request: Request | None,
) -> dict[str, Any]:
    """Claim, dispatch and audit one event with at-most-once provider calls."""

    locked = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == intake.id)
            .with_for_update()
        )
    ).scalar_one()
    await db.refresh(locked, attribute_names=["intake_state"])
    intake = locked
    state = _intake_state(intake)
    audit = state.get(INTAKE_NOTIFICATION_AUDIT_STATE_KEY)
    if not isinstance(audit, dict):
        audit = {}
    existing = audit.get(event_key)
    if isinstance(existing, dict):
        return existing

    record: dict[str, Any] = {
        "event_type": event_type,
        "status": "dispatching",
        "claimed_at": _now().isoformat(),
        **(_request_audit(request) if request is not None else {}),
    }
    audit[event_key] = record
    state[INTAKE_NOTIFICATION_AUDIT_STATE_KEY] = audit
    intake.intake_state = state
    # Commit the claim before calling SES. A worker crash may require manual
    # retry, but it cannot create a duplicate email by replaying this event.
    await db.commit()

    dispatch_error: Exception | None = None
    try:
        deliveries = await _dispatch_intake_notification(
            db,
            intake,
            event_type=event_type,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
    except Exception as exc:  # noqa: BLE001
        dispatch_error = exc
        deliveries = [{"status": "failed", "error": str(exc)[:500]}]
    locked = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == intake.id)
            .with_for_update()
        )
    ).scalar_one()
    await db.refresh(locked, attribute_names=["intake_state"])
    intake = locked
    record = {
        **record,
        "status": "failed" if dispatch_error is not None else "completed",
        "completed_at": _now().isoformat(),
        "deliveries": deliveries,
    }
    state = _intake_state(intake)
    audit = state.get(INTAKE_NOTIFICATION_AUDIT_STATE_KEY)
    if not isinstance(audit, dict):
        audit = {}
    audit[event_key] = record
    # This map is also the permanent at-most-once claim ledger.  Never trim it:
    # replaying an old upload completion must not be able to send again merely
    # because newer uploads pushed its audit entry out of a display window.
    state[INTAKE_NOTIFICATION_AUDIT_STATE_KEY] = audit
    intake.intake_state = state
    await db.commit()
    if dispatch_error is not None:
        raise dispatch_error
    return record


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
    equipment_intake = _is_main_street_equipment_intake(intake)
    intake_label = "Equipment Financing intake" if equipment_intake else "Dealer AI intake"
    subject = f"{intake_label} started: {label}"
    body_text = (
        f"A {'business' if equipment_intake else 'dealer'} started a Qualified Commercial {'Equipment Financing' if equipment_intake else 'AI funding'} review.\n\n"
        f"Business: {_email_line(intake.business_name)}\n"
        f"Contact: {_email_line(intake.full_name)}\n"
        f"Email: {_email_line(intake.email)}\n"
        f"Phone: {_email_line(intake.phone)}\n"
        f"Bucket: {_email_line(intake.bucket.name if intake.bucket else intake.bucket_id)}\n\n"
        f"Review lead: {lead_url}\n"
        f"Open bucket: {bucket_url}\n"
    )
    body_html = (
        f"<p>A {'business' if equipment_intake else 'dealer'} started a Qualified Commercial {'Equipment Financing' if equipment_intake else 'AI funding'} review.</p>"
        "<ul>"
        f"<li><strong>Business:</strong> {html.escape(_email_line(intake.business_name))}</li>"
        f"<li><strong>Contact:</strong> {html.escape(_email_line(intake.full_name))}</li>"
        f"<li><strong>Email:</strong> {html.escape(_email_line(intake.email))}</li>"
        f"<li><strong>Phone:</strong> {html.escape(_email_line(intake.phone))}</li>"
        f"<li><strong>Bucket:</strong> {html.escape(_email_line(intake.bucket.name if intake.bucket else intake.bucket_id))}</li>"
        "</ul>"
        f'<p><a href="{html.escape(lead_url)}">Review {"equipment financing" if equipment_intake else "dealer AI"} lead</a></p>'
        f'<p><a href="{html.escape(bucket_url)}">Open bucket</a></p>'
    )
    try:
        record = await _record_intake_notification_once(
            db,
            intake,
            event_type="intake_started",
            event_key="intake_started",
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            request=request,
        )
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == intake.id)
            .with_for_update()
        )
        await db.refresh(intake, attribute_names=["intake_state"])
        state = _intake_state(intake)
        state["super_admin_intake_started_email"] = {
            "type": "equipment_financing_intake_started" if equipment_intake else "dealer_ai_intake_started",
            "sent_at": record.get("completed_at") or record.get("claimed_at"),
            "deliveries": record.get("deliveries", []),
            **_request_audit(request),
        }
        intake.intake_state = state
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("dealer_ai_intake: super-admin start notification failed intake=%s: %s", intake.id, exc)


async def _record_super_admin_decision_notification(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    result: dict[str, Any],
    *,
    request: Request | None,
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
        event_type = "review_approved" if decision == "approved" else "review_denied"
        record = await _record_intake_notification_once(
            db,
            intake,
            event_type=event_type,
            event_key=event_type,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            request=request,
        )
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == intake.id)
            .with_for_update()
        )
        await db.refresh(intake, attribute_names=["intake_state"])
        state = _intake_state(intake)
        notifications = state.get("super_admin_decision_emails")
        if not isinstance(notifications, dict):
            notifications = {}
        notifications[decision] = {
            "type": f"dealer_ai_{decision}",
            "review_id": str(review_id) if review_id else None,
            "probability_status": result.get("probability_status"),
            "sent_at": record.get("completed_at") or record.get("claimed_at"),
            "deliveries": record.get("deliveries", []),
            **(_request_audit(request) if request is not None else {}),
        }
        state["super_admin_decision_emails"] = notifications
        intake.intake_state = state
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("dealer_ai_intake: super-admin decision notification failed intake=%s decision=%s: %s", intake.id, decision, exc)


async def _record_file_uploaded_notification(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    file: BucketFile,
    *,
    actor_name: str,
    request: Request,
) -> None:
    label = _dealer_label(intake)
    lead_url = _admin_lead_url(intake)
    subject = f"New AI Intake file: {label}"
    body_text = (
        f"{actor_name} uploaded evidence to {label}.\n\n"
        f"File: {file.file_name}\n"
        f"Status: {file.status}\n\n"
        f"Review intake: {lead_url}\n"
    )
    body_html = (
        f"<p>{html.escape(actor_name)} uploaded evidence to "
        f"<strong>{html.escape(label)}</strong>.</p>"
        "<ul>"
        f"<li><strong>File:</strong> {html.escape(file.file_name)}</li>"
        f"<li><strong>Status:</strong> {html.escape(file.status)}</li>"
        "</ul>"
        f'<p><a href="{html.escape(lead_url)}">Review AI Intake file</a></p>'
    )
    try:
        await _record_intake_notification_once(
            db,
            intake,
            event_type="file_uploaded",
            event_key=f"file_uploaded:{file.id}",
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            request=request,
        )
    except Exception as exc:  # noqa: BLE001
        # The evidence was already validated and accepted. Notification
        # delivery must never turn a successful upload into a failed upload.
        log.warning(
            "dealer_ai_intake: file notification failed intake=%s file=%s: %s",
            intake.id,
            file.id,
            exc,
        )


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


async def _load_intake_by_dealer_session(db: AsyncSession, session_token: str, *, allow_pending_signing: bool = False) -> tuple[PublicUnderwritingIntake, DealerIntakeLoginChallenge]:
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
    if not allow_pending_signing:
        await _enforce_production_gate(db, challenge.intake)
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
        if doc.required and is_open_request(doc) and doc.id not in uploaded
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
    actor: Any = None,
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
    await file_events.emit(
        db,
        bucket_id=bucket.id,
        kind="document.requested",
        visibility=file_events.VISIBILITY_CLIENT,
        title=f"We asked for {name}",
        actor=actor,
        target_type="requested_document",
        target_id=doc.id,
    )
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


def _merchant_processing_offer(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """The client-safe summary of the file's merchant-processing offer,
    written by services/merchant_processing.sync_intake_state on every
    status change — the same intake_state pattern as loan_program_fit,
    because these context builders are synchronous and hold only the
    intake. Nothing from the offer's desk-only terms is ever in it."""
    raw = _intake_state(intake).get("merchant_processing_offer")
    return raw if isinstance(raw, dict) else {}


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
    equipment_financing_intent = (
        details.get("financing_equipment_or_vehicle") is True
        or equipment_financing.is_equipment_funding_intent(
            _intake_state(intake).get("funding_intent")
        )
    )
    equipment_fit = equipment_financing.assess_equipment_fit(
        {
            "equipment_financing_intent": True if equipment_financing_intent else False,
            "business_age_years": details.get("years_in_business"),
            "annual_revenue": revenue,
            "credit_score": intake.estimated_credit_score,
            "cash_flow": cash_flow,
            "requested_amount": requested_amount,
        }
    )
    if not _has_uploaded_doc_name(intake, "vendor quote") and not _has_uploaded_doc_name(
        intake, "invoice"
    ):
        equipment_fit["needs"] = [
            *equipment_fit["needs"],
            "A vendor quote or invoice for the equipment",
        ]
        equipment_fit["evidence_complete"] = False
    else:
        equipment_fit["evidence_complete"] = True
    equipment_fit["cash_flow"] = cash_flow
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
        "equipment_financing": equipment_fit,
        "merchant_processing": merchant_processing,
        "transportation_factoring": transportation_factoring,
        "debt_consulting": debt_consulting,
    }


def _compute_main_street_fit(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    """The operating-business screen, from the same inputs the dealer one uses.

    An operating business is not a dealer file. Running the dealer screen over it
    returns programs that do not apply — floorplan, reinsurance-backed, jumbo
    DSCR — and a fundability verdict measured against dealer criteria. This
    routes those files to app/services/main_street_programs.py, which screens the
    Main Street catalogue against the lenders' actual published sheets.

    Intent and industry come from the intake rather than being inferred: they are
    what decide whether this is even a lending file. A point-of-sale enquiry has
    no programs to screen and should not be told it failed any.
    """
    from app.services.main_street_programs import compute_main_street_program_fit

    details = _main_street_details(intake)

    # The relationship is eagerly loaded on the paths that matter, but not on
    # every one; an unloaded relationship must degrade to "no documents yet"
    # rather than raise inside a screen that runs on every chat turn.
    try:
        bucket = intake.bucket
        docs = [
            {"name": d.name, "status": d.status}
            for d in (bucket.requested_documents if bucket is not None else [])
        ]
    except Exception:  # noqa: BLE001 - lazy-load boundary, not a logic error
        docs = []

    km = dict(_key_metrics(intake))
    # The Main Street screen reads the requested amount from key metrics; on this
    # path it lives on the intake row instead.
    if km.get("requested_amount") is None and intake.requested_loan_amount is not None:
        km["requested_amount"] = float(intake.requested_loan_amount)

    return compute_main_street_program_fit(
        intent=details.get("intent"),
        industry=details.get("industry"),
        key_metrics=km,
        details=details,
        documents=docs,
    )


def _program_labels_for(intake: PublicUnderwritingIntake) -> dict[str, str]:
    """Labels matching whichever screen ran, so keys never render raw."""
    if intake.variant == MAIN_STREET_VARIANT:
        from app.services.main_street_programs import MAIN_STREET_PROGRAM_LABELS

        return MAIN_STREET_PROGRAM_LABELS
    return PROGRAM_LABELS


def _apply_loan_program_fit(intake: PublicUnderwritingIntake) -> None:
    """Recomputes and writes intake_state["loan_program_fit"]. Cheap and
    idempotent — safe to call on every chat turn.

    Which screen runs is decided by variant. Both write the same state key, so
    every downstream consumer keeps working; what changes is that an operating
    business is no longer measured against dealer programs.
    """
    state = _intake_state(intake)
    if intake.variant == MAIN_STREET_VARIANT:
        state["loan_program_fit"] = _compute_main_street_fit(intake)
    else:
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
    elif intake.variant == MAIN_STREET_VARIANT:
        _merge_main_street_details(intake, proposed)
        _apply_loan_program_fit(intake)
    elif intake.variant == MCA_VARIANT:
        _merge_mca_details(intake, proposed)
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
    "years_in_business",
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
        if key == "years_in_business":
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 250:
                accepted[key] = float(value)
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
        if _is_package_readiness_output(file):
            continue
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
        if not any(
            not _is_package_readiness_output(file)
            for file in _active_files(intake.bucket)
        ):
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
        "merchant_processing_offer": _merchant_processing_offer(intake) or None,
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


def _merge_main_street_details(
    intake: PublicUnderwritingIntake, proposed: Any
) -> dict[str, Any]:
    """Validate conversational facts without converting this file into a dealer.

    The Main Street prompt emits a different fact shape from the dealer prompt.
    Persisting it through ``_merge_dealer_details`` silently discarded industry,
    credit, and operating-business facts and created a dealer_details object.
    """

    if not isinstance(proposed, dict):
        return {}
    from app.services.main_street_programs import MAIN_STREET_INDUSTRIES, MAIN_STREET_INTENTS

    accepted: dict[str, Any] = {}
    if isinstance(proposed.get("industry"), str) and proposed["industry"].strip() in MAIN_STREET_INDUSTRIES:
        accepted["industry"] = proposed["industry"].strip()
    if isinstance(proposed.get("intent"), str) and proposed["intent"].strip() in MAIN_STREET_INTENTS:
        accepted["intent"] = proposed["intent"].strip()
    for key in ("owns_operating_property", "financing_equipment_or_vehicle", "franchise"):
        if isinstance(proposed.get(key), bool):
            accepted[key] = proposed[key]
    years = proposed.get("years_in_business")
    if isinstance(years, (int, float)) and not isinstance(years, bool) and 0 <= years <= 250:
        accepted["years_in_business"] = float(years)
    requested = proposed.get("requested_amount")
    if isinstance(requested, (int, float)) and not isinstance(requested, bool) and 0 < requested <= 500_000_000:
        accepted["requested_amount"] = float(requested)
        intake.requested_loan_amount = float(requested)
    credit = proposed.get("estimated_credit_score")
    if isinstance(credit, (int, float)) and not isinstance(credit, bool) and 300 <= credit <= 850:
        accepted["estimated_credit_score"] = int(credit)
        intake.estimated_credit_score = int(credit)

    if not accepted:
        return {}
    state = _intake_state(intake)
    existing = dict(state.get("main_street_details") or {})
    # The dedicated equipment route owns its structured intent. Chat can add
    # facts but cannot silently reclassify the application to another product.
    if _is_main_street_equipment_intake(intake):
        accepted["intent"] = "equipment"
        accepted["financing_equipment_or_vehicle"] = True
    existing.update(accepted)
    state["main_street_details"] = existing
    intake.intake_state = state
    return accepted


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
        "equipment_financing_details": (
            state.get("equipment_financing_details")
            if isinstance(state.get("equipment_financing_details"), dict)
            else None
        ),
        "merchant_processing_offer": _merchant_processing_offer(intake) or None,
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
    if intent == "equipment":
        context["underwriting_focus"] = (
            "Equipment Financing is already selected. Review the stated asset, condition, age, "
            "remaining useful life, seller, purchase price, delivery timing and intended business "
            "use together with the vendor quote and operating-business cash flow. Treat a missing "
            "fact as a clear information request, not an automatic rejection, and keep the file in "
            "the Main Street program set rather than applying dealer framing."
        )
        context["custom_instructions"] = (
            "This is a dedicated Equipment Financing intake for an operating business. The product "
            "choice and any structured equipment_financing_details in context are authoritative and "
            "must not be re-inferred. Confirm the vendor quote supports the seller, equipment, price "
            "and delivery, then screen the business evidence. Never promise pricing, timing, approval, "
            "zero down, private-party eligibility, or a credit-score exception."
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
    # Only the shared client/uploader thread belongs in a lender-facing package.
    # Internal admin AI threads are private per operator. Explicit operator facts
    # are promoted into intake_state.chat_facts when written and are consumed by
    # the authoritative-facts resolver below, without exposing private dialogue.
    rows = (
        await db.execute(
            select(BucketAIMessage)
            .where(
                BucketAIMessage.bucket_id == intake.bucket_id,
                BucketAIMessage.audience == "uploader",
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
    attribution = _public_intake_attribution(payload)
    is_business_equipment = _is_business_equipment_application(payload)
    source_channel = "main_street_intake" if is_business_equipment else "dealer_ai_intake"
    # Email match is inherently fragile (same email can belong to more than one
    # client record). Harden the selection: among same-email candidates, prefer
    # one already originated from a dealer AI intake so a new intake reuses the
    # dealer-intake client instead of hijacking an unrelated agent-book client;
    # otherwise fall back to the most recent.
    candidates = (
        await db.execute(select(Client).where(Client.email == email).order_by(Client.created_at.desc()))
    ).scalars().all()
    client = next((c for c in candidates if c.source_channel == source_channel), None) or (
        candidates[0] if candidates else None
    )
    owner = await primary_super_admin(db)
    if client is None:
        client = Client(
            name=payload.full_name.strip(),
            email=email,
            phone=payload.phone,
            referral_source=source_channel,
            originating_agent_id=owner.id if owner else None,
            current_agent_id=owner.id if owner else None,
            source_channel=source_channel,
            lead_source="other",
            lead_temperature="warm",
            financing_support_needed="yes",
            relationship_context="new_lead",
            client_experience_mode="self_directed",
            client_experience_mode_reason=source_channel,
            client_experience_mode_locked_by="firm",
            lead_intake={
                "source": source_channel,
                "business_name": payload.business_name,
                **({"lead_type": "main_street", "funding_intent": "equipment"} if is_business_equipment else {}),
                **({"signup_attribution": attribution} if attribution else {}),
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
        client.source_channel = source_channel
    if not client.referral_source:
        client.referral_source = source_channel
    intake = dict(client.lead_intake or {})
    intake.update({"source": source_channel, "business_name": payload.business_name or intake.get("business_name")})
    if is_business_equipment:
        intake.update({"lead_type": "main_street", "funding_intent": "equipment"})
    if attribution:
        intake["signup_attribution"] = attribution
    client.lead_intake = intake
    await db.flush()
    return client


async def _find_or_create_funding_client(db: AsyncSession, payload: FundingReviewStart) -> Client:
    email = _normalize_email(str(payload.email))
    client = (await db.execute(select(Client).where(Client.email == email).order_by(Client.created_at.desc()))).scalars().first()
    owner = await primary_super_admin(db)
    attribution = _public_intake_attribution(payload)
    lead_payload = {
        "source": "funding_review",
        "investor_name": payload.investor_name,
        "target_property_address": payload.target_property_address,
        "transaction_type": payload.transaction_type,
        "requested_amount": payload.requested_amount,
        "estimated_value_or_purchase_price": payload.estimated_value_or_purchase_price,
        "monthly_rent": payload.monthly_rent,
        "estimated_credit_tier": payload.estimated_credit_tier,
        **({"signup_attribution": attribution} if attribution else {}),
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


async def _create_bucket_for_intake(
    db: AsyncSession,
    client: Client,
    payload: DealerIntakeStart,
    request: Request,
    *,
    room_pin: str | None = None,
) -> tuple[Bucket, BucketUploadLink]:
    owner = await primary_super_admin(db)
    bucket = Bucket(
        name=f"{payload.business_name or payload.full_name} Dealer AI Intake",
        bucket_type="dealer_ai_intake",
        client_name=payload.business_name or payload.full_name,
        name_sync_mode="linked",
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
    passcode = room_pin or _generate_passcode()
    link = BucketUploadLink(
        bucket_id=bucket.id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.full_name.strip(),
        recipient_email=client.email,
        allow_notes=True,
        allow_multiple_sessions=True,
        can_use_ai_chat=True,
        can_view_ai_tasks=True,
    )
    # The room PIN has to be recoverable, or a client can never be handed into
    # their own room: only the hash was stored here, so the code was discarded.
    client_room._store_passcode(link, passcode)
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


async def _create_bucket_for_main_street(
    db: AsyncSession,
    client: Client,
    payload: DealerIntakeStart,
    request: Request,
    *,
    intent: str,
    industry: str,
    room_pin: str | None = None,
) -> tuple[Bucket, BucketUploadLink]:
    """An operating-business file, with the documents its intent actually calls for.

    This exists because routing a Main Street lead through
    ``_create_bucket_for_intake`` gave it a dealer bucket: the dealer checklist,
    the dealer AI framing ("dealer financing with real estate collateral") and
    the dealer program screen. The welcome email was already variant-aware, so a
    restaurant owner was emailed one document list and shown a different one.

    Intent decides the package, not a fixed list. ``business_systems`` seeds no
    documents at all — someone asking about point-of-sale software is having a
    qualification conversation, and demanding two years of tax returns is both
    wrong and a good way to lose them. ``documents_for`` owns that mapping; this
    function only persists what it returns.
    """
    from app.services.main_street_programs import (
        MAIN_STREET_INDUSTRIES,
        MAIN_STREET_INTENTS,
        documents_for,
        intent_kind,
    )

    owner = await primary_super_admin(db)
    kind = intent_kind(intent)
    industry_label = MAIN_STREET_INDUSTRIES[industry]["en"]
    intent_label = MAIN_STREET_INTENTS[intent]["en"]
    display = payload.business_name or payload.full_name

    bucket = Bucket(
        name=f"{display} Main Street Intake",
        bucket_type="main_street_intake",
        client_name=display,
        name_sync_mode="linked",
        purpose="Operating business funding review",
        description=(
            f"Public AI intake for an operating business. Industry: {industry_label}. "
            f"Looking for: {intent_label}."
        ),
        ai_context={
            "review_type": MAIN_STREET_VARIANT,
            "screening_stage": "stage_1_bankability",
            "deal_type": "operating business funding review",
            "documentation_level": "preliminary operating-business screen",
            "collateral_type": "business cash flow",
            "intent": intent,
            "intent_kind": kind,
            "industry": industry,
            "industry_label": industry_label,
            "client_email": client.email,
            # A non-lending intent has no bankability bar to clear, so it gets no
            # required-items list to be measured against.
            "stage_1_required_items": (
                [
                    "last 6 months main operating bank statements",
                    "last 2 years business tax returns (or current-year extension filing)",
                    "current year/YTD P&L and balance sheet",
                    "requested amount",
                    "detailed use of funds",
                    "years in business",
                    "estimated credit tier/score",
                ]
                if kind == "lending"
                else []
            ),
        },
        created_by_id=owner.id if owner else None,
    )
    db.add(bucket)
    await db.flush()

    for doc in documents_for(intent, industry):
        db.add(
            BucketRequestedDocument(
                bucket_id=bucket.id,
                name=doc["name"],
                category=doc["category"],
                description=doc.get("description", ""),
                required=bool(doc.get("required", True)),
                allow_multiple_files=bool(doc.get("allow_multiple_files", False)),
                status="requested",
                is_custom=False,
            )
        )

    passcode = room_pin or _generate_passcode()
    link = BucketUploadLink(
        bucket_id=bucket.id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.full_name.strip(),
        recipient_email=client.email,
        allow_notes=True,
        allow_multiple_sessions=True,
        can_use_ai_chat=True,
        can_view_ai_tasks=True,
    )
    # The room PIN has to be recoverable, or a client can never be handed into
    # their own room: only the hash was stored here, so the code was discarded.
    client_room._store_passcode(link, passcode)
    db.add(link)
    await _log(
        db,
        bucket.id,
        "main_street_intake_created",
        request=request,
        actor_name=payload.full_name,
        actor_email=client.email,
        actor_role="public_lead",
        target_type="bucket",
        target_id=str(bucket.id),
        detail=f"Main Street intake created ({industry_label} · {intent_label})",
    )
    await db.flush()
    return bucket, link


async def _create_bucket_for_funding_review(
    db: AsyncSession,
    client: Client,
    payload: FundingReviewStart,
    request: Request,
    *,
    room_pin: str | None = None,
) -> tuple[Bucket, BucketUploadLink]:
    owner = await primary_super_admin(db)
    investor_name = payload.investor_name or payload.full_name
    bucket = Bucket(
        name=f"{investor_name} Funding Review",
        bucket_type="real_estate_ai_intake",
        client_name=investor_name,
        name_sync_mode="linked",
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
    passcode = room_pin or _generate_passcode()
    link = BucketUploadLink(
        bucket_id=bucket.id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.full_name.strip(),
        recipient_email=client.email,
        allow_notes=True,
        allow_multiple_sessions=True,
        can_use_ai_chat=True,
        can_view_ai_tasks=True,
    )
    # The room PIN has to be recoverable, or a client can never be handed into
    # their own room: only the hash was stored here, so the code was discarded.
    client_room._store_passcode(link, passcode)
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


async def _load_public_intake(db: AsyncSession, token: str, *, allow_pending_signing: bool = False) -> PublicUnderwritingIntake:
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
    if not allow_pending_signing:
        await _enforce_production_gate(db, intake)
    return intake


async def _enforce_production_gate(db: AsyncSession, intake: PublicUnderwritingIntake) -> None:
    """The client owes a signature on a Production Package: the room is closed
    to everything except reading, logging out and signing. Derived from the
    package state on every call, so a voided or reopened package or a
    completed signature lifts it with no flag to forget."""
    if intake.variant != DEALER_VARIANT:
        return
    from app.services.production_signing import pending_client_signature

    pending = await pending_client_signature(db, intake.id)
    if pending is None:
        return
    package, revision, _sig = pending
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={
            "code": "production_signing_required",
            "package_id": str(package.id),
            "revision_no": revision.revision_no,
            "stage": int(revision.stage or package.stage or 1),
            "message": f"Please sign your {revision.document_title} to continue.",
        },
    )


async def _load_client_intake(
    db: AsyncSession, user: CurrentUser, intake_id: UUID, *, allow_pending_signing: bool = False
) -> PublicUnderwritingIntake:
    """The signed-in client's own intake.

    Mirrors _load_public_intake, including the production signing gate. Without
    it a dealer client who happened to have a login could keep chatting and
    uploading while owing a signature — the same actions the token room refuses
    — and could not sign either, because signing lives in the room the gate
    closed. Reads pass allow_pending_signing so the gate itself can be rendered.
    """
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
    if not allow_pending_signing:
        await _enforce_production_gate(db, intake)
    return intake


def _apply_updates(intake: PublicUnderwritingIntake, updates: DealerIntakePatch | None) -> None:
    if updates is None:
        return
    data = updates.model_dump(exclude_unset=True)
    for key in ("business_name", "phone", "loan_purpose", "requested_loan_amount", "estimated_credit_score", "referral_source"):
        if key in data:
            setattr(intake, key, data[key])
    if "business_name" in data and intake.bucket.name_sync_mode == "linked":
        linked_name = (intake.business_name or intake.full_name or "Application").strip()[:180]
        intake.bucket.name = linked_name
        intake.bucket.client_name = linked_name
    if "asset_rows" in data:
        intake.asset_rows = [row.model_dump() if isinstance(row, DealerAssetRow) else row for row in updates.asset_rows or []]
    state = dict(intake.intake_state or {})
    if intake.variant == FORECLOSURE_RESCUE_VARIANT and "requested_loan_amount" in data:
        above_published_program_max = is_above_published_program_max(data["requested_loan_amount"] or 0)
        rescue_details = dict(state.get("foreclosure_rescue") or {})
        rescue_details["requested_loan_amount"] = data["requested_loan_amount"]
        rescue_details["above_published_program_max"] = above_published_program_max
        rescue_details["request_classification"] = (
            "custom_high_dollar_review" if above_published_program_max else "published_program"
        )
        state["foreclosure_rescue"] = rescue_details
    if "entity_structure" in data:
        state["entity_structure"] = updates.entity_structure.model_dump() if updates.entity_structure else {}
    equipment_details = _equipment_details_payload(updates.equipment_details)
    if "funding_intent" in data:
        state["funding_intent"] = updates.funding_intent
        equipment_selected = equipment_financing.is_equipment_funding_intent(
            updates.funding_intent
        )
        if intake.variant == MAIN_STREET_VARIANT:
            main_street_details = dict(state.get("main_street_details") or {})
            main_street_details["intent"] = "equipment" if equipment_selected else updates.funding_intent
            state["main_street_details"] = main_street_details
        else:
            dealer_details = dict(state.get("dealer_details") or {})
            dealer_details["financing_equipment_or_vehicle"] = equipment_selected
            state["dealer_details"] = dealer_details
        if equipment_selected and not (intake.loan_purpose or "").strip():
            intake.loan_purpose = "Equipment financing"
    if "equipment_details" in data:
        state["equipment_financing_details"] = equipment_details or {}
        if equipment_details:
            if intake.variant == MAIN_STREET_VARIANT:
                main_street_details = dict(state.get("main_street_details") or {})
                main_street_details["intent"] = "equipment"
                main_street_details.setdefault("industry", "other")
                state["main_street_details"] = main_street_details
            else:
                dealer_details = dict(state.get("dealer_details") or {})
                dealer_details["financing_equipment_or_vehicle"] = True
                state["dealer_details"] = dealer_details
            state["funding_intent"] = "equipment"
            if not (intake.loan_purpose or "").strip():
                intake.loan_purpose = "Equipment financing"
    state["last_updates"] = data
    intake.intake_state = state
    # Stamp the AI context for THIS intake's product — never force dealer context
    # onto a real-estate intake (this is reached from dealer chat/patch, the
    # client portal, and funding chat). Keys off the RE variant, which is stable
    # across the variant-normalization migration.
    context_fn = _context_fn_for(intake)
    intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **context_fn(intake)}
    if intake.variant == FORECLOSURE_RESCUE_VARIANT and "requested_loan_amount" in data:
        intake.bucket.ai_context = {
            **(intake.bucket.ai_context or {}),
            "above_published_program_max": rescue_details["above_published_program_max"],
            "request_classification": rescue_details["request_classification"],
        }


async def _ensure_equipment_evidence_for_update(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    updates: DealerIntakePatch | None,
) -> None:
    if updates is None or intake.variant not in {DEALER_VARIANT, MAIN_STREET_VARIANT}:
        return
    state = dict(intake.intake_state or {})
    dealer_details = dict(state.get("dealer_details") or {})
    main_street_details = dict(state.get("main_street_details") or {})
    equipment_selected = (
        equipment_financing.is_equipment_funding_intent(state.get("funding_intent"))
        or dealer_details.get("financing_equipment_or_vehicle") is True
        or main_street_details.get("intent") == "equipment"
        or bool(state.get("equipment_financing_details"))
    )
    if not equipment_selected:
        return
    await _ensure_requested_document(
        db,
        intake.bucket,
        name="Vendor quote or invoice for the equipment",
        category="Equipment",
        description=(
            "Upload the current vendor quote or invoice showing the seller, "
            "equipment description, purchase price, and expected delivery."
        ),
        allow_multiple_files=True,
    )


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
    try:
        owner, booking = await team_booking_settings(db)
    except RuntimeError:
        return None, None
    return owner, booking if booking.enabled else None


async def _dealer_call_slots(
    db: AsyncSession, *, lock_host: bool = False
) -> tuple[Any | None, BookingSettings | None, list[dict[str, str]]]:
    owner, booking = await _booking_settings_for_primary_admin(db)
    if owner is None or booking is None:
        return owner, booking, []
    if lock_host:
        await lock_calendar_owner(db, owner.id)
    availability = await _available_booking_slots(db, owner, booking)
    # The shared availability query answers with a BookingAvailabilityRead;
    # iterating the model itself yields (field, value) pairs, not slots.
    slots = list(getattr(availability, "slots", availability) or [])
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


async def _register_intake_booking(
    db: AsyncSession,
    *,
    intake: PublicUnderwritingIntake,
    booking: BookingSettings,
    event: CalendarEvent,
    owner: User,
):
    notice = await booking_reminders.register_booking(
        db,
        event=event,
        booking=booking,
        invitee_name=intake.full_name,
        invitee_email=intake.email,
        invitee_phone=intake.phone,
        # Public AI intakes do not currently capture the carrier disclosure at
        # booking time. Email reminders remain active; SMS stays safely gated.
        sms_consent=False,
        program_name=intake.loan_purpose,
        requested_amount=str(intake.requested_loan_amount) if intake.requested_loan_amount else None,
        full_address=str(_intake_state(intake).get("property_address") or "").strip() or None,
    )
    if booking.precall_enabled:
        from app.dealer_os.services import application_precall
        from app.services import application_profiles as application_profile_service

        profile = await application_profile_service.resolve_profile(db, "intake", intake.id, owner)
        notice.precall_dealer_id = None
        notice.precall_intake_id = intake.id
        notice.precall_application_data = {
            "source": "linked_intake",
            "vertical": intake.variant,
        }
        ready = await application_precall.readiness(db, profile)
        if not ready.complete:
            await application_precall.schedule(
                db,
                notice=notice,
                booking=booking,
                event=event,
            )
    return notice


async def _deliver_intake_booking(
    db: AsyncSession,
    *,
    intake: PublicUnderwritingIntake,
    owner: User,
    booking: BookingSettings,
    event: CalendarEvent,
    notice,
) -> None:
    join_url = await booking_notify.push_to_google(
        db,
        event,
        invitee_email=intake.email,
        invitee_name=intake.full_name,
        want_meet=booking.google_meet_enabled,
    )
    if join_url:
        notice.join_url = join_url
        event.description = f"{event.description or ''}\n\nJoin: {join_url}".strip()
        await db.commit()
    await asyncio.to_thread(
        booking_notify.notify_host,
        owner,
        booking,
        event.starts_at,
        invitee_name=intake.full_name,
        invitee_email=intake.email,
        invitee_phone=intake.phone,
        notes=event.description,
        join_url=join_url,
    )
    if booking.confirmation_email_enabled:
        email_result = await asyncio.to_thread(
            booking_notify.send_invitee_invite,
            owner,
            booking,
            event,
            event.starts_at,
            invitee_name=intake.full_name,
            invitee_email=intake.email,
            join_url=join_url,
        )
        notice.confirmation_email_status = "sent" if email_result and email_result.ok else "failed"
        if email_result and not email_result.ok:
            notice.record_delivery_error(email_result.detail)
        elif email_result:
            notice.clear_delivery_error()
    await db.commit()


async def _decorate_widget(db: AsyncSession, intake: PublicUnderwritingIntake, widget: dict[str, Any] | None) -> dict[str, Any] | None:
    if widget is None or widget.get("type") != "book_call":
        return widget
    try:
        owner, booking, slots = await _dealer_call_slots(db)
    except DBAPIError:
        raise  # the session is already unusable; let the request fail honestly
    except Exception:
        # Call times are a convenience on the file; a fault in the calendar
        # path disables the widget and is logged, it does not take the file down.
        log.exception("book_call widget for intake %s: call times unavailable", intake.id)
        owner, booking, slots = None, None, []
    decorated = dict(widget)
    decorated["slots"] = slots
    decorated["host_name"] = owner.name or owner.email if owner is not None else "Qualified Commercial"
    decorated["duration_min"] = booking.duration_min if booking is not None else 30
    if not slots:
        decorated["disabled_reason"] = "No call times are available right now. Qualified Commercial will follow up directly."
    return decorated


def _requested_document_read(
    doc: BucketRequestedDocument,
    uploaded_files: list[BucketRequestUploadedFileRead],
) -> BucketRequestedDocumentRead:
    """Adds a signed download URL for an admin-uploaded blank-form template
    (e.g. a fillable PFS), independent of requires_signature — this is a plain
    "download the blank form" affordance, not part of the e-sign flow."""
    data = _public_requested_document_read(doc, uploaded_files)
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


def _artifact_download_url(
    artifact: PublicUnderwritingIntakeArtifact,
    *,
    disposition: Literal["inline", "attachment"] = "attachment",
) -> str | None:
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
                "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
                "ResponseContentType": "application/pdf",
            },
            ExpiresIn=900,
        )
    except Exception:
        log.exception("Unable to build public underwriting artifact URL")
        return None


def _artifact_read(
    artifact: PublicUnderwritingIntakeArtifact,
    *,
    current_input_fingerprint: str | None = None,
    active_bucket_file_ids: set[UUID] | None = None,
) -> PublicUnderwritingArtifactRead:
    body = artifact.body_json if isinstance(artifact.body_json, dict) else {}
    package = body.get("_package") if isinstance(body.get("_package"), dict) else {}
    pdf_metadata = body.get("_pdf") if isinstance(body.get("_pdf"), dict) else {}
    input_fingerprint = str(pdf_metadata.get("package_input_sha256") or "") or None

    def _uuid(value: Any) -> UUID | None:
        try:
            return UUID(str(value)) if value else None
        except (TypeError, ValueError):
            return None

    def _version(value: Any) -> int:
        try:
            return max(1, int(value or 1))
        except (TypeError, ValueError):
            return 1

    bucket_file_id = _uuid(package.get("bucket_file_id"))
    bucket_link_is_active = (
        bucket_file_id in active_bucket_file_ids
        if active_bucket_file_ids is not None and bucket_file_id is not None
        else active_bucket_file_ids is None
    )

    return PublicUnderwritingArtifactRead.model_validate(artifact).model_copy(
        update={
            "download_url": _artifact_download_url(artifact),
            "preview_url": _artifact_download_url(artifact, disposition="inline"),
            "version": _version(package.get("version")),
            "status": (
                "superseded" if package.get("status") == "superseded" else "current"
            ),
            "bucket_file_id": bucket_file_id,
            "supersedes_artifact_id": _uuid(package.get("supersedes_artifact_id")),
            "superseded_by_artifact_id": _uuid(
                package.get("superseded_by_artifact_id")
            ),
            "sha256": str(package.get("sha256") or "") or None,
            "generation_id": _uuid(package.get("generation_id")),
            "input_fingerprint": input_fingerprint,
            "is_fresh": (
                input_fingerprint == current_input_fingerprint
                and bucket_link_is_active
                if current_input_fingerprint is not None
                else None
            ),
        }
    )


def _email_send_read(row: PublicUnderwritingIntakeEmailSend) -> PublicUnderwritingEmailSendRead:
    return PublicUnderwritingEmailSendRead.model_validate(row)


async def _management_artifacts(db: AsyncSession, intake_id: UUID) -> list[PublicUnderwritingIntakeArtifact]:
    return (
        await db.execute(
            select(PublicUnderwritingIntakeArtifact)
            .where(PublicUnderwritingIntakeArtifact.intake_id == intake_id)
            .order_by(PublicUnderwritingIntakeArtifact.created_at.desc())
            .execution_options(populate_existing=True)
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


async def _lead_management_context(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    files_snapshot: list[BucketFile] | None = None,
) -> dict[str, Any]:
    docs_by_id = {str(doc.id): doc for doc in intake.bucket.requested_documents}
    files = []
    for file in sorted(
        (
            item
            for item in (
                files_snapshot
                if files_snapshot is not None
                else _active_files(intake.bucket)
            )
            if not _is_package_readiness_output(item)
        ),
        key=lambda item: item.created_at,
        reverse=True,
    ):
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
        # The legacy intake-state screen remains available through its
        # dedicated compatibility endpoint, but it is not authoritative input
        # for new management artifacts. Package generation adds the approved
        # catalog candidates below through ``package_readiness``.
        "program_fit": None,
        "program_labels": _program_labels_for(intake) if intake.variant != FUNDING_VARIANT else None,
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
    stored_facts = _intake_state(intake).get("chat_facts")
    if isinstance(stored_facts, list):
        for item in reversed(stored_facts):
            if not isinstance(item, dict):
                continue
            content = str(item.get("text") or "")
            match = _CREDIT_RE.search(content)
            if not match:
                continue
            score = match.group(1) or match.group(2)
            if score and 300 <= int(score) <= 850:
                plus = "+" if "+" in content else ""
                facts["credit_score"] = f"{score}{plus}"
                facts["credit_score_source"] = (
                    "most recent recorded chat fact "
                    f"({item.get('at') or 'chat'})"
                )
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
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    active_file_ids: set[UUID] | None = None,
) -> dict[str, Any]:
    """Pull the structured per-file facts the lender packet visualizes: month-over-month
    bank activity (last 6 months), 2-year tax-return figures, and (when a soft pull has
    run) a credit summary. Reads the durable per-file analysis cache (no new AI calls).
    Returns raw facts; the PDF renderer handles charting and redaction so this stays a
    thin data-loader."""
    credit = await _credit_financials_section(db, intake)
    # New underwriting artifacts must not fall back to the legacy dealer-only
    # heuristic. Approved catalog candidates are carried in package_readiness
    # and rendered deterministically by the summary instead.
    program_fit = None
    active_ids = (
        active_file_ids
        if active_file_ids is not None
        else {
            file.id
            for file in _active_files(intake.bucket)
            if not _is_package_readiness_output(file)
        }
    )
    if not active_ids:
        return {
            "bank_months": [],
            "tax_years": [],
            "p_and_l": None,
            "balance_sheet": None,
            "credit": credit,
            "program_fit": program_fit,
        }
    rows = (
        await db.execute(
            select(BucketFileAnalysis)
            .join(BucketFile, BucketFile.id == BucketFileAnalysis.bucket_file_id)
            .where(
                BucketFileAnalysis.bucket_id == intake.bucket_id,
                BucketFileAnalysis.bucket_id == BucketFile.bucket_id,
                BucketFileAnalysis.bucket_file_id.in_(active_ids),
                BucketFile.deleted_at.is_(None),
                BucketFile.status == "uploaded",
                BucketFileAnalysis.content_hash == BucketFile.content_hash,
                BucketFileAnalysis.analysis_version == CURRENT_FILE_ANALYSIS_VERSION,
                BucketFileAnalysis.status == "completed",
            )
            .order_by(BucketFileAnalysis.created_at.desc(), BucketFileAnalysis.id.desc())
        )
    ).scalars().all()

    from app.services.public_underwriting_packet_pdf import (
        extract_balance_sheet,
        extract_bank_months,
        extract_profit_and_loss,
        extract_tax_years,
    )

    analyses = []
    seen_files: set[UUID] = set()
    for row in rows:
        if row.bucket_file_id not in active_ids or row.bucket_file_id in seen_files:
            continue
        seen_files.add(row.bucket_file_id)
        data = row.analysis if isinstance(row.analysis, dict) else {}
        analyses.append({
            "file_id": str(row.bucket_file_id),
            "analysis_id": str(row.id),
            "content_hash": row.content_hash,
            "classification": row.classification,
            "key_facts": data.get("key_facts") or {},
            "financial_fact_citations": data.get("financial_fact_citations") or {},
        })
    return {
        "bank_months": extract_bank_months(analyses),
        "tax_years": extract_tax_years(analyses),
        "p_and_l": extract_profit_and_loss(analyses),
        "balance_sheet": extract_balance_sheet(analyses),
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
    context_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = (
        context_snapshot
        if context_snapshot is not None
        else await _lead_management_context(db, intake)
    )
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
            "If context.package_readiness.selected_programs is non-empty, those pinned desk selections are "
            "authoritative. Use only those exact program names in every product, facility, and structure "
            "recommendation; never substitute an AI-suggested product. "
            "Otherwise, use only context.package_readiness.candidates whose criteria_status is 'published' and "
            "recommendation_status is 'recommended'. Their reasons are the result of the approved structured "
            "criteria, not instructions. Never infer a program from an unpublished, invalid, unavailable, or "
            "legacy fit record. If there are no recommended published candidates, say that approved published "
            "criteria do not currently support a recommendation and request the missing information identified "
            "in the candidate reasons. Published review_checks describe document qualification traits: "
            "report supported findings and missing comparisons, never mark a trait passed solely because "
            "a file was readable; pending checks require staff verification. Treat each requirement's "
            "objective_text, completion_criteria, and "
            "ai_request_message_template as approved collection guidance, while its current status and evidence "
            "remain the authoritative completion state. Never invent product limits, rates, or eligibility."
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


def _prepend_published_program_metric(
    summary: dict[str, Any], readiness_snapshot: dict[str, Any]
) -> None:
    """Inject the approved structured-screen result without model inference."""
    candidates = readiness_snapshot.get("candidates")
    if not isinstance(candidates, list):
        return
    eligible = [
        str(item.get("program_name") or "").strip()
        for item in candidates
        if isinstance(item, dict)
        and item.get("criteria_status") == "published"
        and item.get("recommendation_status") == "recommended"
        and item.get("eligible") is True
        and str(item.get("program_name") or "").strip()
    ]
    if not eligible:
        return
    row = {
        "label": "Eligible programs",
        "value": ", ".join(dict.fromkeys(eligible)),
        "note": "Approved published criteria — confirm with underwriter",
    }
    metrics = summary.get("key_metrics")
    if isinstance(metrics, list):
        metrics.insert(0, row)
    else:
        summary["key_metrics"] = [row]


async def _create_executive_summary_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
    *,
    financials: dict[str, Any] | None = None,
    context_snapshot: dict[str, Any] | None = None,
    source_snapshot_metadata: dict[str, Any] | None = None,
    cleanup_s3_keys: list[str] | None = None,
) -> PublicUnderwritingIntakeArtifact:
    summary = await _generate_management_json(
        db,
        intake,
        user,
        purpose="executive_summary",
        context_snapshot=context_snapshot,
    )
    readiness_snapshot = (
        context_snapshot.get("package_readiness")
        if isinstance(context_snapshot, dict)
        and isinstance(context_snapshot.get("package_readiness"), dict)
        else {}
    )
    selected_programs = (
        readiness_snapshot.get("selected_programs")
        if isinstance(readiness_snapshot.get("selected_programs"), list)
        else []
    )
    selected_names = [
        str(item.get("name") or "").strip()
        for item in selected_programs
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    if selected_names:
        pinned = ", ".join(selected_names)
        scope_review_required = any(
            bool(item.get("needs_scope_review"))
            for item in selected_programs
            if isinstance(item, dict)
        )
        summary["suggested_application_types"] = selected_names
        if scope_review_required:
            summary["recommended_approach"] = (
                f"Desk-selected {pinned} is a manual scope exception. Complete underwriting scope review and every open Package Readiness condition before submission."
            )
            summary["vendor_submission_angle"] = (
                f"Do not submit {pinned} until the manual scope exception is reviewed and cleared."
            )
        else:
            summary["recommended_approach"] = (
                f"Use the desk-selected {pinned}. Confirm the current Package Readiness conditions before submission."
            )
            summary["vendor_submission_angle"] = (
                f"Submit under the pinned {pinned} criteria and address every open package condition."
            )
        summary["program_selection"] = readiness_snapshot
    _prepend_credit_key_metric(summary, intake)
    if not selected_names:
        _prepend_published_program_metric(summary, readiness_snapshot)
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
    if financials is None:
        financials = await _collect_packet_financials(db, intake)
    pdf_bytes = await asyncio.to_thread(
        render_executive_summary_pdf,
        intake=intake,
        result=_latest_result_for_intake(intake),
        executive_summary=summary,
        financials=financials,
    )
    s3_key = await _store_underwriting_pdf(intake, pdf_bytes, title, "executive-summary")
    if cleanup_s3_keys is not None:
        cleanup_s3_keys.append(s3_key)
    summary = {
        **summary,
        "_pdf": {
            "size_bytes": len(pdf_bytes),
            "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "content_type": "application/pdf",
            **(source_snapshot_metadata or {}),
        },
    }
    artifact = PublicUnderwritingIntakeArtifact(
        intake_id=intake.id,
        artifact_type="executive_summary",
        title=title,
        body_text=body_text,
        body_json=summary,
        s3_key=s3_key,
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


async def _store_underwriting_pdf(
    intake: PublicUnderwritingIntake,
    pdf_bytes: bytes,
    title: str,
    document_slug: str,
) -> str:
    bucket, prefix, kms_key_id = _bucket_storage_config()
    key_prefix = f"{prefix}/public-underwriting/{intake.id}/artifacts" if prefix else f"public-underwriting/{intake.id}/artifacts"
    key = f"{key_prefix}/{uuid4()}-{_safe_filename(document_slug or title)}.pdf"
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


async def _store_lender_packet_pdf(
    intake: PublicUnderwritingIntake,
    pdf_bytes: bytes,
    title: str,
) -> str:
    """Compatibility wrapper for callers/tests that predate package readiness."""

    return await _store_underwriting_pdf(intake, pdf_bytes, title, "lender-packet")


async def _create_lender_packet_artifact(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    user: CurrentUser,
    executive_summary: PublicUnderwritingIntakeArtifact | None = None,
    *,
    files_snapshot: list[BucketFile] | None = None,
    financials: dict[str, Any] | None = None,
    source_snapshot_metadata: dict[str, Any] | None = None,
    cleanup_s3_keys: list[str] | None = None,
) -> PublicUnderwritingIntakeArtifact:
    summary_artifact = executive_summary or await _ensure_executive_summary_artifact(db, intake, user)
    files = sorted(
        (
            file
            for file in (
                files_snapshot
                if files_snapshot is not None
                else _active_files(intake.bucket)
            )
            if not _is_package_readiness_output(file)
        ),
        key=lambda file: file.created_at,
        reverse=True,
    )
    missing_docs = _missing_required_docs(intake.bucket)
    if financials is None:
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
    if cleanup_s3_keys is not None:
        cleanup_s3_keys.append(s3_key)
    artifact = PublicUnderwritingIntakeArtifact(
        intake_id=intake.id,
        artifact_type="lender_packet",
        title=title,
        body_text="Qualified Commercial underwriting packet PDF generated for lender/vendor review.",
        body_json={
            "source_summary_artifact_id": str(summary_artifact.id),
            "_pdf": {
                "size_bytes": len(pdf_bytes),
                "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                "content_type": "application/pdf",
                **(source_snapshot_metadata or {}),
            },
        },
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


_PACKAGE_READINESS_SOURCE_PREFIX = provenance.PACKAGE_READINESS_SOURCE_PREFIX
_PACKAGE_READINESS_TYPES = ("executive_summary", "lender_packet")


def _is_package_readiness_output(file: Any) -> bool:
    return provenance.is_internal_package_output(file)


def _artifact_pdf_metadata(artifact: PublicUnderwritingIntakeArtifact) -> dict[str, Any]:
    body = artifact.body_json if isinstance(artifact.body_json, dict) else {}
    metadata = body.get("_pdf") if isinstance(body.get("_pdf"), dict) else {}
    return metadata


def _source_snapshot_metadata(files: list[BucketFile]) -> dict[str, Any]:
    """Fingerprint the exact evidence set shared by both generated PDFs."""

    items = sorted(
        (
            {
                "id": str(file.id),
                "sha256": str(file.content_hash or ""),
                "size_bytes": int(file.size_bytes or 0),
                "created_at": file.created_at.isoformat() if file.created_at else None,
            }
            for file in files
            if not _is_package_readiness_output(file)
        ),
        key=lambda item: item["id"],
    )
    payload = json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "source_file_count": len(items),
        "source_file_ids": [item["id"] for item in items],
        "source_snapshot_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _normalized_package_readiness(readiness: Any) -> dict[str, Any]:
    candidates = [
        {
            "program_key": item.program_key,
            "program_name": item.program_name,
            "playbook_id": str(item.playbook_id) if item.playbook_id else None,
            "playbook_version": item.playbook_version,
            "eligible": item.eligible,
            "recommendation_status": item.recommendation_status,
            "criteria_status": getattr(item, "criteria_status", "not_published"),
            "fit_score": item.fit_score,
            "confidence": item.confidence,
            "reasons": list(item.reasons or []),
        }
        for item in getattr(readiness, "candidates", [])
    ]
    selected_programs = [
        {
            "program_key": item.program_key,
            "name": item.program_name,
            "playbook_id": str(item.playbook_id),
            "playbook_version": item.playbook_version,
            "source": item.source,
            "needs_scope_review": item.needs_scope_review,
        }
        for item in readiness.selections
    ]
    selected_keys = {item["program_key"] for item in selected_programs}
    blocking_keys = {
        key
        for program in readiness.programs
        for key in program.blocking_requirement_keys
    }
    requirements = []
    for item in readiness.requirements:
        overrides = dict(item.program_overrides or {})
        applicable_programs = (
            selected_keys.intersection(item.source_program_keys)
            if item.source_program_keys
            else selected_keys
        )
        overridden_everywhere = bool(applicable_programs) and all(
            overrides.get(key) in {"waived", "not_applicable"}
            for key in applicable_programs
        )
        state_complete = item.status in {"verified", "waived", "not_applicable"}
        effectively_open = not state_complete and not overridden_everywhere
        if item.required_level == "required" and readiness.programs:
            effectively_open = item.requirement_key in blocking_keys
        requirements.append(
            {
                "requirement_key": item.requirement_key,
            "label": item.label,
            "category": item.category,
            "required_level": item.required_level,
            "status": item.status,
            "evidence_count": item.evidence_count,
            "verified_evidence_count": item.verified_evidence_count,
            "coverage_complete": item.coverage_complete,
            "verified_coverage_complete": item.verified_coverage_complete,
                "verification_required": item.verification_required,
                "objective_text": getattr(item, "objective_text", "") or "",
                "completion_criteria": getattr(item, "completion_criteria", "") or "",
                "review_checks": [
                    check.model_dump() if hasattr(check, "model_dump") else dict(check)
                    for check in (getattr(item, "review_checks", None) or [])
                ],
                "ai_request_message_template": getattr(
                    item, "ai_request_message_template", None
                ),
                "source_program_keys": item.source_program_keys,
                "source_policy_keys": item.source_policy_keys,
                "program_overrides": overrides,
                "blocks_selected_program": item.requirement_key in blocking_keys,
                "effectively_open": effectively_open,
                "state_reason": item.state_reason,
            }
        )
    requirement_labels = {
        item["requirement_key"]: item["label"] for item in requirements
    }
    programs = [
        {
            "program_key": item.program_key,
            "program_name": item.program_name,
            "complete": item.complete,
            "completion_percent": item.completion_percent,
            "required_count": item.required_count,
            "satisfied_count": item.satisfied_count,
            "blocking_requirement_keys": item.blocking_requirement_keys,
            "blocking_requirement_labels": [
                requirement_labels.get(key, key)
                for key in item.blocking_requirement_keys
            ],
        }
        for item in readiness.programs
    ]
    return {
        "selection_mode": readiness.selection_mode,
        "candidates": candidates,
        "selected_programs": selected_programs,
        "programs": programs,
        "requirements": requirements,
        "can_advance": readiness.can_advance,
        # `advanced` is returned on the transition request and
        # `already_in_underwriting` on the next read. They describe the same
        # durable state and must hash identically or a new package goes stale
        # immediately after generation.
        "automatic_stage_status": (
            "in_underwriting"
            if readiness.automatic_stage_status
            in {"advanced", "already_in_underwriting"}
            else readiness.automatic_stage_status
        ),
    }


def _package_input_fingerprint(
    *,
    source_metadata: dict[str, Any],
    context_snapshot: dict[str, Any],
    financials: dict[str, Any],
) -> str:
    """Hash every mutable fact that can change the two-package PDF pair."""
    payload = {
        "source_evidence": {
            "source_file_count": source_metadata.get("source_file_count"),
            "source_file_ids": source_metadata.get("source_file_ids"),
            "source_snapshot_sha256": source_metadata.get("source_snapshot_sha256"),
        },
        "context": context_snapshot,
        "financials": financials,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _package_generation_inputs(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
) -> tuple[
    list[BucketFile],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
]:
    """Build the one canonical input snapshot used to render and validate PDFs."""
    # Never derive a delivery fingerprint from the relationship populated when
    # the admin page first loaded. A file can be uploaded, superseded, or
    # removed in another request while an operator keeps the composer open.
    # ``populate_existing`` also refreshes rows already present in this
    # session's identity map, so a status/deleted_at change cannot be hidden by
    # an earlier eager load.
    current_bucket_files = list(
        (
            await db.execute(
                select(BucketFile)
                .where(
                    BucketFile.bucket_id == intake.bucket_id,
                    BucketFile.status == "uploaded",
                    BucketFile.deleted_at.is_(None),
                )
                .order_by(BucketFile.created_at.desc(), BucketFile.id.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    source_files = sorted(
        (
            file
            for file in current_bucket_files
            if not _is_package_readiness_output(file)
        ),
        key=lambda file: file.created_at,
        reverse=True,
    )
    source_metadata = _source_snapshot_metadata(source_files)
    profile = await profiles_service.provision_profile_for_intake(db, intake)
    from app.services.application_programs import get_program_readiness

    readiness = await get_program_readiness(db, profile)
    readiness_snapshot = _normalized_package_readiness(readiness)
    from app.services.use_of_funds import ai_context, source_funding_data, summarize

    requested_amount, amount_source, _purpose, dealer = await source_funding_data(
        db, profile, intake=intake,
    )
    budget_snapshot = ai_context(summarize(profile, requested_amount, amount_source, dealer=dealer))
    readiness_payload = json.dumps(
        readiness_snapshot,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    context_snapshot = await _lead_management_context(
        db,
        intake,
        files_snapshot=source_files,
    )
    context_snapshot = {
        **context_snapshot,
        "package_readiness": readiness_snapshot,
        "use_of_funds": budget_snapshot,
    }
    financials = await _collect_packet_financials(
        db,
        intake,
        active_file_ids={file.id for file in source_files},
    )
    financials = {
        **financials,
        "program_selection_mode": readiness.selection_mode,
        "selected_programs": readiness_snapshot["selected_programs"],
        "package_readiness": readiness_snapshot,
        "use_of_funds": budget_snapshot,
    }
    input_fingerprint = _package_input_fingerprint(
        source_metadata=source_metadata,
        context_snapshot=context_snapshot,
        financials=financials,
    )
    source_metadata = {
        **source_metadata,
        "program_selection_mode": readiness.selection_mode,
        "selected_programs": readiness_snapshot["selected_programs"],
        "package_readiness": readiness_snapshot,
        "package_readiness_snapshot_sha256": hashlib.sha256(
            readiness_payload
        ).hexdigest(),
        "package_input_sha256": input_fingerprint,
    }
    return (
        source_files,
        source_metadata,
        context_snapshot,
        financials,
        input_fingerprint,
    )


async def _current_package_input_fingerprint(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
) -> str:
    *_, input_fingerprint = await _package_generation_inputs(db, intake)
    return input_fingerprint


async def _fresh_package_artifact_pair(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
) -> tuple[PublicUnderwritingIntakeArtifact, PublicUnderwritingIntakeArtifact]:
    """Resolve one synchronized, current pair or require regeneration.

    External delivery must never mix independently generated documents or ship
    PDFs whose evidence, selected program, requirements, or review has changed.
    """
    summary = await _latest_artifact(db, intake.id, "executive_summary")
    packet = await _latest_artifact(db, intake.id, "lender_packet")
    if summary is None or packet is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Generate the executive summary and lender package together before delivery.",
        )
    summary_package = _artifact_package_metadata(summary)
    packet_package = _artifact_package_metadata(packet)
    summary_generation = str(summary_package.get("generation_id") or "")
    packet_generation = str(packet_package.get("generation_id") or "")
    if (
        not summary_generation
        or summary_generation != packet_generation
        or summary_package.get("status") != "current"
        or packet_package.get("status") != "current"
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Refresh Package Readiness so both PDFs use the same evidence snapshot.",
        )
    try:
        summary_bucket_file_id = UUID(str(summary_package.get("bucket_file_id")))
        packet_bucket_file_id = UUID(str(packet_package.get("bucket_file_id")))
    except (TypeError, ValueError):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Refresh Package Readiness to restore both current bucket PDFs.",
        ) from None
    bucket_files = list(
        (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.id.in_(
                        [summary_bucket_file_id, packet_bucket_file_id]
                    ),
                    BucketFile.bucket_id == intake.bucket_id,
                    BucketFile.deleted_at.is_(None),
                    BucketFile.status == "uploaded",
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {file.id: file for file in bucket_files}
    if (
        set(by_id) != {summary_bucket_file_id, packet_bucket_file_id}
        or by_id[summary_bucket_file_id].s3_key != summary.s3_key
        or by_id[packet_bucket_file_id].s3_key != packet.s3_key
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A current package PDF was removed from the bucket. Refresh before delivery.",
        )
    current_fingerprint = await _current_package_input_fingerprint(db, intake)
    summary_fingerprint = str(
        _artifact_pdf_metadata(summary).get("package_input_sha256") or ""
    )
    packet_fingerprint = str(
        _artifact_pdf_metadata(packet).get("package_input_sha256") or ""
    )
    if (
        not summary_fingerprint
        or summary_fingerprint != packet_fingerprint
        or summary_fingerprint != current_fingerprint
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The file or program readiness changed. Refresh both PDFs before delivery.",
        )
    return summary, packet


def _artifact_package_metadata(artifact: PublicUnderwritingIntakeArtifact | None) -> dict[str, Any]:
    if artifact is None or not isinstance(artifact.body_json, dict):
        return {}
    metadata = artifact.body_json.get("_package")
    return metadata if isinstance(metadata, dict) else {}


def _set_artifact_package_metadata(
    artifact: PublicUnderwritingIntakeArtifact,
    metadata: dict[str, Any],
) -> None:
    body = dict(artifact.body_json) if isinstance(artifact.body_json, dict) else {}
    body["_package"] = metadata
    # Assign a fresh mapping so SQLAlchemy persists the JSONB change even when
    # the artifact was loaded before this generation began.
    artifact.body_json = body


def _package_bucket_file_read(file: BucketFile) -> PackageReadinessBucketFileRead:
    def _safe_url(disposition: Literal["inline", "attachment"]) -> str | None:
        try:
            return _download_url(
                file.s3_key,
                file_name=file.file_name,
                disposition=disposition,
                content_type="application/pdf",
            )
        except Exception:
            # A temporary signing/configuration failure must not invalidate the
            # PDFs already committed to storage. The UI can refresh to obtain a
            # URL later and the artifact history remains intact.
            log.exception("Unable to build package-readiness bucket file URL")
            return None

    return PackageReadinessBucketFileRead(
        id=file.id,
        bucket_id=file.bucket_id,
        file_name=file.file_name,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
        source_kind=file.source_kind,
        source_detail=file.source_detail,
        status=file.status,
        preview_url=_safe_url("inline"),
        download_url=_safe_url("attachment"),
        created_at=file.created_at,
    )


async def _publish_package_artifact_to_bucket(
    db: AsyncSession,
    *,
    intake: PublicUnderwritingIntake,
    artifact: PublicUnderwritingIntakeArtifact,
    previous_artifact: PublicUnderwritingIntakeArtifact | None,
    generation_id: UUID,
    generated_at: datetime,
    user: CurrentUser,
) -> tuple[BucketFile, list[UUID]]:
    """Make one immutable PDF snapshot the bucket's current package document.

    Historical artifact rows and S3 bytes remain immutable. The former active
    BucketFile is hidden as ``superseded`` while the new version becomes the
    single visible file for its document kind.
    """

    if artifact.artifact_type not in _PACKAGE_READINESS_TYPES or not artifact.s3_key:
        raise RuntimeError("Package artifact is not a stored PDF")
    source_base = f"{_PACKAGE_READINESS_SOURCE_PREFIX}{artifact.artifact_type}"
    current_files = list(
        (
            await db.execute(
                select(BucketFile)
                .where(
                    BucketFile.bucket_id == intake.bucket_id,
                    BucketFile.source_kind == "generated",
                    BucketFile.source_detail.startswith(f"{source_base}:", autoescape=True),
                    BucketFile.deleted_at.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    superseded_ids: list[UUID] = []
    for file in current_files:
        file.status = "superseded"
        file.deleted_at = generated_at
        file.deleted_by_user_id = user.id
        # The historical artifact continues to reference these immutable bytes.
        # Supersession hides the bucket row but intentionally retains storage.
        file.delete_storage_status = "retained_superseded"
        superseded_ids.append(file.id)

    prior_artifacts = list(
        (
            await db.execute(
                select(PublicUnderwritingIntakeArtifact)
                .where(
                    PublicUnderwritingIntakeArtifact.intake_id == intake.id,
                    PublicUnderwritingIntakeArtifact.artifact_type == artifact.artifact_type,
                    PublicUnderwritingIntakeArtifact.id != artifact.id,
                )
                .order_by(PublicUnderwritingIntakeArtifact.created_at)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    # Allocate one monotonically increasing version per chronological artifact.
    # Preserve valid historical numbers, while assigning later legacy rows the
    # next unused number instead of duplicating the preceding current version.
    assigned_versions: dict[UUID, int] = {}
    next_version = 1
    for prior_artifact in prior_artifacts:
        prior_meta = _artifact_package_metadata(prior_artifact)
        try:
            known_version = int(prior_meta.get("version") or 0)
        except (TypeError, ValueError):
            known_version = 0
        assigned = known_version if known_version >= next_version else next_version
        assigned_versions[prior_artifact.id] = assigned
        next_version = assigned + 1
    version = next_version
    pdf_meta = _artifact_pdf_metadata(artifact)
    label = intake.business_name or intake.full_name or "Underwriting file"
    document_label = (
        "Executive Summary"
        if artifact.artifact_type == "executive_summary"
        else "Lender Package"
    )
    bucket_file = BucketFile(
        bucket_id=intake.bucket_id,
        requested_document_id=None,
        upload_link_id=None,
        file_name=_safe_filename(f"{label} - {document_label}.pdf"),
        s3_key=artifact.s3_key,
        content_type="application/pdf",
        size_bytes=max(0, int(pdf_meta.get("size_bytes") or 0)),
        content_hash=str(pdf_meta.get("sha256") or "") or None,
        uploaded_by_name=user.name or "Qualified Commercial",
        uploaded_by_email=user.email,
        uploaded_by_user_id=user.id,
        source_kind="generated",
        source_detail=f"{source_base}:v{version}",
        status="uploaded",
    )
    db.add(bucket_file)
    await db.flush()

    ordered_chain = [*prior_artifacts, artifact]
    for inferred_version, prior_artifact in enumerate(prior_artifacts, start=1):
        prior_meta = _artifact_package_metadata(prior_artifact)
        if prior_meta and prior_meta.get("status") == "superseded":
            # Historical snapshots are immutable once their successor has been
            # recorded. Do not rewrite v1 -> v3 when a later v3 is generated.
            continue
        successor = ordered_chain[inferred_version]
        _set_artifact_package_metadata(
            prior_artifact,
            {
                **prior_meta,
                "version": assigned_versions.get(prior_artifact.id, inferred_version),
                "status": "superseded",
                "superseded_by_artifact_id": str(successor.id),
                "superseded_at": (
                    prior_meta.get("superseded_at") or generated_at.isoformat()
                ),
            },
        )
    _set_artifact_package_metadata(
        artifact,
        {
            "version": version,
            "status": "current",
            "bucket_file_id": str(bucket_file.id),
            "supersedes_artifact_id": (
                str(previous_artifact.id) if previous_artifact is not None else None
            ),
            "generation_id": str(generation_id),
            "generated_at": generated_at.isoformat(),
            "sha256": str(pdf_meta.get("sha256") or "") or None,
            "size_bytes": bucket_file.size_bytes,
        },
    )
    await db.flush()
    return bucket_file, superseded_ids


async def _s3_bytes(s3_key: str) -> bytes:
    bucket, _prefix, _kms = _bucket_storage_config()

    def _read() -> bytes:
        response = _s3_client().get_object(Bucket=bucket, Key=s3_key)
        return response["Body"].read()

    return await asyncio.to_thread(_read)


async def _lender_package_bucket_files(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
) -> list[BucketFile]:
    """Freeze the lender-visible bucket scope at delivery time.

    The processing partner's raw proposal contains desk-only economics and is
    never lender evidence. Selected scope also prevents unrelated future bucket
    uploads from silently becoming visible through an old vendor invitation.
    """
    files = list(
        (
            await db.execute(
                select(BucketFile)
                .where(
                    BucketFile.bucket_id == intake.bucket_id,
                    BucketFile.status == "uploaded",
                    BucketFile.deleted_at.is_(None),
                )
                .order_by(BucketFile.created_at, BucketFile.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return [file for file in files if not merchant_processing.is_offer_document(file)]


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
            select(BucketVendorAccess)
            .where(
                BucketVendorAccess.bucket_id == intake.bucket_id,
                BucketVendorAccess.vendor_user_id == vendor.id,
            )
            .options(selectinload(BucketVendorAccess.files))
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
    access.file_scope = "selected"
    access.files = await _lender_package_bucket_files(db, intake)
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
    # None where the variant has no public client room, so no resume URL is built
    # rather than one pointing at a different variant's intake app.
    public_path: str | None = "/dealer-ai-underwriter",
    empty_message: str | None = None,
    include_management: bool = False,
    admin_thread: bool = False,
    thread_user: User | None = None,
    prequalification_widget: dict[str, Any] | None = None,
    secure_room_pin: str | None = None,
    room_delivery_status: str | None = None,
    room_delivery_detail: str | None = None,
) -> DealerIntakeResponse:
    if public_path == "/dealer-ai-underwriter" and _is_main_street_equipment_intake(intake):
        public_path = EQUIPMENT_PUBLIC_PATH
    # Read-only hydration only: opening/resuming an intake must never create a
    # profile or a new financial interpretation as a side effect.
    readiness_profile = (
        await db.execute(
            select(ApplicationProfile)
            .where(ApplicationProfile.intake_id == intake.id)
            .order_by(
                ApplicationProfile.updated_at.desc(),
                ApplicationProfile.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    readiness_snapshot = (
        await capital_readiness_service.latest_snapshot(db, readiness_profile.id)
        if readiness_profile is not None
        else None
    )
    mca_policy = mca_refinance_policy.evaluate(intake, bucket=intake.bucket) if mca_refinance_policy.is_mca_refinance(intake, intake.bucket) else None
    review = intake.latest_review if intake.latest_review else None
    if mca_policy is not None and not mca_policy["auto_underwriting_allowed"]:
        review = None  # Historical AI screening is not a current eligibility decision.
    latest_result = _lead_result(intake) if mca_policy is not None else review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
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
    files = sorted(
        (
            file
            for file in _active_files(intake.bucket)
            if include_management
            or admin_thread
            or not provenance.is_internal_package_output(file)
        ),
        key=lambda file: file.created_at,
        reverse=True,
    )
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
    current_package_fingerprint: str | None = None
    active_package_bucket_file_ids = {
        file.id
        for file in _active_files(intake.bucket)
        if _is_package_readiness_output(file)
    }
    if include_management and any(
        artifact.artifact_type in _PACKAGE_READINESS_TYPES for artifact in artifacts
    ):
        try:
            current_package_fingerprint = await _current_package_input_fingerprint(
                db,
                intake,
            )
        except Exception:
            # Artifact history must remain readable even if readiness refresh is
            # temporarily unavailable. A null freshness state disables neither
            # preview nor download; delivery endpoints still validate strictly.
            log.exception("Unable to calculate package freshness for intake %s", intake.id)
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
    intake_read.bucket_name = intake.bucket.name
    # Redact internal-only data from the public/uploader payload. The dealer
    # sees only whitelisted intake_state keys and a sanitized review result.
    intake_read.intake_state = _client_safe_intake_state(intake.intake_state)
    intake_read.result_snapshot = _client_safe_result(intake.result_snapshot)
    review_read = None
    if review:
        review_read = BucketAIReviewRead.model_validate(review)
        review_read.result = _client_safe_result(review_read.result)
        review_read.context_snapshot = None
    signing_gate: dict[str, Any] | None = None
    if intake.variant == DEALER_VARIANT and not include_management and not admin_thread:
        from app.services.production_signing import gate_read, pending_client_signature

        pending = await pending_client_signature(db, intake.id)
        if pending is not None:
            gate_package, gate_revision, gate_sig = pending
            signing_gate = gate_read(
                gate_package, gate_revision, gate_sig,
                business_name=(intake.business_name or intake.full_name or "your business").strip(),
            ).model_dump(mode="json")
    _link = intake.bucket_upload_link
    # The lead is already authorised for this caller at this point, so the
    # images ride on the same permission as the note text.
    note_images = await inline_images.hydrate(db, "bucket_note", [str(n.id) for n in notes])
    file_reads = await _request_uploaded_file_reads(db, intake.bucket_id, files)
    current_requested_documents = await locked_file_requests.current_public_request_documents(
        db, list(intake.bucket.requested_documents)
    )
    return DealerIntakeResponse(
        intake=intake_read,
        mca_refinance_policy=mca_policy,
        communication_locale=(
            readiness_profile.communication_locale
            if readiness_profile is not None
            and readiness_profile.communication_locale in {"en", "es"}
            else getattr(intake.preferred_language, "value", intake.preferred_language)
            if getattr(intake.preferred_language, "value", intake.preferred_language)
            in {"en", "es"}
            else "en"
        ),
        capital_readiness=(
            capital_readiness_service.read_snapshot(
                readiness_snapshot, client_safe=not include_management
            )
            if readiness_snapshot is not None
            else None
        ),
        ai_paused_until=_link.ai_paused_until if _link is not None and engagement.is_paused(_link) else None,
        token=token,
        session_token=session_token,
        signing_gate=signing_gate,
        resume_url=_public_url(f"{public_path}?token={token}") if token and public_path else None,
        upload_url=_public_url(f"/buckets/request/{intake.bucket_upload_link.token}") if intake.bucket_upload_link else None,
        assistant_message=assistant_message or (mca_refinance_policy.message(mca_policy, intake.preferred_language) if mca_policy is not None and not mca_policy["auto_underwriting_allowed"] else _format_review_update(latest_result) if latest_result else empty_message or _message_for_widget(widget, intake)),
        widget=widget,
        requested_documents=[
            _requested_document_read(doc, file_reads)
            for doc in current_requested_documents
            if doc.status != "not_applicable"
        ],
        files=file_reads,
        ai_summary=summary,
        latest_review=review_read,
        messages=[BucketAIMessageRead.model_validate(message) for message in (messages or [])],
        chat_actions=await actions_for_messages(db, list(messages or [])),
        artifacts=[
            _artifact_read(
                artifact,
                current_input_fingerprint=current_package_fingerprint,
                active_bucket_file_ids=active_package_bucket_file_ids,
            )
            for artifact in artifacts
        ],
        email_sends=[_email_send_read(row) for row in email_sends],
        notes=[
            BucketNoteRead.model_validate(n).model_copy(
                update={"images": note_images.get(str(n.id), [])}
            )
            for n in notes
        ],
        secure_room_pin=secure_room_pin,
        room_delivery_status=room_delivery_status,
        room_delivery_detail=room_delivery_detail,
    )


async def _start_upload(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerFileUploadInit,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
    source_kind: str = "client_room",
    actor: User | None = None,
    source_detail: str = "",
) -> BucketFileUploadInitResponse:
    """Start an upload into an intake's bucket.

    Six routes with six different trust levels funnel through here — the public
    room, the signed-in borrower, a field rep, a dealer partner, the desk. They
    used to write an identical row, so nothing downstream could tell a client's
    own upload from an operator's on their behalf, and every one of them logged
    the action as the public lead. The caller now says which it is, and the
    actor is the authenticated user rather than a display string the browser
    supplied.
    """
    req = None
    if payload.requested_document_id:
        req = await db.get(BucketRequestedDocument, payload.requested_document_id)
        if (
            req is None
            or req.bucket_id != intake.bucket_id
            or req.status == "not_applicable"
        ):
            raise _stale_requested_document_error(
                "This document request is no longer active. Refresh the intake and choose the current request."
            )
        try:
            await locked_file_requests.require_current_unlocked_copy_upload_target(
                db, req, for_update=True
            )
        except locked_file_requests.StaleUnlockedCopyRequest as exc:
            raise _stale_requested_document_error(str(exc)) from exc
    existing_conditions = [
        BucketFile.bucket_id == intake.bucket_id,
        BucketFile.upload_link_id == intake.bucket_upload_link_id,
        BucketFile.file_name == payload.file_name,
        BucketFile.size_bytes == payload.size_bytes,
        BucketFile.status == "uploading",
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
        uploaded_by_user_id=actor.id if actor is not None else None,
        source_kind=source_kind,
        source_detail=source_detail or None,
        status="uploading",
    )
    db.add(file)
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_file_upload_started",
        request=request,
        user=actor,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role=provenance.actor_role_for(source_kind),
        target_type="file",
        target_id=str(file.id),
        detail=f"{payload.file_name} · {provenance.DOCUMENT_SOURCES.get(source_kind, source_kind)}",
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


class _ZipArchiveTooLarge(ValueError):
    pass


def _read_bucket_object(s3_key: str, *, max_bytes: int | None = None) -> bytes | None:
    bucket, _, _ = _bucket_storage_config()
    body = None
    try:
        obj = _s3_client().get_object(Bucket=bucket, Key=s3_key)
        content_length = int(obj.get("ContentLength") or 0)
        if max_bytes is not None and content_length > max_bytes:
            raise _ZipArchiveTooLarge
        body = obj["Body"]
        data = body.read(max_bytes + 1) if max_bytes is not None else body.read()
        if max_bytes is not None and len(data) > max_bytes:
            raise _ZipArchiveTooLarge
        return data
    except _ZipArchiveTooLarge:
        raise
    except Exception:  # noqa: BLE001
        return None
    finally:
        if body is not None:
            body.close()


def _put_bucket_object(
    s3_key: str,
    content_type: str,
    data: bytes,
    *,
    prevent_overwrite: bool = False,
) -> str | None:
    bucket, _, kms_key_id = _bucket_storage_config()
    params = dict(
        Bucket=bucket,
        Key=s3_key,
        Body=data,
        ContentType=content_type,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=kms_key_id,
    )
    if prevent_overwrite:
        params["IfNoneMatch"] = "*"
    response = _s3_client().put_object(**params)
    return response.get("VersionId")


def _zip_directory_metadata(raw: bytes) -> tuple[int, int] | None:
    """Read entry count and central-directory size without opening the ZIP.

    ``zipfile.ZipFile`` materializes the entire central directory. Checking the
    end record first prevents a small public archive containing hundreds of
    thousands of zero-byte entries from exhausting a worker before the normal
    per-entry cap can run. ZIP64 entry-count overflow is rejected here because
    this upload path intentionally supports only a small evidence bundle.
    """

    signature = b"PK\x05\x06"
    start = max(0, len(raw) - 65_557)
    offset = raw.rfind(signature, start)
    if offset < start or offset + 22 > len(raw):
        return None
    # This evidence-bundle path never needs ZIP64 (60 entries / 100 MiB max).
    # Python's ZipFile follows a ZIP64 locator and replaces the classic EOCD
    # values, so trusting only forged-small classic fields would bypass the
    # pre-materialization limits below.
    if offset >= 20 and raw[offset - 20 : offset - 16] == b"PK\x06\x07":
        return None
    unpacked = struct.unpack_from("<4s4H2LH", raw, offset)
    comment_length = unpacked[7]
    if offset + 22 + comment_length != len(raw):
        return None
    if (
        unpacked[1] != 0
        or unpacked[2] != 0
        or unpacked[3] != unpacked[4]
        or unpacked[4] == 0xFFFF
        or unpacked[5] == 0xFFFFFFFF
        or unpacked[6] == 0xFFFFFFFF
    ):
        return None
    return int(unpacked[4]), int(unpacked[5])


def _mark_zip_retry(
    file: BucketFile,
    *,
    reason: str,
    extracted: int = 0,
) -> None:
    """Persist a bounded retry state after a transient archive failure."""

    prior_runs = 0
    try:
        prior = json.loads(getattr(file, "extraction_reason", None) or "[]")
        if isinstance(prior, list) and prior and isinstance(prior[0], dict):
            prior_runs = int(prior[0].get("runs") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        prior_runs = 0
    runs = prior_runs + 1
    if runs >= ZIP_FETCH_MAX_RUNS:
        file.extraction_status = "partial" if extracted else "skipped"
    else:
        file.extraction_status = "retryable"
    file.extraction_reason = json.dumps(
        [{"entry": file.file_name, "reason": reason, "runs": runs}]
    )


async def _extract_zip_bucket_files(
    db: AsyncSession,
    file: BucketFile,
    request: Request | None,
    *,
    actor_name: str,
    actor_email: str,
) -> None:
    if not _is_zip_upload(file):
        return
    locked_file = (
        await db.execute(
            select(BucketFile)
            .where(BucketFile.id == file.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_file is None:
        return
    file = locked_file
    if file.extraction_status in {"extracted", "partial", "skipped"}:
        return
    try:
        raw = None
        for attempt in range(ZIP_FETCH_READ_ATTEMPTS):
            raw = await asyncio.to_thread(
                _read_bucket_object,
                file.s3_key,
                max_bytes=ZIP_MAX_ARCHIVE_BYTES,
            )
            if raw is not None:
                break
            if attempt + 1 < ZIP_FETCH_READ_ATTEMPTS:
                await asyncio.sleep(0.2 * (2**attempt))
    except _ZipArchiveTooLarge:
        file.extraction_status = "skipped"
        file.extraction_reason = json.dumps(
            [{"entry": file.file_name, "reason": "zip_archive_too_large"}]
        )
        return
    if raw is None:
        # Storage can be momentarily unavailable immediately after a direct or
        # multipart upload completes. The persisted analysis drain retries this
        # state on later ticks, with a cap so one missing object cannot starve
        # the queue forever.
        _mark_zip_retry(file, reason="zip_fetch_failed")
        return
    skipped: list[dict[str, str]] = []
    extracted = 0
    total_bytes = 0
    directory = _zip_directory_metadata(raw)
    if directory is None:
        # ``ZipFile`` deliberately accepts bytes after the end record. Reject
        # those archives here instead of bypassing the bounded central-directory
        # pre-scan and allowing an attacker-controlled entry list to be
        # materialized in worker memory.
        file.extraction_status = "skipped"
        file.extraction_reason = json.dumps(
            [{"entry": file.file_name, "reason": "zip_parse_failed"}]
        )
        return
    if (
        directory[0] > ZIP_MAX_ENTRIES
        or directory[0] == 0xFFFF
        or directory[1] > ZIP_MAX_CENTRAL_DIRECTORY_BYTES
    ):
        file.extraction_status = "skipped"
        file.extraction_reason = json.dumps(
            [{"entry": file.file_name, "reason": "zip_entry_limit"}]
        )
        return
    _, prefix, _ = _bucket_storage_config()
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            for index, member in enumerate(archive.infolist()):
                if index >= ZIP_MAX_ENTRIES:
                    skipped.append({"entry": member.filename, "reason": "zip_entry_limit"})
                    break
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
                    # A previous attempt may have committed this child before a
                    # later entry failed. Count it as recovered evidence so the
                    # eventual terminal parent state is accurate.
                    extracted += 1
                    continue
                try:
                    data = archive.read(member)
                except RuntimeError:
                    skipped.append({"entry": entry_path, "reason": "zip_entry_encrypted"})
                    continue
                total_bytes += len(data)
                content_hash = hashlib.sha256(data).hexdigest()
                existing_content = (
                    await db.execute(
                        select(BucketFile.id)
                        .where(
                            BucketFile.bucket_id == file.bucket_id,
                            BucketFile.content_hash == content_hash,
                            BucketFile.deleted_at.is_(None),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing_content is not None:
                    skipped.append({"entry": entry_path, "reason": "duplicate_existing_file"})
                    continue
                content_type = _sanitize_upload_content_type(_guess_entry_content_type(entry_path))
                child_id = uuid4()
                safe = _safe_filename(entry_path.split("/")[-1])
                child_key = f"{prefix}/uploads/{file.bucket_id}/{child_id}-zip-{safe}"
                _put_bucket_object(child_key, content_type, data)
                child = BucketFile(
                    id=child_id,
                    bucket_id=file.bucket_id,
                    requested_document_id=None,
                    upload_link_id=file.upload_link_id,
                    file_name=entry_path[:255],
                    s3_key=child_key,
                    content_type=content_type,
                    size_bytes=len(data),
                    content_hash=content_hash,
                    uploaded_by_name=actor_name,
                    uploaded_by_email=actor_email,
                    uploaded_by_user_id=file.uploaded_by_user_id,
                    source_kind="zip_extract",
                    source_detail=f"From {file.file_name}"[:200],
                    status="uploaded",
                    parent_zip_file_id=file.id,
                    zip_entry_path=entry_path,
                    extraction_status="extracted",
                )
                db.add(child)
                extracted += 1
    except zipfile.BadZipFile:
        skipped.append({"entry": file.file_name, "reason": "zip_parse_failed"})
    except Exception:  # noqa: BLE001
        log.exception("ZIP extraction failed file=%s", file.id)
        _mark_zip_retry(file, reason="zip_extract_failed", extracted=extracted)
        return
    file.extraction_status = "extracted" if extracted and not skipped else "partial" if extracted else "skipped"
    file.extraction_reason = json.dumps(skipped[-80:])
    if extracted:
        try:
            await _log(
                db,
                file.bucket_id,
                "dealer_ai_zip_extracted",
                request=request,
                actor_name=actor_name,
                actor_email=actor_email,
                actor_role=provenance.actor_role_for(
                    getattr(file, "source_kind", None) or "client_room"
                ),
                target_type="file",
                target_id=str(file.id),
                detail=f"Extracted {extracted} supported file(s) from {file.file_name}",
            )
        except Exception:  # noqa: BLE001
            # The children are already valid evidence. An audit-log outage must
            # not prevent the shared completion path from flushing and queuing
            # them for analysis.
            log.exception("ZIP extraction activity log failed file=%s", file.id)


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
        req = None
        if file.requested_document_id:
            req = await db.get(BucketRequestedDocument, file.requested_document_id)
            if (
                req is None
                or req.bucket_id != intake.bucket_id
                or req.status == "not_applicable"
            ):
                raise _stale_requested_document_error(
                    "This document request is no longer active. Refresh the intake and choose the current request."
                )
            try:
                await locked_file_requests.require_current_unlocked_copy_upload_target(
                    db, req, for_update=True
                )
            except locked_file_requests.StaleUnlockedCopyRequest as exc:
                raise _stale_requested_document_error(str(exc)) from exc
        await _validate_completed_bucket_file(
            db,
            file,
            request,
            action="dealer_ai_file_upload_rejected",
            actor_name=actor_name,
            actor_email=actor_email,
            actor_role=provenance.actor_role_for(file.source_kind or "client_room"),
        )
        file.status = "uploaded"
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
        # The row knows who really did it; the log used to claim the public lead
        # for everyone, including the desk.
        actor_role=provenance.actor_role_for(file.source_kind or "client_room"),
        target_type="file",
        target_id=str(file.id),
        detail=f"{file.file_name} · {provenance.describe_document(file)}",
    )
    # Queue this file (and any files extracted from it as a zip) for background
    # analysis so the review composes from a warm per-file cache.
    try:
        await _reconcile_completed_bucket_file(
            db,
            file,
            request,
            actor_name=actor_name,
            actor_email=actor_email,
        )
    except Exception:  # noqa: BLE001
        log.exception("enqueue file analysis failed intake=%s file=%s", intake.id, file.id)
    # One timeline line for the upload itself; files extracted from a zip
    # ride on their parent's line. The partner's offer sheet is not evidence.
    if not merchant_processing.is_offer_document(file):
        await file_events.emit(
            db,
            intake_id=intake.id,
            kind="document.received",
            visibility=(
                file_events.VISIBILITY_CLIENT
                if file.source_kind in ("client_room", "public_form")
                else file_events.VISIBILITY_TEAM
            ),
            title=f"{file.file_name} was received",
            actor=file.uploaded_by_user_id,
            actor_label=actor_name,
            target_type="file",
            target_id=file.id,
        )
    await db.commit()
    await db.refresh(file)
    if not merchant_processing.is_offer_document(file):
        # The accepted evidence transaction is durable before notification
        # claiming commits and before any external delivery call is attempted.
        await _record_file_uploaded_notification(
            db,
            intake,
            file,
            actor_name=actor_name,
            request=request,
        )
    return file


async def _sign_requested_document(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerDocumentSignRequest,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
    allow_success_fee_agreement: bool = False,
) -> BucketFile:
    """Generic e-sign fulfillment for a requires_signature BucketRequestedDocument.
    Shared by BOTH the dealer and funding-review public routers — identical
    mechanism for both verticals; only the requested document's template/text
    (admin-supplied) differs. Renders the certificate PDF, stores it as a
    normal BucketFile linked via requested_document_id (which alone satisfies
    the checklist via the existing upload-driven status recalculation)."""
    from app.services import document_signature as sig_service
    from app.services.payment_authorization import client_ip

    req = (
        await db.execute(
            select(BucketRequestedDocument)
            .where(BucketRequestedDocument.id == payload.requested_document_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if req is None or req.bucket_id != intake.bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requested document not found")
    if not req.requires_signature:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This requested document does not require a signature")
    is_success_fee = req.signature_kind == "success_fee_agreement"
    if is_success_fee and not allow_success_fee_agreement:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Sign this fee agreement from the authenticated application room",
        )
    if req.status == "uploaded":
        if is_success_fee:
            existing_signature = (
                await db.execute(
                    select(BucketDocumentSignature)
                    .where(BucketDocumentSignature.requested_document_id == req.id)
                    .order_by(BucketDocumentSignature.signed_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            existing_file = (
                await db.get(BucketFile, existing_signature.result_file_id)
                if existing_signature and existing_signature.result_file_id
                else None
            )
            if existing_file is not None and existing_file.deleted_at is None:
                return existing_file
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
        sig_service.CREDIT_AUTHORIZATION_DOCUMENT_VERSION if is_credit_auth
        else "success-fee-2026-10-06-v1" if is_success_fee
        else "custom-1"
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

    if is_success_fee:
        from app.services import ach_fee_workflow

        profile = (
            await db.execute(
                select(ApplicationProfile).where(
                    ApplicationProfile.primary_bucket_id == intake.bucket_id
                )
            )
        ).scalar_one_or_none()
        if profile is None or not await ach_fee_workflow.prepared_agreement_is_current(
            db, profile=profile, requested=req
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The fee agreement is stale; ask QC to prepare a current agreement",
            )
        _qc_signature, qc_signature_png = (
            await ach_fee_workflow.qc_signature_for_prepared_agreement(db, req)
        )
    else:
        qc_signature_png = None

    sig_bytes = b""
    sig_hash = None
    sig_content_type = "image/png"
    if payload.signature_data_url:
        sig_bytes, sig_hash, sig_content_type = sig_service.decode_signature_data_url(
            payload.signature_data_url
        )
    elif not is_success_fee:
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
    if sig_bytes:
        sig_ext = "png" if "png" in sig_content_type else "bin"
        sig_key = f"{prefix}/signatures/{intake.bucket_id}/{signature.id}/signature.{sig_ext}"
        _put_bucket_object(sig_key, sig_content_type, sig_bytes)
        signature.signature_s3_key = sig_key
        signature.signature_hash = sig_hash
    else:
        signature.signature_hash = hashlib.sha256(
            "|".join(
                (
                    signature.document_hash,
                    signature.typed_name,
                    signature.signed_at.isoformat(),
                    signature.ip_address or "",
                )
            ).encode("utf-8")
        ).hexdigest()

    title = "Credit Report Authorization Certificate" if is_credit_auth else f"{req.name} — Signed Certificate"
    pdf_bytes = sig_service.render_signature_certificate_pdf(
        signature=signature,
        title=title,
        document_text=document_text,
        extra_rows=extra_rows,
        qc_signature_png=qc_signature_png,
    )
    cert_key = f"{prefix}/signatures/{intake.bucket_id}/{signature.id}/certificate.pdf"
    cert_version_id = _put_bucket_object(
        cert_key,
        "application/pdf",
        pdf_bytes,
        prevent_overwrite=True,
    )
    signature.certificate_s3_key = cert_key
    signature.certificate_hash = hashlib.sha256(pdf_bytes).hexdigest()

    result_file = BucketFile(
        bucket_id=intake.bucket_id,
        requested_document_id=req.id,
        upload_link_id=intake.bucket_upload_link_id,
        file_name=f"{req.name} - Signed Certificate.pdf"[:255],
        s3_key=cert_key,
        s3_version_id=cert_version_id,
        content_type="application/pdf",
        size_bytes=len(pdf_bytes),
        uploaded_by_name=actor_name,
        uploaded_by_email=actor_email,
        source_kind="generated",
        source_detail=("payment_agreement" if is_success_fee else "signed_document"),
        status="uploaded",
        content_hash=hashlib.sha256(pdf_bytes).hexdigest(),
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
    if not is_success_fee:
        await db.commit()
    await db.refresh(result_file)

    if actor_email and not is_success_fee:
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
    from app.services.drafted_forms import store_form_pdf

    result_file = await store_form_pdf(
        db,
        bucket_id=intake.bucket_id,
        upload_link_id=intake.bucket_upload_link_id,
        requested_document=req,
        pdf_bytes=pdf_bytes,
        file_label=file_label,
        classification=classification,
        key_facts=key_facts,
        actor_name=actor_name,
        actor_email=actor_email,
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
    req = next(
        (
            doc
            for doc in intake.bucket.requested_documents
            if getattr(doc, "requirement_key", None) == "owner_personal_financial_statement"
            or doc.category == "Personal Financials"
        ),
        None,
    )
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
    stored = await _store_drafted_form_pdf(
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
    await _persist_pfs_statement(db, intake, payload, bucket_file=stored)
    await file_events.emit(
        db,
        intake_id=intake.id,
        kind="document.received",
        visibility=file_events.VISIBILITY_CLIENT,
        title="Personal financial statement was submitted",
        actor=stored.uploaded_by_user_id,
        actor_label=actor_name,
        target_type="file",
        target_id=stored.id,
    )
    return stored


async def _persist_pfs_statement(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerPfsSubmission,
    *,
    bucket_file: BucketFile,
) -> None:
    """Keep the numbers, not just the picture of them.

    The PDF has always satisfied the checklist; what was missing was any way to
    reopen the statement, correct it, or finish it for a borrower. Saving the
    rows here means every surface that already submits a PFS starts persisting
    today, before the browser moves to the Form 413 layout.

    Deliberately non-fatal. A borrower who has just filled in a financial
    statement must not see it rejected because we could not file it — the PDF
    is already stored and already satisfies the request, so a failure here costs
    us the structured copy and costs them nothing.
    """
    from app.services import application_profiles as profile_service
    from app.services import financial_statements

    try:
        profile = await profile_service.provision_profile_for_intake(db, intake)
        body = financial_statements.from_legacy_submission(
            assets=list(payload.assets),
            liabilities=list(payload.liabilities),
            owner_full_name=payload.owner_full_name,
            statement_date=payload.statement_date,
        )
        statement = await financial_statements.save_statement(
            db,
            profile,
            body=body,
            statement_date=payload.statement_date,
            status="submitted",
        )
        statement.bucket_file_id = bucket_file.id
        await db.flush()
    except Exception:  # noqa: BLE001
        log.exception(
            "pfs: could not persist the structured statement for intake %s", intake.id
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
    req = next(
        (
            doc
            for doc in intake.bucket.requested_documents
            if getattr(doc, "requirement_key", None) == "business_debt_schedule"
            or doc.category == "Debts"
        ),
        None,
    )
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
    stored = await _store_drafted_form_pdf(
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
    await _persist_client_debt_rows(db, intake, payload)
    await file_events.emit(
        db,
        intake_id=intake.id,
        kind="document.received",
        visibility=file_events.VISIBILITY_CLIENT,
        title="Debt schedule was submitted",
        actor=stored.uploaded_by_user_id,
        actor_label=actor_name,
        target_type="file",
        target_id=stored.id,
    )
    return stored


async def _persist_client_debt_rows(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerDebtScheduleSubmission,
) -> None:
    """Put what the borrower typed on the actual debt schedule.

    There were two debt schedules that never spoke: this form, which rendered a
    PDF and dropped its rows, and `dos_debts`, which is the one the DSCR
    denominator, the confirmation workflow and the refinance workbench read. A
    borrower could fill this in and the DSCR would never know.

    Rows land as `origin='client_form'` — the same standing as a row extracted
    from a document the borrower uploaded, which is the honest comparison: both
    are the borrower's own account of what they owe, neither is confirmed by the
    desk. The desk's existing confirmation step is what promotes them, and
    `count_in_dscr` keeps its existing rule rather than gaining an exception.

    Non-fatal, like the statement: the PDF already satisfies the checklist, so a
    failure here costs the structured copy and costs the borrower nothing.
    """
    from app.dealer_os.models import DealerDebt
    from app.services import application_profiles as profile_service

    try:
        profile = await profile_service.provision_profile_for_intake(db, intake)
        existing = (
            (
                await db.execute(
                    select(DealerDebt).where(
                        DealerDebt.profile_id == profile.id,
                        DealerDebt.origin == "client_form",
                    )
                )
            )
            .scalars()
            .all()
        )
        # The form is the borrower's whole answer, so a resubmission replaces
        # what they said last time rather than appending a second copy of it.
        for row in existing:
            await db.delete(row)
        await db.flush()

        for entry in payload.debts:
            db.add(
                DealerDebt(
                    profile_id=profile.id,
                    dealer_id=profile.dealer_id,
                    lender=entry.lender[:180],
                    balance=entry.balance,
                    monthly_payment=entry.monthly_payment,
                    origin="client_form",
                    status="active",
                )
            )
        await db.flush()
    except Exception:  # noqa: BLE001
        log.exception(
            "debt schedule: could not persist client rows for intake %s", intake.id
        )


def _require_super_admin(user: CurrentUser) -> None:
    """Compatibility gate for the operator intake workspace.

    The route family pre-dates the LOAN_EXEC workspace. Governance-only routes
    use ``_require_governance_admin`` below.
    """
    _require_intake_operator(user)


def _require_intake_operator(user: CurrentUser) -> None:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Underwriter access required")


def _require_governance_admin(user: CurrentUser) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


def _lead_result(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    if mca_refinance_policy.is_mca_refinance(intake, getattr(intake, "bucket", None)):
        policy = mca_refinance_policy.evaluate(intake, bucket=getattr(intake, "bucket", None))
        if not policy["auto_underwriting_allowed"]:
            return mca_refinance_policy.blocked_result(policy)
    review = intake.latest_review if intake.latest_review else None
    if review and isinstance(review.result, dict):
        return review.result
    return intake.result_snapshot if isinstance(intake.result_snapshot, dict) else {}


def _lead_row(
    intake: PublicUnderwritingIntake,
    *,
    underwriting_status: str | None = None,
    effective_profile_hold: bool = False,
) -> DealerAILeadRow:
    result = _lead_result(intake)
    workspace_state, workspace_state_reason = intake_lifecycle.intake_workspace_state(
        bucket_archived_at=intake.bucket.archived_at,
        intake_status=intake.status,
        outcome_status=intake.outcome_status,
        foreclosure_status=intake.foreclosure_rescue_status,
        underwriting_status=underwriting_status,
    )
    active_files = [
        file
        for file in _active_files(intake.bucket)
        if not _is_package_readiness_output(file)
    ]
    missing_docs = _missing_required_docs(intake.bucket)
    return DealerAILeadRow(
        id=intake.id,
        mca_refinance_policy=(mca_refinance_policy.evaluate(intake, bucket=intake.bucket) if mca_refinance_policy.is_mca_refinance(intake, intake.bucket) else None),
        variant=intake.variant,
        client_id=intake.client_id,
        bucket_id=intake.bucket_id,
        bucket_name=intake.bucket.name,
        full_name=intake.full_name,
        email=intake.email,
        phone=intake.phone,
        business_name=intake.business_name,
        referral_source=intake.referral_source,
        opened_by_name=intake.broker.name if intake.broker else "House desk",
        opened_by_role="Dealer partner" if intake.broker else "Internal",
        status=intake.status,
        outcome_status=intake.outcome_status,
        underwriting_status=underwriting_status,
        workspace_state=workspace_state,
        workspace_state_reason=workspace_state_reason,
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
        archived_at=intake.bucket.archived_at,
        protected_until=max(
            (
                value
                for value in (
                    getattr(intake, "protected_until", None),
                    getattr(intake.bucket, "protected_until", None),
                )
                if value is not None
            ),
            default=None,
        ),
        legal_hold=bool(getattr(intake, "legal_hold", False)),
        effective_legal_hold=(
            bool(getattr(intake, "legal_hold", False))
            or bool(getattr(intake.bucket, "legal_hold", False))
            or effective_profile_hold
        ),
        retention_state=(
            "legal_hold"
            if getattr(intake, "legal_hold", False)
            or getattr(intake.bucket, "legal_hold", False)
            or effective_profile_hold
            else (
                "protected"
                if getattr(intake, "retention_state", "unclassified") == "protected"
                or getattr(intake.bucket, "retention_state", "unclassified")
                == "protected"
                else getattr(intake, "retention_state", "unclassified")
            )
        ),
        delete_requested_at=intake.delete_requested_at,
        delete_requested_by=intake.delete_requested_by.name if intake.delete_requested_by else None,
    )


def _intake_search_clause(query: str):
    raw = query.strip()
    needle = f"%{raw.lower()}%"
    clauses = [
        func.lower(PublicUnderwritingIntake.full_name).like(needle),
        func.lower(PublicUnderwritingIntake.email).like(needle),
        func.lower(PublicUnderwritingIntake.business_name).like(needle),
        func.lower(PublicUnderwritingIntake.phone).like(needle),
        func.lower(Bucket.name).like(needle),
        func.lower(Bucket.client_name).like(needle),
    ]
    digits = re.sub(r"\D+", "", raw)
    if digits:
        phone_digits = func.replace(
            func.replace(
                func.replace(
                    func.replace(
                        func.replace(func.coalesce(PublicUnderwritingIntake.phone, ""), " ", ""),
                        "-",
                        "",
                    ),
                    "(",
                    "",
                ),
                ")",
                "",
            ),
            "+",
            "",
        )
        clauses.append(phone_digits.like(f"%{digits}%"))
    return or_(*clauses)


def _closed_intake_lifecycle_clause():
    """SQL counterpart to ``intake_workspace_state`` for queue pagination.

    A lifecycle-closed file belongs in the archive/closed queue but its bucket
    remains unarchived so reviewers can still inspect and download evidence.
    The correlated profile check also repairs visibility for older rows whose
    canonical underwriting status changed before intake mirroring existed.
    """

    def profile_has_status(*values: str):
        return (
            select(ApplicationProfile.id)
            .where(
                ApplicationProfile.intake_id == PublicUnderwritingIntake.id,
                ApplicationProfile.underwriting_status.in_(values),
            )
            .exists()
        )

    foreclosure_status = func.coalesce(
        PublicUnderwritingIntake.foreclosure_rescue_status, ""
    )
    canonical_funded = or_(
        profile_has_status("closed_won"),
        foreclosure_status == "funded",
    )
    closed_lifecycle = or_(
        profile_has_status(*intake_lifecycle.TERMINAL_UNDERWRITING_STATUSES),
        foreclosure_status.in_(intake_lifecycle.TERMINAL_FORECLOSURE_STATUSES),
        PublicUnderwritingIntake.status.in_(intake_lifecycle.TERMINAL_INTAKE_STATUSES),
        PublicUnderwritingIntake.outcome_status.in_(
            intake_lifecycle.TERMINAL_OUTCOME_STATUSES
        ),
    )
    return and_(~canonical_funded, closed_lifecycle)


def _intake_status_filter_clause(status_filter: str):
    """Resolve user-facing lifecycle aliases against authoritative sources."""

    normalized = status_filter.strip().lower()

    def profile_status(*values: str):
        return (
            select(ApplicationProfile.id)
            .where(
                ApplicationProfile.intake_id == PublicUnderwritingIntake.id,
                ApplicationProfile.underwriting_status.in_(values),
            )
            .exists()
        )
    if normalized in {"denied", "disqualified"}:
        return or_(
            PublicUnderwritingIntake.status.in_(("denied", "disqualified")),
            PublicUnderwritingIntake.outcome_status.in_(("denied", "disqualified")),
            func.coalesce(PublicUnderwritingIntake.foreclosure_rescue_status, "")
            == "declined",
            profile_status("denied"),
        )
    if normalized in {"closed_lost", "lost", "withdrawn", "withdrawn_expired"}:
        return or_(
            PublicUnderwritingIntake.status.in_(("closed", "closed_lost")),
            func.coalesce(PublicUnderwritingIntake.foreclosure_rescue_status, "")
            == "withdrawn_expired",
            profile_status("closed_lost"),
        )
    if normalized in {"closed_won", "funded"}:
        return or_(
            func.coalesce(PublicUnderwritingIntake.foreclosure_rescue_status, "")
            == "funded",
            profile_status("closed_won"),
        )
    if normalized == "completed":
        # Legacy ``completed`` also carried funded rows.  The UI exposes a
        # dedicated Funded filter now, so Review complete must exclude records
        # whose canonical lifecycle proves funding.
        canonical_funded = or_(
            func.coalesce(PublicUnderwritingIntake.foreclosure_rescue_status, "")
            == "funded",
            profile_status("closed_won"),
        )
        return and_(
            PublicUnderwritingIntake.status == "completed",
            ~canonical_funded,
        )
    return PublicUnderwritingIntake.status == status_filter


async def _underwriting_statuses_by_intake(
    db: AsyncSession,
    intake_ids: list[UUID],
) -> dict[UUID, str]:
    if not intake_ids:
        return {}
    rows = (
        await db.execute(
            select(
                ApplicationProfile.intake_id,
                ApplicationProfile.underwriting_status,
            ).where(ApplicationProfile.intake_id.in_(intake_ids))
        )
    ).all()
    return {
        intake_id: underwriting_status
        for intake_id, underwriting_status in rows
        if intake_id is not None
    }


async def _profile_hold_intake_ids(
    db: AsyncSession,
    intake_ids: list[UUID],
) -> set[UUID]:
    if not intake_ids:
        return set()
    rows = (
        await db.execute(
            select(ApplicationProfile.intake_id).where(
                ApplicationProfile.intake_id.in_(intake_ids),
                ApplicationProfile.legal_hold.is_(True),
            )
        )
    ).scalars().all()
    return {row for row in rows if row is not None}


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
    # This is shared with operator-files/application-profiles so secondary
    # interfaces cannot bypass the signed-agreement gate.
    await require_dealer_partner_standing(db, user)


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
                PublicUnderwritingIntake.variant == DEALER_VARIANT,
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
    if mca_refinance_policy.is_mca_refinance(intake, intake.bucket):
        policy = mca_refinance_policy.persist(intake)
        if not policy["auto_underwriting_allowed"]:
            if policy["review_mode"] == "manual_review":
                mca_refinance_policy.persist(intake, request_manual=True)
                intake.status = "manual_review"
                await _log(db, intake.bucket_id, "mca_refinance_manual_review_requested", request=request,
                           actor_name=actor_name, actor_email=actor_email, actor_role=actor_role,
                           target_type="mca_refinance_intake", target_id=str(intake.id),
                           detail="Application accepted for human review; automatic underwriting not run: " + ", ".join(policy["reason_codes"]))
            return None
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
        file_ids=[
            str(file.id)
            for file in _active_files(intake.bucket)
            if not _is_package_readiness_output(file)
        ],
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
    await mca_refinance_policy.require_automatic(db, intake.bucket, intake=intake)
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
        file_ids=[
            str(file.id)
            for file in _active_files(intake.bucket)
            if not _is_package_readiness_output(file)
        ],
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
                if isinstance(review.result, dict):
                    await _record_super_admin_decision_notification(
                        db,
                        intake,
                        review.result,
                        request=None,
                        review_id=review.id,
                    )
        except Exception:  # noqa: BLE001
            await db.rollback()
            log.exception("background review snapshot failed review=%s", review_id)


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


class DealerPartnerOption(BaseModel):
    id: UUID
    name: str
    email: str


class AssignPartnerRequest(BaseModel):
    broker_user_id: UUID | None = None


async def _load_dealer_partner_user(db: AsyncSession, user_id: UUID) -> User:
    partner = await db.get(User, user_id)
    if (
        partner is None
        or partner.role != Role.DEALER_PARTNER
        or partner.deleted_at is not None
        or getattr(partner, "account_status", "active") != "active"
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected user is not an active dealer partner.")
    return partner


async def _create_admin_ai_lead_core(
    payload: AdminLeadCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession,
    *,
    commit: bool = True,
    client_override: Client | None = None,
) -> DealerIntakeResponse:
    """Create an operator-owned intake after the caller has authorized access."""
    if payload.variant not in _ADMIN_VARIANT_CONSTANTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"variant must be one of {', '.join(sorted(_ADMIN_VARIANT_CONSTANTS))}",
        )
    is_re = payload.variant == "real_estate"
    is_ms = payload.variant == "main_street"
    is_mca = payload.variant == "mca_refinance"
    variant_const = _ADMIN_VARIANT_CONSTANTS[payload.variant]

    # Normalized here rather than at the edge so an unrecognized value becomes
    # "other"/"not_sure" instead of 422-ing a lead an operator is mid-way through
    # creating. Both are needed before the bucket exists: intent decides which
    # documents get seeded, and seeding the wrong package is the whole bug this
    # branch was added to fix.
    ms_intent = normalize_intent(payload.intent) if is_ms else None
    ms_industry = normalize_industry(payload.industry) if is_ms else None
    equipment_details = _equipment_details_payload(payload.equipment_details)
    equipment_application = (
        not is_re
        and not is_mca
        and _is_equipment_application(
            funding_intent=ms_intent if is_ms else payload.loan_purpose,
            details=payload.equipment_details,
        )
    )

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

    actor_is_client = user.role == Role.CLIENT
    provenance = {
        ("created_by_client" if actor_is_client else "created_by_admin"): {
            "user_id": str(user.id),
            "name": user.name,
            "email": user.email,
            "at": _now().isoformat(),
        },
        "on_behalf_of_client": not actor_is_client,
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
        client = client_override or await _find_or_create_funding_client(db, adapter)
        bucket, link = await _create_bucket_for_funding_review(
            db, client, adapter, request, room_pin=payload.secure_room_pin
        )
    elif is_ms:
        adapter = DealerIntakeStart(
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            business_name=payload.business_name,
            funding_intent="equipment" if equipment_application else ms_intent,
            equipment_details=payload.equipment_details,
        )
        client = client_override or await _find_or_create_client(db, adapter)
        bucket, link = await _create_bucket_for_main_street(
            db,
            client,
            adapter,
            request,
            intent=ms_intent,
            industry=ms_industry,
            room_pin=payload.secure_room_pin,
        )
    elif is_mca:
        adapter = McaRefiStart(
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            business_name=payload.business_name,
        )
        client = client_override or await _find_or_create_mca_client(db, adapter)
        bucket, link = await _create_bucket_for_mca_refi(
            db,
            client,
            adapter,
            request,
            room_pin=payload.secure_room_pin,
        )
    else:
        adapter = DealerIntakeStart(
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            business_name=payload.business_name,
            funding_intent="equipment" if equipment_application else None,
            equipment_details=payload.equipment_details,
        )
        client = client_override or await _find_or_create_client(db, adapter)
        bucket, link = await _create_bucket_for_intake(
            db, client, adapter, request, room_pin=payload.secure_room_pin
        )

    # CRM traceability: mark that this client/lead originated from an admin action.
    if not actor_is_client and isinstance(client.lead_intake, dict):
        client.lead_intake = {**client.lead_intake, "created_by_admin": str(user.id)}

    token = _new_public_token()
    intake_state: dict[str, Any] = {
        "messages": [],
        "source": (
            "funding_review"
            if is_re
            else "main_street_intake"
            if is_ms
            else "mca_refinance"
            if is_mca
            else "dealer_ai_intake"
        ),
        "admin_provenance": provenance,
    }
    if is_ms:
        # The key _main_street_details() reads. Without it _main_street_context
        # falls back to "other"/"not_sure" and the program screen runs blind.
        intake_state["main_street_details"] = {
            "intent": ms_intent,
            "industry": ms_industry,
        }
    if equipment_application:
        intake_state["funding_intent"] = "equipment"
        if not is_ms:
            intake_state["dealer_details"] = {
                "financing_equipment_or_vehicle": True,
            }
        if equipment_details:
            intake_state["equipment_financing_details"] = equipment_details
    if payload.capital_readiness_diagnostic is not None:
        intake_state["capital_readiness_diagnostic"] = (
            payload.capital_readiness_diagnostic.model_dump(mode="json", by_alias=True)
        )
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
        source_kind="internal_user",
        source_actor_name=(user.name or user.email or "")[:200],
        source_user_id=user.id,
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=_hash_token(token),
        variant=variant_const,
        full_name=payload.full_name.strip(),
        email=client.email or _normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=(payload.investor_name if is_re else payload.business_name),
        loan_purpose=(
            payload.transaction_type
            if is_re
            else "Equipment financing"
            if equipment_application
            else payload.loan_purpose or ("MCA refinance" if is_mca else None)
        ),
        requested_loan_amount=payload.requested_amount,
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
    if equipment_application:
        await _configure_equipment_bucket(
            db,
            bucket,
            details=equipment_details,
        )
    await _log(
        db,
        bucket.id,
        "capital_readiness_claim_created"
        if actor_is_client
        else "dealer_ai_lead_created_by_admin",
        request=request,
        user=user,
        actor_role=(
            "client"
            if actor_is_client
            else "super_admin"
            if user.role == Role.SUPER_ADMIN
            else "underwriter"
        ),
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=(
            f"Client claimed a Capital Readiness diagnostic for {intake.email}"
            if actor_is_client
            else f"Admin created {payload.variant} lead for {intake.email}"
        ),
    )
    welcome_text = _admin_created_welcome(variant_const)
    db.add(_persist_admin_welcome_message(bucket.id, welcome_text))
    if commit:
        await db.commit()
    else:
        await db.flush()
    intake = await _load_admin_dealer_lead(db, intake.id)

    email_note = ""
    delivery_status = "created"
    delivery_detail = "Room created without sending"
    if payload.notify_client:
        room_path = f"/buckets/request/{link.token}"
        delivery = await consent_delivery.deliver_link_checked(
            db,
            channel="email",
            to_email=intake.email,
            to_phone=intake.phone,
            business_name=intake.business_name or intake.full_name,
            purpose="open the secure application room",
            path=room_path,
            rep_name=user.name,
            origin=get_settings().frontend_app_url,
        )
        delivery_status = "sent" if delivery.ok else "failed"
        delivery_detail = delivery.detail
        db.add(
            ApplicationRoomDelivery(
                bucket_id=bucket.id,
                action_kind="room_created",
                channel="email",
                recipient_email=intake.email,
                recipient_phone=intake.phone,
                status=delivery_status,
                detail=delivery.detail,
                provider_result={
                    "accepted": delivery.ok,
                    "email_ok": delivery.email_ok,
                    "sms_ok": delivery.sms_ok,
                },
                created_by_user_id=user.id,
            )
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        intake = await _load_admin_dealer_lead(db, intake.id)
        email_note = (
            " The secure room link was emailed to the client; share the PIN separately."
            if delivery.ok
            else " Room email delivery failed; copy the room link and share the PIN separately."
        )

    return await _response(
        db,
        intake,
        token=token,
        # None for Main Street: it has no client room yet, and the dealer room
        # would ask an operating business floorplan and collateral questions.
        public_path=(
            FUNDING_PUBLIC_PATH if is_re
            else None if is_ms
            else MCA_PUBLIC_PATH if payload.variant == "mca_refinance"
            else "/dealer-ai-underwriter"
        ),
        include_management=True,
        admin_thread=True,
        thread_user=user,
        assistant_message=welcome_text + email_note,
        secure_room_pin=payload.secure_room_pin,
        room_delivery_status=delivery_status,
        room_delivery_detail=delivery_detail,
    )


# ── Dealer-partner (broker) endpoints ───────────────────────────────────
# Curated subset for Role.DEALER_PARTNER: create/view/chat/upload/run-review
# on their OWN leads only (broker_id == user.id). No credit-pull, program-fit,
# vendor-email, exports, client-thread, or drive-ingest routes exist here —
# those stay admin-only by design (see plan doc for the full rationale).


async def _build_channel_inbox(
    db: AsyncSession, user: User, intakes: list[PublicUnderwritingIntake]
) -> DealerLeadInboxResponse:
    # Only leads that actually have a team<->partner thread belong in the inbox.
    threaded = [i for i in intakes if any(n.visibility == "admin" for n in i.bucket.notes)]
    unread = await _channel_unread_by_intake(db, user=user, intakes=threaded)
    items = [_inbox_item(i, unread.get(i.id, 0)) for i in threaded]
    items.sort(key=lambda it: it.last_message_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return DealerLeadInboxResponse(items=items, total_unread=sum(it.unread_count for it in items))


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


async def _request_pfs(db: AsyncSession, intake: PublicUnderwritingIntake, owner_name: str | None, *, actor: Any = None) -> BucketRequestedDocument:
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
    doc = await _ensure_requested_document(db, intake.bucket, name=name, category="Personal Financials", description=description, allow_multiple_files=True, actor=actor)
    await db.commit()
    return doc


async def _request_debt_schedule(db: AsyncSession, intake: PublicUnderwritingIntake, *, actor: Any = None) -> BucketRequestedDocument:
    """Shared by admin+broker request-debt-schedule endpoints. Idempotent via
    _ensure_requested_document — a lead only ever has one debt schedule, so
    there is no owner_name variant here (unlike PFS)."""
    doc = await _ensure_requested_document(
        db,
        intake.bucket,
        name="Debt schedule",
        category="Debts",
        description="Upload a schedule of all outstanding business debt: lender, balance, and monthly payment for each.",
        actor=actor,
    )
    await db.commit()
    return doc


_CONTRACT_DOC_NAME: dict[ContractType, str] = {
    ContractType.SBA_ENGAGEMENT: "SBA Advisory and Packaging Engagement Agreement",
    ContractType.CLIENT_ENGAGEMENT: "Capital Advisory and Placement Engagement Agreement",
    ContractType.CONSULTING_ADDENDUM: "Consulting and Fee Schedule Addendum",
}


class LeadDscrPotentialResponse(BaseModel):
    """Admin-only read of the deterministic DSCR-potential screen for a
    real-estate lead — same live-recompute pattern as LeadProgramFitResponse
    so the admin always sees figures derived from the current facts."""
    computed: bool
    potential: dict[str, Any] = Field(default_factory=dict)


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


async def _client_chat_turn(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    message: str,
    *,
    actor_name: str,
    user: User | None = None,
) -> tuple[list[Any], str | None, bool]:
    """Record the borrower's own turn in the client thread.

    While the desk has taken the conversation over, the message is recorded and
    the assistant is not asked for anything — the borrower is talking to a person,
    and an AI answer on top of that is exactly what the takeover exists to stop.
    Returns (messages, assistant_message, paused)."""
    link = intake.bucket_upload_link
    if link is not None and engagement.is_paused(link):
        row = await create_human_message(
            db,
            bucket=intake.bucket,
            audience="uploader",
            message=message,
            actor_name=actor_name,
            user=user,
            upload_link=link,
            sender_kind="client",
        )
        return [row], TAKEOVER_CLIENT_NOTICE, True
    chat_messages, _, _ = await create_chat_reply(
        db,
        bucket=intake.bucket,
        audience="uploader",
        message=message,
        actor_name=actor_name,
        user=user,
        upload_link=link,
        preferred_language=intake.preferred_language,
        intake_id=getattr(intake, "id", None),
        sender_kind="client",
    )
    return chat_messages, (chat_messages[-1].content if chat_messages else None), False


class ClientSmsMessageRead(BaseModel):
    id: UUID
    direction: str
    phone_e164: str
    body: str | None = None
    provider: str
    provider_message_id: str
    status: str
    detail: str
    context: str
    portal_message_id: UUID | None = None
    created_at: datetime


class ClientSmsStateRead(BaseModel):
    phone: str | None = None
    can_send: bool = False
    transactional_consented: bool = False
    opted_out: bool = False
    provider_available: bool = False
    blocked_reason: str | None = None


class ClientThreadResponse(BaseModel):
    messages: list[BucketAIMessageRead] = Field(default_factory=list)
    # Set while the desk has taken the conversation over and the AI is standing down.
    ai_paused_until: datetime | None = None
    # SMS follows the normalized client number. A row linked to a portal message
    # is rendered as a second transport on that message rather than a duplicate.
    sms_messages: list[ClientSmsMessageRead] = Field(default_factory=list)
    sms_state: ClientSmsStateRead = Field(default_factory=ClientSmsStateRead)


async def _client_thread_response(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    *,
    paused_until: datetime | None = None,
) -> ClientThreadResponse:
    from app.dealer_os.services import sms_consent as sms_consent_svc
    from app.services.sms import is_opted_out, sms_available, unavailable_reason

    messages = await _client_thread_messages(db, intake)
    link = intake.bucket_upload_link
    if paused_until is None and link and engagement.is_paused(link):
        paused_until = link.ai_paused_until
    phone = consent_delivery.normalize_phone(intake.phone)
    rows: list[SmsMessage] = []
    if phone:
        rows = list(
            (
                await db.execute(
                    select(SmsMessage)
                    .where(SmsMessage.phone_e164 == phone)
                    .order_by(SmsMessage.created_at.asc())
                    .limit(200)
                )
            ).scalars().all()
        )
    opted_out = bool(phone and await is_opted_out(db, phone))
    consent = await sms_consent_svc.consent_for(db, phone_e164=phone, kind="transactional") if phone else None
    ready = sms_available()
    reason = None
    if not phone:
        reason = "No mobile number is on this file."
    elif opted_out:
        reason = "This number opted out of text messages."
    elif consent is None:
        reason = "Transactional SMS consent is required."
    elif not ready:
        reason = unavailable_reason() or "SMS delivery is not configured."
    return ClientThreadResponse(
        messages=[BucketAIMessageRead.model_validate(message) for message in messages],
        ai_paused_until=paused_until,
        sms_messages=[
            ClientSmsMessageRead(
                id=row.id,
                direction=row.direction,
                phone_e164=row.phone_e164,
                body=row.body,
                provider=row.provider,
                provider_message_id=row.provider_message_id,
                status=row.status,
                detail=row.detail,
                context=row.context,
                portal_message_id=row.portal_message_id,
                created_at=row.created_at,
            )
            for row in rows
        ],
        sms_state=ClientSmsStateRead(
            phone=phone,
            can_send=bool(phone and consent and not opted_out and ready),
            transactional_consented=consent is not None,
            opted_out=opted_out,
            provider_available=ready,
            blocked_reason=reason,
        ),
    )


# Shown to the borrower in place of an AI answer while a human is replying. It is
# never written to the thread and never comes from the model.
TAKEOVER_CLIENT_NOTICE = (
    "Your underwriter is replying to you here. They will answer you directly, so "
    "the assistant is standing by."
)


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
    It validates that both current PDFs belong to one fresh generation and
    regenerates the email template (side-effects the caller is expected to
    commit)."""
    summary_artifact, packet_artifact = await _fresh_package_artifact_pair(
        db,
        intake,
    )
    email_draft = await _generate_management_json(
        db,
        intake,
        user,
        purpose="vendor_email",
        extra={"executive_summary": summary_artifact.body_json, "lender_packet_title": packet_artifact.title},
    )

    label = _safe_filename(intake.business_name or intake.full_name or "lead")
    # The processing partner's proposal (residual lines and all) is not a
    # borrower document and never ships to a lender.
    files = sorted(
        (
            f
            for f in _active_files(intake.bucket)
            if not merchant_processing.is_offer_document(f)
            and not _is_package_readiness_output(f)
        ),
        key=lambda f: f.file_name.lower(),
    )
    manifest_lines = [
        "Qualified Commercial — Underwriting Package",
        f"Borrower/entity: {intake.business_name or intake.full_name or '-'}",
        f"Contact: {intake.full_name or '-'} <{intake.email or '-'}>",
        f"Generated: {_now().strftime('%b %d, %Y %I:%M %p UTC')}",
        "",
        "Contents:",
        "  executive-summary.pdf  — concise underwriter executive summary",
        "  lender-packet.pdf       — formatted lender/vendor packet",
        "  email-template.txt      — ready-to-edit vendor outreach email",
        f"  documents/              — {len(files)} uploaded file(s)",
        "",
        "Uploaded documents:",
    ]

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Executive summary PDF. Legacy artifacts created before PDF support
        # retain the markdown fallback so old files remain downloadable.
        if summary_artifact.s3_key:
            try:
                zf.writestr("executive-summary.pdf", await _s3_bytes(summary_artifact.s3_key))
            except Exception:  # noqa: BLE001
                log.exception("package.zip: executive summary fetch failed intake=%s", intake.id)
                zf.writestr(
                    "executive-summary.md",
                    summary_artifact.body_text or "Awaiting AI executive summary.",
                )
        else:
            zf.writestr(
                "executive-summary.md",
                summary_artifact.body_text or "Awaiting AI executive summary.",
            )
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


async def _intake_chat_action(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    action_id: UUID,
) -> BucketAIChatAction:
    action = (
        await db.execute(
            select(BucketAIChatAction).where(
                BucketAIChatAction.id == action_id,
                BucketAIChatAction.bucket_id == intake.bucket_id,
                BucketAIChatAction.upload_link_id == intake.bucket_upload_link_id,
            )
        )
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat action not found")
    if action.expires_at <= _now() and action.status == "available":
        action.status = "expired"
        await db.flush()
    if action.status in {"expired", "disabled"}:
        raise HTTPException(status.HTTP_410_GONE, "This action is no longer available")
    return action


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
    if intake.variant not in {FUNDING_VARIANT, FORECLOSURE_RESCUE_VARIANT}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Funding review not found")


DEALER_VARIANT = DEALER_INTAKE_VARIANT
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
    "foreclosure_rescue": FORECLOSURE_RESCUE_VARIANT,
}

_VARIANT_LABELS: dict[str, str] = {
    DEALER_VARIANT: "dealer capital",
    FUNDING_VARIANT: "real estate DSCR/investor",
    MAIN_STREET_VARIANT: "operating business",
    MCA_VARIANT: "MCA refinance",
    FORECLOSURE_RESCUE_VARIANT: "commercial foreclosure rescue",
}


def _variant_label(variant: str | None) -> str:
    """Human label used in executive summaries and lender emails.

    Was a two-way ternary defaulting to "dealer capital", which mislabelled every
    Main Street lead in customer-visible output.
    """
    return _VARIANT_LABELS.get(variant or "", "commercial capital")


def _require_dealer_intake(intake: PublicUnderwritingIntake) -> None:
    """Allow the legacy dealer room and its dedicated equipment handoff only.

    The equipment page safely reuses this route family, but its intake remains
    a Main Street file. Other Main Street, MCA, and real-estate tokens stay
    undiscoverable here.
    """
    if intake.variant != DEALER_VARIANT and not _is_main_street_equipment_intake(intake):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI intake not found")


async def _load_funding_intake_by_session(db: AsyncSession, request: Request) -> tuple[PublicUnderwritingIntake, DealerIntakeLoginChallenge, str]:
    session_token = request.headers.get("x-funding-session") or request.headers.get("X-Funding-Session")
    if not session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Funding review session is required")
    intake, challenge = await _load_intake_by_dealer_session(db, session_token)
    _require_funding_intake(intake)
    return intake, challenge, session_token


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


class McaRefiStart(PublicIntakeAttribution):
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: RequiredPhone
    business_name: str | None = Field(default=None, max_length=180)
    active_mca_count: int | None = Field(default=None, ge=0, le=50, strict=True)
    requested_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    requested_term_months: int | None = Field(default=None, ge=1, le=1200, strict=True)
    manual_review_requested: bool = Field(default=False, strict=True)
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
    advances: list[McaAdvanceRow] = Field(min_length=1, max_length=50)
    acknowledgment: bool


class McaRefiQualification(BaseModel):
    active_mca_count: int | None = Field(default=None, ge=0, le=50, strict=True)
    requested_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    requested_term_months: int | None = Field(default=None, ge=1, le=1200, strict=True)
    manual_review_requested: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def require_facts_or_manual_request(self) -> McaRefiQualification:
        if not self.manual_review_requested and (self.active_mca_count is None or self.requested_amount is None):
            raise ValueError("Active MCA count and requested amount are required unless requesting manual desk review.")
        return self


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
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 50 and float(value).is_integer():
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
    mca_refinance_policy.persist(intake)


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
        "mca_qualification": state.get("mca_qualification"),
        "mca_terms_summary": state.get("mca_terms_summary"),
        "mca_refinance_policy": mca_refinance_policy.evaluate(intake, bucket=intake.bucket),
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
    attribution = _public_intake_attribution(payload)
    lead_payload = {
        "source": "mca_refinance",
        "business_name": payload.business_name,
        "remaining_payback": payload.remaining_payback,
        "months_remaining": payload.months_remaining,
        "payment_frequency": payload.payment_frequency,
        **({"signup_attribution": attribution} if attribution else {}),
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


async def _create_bucket_for_mca_refi(
    db: AsyncSession,
    client: Client,
    payload: McaRefiStart,
    request: Request,
    *,
    room_pin: str | None = None,
) -> tuple[Bucket, BucketUploadLink]:
    owner = await primary_super_admin(db)
    business = payload.business_name or payload.full_name
    bucket = Bucket(
        name=f"{business} MCA Refinance",
        bucket_type="mca_refinance_intake",
        client_name=business,
        name_sync_mode="linked",
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
    passcode = room_pin or _generate_passcode()
    link = BucketUploadLink(
        bucket_id=bucket.id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.full_name.strip(),
        recipient_email=client.email,
        allow_notes=True,
        allow_multiple_sessions=True,
        can_use_ai_chat=True,
        can_view_ai_tasks=True,
    )
    # The room PIN has to be recoverable, or a client can never be handed into
    # their own room: only the hash was stored here, so the code was discarded.
    client_room._store_passcode(link, passcode)
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


# One public pull per intake, throttled per IP — this endpoint spends bureau
# money on an unauthenticated (token-bearing) caller, so it is deliberately
# stingier than /start.
_MCA_PULL_LAST_BY_IP: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Route handlers live beside this module, one file per router
# (app/routers/dealer_ai_intake_routes/). Importing them registers the routes;
# the star imports keep every handler reachable as an attribute of this module,
# which tests and callers rely on. Helpers and service imports stay here on
# purpose: tests patch them on this module, and the handlers resolve them
# through it at call time.
# ---------------------------------------------------------------------------
from app.routers.dealer_ai_intake_routes.admin import *  # noqa: E402, F401, F403
from app.routers.dealer_ai_intake_routes.broker import *  # noqa: E402, F401, F403
from app.routers.dealer_ai_intake_routes.client import *  # noqa: E402, F401, F403
from app.routers.dealer_ai_intake_routes.funding_review import *  # noqa: E402, F401, F403
from app.routers.dealer_ai_intake_routes.mca_refinance import *  # noqa: E402, F401, F403
from app.routers.dealer_ai_intake_routes.public_intake import *  # noqa: E402, F401, F403
