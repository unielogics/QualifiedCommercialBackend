"""Durable, consented resource capture and an explicitly limited prefill capability."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.config import get_settings
from app.models.activity import Activity
from app.models.client import Client
from app.models.resource_assessment import ResourceAssessment

CONSENT_VERSION = "business-funding-resource-v1"
TOKEN_DAYS = 14
_LAST_CAPTURE: dict[str, float] = {}
ATTRIBUTION_KEYS = {"source", "page", "campaign", "cta", "utm_source", "utm_medium", "utm_campaign",
                    "utm_term", "utm_content", "gclid", "gbraid", "wbraid"}


def _secret() -> bytes:
    settings = get_settings()
    secret = (getattr(settings, "resource_token_secret", "") or settings.clerk_secret_key
              or settings.provider_secrets_encryption_key)
    if not secret:
        raise HTTPException(503, "Resource access is temporarily unavailable. Please try again.")
    return hashlib.sha256(("qc-resource-assessment-v1:" + secret).encode()).digest()


def token_for(capture_id: UUID) -> str:
    identifier = capture_id.hex
    return identifier + "." + hmac.new(_secret(), identifier.encode(), hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def assessment_id(key: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://qualifiedcommercial.com/resources/assessment/{key}")


def safe_attribution(values: dict[str, Any]) -> dict[str, str]:
    result = {key: str(value).strip()[:256] for key, value in values.items()
              if key in ATTRIBUTION_KEYS and isinstance(value, str) and value.strip()}
    page = result.get("page")
    if page and (not page.startswith("/") or page.startswith("//") or "?" in page or "#" in page):
        result.pop("page")
    return result


def validate_json_object(value: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Use finite, valid answers.") from exc
    if len(encoded) > 65536:
        raise ValueError("The resource answers are too large.")
    return value


async def resolve(db, token: str, *, goal: str | None = None, email: str | None = None) -> ResourceAssessment:
    try:
        identifier, _ = token.split(".", 1)
        capture_id = UUID(hex=identifier)
    except (ValueError, AttributeError):
        raise HTTPException(404, "Resource access is invalid or expired.") from None
    if not hmac.compare_digest(token_for(capture_id), token):
        raise HTTPException(404, "Resource access is invalid or expired.")
    capture = await db.get(ResourceAssessment, capture_id)
    if (capture is None or capture.token_expires_at <= datetime.now(UTC)
            or not hmac.compare_digest(capture.resource_token_hash, token_hash(token))):
        raise HTTPException(404, "Resource access is invalid or expired.")
    if goal is not None and capture.goal != goal:
        raise HTTPException(422, "This result belongs to a different funding goal.")
    if email is not None and capture.contact.get("email", "").casefold() != email.strip().casefold():
        raise HTTPException(422, "Use the email associated with your resource result, or start without that result.")
    return capture


def _payload_matches(capture: ResourceAssessment, payload) -> bool:
    return (capture.contact == payload.contact.model_dump(mode="json") and capture.answers == payload.answers
            and capture.goal == payload.goal and capture.resource_id == payload.resource_id
            and capture.locale == payload.locale and capture.email_marketing_consent == payload.email_marketing_consent)


async def capture(db, payload, request) -> dict[str, Any]:
    from app import request_context
    from app.services.funding_resource_calculations import calculate_funding_report
    from app.services.payment_authorization import primary_super_admin

    identifier = assessment_id(payload.idempotency_key)
    # Serialize both same-request retries and same-email captures without putting contact data in logs.
    email = str(payload.contact.email).casefold()
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                     {"key": "resource-request:" + str(identifier)})
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                     {"key": "resource-capture:" + hashlib.sha256(email.encode()).hexdigest()})
    existing = await db.get(ResourceAssessment, identifier)
    if existing is not None:
        if not _payload_matches(existing, payload):
            raise HTTPException(409, "Start a new resource request for changed answers or contact details.")
        if existing.token_expires_at <= datetime.now(UTC):
            raise HTTPException(410, "This result expired. Please run a new check.")
        return {"status": "received", "capture_id": str(existing.id), "resource_token": token_for(existing.id), "report": existing.report}
    ip = request_context.client_ip(request) or "unknown"
    now_monotonic = time.monotonic()
    if now_monotonic - _LAST_CAPTURE.get(ip, -1000) < 5:
        raise HTTPException(429, "Please wait a moment before requesting another result.")
    if len(_LAST_CAPTURE) > 10000:
        _LAST_CAPTURE.clear()
    _LAST_CAPTURE[ip] = now_monotonic
    report = calculate_funding_report(payload.goal, payload.answers)
    capability = token_for(identifier)
    client = (await db.execute(select(Client).where(func.lower(Client.email) == email)
                              .order_by(Client.created_at.desc()).limit(1))).scalar_one_or_none()
    if client is None:
        owner = await primary_super_admin(db)
        client = Client(id=uuid5(NAMESPACE_URL, f"https://qualifiedcommercial.com/resources/client/{identifier}"),
            name=payload.contact.name, email=email, phone=payload.contact.phone, source_channel="resource_funnel",
            referral_source="business_funding_check", lead_source="other", lead_temperature="warm", stage="lead",
            financing_support_needed="yes", contact_permission="agent_will_introduce_first", relationship_context="new_lead",
            language="Spanish" if payload.locale == "es" else "English",
            originating_agent_id=owner.id if owner else None, current_agent_id=owner.id if owner else None,
            lead_intake={"resource_id": payload.resource_id, "resource_goal": payload.goal})
        db.add(client)
        await db.flush()
    now = datetime.now(UTC)
    row = ResourceAssessment(id=identifier, client_id=client.id, idempotency_key=str(payload.idempotency_key),
        goal=payload.goal, locale=payload.locale, resource_id=payload.resource_id,
        contact=payload.contact.model_dump(mode="json"), answers=payload.answers, assumptions={}, report=report,
        attribution=safe_attribution(payload.attribution), email_marketing_consent=payload.email_marketing_consent,
        consent_version=CONSENT_VERSION, created_at=now, token_expires_at=now + timedelta(days=TOKEN_DAYS),
        nurture_next_at=now if payload.email_marketing_consent else None,
        resource_token_hash=token_hash(capability))
    db.add(row)
    db.add(Activity(id=uuid5(identifier, "resource.lead_received"), client_id=client.id,
        actor_label="public_resource", kind="resource.lead_received", summary="Business funding result requested",
        payload={"assessment_id": str(identifier), "goal": payload.goal, "resource_id": payload.resource_id,
                 "consent_version": CONSENT_VERSION, "privacy_acknowledged": True,
                 "email_marketing_consent": payload.email_marketing_consent, "consented_at": now.isoformat(),
                 "attribution": row.attribution, "enrollment": "pending" if payload.email_marketing_consent else "not_opted_in"}))
    await db.commit()
    return {"status": "received", "capture_id": str(identifier), "resource_token": capability, "report": report}


async def link_application(db, capture: ResourceAssessment, intake) -> None:
    """Stop every same-client resource sequence after an actual application start."""
    if intake.client_id != capture.client_id:
        raise HTTPException(409, "The application cannot be linked to this resource result.")
    now = datetime.now(UTC)
    pending = list((await db.scalars(select(ResourceAssessment).where(
        ResourceAssessment.client_id == capture.client_id, ResourceAssessment.application_started_at.is_(None)
    ).with_for_update())).all())
    for row in pending:
        row.application_started_at = now
        row.nurture_next_at = None
        row.application_intake_id = intake.id
        db.add(Activity(id=uuid5(row.id, "resource.application_started"), client_id=row.client_id,
            actor_label="public_application", kind="resource.application_started", summary="Started an AI funding application",
            payload={"assessment_id": str(row.id), "intake_id": str(intake.id), "goal": row.goal}))
    state = dict(intake.intake_state or {})
    state["resource_assessment"] = {"id": str(capture.id), "goal": capture.goal, "answers": capture.answers,
        "report": capture.report, "attribution": capture.attribution, "verification_status": "self_reported_unverified"}
    intake.intake_state = state
