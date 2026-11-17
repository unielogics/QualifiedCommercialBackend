"""Term applications reuse the Main Street engine without entering MCA policy."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import func, select, text

from app import request_context
from app.models.activity import Activity
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import resource_assessments

_STARTS: dict[str, float] = {}
_RESUMES: dict[str, float] = {}
_CHATS: dict[str, float] = {}


def intake_id(key: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://qualifiedcommercial.com/public/business/intake/{key}")


def request_stamp(payload) -> str:
    data = payload.model_dump(mode="json", exclude={"secure_room_pin", "idempotency_key", "resource_handoff_token"})
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def term_context() -> dict:
    return {"funding_goal": "term_loan", "funding_intent": "working_capital", "intent": "working_capital",
        "deal_type": "Operating-business term-loan review", "review_type": "main_street_v1",
        "custom_instructions": "The applicant chose a business term loan. Review the operating business and existing obligations. "
        "Keep this goal fixed; do not substitute an MCA, RBF, dealer, or real-estate product. Use reviewed program criteria "
        "and accepted source evidence. Public calculator answers are self-reported and do not establish approval or lending capacity. "
        "Ask focused questions about the source documents, term-loan use, earnings, debt service and bank conduct. "
        "Do not invent criteria, promise approval, perform a credit pull, or expose internal risk commentary."}


def is_term_intake(intake) -> bool:
    return (getattr(intake, "variant", None) == "main_street_v1"
            and (getattr(intake, "intake_state", None) or {}).get("funding_goal") == "term_loan")


def result_for(intake, link):
    from app.dealer_os.services.client_room import room_url
    url = room_url(link.token) + "?tab=ai-review"
    return {"intake_id": str(intake.id), "variant": "main_street_v1", "funding_intent": "working_capital",
            "room_url": url, "resume_url": url}


async def start(db, payload, request):
    from app.enums import Language
    from app.routers import buckets
    from app.routers import dealer_ai_intake as core
    from app.services import business_profile, onboarding_consent
    from app.services.payment_authorization import primary_super_admin

    email = str(payload.email).casefold()
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "business-public-request:" + str(payload.idempotency_key)})
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": "business-public-start:" + hashlib.sha256(email.encode()).hexdigest()})
    existing = await db.get(PublicUnderwritingIntake, intake_id(payload.idempotency_key))
    if existing is not None:
        intake = await core._load_admin_dealer_lead(db, existing.id)
        link = intake.bucket_upload_link
        if (not is_term_intake(intake) or (intake.intake_state or {}).get("business_public_request_stamp") != request_stamp(payload)
                or link is None or not core.client_room.link_is_usable(link)):
            raise HTTPException(409, "Use your saved room or ask your advisor to review this request.")
        if not buckets._verify_passcode(payload.secure_room_pin, link.passcode_hash,
            attempt_scope=request_context.client_ip(request) or "unknown"):
            raise HTTPException(403, "Invalid room PIN")
        return result_for(intake, link)
    core._throttle_or_429(_STARTS, request_context.client_ip(request) or "unknown", 20,
                         "Please wait a moment before starting another file.")
    capture = (await resource_assessments.resolve(db, payload.resource_handoff_token, goal="term_loan", email=email)
               if payload.resource_handoff_token else None)
    # A resource capability may create a new application; it never unlocks an existing one.
    duplicate = (await db.execute(select(PublicUnderwritingIntake.id).where(
        func.lower(PublicUnderwritingIntake.email) == email, PublicUnderwritingIntake.variant == "main_street_v1",
        PublicUnderwritingIntake.intake_state["funding_goal"].astext == "term_loan",
        PublicUnderwritingIntake.archived_at.is_(None))
        .limit(1))).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(409, "A term-loan file already exists. Use your saved room or request its resume link.")
    client = await db.get(Client, capture.client_id) if capture else None
    if capture and (client is None or (client.email or "").casefold() != email):
        raise HTTPException(409, "Please request a new resource result.")
    if client is None:
        found = (await db.execute(select(Client.id).where(func.lower(Client.email) == email).limit(1))).scalar_one_or_none()
        if found is not None:
            raise HTTPException(409, "Use your saved room or contact your advisor. You can request a resume link below.")
        owner = await primary_super_admin(db)
        client = Client(id=uuid5(NAMESPACE_URL, f"https://qualifiedcommercial.com/public/business/client/{payload.idempotency_key}"),
            name=payload.full_name, email=email, phone=payload.phone, source_channel="business_term_intake",
            referral_source="business_term_intake", lead_source="other", lead_temperature="warm", stage="lead",
            financing_support_needed="yes", contact_permission="agent_will_introduce_first", relationship_context="new_lead",
            language="Spanish" if payload.locale == "es" else "English", originating_agent_id=owner.id if owner else None,
            current_agent_id=owner.id if owner else None, client_experience_mode="self_directed", lead_intake={})
        db.add(client)
        await db.flush()
    onboarding_consent.require_application_choices(payload)
    bucket, link = await core._create_bucket_for_main_street(db, client, payload, request,
        intent="working_capital", industry=payload.industry, room_pin=payload.secure_room_pin)
    link.passcode_set_by_client_at = datetime.now(UTC)
    bucket.ai_context = {**(bucket.ai_context or {}), **term_context()}
    consent = {**onboarding_consent.choices_record(payload), "terms_accepted": True, "privacy_accepted": True,
        "terms_version": core.TERMS_VERSION, "privacy_version": core.PRIVACY_VERSION, "credit_pull_authorized": False,
        **core._request_audit(request)}
    intake = PublicUnderwritingIntake(id=intake_id(payload.idempotency_key), variant="main_street_v1",
        source_kind="public_form", source_detail="Business term-loan AI intake", client_id=client.id,
        bucket_id=bucket.id, bucket_upload_link_id=link.id, token_hash=core._hash_token(secrets.token_urlsafe(48)),
        full_name=payload.full_name, email=email, phone=payload.phone, business_name=payload.business_name,
        loan_purpose="Business term loan", requested_loan_amount=payload.requested_amount,
        preferred_language=Language(payload.locale), referral_source="business_term_intake",
        intake_state={**business_profile.state_entry(payload), "messages": [], "source": "business_term_intake",
            "funding_goal": "term_loan", "funding_intent": "working_capital", "application_completion": "client",
            "main_street_details": {"intent": "working_capital", "industry": payload.industry,
                "years_in_business": payload.years_in_business, "requested_amount": payload.requested_amount,
                "estimated_credit_band": payload.credit_band},
            "signup_attribution": resource_assessments.safe_attribution(payload.attribution),
            "business_public_request_stamp": request_stamp(payload), "legal_acceptance": consent})
    intake.bucket = bucket
    db.add(intake)
    await db.flush()
    await core.profiles_service.provision_profile_for_intake(db, intake)
    if capture:
        await resource_assessments.link_application(db, capture, intake)
    db.add(Activity(client_id=client.id, actor_label="public_business_intake", kind="business.intake_started",
        summary="Started a business term-loan AI application", payload={"intake_id": str(intake.id), "goal": "term_loan"}))
    await db.commit()
    await onboarding_consent.record_for_start(db, intake, payload, request, entry="starting a business term-loan application")
    return result_for(intake, link)


async def authorize_room(db, token, passcode, request):
    from app.routers import application_profiles as rooms
    from app.routers import dealer_ai_intake as core
    link, profile = await rooms._public_application_room(db, token, passcode, request)
    if not profile.intake_id:
        raise HTTPException(404, "Business AI review is not enabled for this room")
    intake = await core._load_admin_dealer_lead(db, profile.intake_id)
    if (not is_term_intake(intake) or intake.bucket_id != link.bucket_id
            or intake.bucket_upload_link_id != link.id or profile.funding_category != "working_capital"):
        raise HTTPException(404, "Business AI review is not enabled for this room")
    if not link.can_use_ai_chat:
        raise HTTPException(403, "AI review is disabled for this room")
    return intake, link, profile


async def room_state(db, intake, link, profile):
    from app.models.bucket import BucketAIMessage, BucketAIReview
    from app.services.financial_publication import review_is_financially_stale

    latest = (await db.execute(select(BucketAIReview).where(BucketAIReview.bucket_id == link.bucket_id)
        .order_by(BucketAIReview.created_at.desc()).limit(1))).scalar_one_or_none()
    messages = list((await db.scalars(select(BucketAIMessage).where(BucketAIMessage.bucket_id == link.bucket_id,
        BucketAIMessage.upload_link_id == link.id, BucketAIMessage.audience == "uploader")
        .order_by(BucketAIMessage.created_at.desc(), BucketAIMessage.id.desc()).limit(80))).all())
    status = "not_started" if latest is None else "running" if latest.status in {"processing", "running"} else latest.status
    if status not in {"not_started", "queued", "running", "completed", "failed"}:
        status = "failed"
    if latest is not None and review_is_financially_stale(latest):
        status = "not_started"
    source = latest.progress if latest is not None and isinstance(latest.progress, dict) else {}
    progress = {"stage": status, "label": "Review the requested business records, then start an AI review.",
                "percent": None, "files_total": 0, "files_done": 0}
    for key in ("percent", "files_total", "files_done"):
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 10000:
            progress[key] = min(value, 100) if key == "percent" else int(value)
    requirements = [{"key": str(doc.id), "label": doc.name, "status": doc.status,
        "received": 1 if doc.status == "uploaded" else 0, "required": 1, "unit": "documents"}
        for doc in intake.bucket.requested_documents if doc.required]
    return {"funding_intent": "working_capital", "business_name": intake.business_name,
        "preferred_language": profile.communication_locale if profile.communication_locale in {"en", "es"} else "en",
        "payment_methods_enabled": False, "status": status, "progress": progress,
        "messages": [{"id": str(row.id), "role": row.role, "content": row.content,
            "sender_kind": row.sender_kind, "author_name": row.author_name,
            "created_at": row.created_at.isoformat() if row.created_at else None} for row in reversed(messages)],
        "summary": "Your business sources have been reviewed. Staff must verify program fit and any proposed terms."
                   if status == "completed" else "Add the requested business records. Ask the AI about missing information and the term-loan review.",
        "requirements": requirements, "actions": [{"kind": "upload", "label": "Upload the requested records"},
            {"kind": "debt_schedule", "label": "Complete the business debt schedule"},
            {"kind": "application", "label": "Complete application details"}],
        "policy_status": "ready_for_staff_review" if status == "completed" else "needs_information"}


async def resume(db, email, request):
    from sqlalchemy.orm import selectinload

    from app.models.bucket import Bucket
    from app.routers import dealer_ai_intake as core
    email = email.strip().casefold()
    core._throttle_or_429(_RESUMES, request_context.client_ip(request) or "unknown", 60,
                         "Please wait before requesting another resume link.")
    core._throttle_or_429(_RESUMES, "email:" + hashlib.sha256(email.encode()).hexdigest(), 60,
                         "Please wait before requesting another resume link.")
    intake = (await db.execute(select(PublicUnderwritingIntake).join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
        .where(func.lower(PublicUnderwritingIntake.email) == email, PublicUnderwritingIntake.variant == "main_street_v1",
            PublicUnderwritingIntake.intake_state["funding_goal"].astext == "term_loan", Bucket.archived_at.is_(None))
        .options(selectinload(PublicUnderwritingIntake.bucket_upload_link))
        .order_by(PublicUnderwritingIntake.created_at.desc()).limit(1))).scalar_one_or_none()
    if intake and intake.bucket_upload_link and core.client_room.link_is_usable(intake.bucket_upload_link):
        saved = intake.bucket_upload_link.recipient_email
        if saved and saved.casefold() == intake.email.casefold():
            try:
                await core._record_resume_email(intake, token="", request=request, reason="business_term_resume_requested",
                    public_path="/business-funding-intake", review_label="business term-loan review", room_label="business term-loan file")
                await db.commit()
            except Exception:
                await db.rollback()
                logging.getLogger(__name__).exception("Business resume delivery failed intake=%s", intake.id)
    return {"status": "received", "message": "If a matching term-loan file is available, its resume link will be emailed to the saved recipient. Your PIN is not included."}
