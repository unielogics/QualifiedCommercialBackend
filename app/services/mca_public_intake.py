"""Public standard MCA application and PIN-scoped room review.

No email-only adoption, raw intake token disclosure, credit pull, or automatic
invitation. Existing client identity requires the original request capability
or the already-authorized staff adoption workflow.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException, Request
from sqlalchemy import func, select, text

from app import request_context
from app.models.activity import Activity
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import mca_funding_policy as policy

CONSENT_VERSION = "mca-ai-intake-v3"
CONSENT_TEXT = "I authorize Qualified Commercial to create a secure MCA funding review file and review the documents I provide. My answers are accurate to the best of my knowledge; unknown answers require document review. I authorize contact by phone or email about this review. This is not approval or authorization for a credit pull."
CONSENT_TEXT_ES = "Autorizo a Qualified Commercial a crear un expediente seguro para revisar mi solicitud de MCA y los documentos que proporcione. Mis respuestas son correctas según mi conocimiento; las respuestas desconocidas requieren revisión documental. Autorizo contacto por teléfono o correo sobre esta revisión. Esto no es una aprobación ni una autorización para consultar mi crédito."
_STARTS: dict[str, float] = {}
_RESUMES: dict[str, float] = {}
_CHATS: dict[str, float] = {}


def intake_id(key: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://qualifiedcommercial.com/public/mca/intake/{key}")


def request_stamp(payload) -> str:
    values = payload.model_dump(mode="json", exclude={"secure_room_pin", "idempotency_key"})
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def result_for(intake, link) -> dict:
    from app.dealer_os.services.client_room import room_url

    url = room_url(link.token) + "?tab=ai-review"
    return {"intake_id": intake.id, "variant": "main_street_v1", "funding_intent": policy.PROGRAM_KEY,
            "room_url": url, "resume_url": url}


async def _retry(db, existing, payload, request):
    from app.routers import buckets
    from app.routers import dealer_ai_intake as core

    intake = await core._load_admin_dealer_lead(db, existing.id)
    state = intake.intake_state or {}
    link = intake.bucket_upload_link
    if (not core._is_main_street_mca_intake(intake) or state.get("mca_public_request_stamp") != request_stamp(payload)
            or link is None or not core.client_room.link_is_usable(link)):
        raise HTTPException(409, "Use your saved secure room or ask your advisor to review this request.")
    if not buckets._verify_passcode(payload.secure_room_pin, link.passcode_hash,
                                  attempt_scope=request_context.client_ip(request) or "unknown"):
        raise HTTPException(403, "Invalid room PIN")
    return result_for(intake, link)


async def start(db, payload, request: Request) -> dict:
    from app.enums import Language
    from app.routers import dealer_ai_intake as core
    from app.routers.mca_leads import _lead_id
    from app.services.payment_authorization import primary_super_admin

    email = str(payload.email).casefold()
    # Serialize duplicate submit/retries without putting applicant email in logs.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                     {"key": "mca-public-start:" + hashlib.sha256(email.encode()).hexdigest()})
    existing = await db.get(PublicUnderwritingIntake, intake_id(payload.idempotency_key))
    if existing is not None:
        return await _retry(db, existing, payload, request)
    core._throttle_or_429(_STARTS, request_context.client_ip(request) or "unknown", 20,
                         "Please wait a moment before starting another file.")
    client = await db.get(Client, _lead_id(payload.idempotency_key))
    if client is not None:
        # A prior public lead made with this exact capability may be promoted;
        # an arbitrary matching email or caller-supplied client ID may not.
        saved = client.lead_intake if isinstance(client.lead_intake, dict) else {}
        if (client.source_channel != "mca_funding" or (client.email or "").casefold() != email
                or any(saved.get(key) != getattr(payload, key) for key in (
                    "active_mca_count", "has_modified_mca", "last_year_tax_earnings", "tax_year"))):
            raise HTTPException(409, "Use your saved secure room or ask your advisor to review this request.")
    else:
        found = (await db.execute(select(Client.id).where(func.lower(Client.email) == email).limit(1))).scalar_one_or_none()
        if found is not None:
            raise HTTPException(409, "For secure access, use your saved room link or contact your advisor. You can request a resume link below.")
        owner = await primary_super_admin(db)
        client = Client(id=_lead_id(payload.idempotency_key), name=payload.full_name, email=email, phone=payload.phone,
            source_channel="mca_funding", referral_source="mca_funding", lead_source="other", lead_temperature="warm",
            stage="lead", financing_support_needed="yes", relationship_context="new_lead",
            contact_permission="agent_will_introduce_first", language="Spanish" if payload.locale == "es" else "English",
            originating_agent_id=owner.id if owner else None, current_agent_id=owner.id if owner else None,
            client_experience_mode="self_directed", client_experience_mode_reason="mca_funding_intake",
            lead_intake={})
        db.add(client)
        await db.flush()
    declaration = payload.model_dump(mode="json", exclude={"secure_room_pin", "idempotency_key", "website"})
    consent = {"consent_version": CONSENT_VERSION, "consent_text": CONSENT_TEXT_ES if payload.locale == "es" else CONSENT_TEXT,
               "consented_at": datetime.now(UTC).isoformat(), "terms_accepted": True, "privacy_accepted": True,
               "terms_version": core.TERMS_VERSION, "privacy_version": core.PRIVACY_VERSION,
               "allowed_contact_channels": ["phone", "email"], "credit_pull_authorized": False,
               **core._request_audit(request)}
    client.lead_intake = {**(client.lead_intake or {}), "funding_intent": policy.PROGRAM_KEY,
        "business_name": payload.business_name, "mca_intake_declaration": declaration,
        "mca_intake_consent": consent, "mca_product_policy": policy.declaration_metadata()}
    # Existing builder needs contact/business only; the full application is
    # completed in the room, preserving unknown industry rather than guessing.
    bucket, link = await core._create_bucket_for_main_street(db, client, payload, request,
        intent=policy.PROGRAM_KEY, industry="other", room_pin=payload.secure_room_pin)
    link.passcode_set_by_client_at = datetime.now(UTC)
    intake = PublicUnderwritingIntake(id=intake_id(payload.idempotency_key), variant=core.MAIN_STREET_VARIANT,
        source_kind="public_form", source_detail="Main Street standard MCA AI intake", client_id=client.id,
        bucket_id=bucket.id, bucket_upload_link_id=link.id, token_hash=core._hash_token(secrets.token_urlsafe(48)),
        full_name=payload.full_name, email=email, phone=payload.phone, business_name=payload.business_name,
        loan_purpose="Merchant cash advance", referral_source="mca_funding", preferred_language=Language(payload.locale),
        intake_state={"messages": [], "source": "mca_funding_intake", "lead_type": "main_street",
            "funding_intent": policy.PROGRAM_KEY, "application_completion": "client",
            "main_street_details": {"intent": policy.PROGRAM_KEY, "industry": None},
            "signup_attribution": payload.attribution.model_dump(mode="json"),
            "mca_funding_declaration": declaration, "mca_product_policy": policy.declaration_metadata(),
            "mca_public_request_stamp": request_stamp(payload), "legal_acceptance": consent})
    db.add(intake)
    await db.flush()
    await core.profiles_service.provision_profile_for_intake(db, intake)
    db.add(Activity(client_id=client.id, actor_label="public_mca_intake", kind="mca.intake_started",
                    summary="Started secure standard MCA review", payload={"intake_id": str(intake.id), "consent_version": CONSENT_VERSION}))
    await db.commit()
    return result_for(intake, link)


async def authorize_room(db, token: str, passcode: str, request: Request):
    from app.routers import application_profiles as rooms
    from app.routers import dealer_ai_intake as core

    link, profile = await rooms._public_application_room(db, token, passcode, request)
    if not getattr(profile, "intake_id", None):
        raise HTTPException(404, "MCA AI review is not enabled for this room")
    intake = await core._load_admin_dealer_lead(db, profile.intake_id)
    if (not core._is_main_street_mca_intake(intake) or intake.bucket_id != link.bucket_id
            or intake.bucket_upload_link_id != link.id or getattr(profile, "funding_category", None) != policy.PROGRAM_KEY):
        raise HTTPException(404, "MCA AI review is not enabled for this room")
    if not link.can_use_ai_chat:
        raise HTTPException(403, "AI review is disabled for this room")
    return intake, link, profile


async def room_state(db, intake, link, profile) -> dict:
    from app.models.bucket import BucketAIMessage, BucketAIReview
    from app.routers.dealer_ai_intake import CHAT_TURN_ORDER
    from app.services import application_programs

    readiness = await application_programs.get_program_readiness(db, profile)
    context = await application_programs.profile_fit_context(db, profile)
    assessment = context.get("mca_funding_policy") or {}
    latest = (await db.execute(select(BucketAIReview).where(BucketAIReview.bucket_id == link.bucket_id)
        .order_by(BucketAIReview.created_at.desc()).limit(1))).scalar_one_or_none()
    messages = list((await db.scalars(select(BucketAIMessage).where(BucketAIMessage.bucket_id == link.bucket_id,
        BucketAIMessage.upload_link_id == link.id, BucketAIMessage.audience == "uploader")
        .order_by(BucketAIMessage.created_at.desc(), CHAT_TURN_ORDER.desc(), BucketAIMessage.id.desc()).limit(80))).all())
    status = "not_started" if latest is None else "running" if latest.status in {"processing", "running"} else "failed" if latest.status in {"failed", "error"} else latest.status
    from app.services.financial_publication import review_is_financially_stale

    stale = latest is not None and review_is_financially_stale(latest)
    if stale:
        status = "not_started"
    progress = {"stage": status, "label": "Upload the three requested items or complete the debt schedule to start the review.",
                "percent": None, "files_total": 0, "files_done": 0}
    if status == "failed":
        progress["label"] = "The review needs attention. Please retry or contact your advisor."
    elif stale:
        progress["label"] = "Saved information changed. Start a fresh review of the current documents and debt schedule."
    elif latest is not None:
        source = latest.progress if isinstance(latest.progress, dict) else {}
        for key in ("stage", "label"):
            if isinstance(source.get(key), str):
                progress[key] = source[key][:240]
        for key in ("percent", "files_total", "files_done"):
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 10000:
                progress[key] = min(value, 100) if key == "percent" else int(value)
    required = {"business_bank_statements_6_months", "business_debt_schedule", "business_tax_returns_1_years"}
    requirements = []
    for item in readiness.requirements:
        if item.requirement_key in required:
            coverage = getattr(item, "coverage", None) or {}
            requirements.append({"key": item.requirement_key, "label": item.label, "status": item.status,
                "received": coverage.get("current", 0), "required": coverage.get("required", 1), "unit": coverage.get("unit", "documents")})
    locale = getattr(profile, "communication_locale", None)
    if locale not in {"en", "es"}:
        locale = getattr(intake, "preferred_language", "en")
    actions = [{"kind": "upload", "label": "Upload the requested documents"},
               {"kind": "debt_schedule", "label": "Complete the business debt schedule"}]
    if (getattr(intake, "intake_state", None) or {}).get("application_completion") == "client":
        actions.append({"kind": "application", "label": "Complete application details"})
    payment_methods_enabled = False
    if getattr(profile, "id", None) is not None:
        from app.services.mca_room_capabilities import payment_methods_available

        payment_methods_enabled = await payment_methods_available(db, profile.id)
    return {"funding_intent": policy.PROGRAM_KEY, "business_name": intake.business_name, "status": status,
        "preferred_language": locale if locale in {"en", "es"} else "en",
        "payment_methods_enabled": payment_methods_enabled,
        "progress": progress,
        "messages": [{"id": str(row.id), "role": row.role, "content": row.content,
                      "sender_kind": getattr(row, "sender_kind", None), "author_name": getattr(row, "author_name", None),
                      "created_at": row.created_at.isoformat() if row.created_at else None} for row in reversed(messages)],
        # Do not pass through AI context, management metrics, provider errors,
        # or free-form internal review output in the applicant room.
        "summary": ("Your current sources have been reviewed. Final terms and approval require staff review."
                    if status == "completed" else
                    "Add your documents and complete the debt schedule. The AI can help identify missing information within the three requested items."),
        "requirements": requirements, "actions": actions,
        "policy_status": "ready_for_staff_review" if assessment.get("auto_underwriting_allowed") else assessment.get("recommendation_status", "pending_review")}


async def resume(db, email: str, request: Request) -> dict:
    from sqlalchemy.orm import selectinload

    from app.models.bucket import Bucket
    from app.routers import dealer_ai_intake as core

    email = email.strip().casefold()
    # Same throttling result for known/unknown addresses; never return a token.
    core._throttle_or_429(_RESUMES, request_context.client_ip(request) or "unknown", 60,
                         "Please wait a moment before requesting another link.")
    core._throttle_or_429(_RESUMES, "email:" + hashlib.sha256(email.encode()).hexdigest(), 60,
                         "Please wait a moment before requesting another link.")
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                     {"key": "mca-resume:" + hashlib.sha256(email.encode()).hexdigest()})
    state = PublicUnderwritingIntake.intake_state
    intake = (await db.execute(select(PublicUnderwritingIntake).join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
        .where(func.lower(PublicUnderwritingIntake.email) == email, PublicUnderwritingIntake.variant == "main_street_v1",
               state["funding_intent"].astext == policy.PROGRAM_KEY, Bucket.archived_at.is_(None))
        .options(selectinload(PublicUnderwritingIntake.bucket_upload_link))
        .order_by(PublicUnderwritingIntake.created_at.desc()).limit(1))).scalar_one_or_none()
    if intake is not None and intake.bucket_upload_link is not None and core.client_room.link_is_usable(intake.bucket_upload_link):
        # Existing delivery records expose no delivery outcome to this caller.
        # The destination is always the saved file recipient, never caller input.
        saved_email = intake.bucket_upload_link.recipient_email
        if saved_email and saved_email.casefold() == intake.email.casefold():
            now = datetime.now(UTC)
            metadata = dict(intake.intake_state or {})
            recent = []
            for value in metadata.get("mca_resume_requests") or []:
                try:
                    sent = datetime.fromisoformat(value)
                    if sent.tzinfo is not None and 0 <= (now - sent).total_seconds() < 3600:
                        recent.append(value)
                except (TypeError, ValueError):
                    continue
            if len(recent) < 5 and (not recent or (now - datetime.fromisoformat(recent[-1])).total_seconds() >= 60):
                # Claim before calling the provider; retries cannot duplicate a
                # delivery, and an error never exposes file existence.
                intake.intake_state = {**metadata, "mca_resume_requests": [*recent, now.isoformat()]}
                await db.commit()
                try:
                    await core._record_resume_email(intake, token="", request=request, reason="mca_room_resume_requested",
                        public_path="/mca-funding-intake", review_label="MCA funding review", room_label="MCA funding file")
                    await db.commit()
                except Exception:  # noqa: BLE001 - same public response for absent file/provider failure
                    await db.rollback()
                    logging.getLogger(__name__).exception("MCA resume delivery failed intake=%s", intake.id)
    return {"status": "received", "message": "If a matching MCA file is available, a resume link will be emailed to its saved recipient. Your PIN is not included."}
