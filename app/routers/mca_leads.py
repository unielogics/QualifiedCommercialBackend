"""Public MCA acquisition form, saved into the existing CRM lead queue.

This is a request for a funding review, not an approval or a credit application.
Qualification is enforced before writes; no new database tables are required.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import request_context
from app.db import get_db
from app.models.activity import Activity
from app.models.client import Client
from app.schemas.phone import RequiredPhone
from app.services.payment_authorization import primary_super_admin

router = APIRouter(prefix="/public/mca", tags=["public-mca"])
CONSENT_VERSION = "mca-funding-review-v1"
CONSENT_TEXT = (
    "I authorize Qualified Commercial to contact me by phone or email about this funding review. "
    "I confirm that my business has no more than 2 active MCAs. "
    "I have read the Privacy Policy and Terms."
)
CONSENT_TEXT_ES = (
    "Autorizo a Qualified Commercial a contactarme por teléfono o correo sobre esta revisión. "
    "Confirmo que mi negocio no tiene más de 2 MCA vigentes. "
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
    requested_amount: Range | Literal["not_sure"]
    monthly_revenue: Range
    time_in_business: Literal["under_6_months", "6_12_months", "1_2_years", "2_plus_years"]
    consent: Literal[True]
    locale: Literal["en", "es"] = "en"
    attribution: Attribution
    website: str = Field(default="", max_length=0)


class McaLeadResult(BaseModel):
    status: Literal["received"] = "received"


def _lead_id(key: UUID) -> UUID:
    # Deterministic primary key makes retries atomic without a schema migration.
    # Domain separation keeps caller-provided UUIDs out of the CRM's ID space.
    return uuid5(NAMESPACE_URL, f"https://qualifiedcommercial.com/public/mca/leads/{key}")


def _check_replay(client: Client, payload: McaLeadRequest) -> None:
    if client.source_channel != "mca_funding" or (client.email or "").casefold() != str(payload.email).casefold():
        raise HTTPException(status.HTTP_409_CONFLICT, "Start a new request for these contact details.")


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
