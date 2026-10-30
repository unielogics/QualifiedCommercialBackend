"""/public/dealer-ai-intake — route handlers of the AI intake.

Moved out of app/routers/dealer_ai_intake.py, which keeps every helper,
constant, router object and service import: thirty-one test files patch names
there, so those references go through `core` and resolve at call time, and the
old module still exposes these handlers by name. Importing this module is what
registers the routes.
"""

from __future__ import annotations

# ruff: noqa: B008, E501
import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.routers.dealer_ai_intake as core

__all__ = [
    "start_dealer_intake",
    "start_dealer_login",
    "verify_dealer_login",
    "get_dealer_session",
    "logout_dealer_session",
    "send_dealer_resume_link",
    "download_dealer_intelligence_pdf",
    "get_dealer_intake",
    "update_dealer_intake",
    "dealer_intake_chat",
    "dealer_upload_init",
    "dealer_upload_complete",
    "dealer_sign_requested_document",
    "dealer_submit_pfs",
    "dealer_submit_debt_schedule",
    "run_dealer_review",
    "router_secure_room",
    "book_dealer_call"
]


@core.router.post("/start", response_model=core.DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def start_dealer_intake(
    payload: core.DealerIntakeStart,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    await core._guard_arf_attribution(db, payload)
    if not payload.terms_accepted or not payload.privacy_accepted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Terms and Privacy Policy acceptance is required.")
    core._throttle_or_429(
        core._START_LAST_BY_IP,
        (request.client.host if request.client else "?") or "?",
        core._START_MIN_INTERVAL_SECONDS,
        "Please wait a moment before starting another review.",
    )
    business_equipment = core._is_business_equipment_application(payload)
    target_variant = core.MAIN_STREET_VARIANT if business_equipment else core.DEALER_VARIANT
    existing = await core._latest_active_intake_by_email(
        db, str(payload.email), variant=target_variant
    )
    if existing is not None:
        await core._start_login_challenge(
            db,
            email=str(payload.email),
            request=request,
            reason="existing_equipment_intake_start" if business_equipment else "existing_intake_start",
            variant=target_variant,
            review_label="equipment financing review" if business_equipment else "dealer funding review",
            event_prefix="equipment_financing" if business_equipment else "dealer_ai",
            target_type="equipment_financing_intake" if business_equipment else "dealer_ai_intake",
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            (
                "A secure Equipment Financing file already exists for this email. We sent a short access code so you can continue it."
                if business_equipment
                else "A secure dealer file already exists for this email. We sent a short access code so you can continue that file."
            ),
        )
    client = await core._find_or_create_client(db, payload)
    if business_equipment:
        bucket, link = await core._create_bucket_for_main_street(
            db,
            client,
            payload,
            request,
            intent="equipment",
            industry="other",
        )
    else:
        bucket, link = await core._create_bucket_for_intake(db, client, payload, request)
    token = core._new_public_token()
    attribution = core._public_intake_attribution(payload)
    equipment_application = core._is_equipment_application(
        funding_intent=payload.funding_intent,
        details=payload.equipment_details,
    )
    equipment_details = core._equipment_details_payload(payload.equipment_details)
    intake_state: dict[str, Any] = {
        "messages": [],
        "source": "equipment_financing_intake" if business_equipment else "dealer_ai_intake",
        **(
            {
                "capital_readiness_diagnostic": payload.capital_readiness_diagnostic.model_dump(
                    mode="json", by_alias=True
                )
            }
            if payload.capital_readiness_diagnostic
            else {}
        ),
        **({"signup_attribution": attribution} if attribution else {}),
        **({"funding_intent": payload.funding_intent} if payload.funding_intent else {}),
        **(
            {"main_street_details": {"intent": "equipment", "industry": "other"}}
            if business_equipment
            else {"dealer_details": {"financing_equipment_or_vehicle": True}}
            if equipment_application
            else {}
        ),
        **(
            {"equipment_financing_details": equipment_details}
            if equipment_details
            else {}
        ),
        "legal_acceptance": {
            "terms_accepted": payload.terms_accepted,
            "privacy_accepted": payload.privacy_accepted,
            "terms_version": payload.terms_version or core.TERMS_VERSION,
            "privacy_version": payload.privacy_version or core.PRIVACY_VERSION,
            **core._request_audit(request),
        },
    }
    intake = core.PublicUnderwritingIntake(
        variant=target_variant,
        source_kind="public_form",
        source_detail="Equipment Financing intake form" if business_equipment else "Dealer intake form",
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=core._hash_token(token),
        full_name=payload.full_name.strip(),
        email=client.email or core._normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        requested_loan_amount=payload.requested_amount,
        loan_purpose="Equipment financing" if equipment_application else None,
        referral_source=(attribution or {}).get("source") or ("equipment_financing_intake" if business_equipment else "dealer_ai_intake"),
        preferred_language=payload.preferred_language,
        intake_state=intake_state,
    )
    db.add(intake)
    if equipment_application:
        await core._configure_equipment_bucket(
            db,
            bucket,
            details=equipment_details,
        )
    await db.commit()
    intake = await core._load_public_intake(db, token)
    email_record = await core._record_resume_email(
        intake,
        token=token,
        request=request,
        reason="equipment_intake_created" if business_equipment else "intake_created",
        public_path=core.EQUIPMENT_PUBLIC_PATH if business_equipment else "/dealer-ai-underwriter",
        review_label="equipment financing review" if business_equipment else "dealer funding review",
        room_label="equipment financing file" if business_equipment else "dealer financing file",
    )
    await core._record_super_admin_intake_notification(db, intake, request=request)
    await db.commit()
    intake = await core._load_public_intake(db, token)
    email_note = core._dealer_start_email_note(intake.preferred_language, ok=email_record.get("ok") is True)
    return await core._response(
        db,
        intake,
        token=token,
        assistant_message=(
            core._equipment_welcome(intake.preferred_language, email_note=email_note)
            if business_equipment
            else core._dealer_welcome(intake.preferred_language, email_note=email_note)
        ),
        public_path=core.EQUIPMENT_PUBLIC_PATH if business_equipment else "/dealer-ai-underwriter",
    )

@core.router.post("/login/start", response_model=core.DealerLoginStartResponse)
async def start_dealer_login(
    payload: core.DealerLoginStartRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerLoginStartResponse:
    business_equipment = core._is_business_equipment_application(payload)
    login_required = await core._start_login_challenge(
        db,
        email=str(payload.email),
        request=request,
        reason="equipment_login_requested" if business_equipment else "dealer_login_requested",
        variant=core.MAIN_STREET_VARIANT if business_equipment else core.DEALER_VARIANT,
        review_label="equipment financing review" if business_equipment else "dealer funding review",
        event_prefix="equipment_financing" if business_equipment else "dealer_ai",
        target_type="equipment_financing_intake" if business_equipment else "dealer_ai_intake",
    )
    await db.commit()
    return core.DealerLoginStartResponse(
        login_required=login_required,
        message=(
            (
                "We found an existing secure Equipment Financing file for this email. Enter the code we sent to continue."
                if business_equipment
                else "We found an existing secure dealer file for this email. Enter the code we sent to continue."
            )
            if login_required
            else (
                "No existing Equipment Financing file was found. Complete Step 1 to start a new review."
                if business_equipment
                else "No existing secure dealer file was found. Complete Step 1 to start a new review."
            )
        ),
    )

@core.router.post("/login/verify", response_model=core.DealerIntakeResponse)
async def verify_dealer_login(
    payload: core.DealerLoginVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    email = core._normalize_email(str(payload.email))
    email_hash = core._hash_token(email)
    challenge = (
        await db.execute(
            select(core.DealerIntakeLoginChallenge)
            .where(
                core.DealerIntakeLoginChallenge.email_hash == email_hash,
                core.DealerIntakeLoginChallenge.used_at.is_(None),
                core.DealerIntakeLoginChallenge.revoked_at.is_(None),
                core.DealerIntakeLoginChallenge.expires_at > core._now(),
            )
            .order_by(core.DealerIntakeLoginChallenge.created_at.desc())
        )
    ).scalars().first()
    if challenge is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    if challenge.attempt_count >= core.DEALER_LOGIN_MAX_ATTEMPTS:
        challenge.revoked_at = core._now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    if core._hash_token(payload.code.strip()) != challenge.code_hash:
        challenge.attempt_count += 1
        if challenge.attempt_count >= core.DEALER_LOGIN_MAX_ATTEMPTS:
            challenge.revoked_at = core._now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    session_token = secrets.token_urlsafe(40)
    public_token = core._new_public_token()
    challenge.used_at = core._now()
    challenge.session_hash = core._hash_token(session_token)
    challenge.session_expires_at = core._now() + timedelta(hours=core.DEALER_LOGIN_SESSION_TTL_HOURS)
    intake = await db.get(core.PublicUnderwritingIntake, challenge.intake_id)
    if intake is None:
        challenge.revoked_at = core._now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    core._require_dealer_intake(intake)
    intake.token_hash = core._hash_token(public_token)
    await core._log(
        db,
        intake.bucket_id,
        "dealer_ai_login_verified",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="dealer_ai_intake",
        target_id=str(intake.id),
        detail=(
            "Equipment Financing continuation login verified"
            if core._is_main_street_equipment_intake(intake)
            else "Dealer AI continuation login verified"
        ),
    )
    await db.commit()
    intake = await core._load_public_intake(db, public_token)
    return await core._response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        assistant_message=core._dealer_route_welcome_back(intake),
        public_path=core._dealer_route_public_path(intake),
    )

@core.router.get("/session", response_model=core.DealerIntakeResponse)
async def get_dealer_session(request: Request, db: AsyncSession = Depends(core.get_db)) -> core.DealerIntakeResponse:
    session_token = core._dealer_session_from_request(request)
    if not session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Dealer session is required")
    intake, _challenge = await core._load_intake_by_dealer_session(db, session_token, allow_pending_signing=True)
    core._require_dealer_intake(intake)
    public_token = core._new_public_token()
    intake.token_hash = core._hash_token(public_token)
    await db.commit()
    intake = await core._load_public_intake(db, public_token, allow_pending_signing=True)
    return await core._response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        assistant_message=core._dealer_route_welcome_back(intake),
        public_path=core._dealer_route_public_path(intake),
    )

@core.router.post("/logout", response_model=core.DealerLogoutResponse)
async def logout_dealer_session(
    payload: core.DealerLogoutRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerLogoutResponse:
    session_token = payload.session_token or core._dealer_session_from_request(request)
    if session_token:
        challenge = (
            await db.execute(
                select(core.DealerIntakeLoginChallenge).where(
                    core.DealerIntakeLoginChallenge.session_hash == core._hash_token(session_token),
                    core.DealerIntakeLoginChallenge.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if challenge is not None:
            challenge.revoked_at = core._now()
            await db.commit()
    return core.DealerLogoutResponse()

@core.router.post("/resume-link", response_model=core.DealerResumeLinkResponse)
async def send_dealer_resume_link(
    payload: core.DealerResumeLinkRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerResumeLinkResponse:
    email = core._normalize_email(str(payload.email))
    business_equipment = core._is_business_equipment_application(payload)
    target_variant = core.MAIN_STREET_VARIANT if business_equipment else core.DEALER_VARIANT
    intake = (
        await db.execute(
            select(core.PublicUnderwritingIntake)
            .where(
                core.PublicUnderwritingIntake.email == email,
                core.PublicUnderwritingIntake.variant == target_variant,
            )
            .options(selectinload(core.PublicUnderwritingIntake.bucket))
            .order_by(core.PublicUnderwritingIntake.updated_at.desc())
        )
    ).scalars().first()
    if intake is not None and intake.bucket is not None and intake.bucket.archived_at is None:
        token = core._new_public_token()
        intake.token_hash = core._hash_token(token)
        await core._record_resume_email(
            intake,
            token=token,
            request=request,
            reason="equipment_resume_link_requested" if business_equipment else "resume_link_requested",
            public_path=core.EQUIPMENT_PUBLIC_PATH if business_equipment else "/dealer-ai-underwriter",
            review_label="equipment financing review" if business_equipment else "dealer funding review",
            room_label="equipment financing file" if business_equipment else "dealer financing file",
        )
        await core._log(
            db,
            intake.bucket_id,
            "dealer_ai_resume_link_requested",
            request=request,
            actor_name=intake.full_name,
            actor_email=intake.email,
            actor_role="public_lead",
            target_type="dealer_ai_intake",
            target_id=str(intake.id),
            detail=(
                "Equipment Financing resume link requested by email"
                if business_equipment
                else "Dealer AI resume link requested by email"
            ),
        )
        await db.commit()
    return core.DealerResumeLinkResponse()

@core.router.get("/intelligence.pdf")
async def download_dealer_intelligence_pdf(
    request: Request,
    token: str | None = None,
    db: AsyncSession = Depends(core.get_db),
) -> Response:
    if token:
        intake = await core._load_public_intake(db, token)
    else:
        session_token = core._dealer_session_from_request(request)
        if not session_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Dealer session or resume token required")
        intake, _challenge = await core._load_intake_by_dealer_session(db, session_token)
    core._require_dealer_intake(intake)
    review = intake.latest_review if intake.latest_review else None
    latest_result = review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
    files = sorted(
        (
            file
            for file in core._active_files(intake.bucket)
            if not core._is_package_readiness_output(file)
        ),
        key=lambda file: file.created_at,
        reverse=True,
    )
    missing_docs = core._missing_required_docs(intake.bucket)
    pdf_bytes = await asyncio.to_thread(
        core.render_dealer_intelligence_pdf,
        intake=intake,
        files=files,
        missing_docs=missing_docs,
        result=latest_result,
    )
    filename = core._safe_filename(
        f"{'equipment-financing' if core._is_main_street_equipment_intake(intake) else 'dealer-intelligence'}-"
        f"{intake.business_name or intake.full_name or 'review'}.pdf"
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@core.router.get("/{token}", response_model=core.DealerIntakeResponse)
async def get_dealer_intake(token: str, db: AsyncSession = Depends(core.get_db)) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token, allow_pending_signing=True)
    core._require_dealer_intake(intake)
    return await core._response(db, intake, token=token)

@core.router.patch("/{token}", response_model=core.DealerIntakeResponse)
async def update_dealer_intake(
    token: str,
    payload: core.DealerIntakePatch,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    update_data = payload.model_dump(exclude_unset=True)
    core._apply_updates(intake, payload)
    await core._ensure_equipment_evidence_for_update(db, intake, payload)
    await core._log_dealer_update_events(
        db,
        intake,
        update_data,
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
    )
    await db.commit()
    intake = await core._load_public_intake(db, token)
    return await core._response(db, intake, token=token)

@core.router.post("/{token}/chat", response_model=core.DealerIntakeResponse)
async def dealer_intake_chat(
    token: str,
    payload: core.DealerChatRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    update_data = payload.updates.model_dump(exclude_unset=True) if payload.updates else {}
    core._apply_updates(intake, payload.updates)
    await core._ensure_equipment_evidence_for_update(db, intake, payload.updates)
    core._record_chat_fact(intake, payload.message)
    await core._log_dealer_update_events(
        db,
        intake,
        update_data,
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
    )
    intake.last_message_at = core._now()
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        chat_messages, assistant_message, paused = await core._client_chat_turn(
            db, intake, payload.message.strip(), actor_name=intake.full_name
        )
        messages = chat_messages
        if chat_messages and not paused:
            raw = chat_messages[-1].metadata_json.get("raw") if isinstance(chat_messages[-1].metadata_json, dict) else None
            proposed_facts = raw.get("proposed_borrower_facts") if isinstance(raw, dict) else None
            if intake.variant == core.MAIN_STREET_VARIANT:
                core._merge_main_street_details(intake, proposed_facts)
            else:
                newly_accepted = core._merge_dealer_details(intake, proposed_facts)
                await core._apply_dealer_detail_documents(db, intake, newly_accepted)
    core._apply_loan_program_fit(intake)
    await db.commit()
    intake = await core._load_public_intake(db, token)
    return await core._response(
        db,
        intake,
        token=token,
        assistant_message=assistant_message,
        messages=messages,
    )

@core.router.post("/{token}/files/upload-init", response_model=core.BucketFileUploadInitResponse)
async def dealer_upload_init(
    token: str,
    payload: core.DealerFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFileUploadInitResponse:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    return await core._start_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email,
                               source_kind="client_room")

@core.router.post("/{token}/files/complete", response_model=core.BucketFileRead)
async def dealer_upload_complete(
    token: str,
    payload: core.DealerUploadComplete,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    return await core._complete_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)

@core.router.post("/{token}/requested-documents/sign", response_model=core.BucketFileRead)
async def dealer_sign_requested_document(
    token: str,
    payload: core.DealerDocumentSignRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    return await core._sign_requested_document(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)

@core.router.post("/{token}/requested-documents/pfs", response_model=core.BucketFileRead)
async def dealer_submit_pfs(
    token: str,
    payload: core.DealerPfsSubmission,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    return await core._submit_pfs_form(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)

@core.router.post("/{token}/requested-documents/debt-schedule", response_model=core.BucketFileRead)
async def dealer_submit_debt_schedule(
    token: str,
    payload: core.DealerDebtScheduleSubmission,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    return await core._submit_debt_schedule_form(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)

@core.router.post("/{token}/run-review", response_model=core.DealerIntakeResponse)
async def run_dealer_review(
    token: str,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    # Per-token cooldown: run-review triggers a heavy Bedrock pass over up to 8
    # files; without this a token holder can replay it to amplify LLM cost.
    core._throttle_or_429(
        core._REVIEW_LAST_BY_TOKEN,
        core._hash_token(token),
        core._REVIEW_MIN_INTERVAL_SECONDS,
        "A review was just started. Please wait a moment before running another.",
    )
    fresh_review = await core._execute_intake_review(
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
        await core._record_super_admin_decision_notification(
            db,
            intake,
            fresh_review.result,
            request=request,
            review_id=fresh_review.id,
        )
        _, _, slots = await core._dealer_call_slots(db)
        state = core._intake_state(intake)
        if slots and not state.get("call_options_shown_at"):
            state["call_options_shown_at"] = core._now().isoformat()
            intake.intake_state = state
            await core._log(
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
    intake = await core._load_public_intake(db, token)
    return await core._response(db, intake, token=token)

@core.router.post("/{token}/secure-room", response_model=core.SecureRoomHandoffResponse)
async def router_secure_room(
    token: str,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.SecureRoomHandoffResponse:
    """Open this file's secure room, where the bank connection and the
    agreements live. The client is already verified on this intake."""
    intake = await core._load_public_intake(db, token)
    return await core._secure_room_handoff(db, intake, request, tab="todo")

@core.router.post("/{token}/book-call", response_model=core.DealerIntakeResponse)
async def book_dealer_call(
    token: str,
    payload: core.DealerBookCallRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_dealer_intake(intake)
    if core._call_booked(intake):
        return await core._response(
            db,
            intake,
            token=token,
            assistant_message="Your call is already booked. Keep uploading baseline documents here if anything is still missing before the meeting.",
        )
    starts_at = core._to_utc_minute(payload.starts_at)
    owner, booking, slots = await core._dealer_call_slots(db, lock_host=True)
    if owner is None or booking is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Call scheduling is not available right now.")
    if not any(abs((datetime.fromisoformat(slot["starts_at"]) - starts_at).total_seconds()) < 1 for slot in slots):
        raise HTTPException(status.HTTP_409_CONFLICT, "That call time is no longer available. Choose another time.")

    who = f"{intake.full_name} <{intake.email}>"
    equipment_intake = core._is_main_street_equipment_intake(intake)
    description = (
        f"Booked from {'Equipment Financing Review' if equipment_intake else 'Dealer AI Underwriter'}.\n"
        f"{'Equipment financing' if equipment_intake else 'Dealer'} intake: {intake.id}\n"
        f"Bucket: {intake.bucket_id}\n"
        f"Business: {intake.business_name or '(not provided)'}\n"
        f"Name: {intake.full_name}\n"
        f"Email: {intake.email}\n"
        f"Phone: {intake.phone or '(not provided)'}\n"
        f"Requested amount: {intake.requested_loan_amount or '(not provided)'}\n"
        f"Use of funds: {intake.loan_purpose or '(not provided)'}\n"
    )
    ev = core.CalendarEvent(
        loan_id=None,
        kind=core.CalendarEventKind.CALL,
        title=f"{'Equipment financing' if equipment_intake else 'Dealer AI'} call: {intake.business_name or intake.full_name}",
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=booking.duration_min,
        status=core.CalendarEventStatus.PENDING,
        source=core.CalendarEventSource.AUTO,
        owner_user_id=owner.id,
        external_ref_kind="dealer_ai_intake",
        external_ref_id=str(intake.id),
    )
    db.add(ev)
    await db.flush()
    notice = await core._register_intake_booking(
        db, intake=intake, booking=booking, event=ev, owner=owner
    )
    # The calendar needs an appointment row to act on this booking; the
    # intake is its file, so it is linked from the start (origin=intake).
    from app.dealer_os.services import booking_appointments

    await booking_appointments.create_booking_appointment(
        db,
        event=ev,
        host=owner,
        booking=booking,
        origin="intake",
        invitee_name=intake.full_name,
        invitee_email=intake.email,
        invitee_phone=intake.phone,
        company=intake.business_name,
        notes=f"Booked from the AI intake ({intake.variant}).",
        program_name=intake.loan_purpose,
        requested_amount=str(intake.requested_loan_amount) if intake.requested_loan_amount else None,
        converted_intake_id=intake.id,
        precall_intake_id=intake.id if booking.precall_enabled else None,
        contact_source="ai_intake",
    )

    state = core._intake_state(intake)
    state["call_booking"] = {
        "event_id": str(ev.id),
        "starts_at": starts_at.isoformat(),
        "booked_at": core._now().isoformat(),
        "host_user_id": str(owner.id),
        "host_email": owner.email,
    }
    intake.intake_state = state
    db.add(
        core.Activity(
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
    await core._log(
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
    await core._deliver_intake_booking(
        db, intake=intake, owner=owner, booking=booking, event=ev, notice=notice
    )
    intake = await core._load_public_intake(db, token)
    return await core._response(
        db,
        intake,
        token=token,
        assistant_message="Your call is booked. Keep uploading baseline documents here if anything is still missing before the meeting.",
    )
