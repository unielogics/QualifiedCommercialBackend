"""/public/mca-refinance — route handlers of the AI intake.

Moved out of app/routers/dealer_ai_intake.py, which keeps every helper,
constant, router object and service import: thirty-one test files patch names
there, so those references go through `core` and resolve at call time, and the
old module still exposes these handlers by name. Importing this module is what
registers the routes.
"""

from __future__ import annotations

# ruff: noqa: B008, E501
import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.routers.dealer_ai_intake as core

__all__ = [
    "execute_public_intake_chat_action",
    "download_public_intake_chat_template",
    "start_mca_refinance",
    "start_mca_refinance_login",
    "verify_mca_refinance_login",
    "get_mca_refinance_session",
    "logout_mca_refinance_session",
    "get_mca_refinance",
    "mca_refinance_chat",
    "update_mca_refinance_qualification",
    "mca_refinance_upload_init",
    "mca_refinance_upload_complete",
    "mca_refinance_sign_requested_document",
    "submit_mca_refinance_terms",
    "mca_refinance_credit_pull",
    "run_mca_refinance_review",
    "mca_router_secure_room",
    "book_mca_refinance_call"
]


@core.router.post("/{token}/chat-actions/{action_id}", response_model=core.IntakeChatActionResult)
@core.funding_router.post("/{token}/chat-actions/{action_id}", response_model=core.IntakeChatActionResult)
@core.mca_router.post("/{token}/chat-actions/{action_id}", response_model=core.IntakeChatActionResult)
async def execute_public_intake_chat_action(
    token: str,
    action_id: UUID,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.IntakeChatActionResult:
    intake = await core._load_public_intake(db, token)
    action = await core._intake_chat_action(db, intake, action_id)
    if intake.bucket_upload_link is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This intake has no active secure room")
    public_action_segment = (
        "funding-review"
        if intake.variant == core.FUNDING_VARIANT
        else "mca-refinance"
        if intake.variant == core.MCA_VARIANT
        else "dealer-ai-intake"
    )
    result = await core.execute_room_action(
        db,
        action=action,
        link=intake.bucket_upload_link,
        download_url_override=(
            f"/api/v1/public/{public_action_segment}/{token}/chat-actions/{action.id}/template"
        ),
    )
    await core._log(
        db,
        intake.bucket_id,
        f"chat_action_{result.status}",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="chat_action",
        target_id=str(action.id),
        detail=f"{action.action_type}: {result.detail}"[:500],
    )
    await db.commit()
    return result

@core.router.get("/{token}/chat-actions/{action_id}/template")
@core.funding_router.get("/{token}/chat-actions/{action_id}/template")
@core.mca_router.get("/{token}/chat-actions/{action_id}/template")
async def download_public_intake_chat_template(
    token: str,
    action_id: UUID,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> Response:
    intake = await core._load_public_intake(db, token)
    action = await core._intake_chat_action(db, intake, action_id)
    if action.action_type != "download_template":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    template = core.template_for_action(action)
    action.status = "executed"
    action.executed_at = action.executed_at or core._now()
    action.result = action.result or {"detail": "Template downloaded"}
    await core._log(
        db,
        intake.bucket_id,
        "chat_template_downloaded",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="chat_action",
        target_id=str(action.id),
        detail=template.filename,
    )
    await db.commit()
    return Response(
        content=template.content,
        media_type=template.content_type,
        headers={"Content-Disposition": f'attachment; filename="{template.filename}"'},
    )

@core.mca_router.post("/start", response_model=core.DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def start_mca_refinance(
    payload: core.McaRefiStart,
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
    existing = await core._latest_active_intake_by_email(db, str(payload.email), variant=core.MCA_VARIANT)
    if existing is not None:
        await core._start_login_challenge(
            db,
            email=str(payload.email),
            request=request,
            reason="existing_mca_refinance_start",
            variant=core.MCA_VARIANT,
            review_label="MCA refinance review",
            event_prefix="mca_refinance",
            target_type="mca_refinance_intake",
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A secure MCA refinance review already exists for this email. We sent a short access code so you can continue that file.",
        )
    client = await core._find_or_create_mca_client(db, payload)
    bucket, link = await core._create_bucket_for_mca_refi(db, client, payload, request)
    token = core._new_public_token()
    attribution = core._public_intake_attribution(payload)
    seeded: dict[str, Any] = {}
    if payload.remaining_payback is not None:
        seeded["remaining_payback"] = payload.remaining_payback
    if payload.months_remaining is not None:
        seeded["months_remaining"] = payload.months_remaining
    if payload.payment_frequency in core._MCA_REMITS_PER_MONTH:
        seeded["payment_frequency"] = payload.payment_frequency
    intake = core.PublicUnderwritingIntake(
        source_kind="public_form",
        source_detail="MCA refinance form",
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=core._hash_token(token),
        variant=core.MCA_VARIANT,
        full_name=payload.full_name.strip(),
        email=client.email or core._normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        loan_purpose="mca_refinance",
        requested_loan_amount=payload.requested_amount if payload.requested_amount is not None else payload.remaining_payback,
        referral_source=(attribution or {}).get("source") or "mca_refinance",
        preferred_language=payload.preferred_language,
        intake_state={
            "messages": [],
            "source": "mca_refinance",
            **({"mca_manual_review_requested_at": core._now().isoformat()} if payload.manual_review_requested else {}),
            "mca_qualification": {
                "active_mca_count": payload.active_mca_count,
                "requested_amount": payload.requested_amount if payload.requested_amount is not None else payload.remaining_payback,
                "requested_term_months": payload.requested_term_months,
            },
            **({"signup_attribution": attribution} if attribution else {}),
            **({"mca_details": seeded} if seeded else {}),
            "legal_acceptance": {
                "terms_accepted": payload.terms_accepted,
                "privacy_accepted": payload.privacy_accepted,
                "terms_version": payload.terms_version or core.TERMS_VERSION,
                "privacy_version": payload.privacy_version or core.PRIVACY_VERSION,
                **core._request_audit(request),
            },
        },
    )
    intake.bucket = bucket
    core.mca_refinance_policy.persist(intake)
    db.add(intake)
    await db.commit()
    intake = await core._load_public_intake(db, token)
    await core._record_resume_email(
        intake,
        token=token,
        request=request,
        reason="mca_refinance_created",
        public_path=core.MCA_PUBLIC_PATH,
        review_label="MCA refinance review",
        room_label="MCA refinance file",
    )
    await db.commit()
    intake = await core._load_public_intake(db, token)
    return await core._response(db, intake, token=token, public_path=core.MCA_PUBLIC_PATH, assistant_message=core._mca_empty_message(intake.preferred_language))

@core.mca_router.post("/login/start", response_model=core.DealerLoginStartResponse)
async def start_mca_refinance_login(
    payload: core.DealerLoginStartRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerLoginStartResponse:
    login_required = await core._start_login_challenge(
        db,
        email=str(payload.email),
        request=request,
        reason="mca_refinance_login_requested",
        variant=core.MCA_VARIANT,
        review_label="MCA refinance review",
        event_prefix="mca_refinance",
        target_type="mca_refinance_intake",
    )
    await db.commit()
    return core.DealerLoginStartResponse(
        login_required=login_required,
        message=(
            "We found an existing MCA refinance review for this email. Enter the code we sent to continue."
            if login_required
            else "No existing review was found. Complete the form to start a new one."
        ),
    )

@core.mca_router.post("/login/verify", response_model=core.DealerIntakeResponse)
async def verify_mca_refinance_login(
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
                core.PublicUnderwritingIntake.variant == core.MCA_VARIANT,
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
    if intake is None or intake.variant != core.MCA_VARIANT:
        challenge.revoked_at = core._now()
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired access code")
    intake.token_hash = core._hash_token(public_token)
    await core._log(
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
    intake = await core._load_public_intake(db, public_token)
    return await core._response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        public_path=core.MCA_PUBLIC_PATH,
        assistant_message=core._mca_welcome_back(intake.preferred_language),
    )

@core.mca_router.get("/session", response_model=core.DealerIntakeResponse)
async def get_mca_refinance_session(request: Request, db: AsyncSession = Depends(core.get_db)) -> core.DealerIntakeResponse:
    intake, _challenge, session_token = await core._load_mca_intake_by_session(db, request)
    public_token = core._new_public_token()
    intake.token_hash = core._hash_token(public_token)
    await db.commit()
    intake = await core._load_public_intake(db, public_token)
    return await core._response(
        db,
        intake,
        token=public_token,
        session_token=session_token,
        public_path=core.MCA_PUBLIC_PATH,
        assistant_message=core._mca_welcome_back(intake.preferred_language),
    )

@core.mca_router.post("/logout", response_model=core.DealerLogoutResponse)
async def logout_mca_refinance_session(
    payload: core.DealerLogoutRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerLogoutResponse:
    session_token = payload.session_token or request.headers.get("x-mca-session") or request.headers.get("X-Mca-Session")
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

@core.mca_router.get("/{token}", response_model=core.DealerIntakeResponse)
async def get_mca_refinance(token: str, db: AsyncSession = Depends(core.get_db)) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    return await core._response(db, intake, token=token, public_path=core.MCA_PUBLIC_PATH, empty_message=core._mca_empty_message(intake.preferred_language))

@core.mca_router.post("/{token}/chat", response_model=core.DealerIntakeResponse)
async def mca_refinance_chat(
    token: str,
    payload: core.DealerChatRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    core._apply_updates(intake, payload.updates)
    await core._ensure_equipment_evidence_for_update(db, intake, payload.updates)
    core._record_chat_fact(intake, payload.message)
    intake.last_message_at = core._now()
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **core._mca_context(intake)}
        chat_messages, assistant_message, paused = await core._client_chat_turn(
            db, intake, payload.message.strip(), actor_name=intake.full_name
        )
        messages = chat_messages
        if chat_messages and not paused:
            raw = chat_messages[-1].metadata_json.get("raw") if isinstance(chat_messages[-1].metadata_json, dict) else None
            proposed_facts = raw.get("proposed_borrower_facts") if isinstance(raw, dict) else None
            core._merge_mca_details(intake, proposed_facts)
    await db.commit()
    intake = await core._load_public_intake(db, token)
    return await core._response(
        db,
        intake,
        token=token,
        public_path=core.MCA_PUBLIC_PATH,
        assistant_message=assistant_message,
        messages=messages,
    )

@core.mca_router.post("/{token}/qualification", response_model=core.DealerIntakeResponse)
async def update_mca_refinance_qualification(
    token: str,
    payload: core.McaRefiQualification,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Save declarations for new and legacy/upload-only files, without underwriting."""
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    state = core._intake_state(intake)
    declaration = dict(state.get("mca_qualification") or {})
    declaration.update(payload.model_dump(exclude_unset=True, exclude_none=True, exclude={"manual_review_requested"}))
    state["mca_qualification"] = declaration
    intake.intake_state = state
    if payload.requested_amount is not None:
        intake.requested_loan_amount = payload.requested_amount
    policy = core.mca_refinance_policy.persist(intake, request_manual=payload.manual_review_requested)
    await core._log(db, intake.bucket_id, "mca_refinance_manual_review_requested" if payload.manual_review_requested else "mca_refinance_qualification_saved", request=request,
               actor_name=intake.full_name, actor_email=intake.email, actor_role="public_lead",
               target_type="mca_refinance_intake", target_id=str(intake.id),
               detail="Review route: " + policy["review_mode"])
    await db.commit()
    return await core._response(db, intake, token=token, public_path=core.MCA_PUBLIC_PATH)

@core.mca_router.post("/{token}/files/upload-init", response_model=core.BucketFileUploadInitResponse)
async def mca_refinance_upload_init(
    token: str,
    payload: core.DealerFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFileUploadInitResponse:
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    return await core._start_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email,
                               source_kind="client_room")

@core.mca_router.post("/{token}/files/complete", response_model=core.BucketFileRead)
async def mca_refinance_upload_complete(
    token: str,
    payload: core.DealerUploadComplete,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    return await core._complete_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)

@core.mca_router.post("/{token}/requested-documents/sign", response_model=core.BucketFileRead)
async def mca_refinance_sign_requested_document(
    token: str,
    payload: core.DealerDocumentSignRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    return await core._sign_requested_document(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)

@core.mca_router.post("/{token}/requested-documents/mca-terms", response_model=core.BucketFileRead)
async def submit_mca_refinance_terms(
    token: str,
    payload: core.McaTermsSubmission,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    """The typed-in path for the third checklist item. Renders a PDF and writes
    a TRUSTED BucketFileAnalysis directly from the structured input (the
    _store_drafted_form_pdf trick), so the terms flow into
    extract_debt_schedule and the metrics pipeline identically to an analyzed
    upload — monthly payments are normalized from each advance's remittance
    frequency."""
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
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
        monthly = adv.payment_amount * core._MCA_REMITS_PER_MONTH[adv.payment_frequency]
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
    stored = await core._store_drafted_form_pdf(
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
    state = core._intake_state(intake)
    state["mca_terms_summary"] = {"active_mca_count": len(debts), "remaining_payback": round(total_balance, 2)}
    intake.intake_state = state
    first = payload.advances[0]
    core._merge_mca_details(
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

@core.mca_router.post("/{token}/credit-pull", response_model=core.DealerIntakeResponse)
async def mca_refinance_credit_pull(
    token: str,
    payload: core.PublicCreditPullRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """The first PUBLIC soft-pull endpoint. Consent is the signed
    credit-authorization document on THIS intake — the endpoint refuses to run
    without it, exactly like the admin path (run_lead_credit_pull), and the
    result lands in the same state["credit_pull"] cross-reference. One pull
    per intake: a second call 409s instead of re-billing the bureau."""
    core._throttle_or_429(
        core._MCA_PULL_LAST_BY_IP,
        (request.client.host if request.client else "?") or "?",
        30.0,
        "Please wait a moment before requesting the credit check again.",
    )
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    policy = core.mca_refinance_policy.persist(intake)
    if not policy["auto_underwriting_allowed"]:
        await db.commit()
        return await core._response(db, intake, token=token, public_path=core.MCA_PUBLIC_PATH,
                               assistant_message=core.mca_refinance_policy.message(policy, intake.preferred_language))
    if intake.client_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This review has no linked client")
    if core._credit_pull_state(intake).get("fico") is not None or core._credit_pull_state(intake).get("pull_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The credit check has already been completed for this file.")

    doc = core._credit_authorization_doc(intake)
    if doc is None or doc.status != "uploaded":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Sign the credit authorization first — the soft check cannot run without it.",
        )
    signature = (
        await db.execute(
            select(core.BucketDocumentSignature)
            .where(core.BucketDocumentSignature.requested_document_id == doc.id)
            .order_by(core.BucketDocumentSignature.created_at.desc())
        )
    ).scalars().first()
    applicant_data = signature.applicant_data if signature else None
    if not applicant_data:
        raise HTTPException(status.HTTP_409_CONFLICT, "No applicant identity data on file for this signature")

    client = await db.get(core.Client, intake.client_id)
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

    state = core._intake_state(intake)
    state["credit_pull"] = {
        "pull_id": str(pull.id),
        "fico": pull.fico,
        "pulled_at": pull.pulled_at.isoformat() if pull.pulled_at else None,
        "expires_at": pull.expires_at.isoformat() if pull.expires_at else None,
    }
    intake.intake_state = state
    await core._log(
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
    intake = await core._load_public_intake(db, token)
    return await core._response(
        db,
        intake,
        token=token,
        public_path=core.MCA_PUBLIC_PATH,
        assistant_message="The soft credit check is done — it does not affect your score. That closes another of the three items.",
    )

@core.mca_router.post("/{token}/run-review", response_model=core.DealerIntakeResponse)
async def run_mca_refinance_review(
    token: str,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    await core._execute_intake_review(
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
    intake = await core._load_public_intake(db, token)
    return await core._response(db, intake, token=token, public_path=core.MCA_PUBLIC_PATH)

@core.mca_router.post("/{token}/secure-room", response_model=core.SecureRoomHandoffResponse)
async def mca_router_secure_room(
    token: str,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.SecureRoomHandoffResponse:
    """Open this file's secure room, where the bank connection and the
    agreements live. The client is already verified on this intake."""
    intake = await core._load_public_intake(db, token)
    return await core._secure_room_handoff(db, intake, request, tab="todo")

@core.mca_router.post("/{token}/book-call", response_model=core.DealerIntakeResponse)
async def book_mca_refinance_call(
    token: str,
    payload: core.DealerBookCallRequest,
    request: Request,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_public_intake(db, token)
    core._require_mca_intake(intake)
    if core._call_booked(intake):
        return await core._response(
            db,
            intake,
            token=token,
            public_path=core.MCA_PUBLIC_PATH,
            assistant_message="Your call is already booked. If any of the three items is still open, closing it before the meeting speeds everything up.",
        )
    starts_at = core._to_utc_minute(payload.starts_at)
    owner, booking, slots = await core._dealer_call_slots(db, lock_host=True)
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
    ev = core.CalendarEvent(
        loan_id=None,
        kind=core.CalendarEventKind.CALL,
        title=f"MCA refinance call: {intake.business_name or intake.full_name}",
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=booking.duration_min,
        status=core.CalendarEventStatus.PENDING,
        source=core.CalendarEventSource.AUTO,
        owner_user_id=owner.id,
        external_ref_kind="mca_refinance_intake",
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
    await core._log(
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
    await core._deliver_intake_booking(
        db, intake=intake, owner=owner, booking=booking, event=ev, notice=notice
    )
    intake = await core._load_public_intake(db, token)
    return await core._response(
        db,
        intake,
        token=token,
        public_path=core.MCA_PUBLIC_PATH,
        assistant_message="Your call is booked. Anything still open on the three-item checklist can be finished right here before the meeting.",
    )
