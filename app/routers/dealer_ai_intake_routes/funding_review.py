"""/public/funding-review — route handlers of the AI intake.

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

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.routers.dealer_ai_intake as core

__all__ = [
    "start_funding_review",
    "start_funding_review_login",
    "verify_funding_review_login",
    "get_funding_review_session",
    "logout_funding_review_session",
    "download_funding_review_intelligence_pdf",
    "get_funding_review",
    "funding_review_chat",
    "funding_review_upload_init",
    "funding_review_upload_complete",
    "funding_review_sign_requested_document",
    "run_funding_review",
    "funding_router_secure_room",
    "book_funding_review_call"
]


@core.funding_router.post("/start", response_model=core.DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def start_funding_review(
    payload: core.FundingReviewStart,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    await core._guard_arf_attribution(db, payload)
    if not payload.terms_accepted or not payload.privacy_accepted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Terms and Privacy Policy acceptance is required.")
    existing = await core._latest_active_intake_by_email(db, str(payload.email), variant=core.FUNDING_VARIANT)
    if existing is not None:
        await core._start_login_challenge(
            db,
            email=str(payload.email),
            request=request,
            reason="existing_funding_review_start",
            variant=core.FUNDING_VARIANT,
            review_label="real estate funding review",
            event_prefix="funding_review",
            target_type="funding_review_intake",
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A secure funding review already exists for this email. We sent a short access code so you can continue that file.",
        )
    client = await core._find_or_create_funding_client(db, payload)
    bucket, link = await core._create_bucket_for_funding_review(db, client, payload, request)
    token = core._new_public_token()
    attribution = core._public_intake_attribution(payload)
    basics = {
        "investor_name": payload.investor_name,
        "target_property_address": payload.target_property_address,
        "transaction_type": payload.transaction_type,
        "requested_amount": payload.requested_amount,
        "estimated_value_or_purchase_price": payload.estimated_value_or_purchase_price,
        "monthly_rent": payload.monthly_rent,
        "estimated_credit_tier": payload.estimated_credit_tier,
    }
    intake = core.PublicUnderwritingIntake(
        source_kind="public_form",
        source_detail="Funding review form",
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=core._hash_token(token),
        variant=core.FUNDING_VARIANT,
        full_name=payload.full_name.strip(),
        email=client.email or core._normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.investor_name,
        loan_purpose=payload.transaction_type,
        requested_loan_amount=payload.requested_amount,
        referral_source=(attribution or {}).get("source") or "funding_review",
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
            "funding_review_basics": basics,
            "legal_acceptance": {
                "terms_accepted": payload.terms_accepted,
                "privacy_accepted": payload.privacy_accepted,
                "terms_version": payload.terms_version or core.TERMS_VERSION,
                "privacy_version": payload.privacy_version or core.PRIVACY_VERSION,
                **core._request_audit(request),
            },
        },
    )
    db.add(intake)
    await db.commit()
    intake = await core._load_public_intake(db, token)
    await core._record_resume_email(
        intake,
        token=token,
        request=request,
        reason="funding_review_created",
        public_path=core.FUNDING_PUBLIC_PATH,
        review_label="real estate funding review",
        room_label="real estate funding review file",
    )
    await db.commit()
    intake = await core._load_public_intake(db, token)
    return await core._response(db, intake, token=token, public_path=core.FUNDING_PUBLIC_PATH, assistant_message=core._funding_empty_message(intake.preferred_language))

@core.funding_router.post("/login/start", response_model=core.DealerLoginStartResponse)
async def start_funding_review_login(
    payload: core.DealerLoginStartRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerLoginStartResponse:
    login_required = await core._start_login_challenge(
        db,
        email=str(payload.email),
        request=request,
        reason="funding_review_login_requested",
        variant=core.FUNDING_VARIANT,
        review_label="real estate funding review",
        event_prefix="funding_review",
        target_type="funding_review_intake",
    )
    await db.commit()
    return core.DealerLoginStartResponse(
        login_required=login_required,
        message=(
            "We found an existing funding review for this email. Enter the code we sent to continue."
            if login_required
            else "No existing funding review was found. Complete Step 1 to start a new review."
        ),
    )

@core.funding_router.post("/login/verify", response_model=core.DealerIntakeResponse)
async def verify_funding_review_login(
    payload: core.DealerLoginVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    email_hash = core._hash_token(core._normalize_email(str(payload.email)))
    challenge = (
        await db.execute(
            select(core.DealerIntakeLoginChallenge)
            .join(core.PublicUnderwritingIntake, core.DealerIntakeLoginChallenge.intake_id == core.PublicUnderwritingIntake.id)
            .where(
                core.PublicUnderwritingIntake.variant == core.FUNDING_VARIANT,
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
    if intake is None or intake.variant != core.FUNDING_VARIANT:
        challenge.revoked_at = core._now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    intake.token_hash = core._hash_token(public_token)
    await core._log(
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
    intake = await core._load_public_intake(db, public_token)
    return await core._response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        public_path=core.FUNDING_PUBLIC_PATH,
        assistant_message=core._re_welcome_back(intake.preferred_language),
    )

@core.funding_router.get("/session", response_model=core.DealerIntakeResponse)
async def get_funding_review_session(request: Request, db: AsyncSession = Depends(core.get_db)) -> core.DealerIntakeResponse:
    intake, _challenge, session_token = await core._load_funding_intake_by_session(db, request)
    public_token = core._new_public_token()
    intake.token_hash = core._hash_token(public_token)
    await db.commit()
    intake = await core._load_public_intake(db, public_token)
    return await core._response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        public_path=core.FUNDING_PUBLIC_PATH,
        assistant_message=core._re_welcome_back(intake.preferred_language),
    )

@core.funding_router.post("/logout", response_model=core.DealerLogoutResponse)
async def logout_funding_review_session(
    payload: core.DealerLogoutRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerLogoutResponse:
    session_token = payload.session_token or request.headers.get("x-funding-session") or request.headers.get("X-Funding-Session")
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

@core.funding_router.get("/intelligence.pdf")
async def download_funding_review_intelligence_pdf(
    request: Request,
    token: str | None = None,
    db: AsyncSession = Depends(core.get_db),
) -> Response:
    if token:
        intake = await core._load_public_intake(db, token)
        core._require_funding_intake(intake)
    else:
        intake, _challenge, _session_token = await core._load_funding_intake_by_session(db, request)
    review = intake.latest_review if intake.latest_review else None
    latest_result = review.result if review and isinstance(review.result, dict) else intake.result_snapshot if isinstance(intake.result_snapshot, dict) else None
    pdf_bytes = await asyncio.to_thread(
        core.render_dealer_intelligence_pdf,
        intake=intake,
        files=sorted(
            (
                file
                for file in core._active_files(intake.bucket)
                if not core._is_package_readiness_output(file)
            ),
            key=lambda file: file.created_at,
            reverse=True,
        ),
        missing_docs=core._missing_required_docs(intake.bucket),
        result=latest_result,
    )
    filename = core._safe_filename(f"funding-intelligence-{intake.business_name or intake.full_name or 'review'}.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@core.funding_router.get("/{token}", response_model=core.DealerIntakeResponse)
async def get_funding_review(token: str, db: AsyncSession = Depends(core.get_db)) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_funding_intake(intake)
    return await core._response(db, intake, token=token, public_path=core.FUNDING_PUBLIC_PATH, empty_message=core._funding_empty_message(intake.preferred_language))

@core.funding_router.post("/{token}/chat", response_model=core.DealerIntakeResponse)
async def funding_review_chat(
    token: str,
    payload: core.DealerChatRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_funding_intake(intake)
    core._apply_updates(intake, payload.updates)
    await core._ensure_equipment_evidence_for_update(db, intake, payload.updates)
    core._record_chat_fact(intake, payload.message)
    intake.last_message_at = core._now()
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **core._funding_review_context(intake)}
        chat_messages, assistant_message, paused = await core._client_chat_turn(
            db, intake, payload.message.strip(), actor_name=intake.full_name
        )
        messages = chat_messages
        if chat_messages and not paused:
            raw = chat_messages[-1].metadata_json.get("raw") if isinstance(chat_messages[-1].metadata_json, dict) else None
            proposed_facts = raw.get("proposed_borrower_facts") if isinstance(raw, dict) else None
            core._merge_funding_review_details(intake, proposed_facts)
    await db.commit()
    intake = await core._load_public_intake(db, token)

    prequal_widget = None
    if core._re_prequal_ready(intake) and await core._latest_artifact(db, intake.id, "prequalification") is None:
        acting_admin = await core.primary_super_admin(db)
        if acting_admin is not None:
            artifact = await core._create_prequalification_artifact(db, intake, acting_admin)
            await db.commit()
            prequal_widget = core._prequalification_widget(artifact)

    return await core._response(
        db,
        intake,
        token=token,
        public_path=core.FUNDING_PUBLIC_PATH,
        assistant_message=assistant_message,
        messages=messages,
        prequalification_widget=prequal_widget,
    )

@core.funding_router.post("/{token}/files/upload-init", response_model=core.BucketFileUploadInitResponse)
async def funding_review_upload_init(
    token: str,
    payload: core.DealerFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFileUploadInitResponse:
    intake = await core._load_public_intake(db, token)
    core._require_funding_intake(intake)
    return await core._start_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email,
                               source_kind="client_room")

@core.funding_router.post("/{token}/files/complete", response_model=core.BucketFileRead)
async def funding_review_upload_complete(
    token: str,
    payload: core.DealerUploadComplete,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_public_intake(db, token)
    core._require_funding_intake(intake)
    uploaded = await core._complete_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)
    if intake.variant == core.FORECLOSURE_RESCUE_VARIANT and intake.foreclosure_rescue_status in {
        "new_rescue", "initial_docs_pending"
    }:
        initial_docs = (
            await db.execute(
                select(core.BucketRequestedDocument).where(
                    core.BucketRequestedDocument.bucket_id == intake.bucket_id,
                    core.BucketRequestedDocument.category == "Initial Review",
                    core.BucketRequestedDocument.required.is_(True),
                )
            )
        ).scalars().all()
        satisfied = {"uploaded", "received_unverified", "verified", "waived", "not_applicable"}
        if initial_docs and all(document.status in satisfied for document in initial_docs):
            intake.foreclosure_rescue_status = "ready_for_initial_review"
            intake.status = "reviewing"
            await db.commit()
    return uploaded

@core.funding_router.post("/{token}/requested-documents/sign", response_model=core.BucketFileRead)
async def funding_review_sign_requested_document(
    token: str,
    payload: core.DealerDocumentSignRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_public_intake(db, token)
    core._require_funding_intake(intake)
    return await core._sign_requested_document(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)

@core.funding_router.post("/{token}/run-review", response_model=core.DealerIntakeResponse)
async def run_funding_review(
    token: str,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_funding_intake(intake)
    await core._execute_intake_review(
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
    intake = await core._load_public_intake(db, token)
    return await core._response(db, intake, token=token, public_path=core.FUNDING_PUBLIC_PATH)

@core.funding_router.post("/{token}/secure-room", response_model=core.SecureRoomHandoffResponse)
async def funding_router_secure_room(
    token: str,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.SecureRoomHandoffResponse:
    """Open this file's secure room, where the bank connection and the
    agreements live. The client is already verified on this intake."""
    intake = await core._load_public_intake(db, token)
    return await core._secure_room_handoff(db, intake, request, tab="todo")

@core.funding_router.post("/{token}/book-call", response_model=core.DealerIntakeResponse)
async def book_funding_review_call(
    token: str,
    payload: core.DealerBookCallRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_funding_intake(intake)
    if core._call_booked(intake):
        return await core._response(
            db,
            intake,
            token=token,
            public_path=core.FUNDING_PUBLIC_PATH,
            assistant_message="Your call is already booked. Keep uploading property and rent evidence here if anything is still missing before the meeting.",
        )
    starts_at = core._to_utc_minute(payload.starts_at)
    owner, booking, slots = await core._dealer_call_slots(db, lock_host=True)
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
    ev = core.CalendarEvent(
        loan_id=None,
        kind=core.CalendarEventKind.CALL,
        title=f"Funding review call: {intake.business_name or intake.full_name}",
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=booking.duration_min,
        status=core.CalendarEventStatus.PENDING,
        source=core.CalendarEventSource.AUTO,
        owner_user_id=owner.id,
        external_ref_kind="funding_review_intake",
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
    await core._log(
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
    await core._deliver_intake_booking(
        db, intake=intake, owner=owner, booking=booking, event=ev, notice=notice
    )
    intake = await core._load_public_intake(db, token)
    return await core._response(
        db,
        intake,
        token=token,
        public_path=core.FUNDING_PUBLIC_PATH,
        assistant_message="Your call is booked. Keep uploading property, rent, and PITIA evidence here if anything is still missing before the meeting.",
    )
