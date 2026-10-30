"""/buckets/client/intakes — route handlers of the AI intake.

Moved out of app/routers/dealer_ai_intake.py, which keeps every helper,
constant, router object and service import: thirty-one test files patch names
there, so those references go through `core` and resolve at call time, and the
old module still exposes these handlers by name. Importing this module is what
registers the routes.
"""

from __future__ import annotations

# ruff: noqa: B008, E501
import hashlib
import json
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.routers.dealer_ai_intake as core

__all__ = [
    "claim_capital_readiness_diagnostic",
    "list_my_dealer_intakes",
    "get_my_dealer_intake",
    "my_dealer_intake_chat",
    "execute_my_intake_chat_action",
    "download_my_intake_chat_template",
    "my_dealer_upload_init",
    "my_dealer_upload_complete",
    "my_dealer_submit_pfs",
    "my_dealer_submit_debt_schedule",
    "read_client_financial_statement",
    "save_client_financial_statement"
]


@core.client_router.post(
    "/capital-readiness/claim",
    response_model=core.CapitalReadinessClaimResponse,
    status_code=status.HTTP_201_CREATED,
)
async def claim_capital_readiness_diagnostic(
    payload: core.CapitalReadinessClaimRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.CapitalReadinessClaimResponse:
    """Create one real Main Street intake from a consented public diagnostic.

    The browser-computed result is retained only as self-reported evidence. It
    never becomes the canonical score; the readiness engine recalculates from
    the typed answers and gives them lower confidence until reviewed evidence
    arrives.
    """

    if user.role != core.Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client account required")
    if not payload.terms_accepted or not payload.privacy_accepted:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Terms and Privacy Policy acceptance is required.",
        )
    diagnostic = payload.capital_readiness_diagnostic
    if diagnostic.answers.business_type != "main_street":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This claim endpoint accepts Main Street diagnostics only.",
        )
    # A client row may not exist yet for invited/legacy accounts.  Lock the
    # durable parent first so two first-use claim requests cannot both observe
    # no Client and race the unique clients.user_id constraint.
    locked_user = (
        await db.execute(select(core.User).where(core.User.id == user.id).with_for_update())
    ).scalar_one()
    client = (
        await db.execute(
            select(core.Client).where(core.Client.user_id == user.id).with_for_update()
        )
    ).scalar_one_or_none()
    if client is None:
        client = core.Client(
            user_id=user.id,
            name=(
                payload.full_name
                or locked_user.name
                or locked_user.email.split("@", 1)[0]
            ).strip(),
            email=locked_user.email.lower(),
            phone=payload.phone,
            language="Spanish" if diagnostic.locale == "es" else "English",
            referral_source="capital_readiness_diagnostic",
        )
        db.add(client)
        await db.flush()

    claim_hash = hashlib.sha256(
        json.dumps(
            payload.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    existing_rows = list(
        (
            await db.execute(
                select(core.PublicUnderwritingIntake)
                .where(
                    core.PublicUnderwritingIntake.client_id == client.id,
                    core.PublicUnderwritingIntake.variant == core.MAIN_STREET_VARIANT,
                )
                .order_by(core.PublicUnderwritingIntake.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for existing in existing_rows:
        claim = (existing.intake_state or {}).get("capital_readiness_claim") or {}
        if claim.get("idempotency_key") != str(payload.idempotency_key):
            continue
        if claim.get("request_hash") != claim_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That idempotency key was already used with different answers.",
            )
        profile = await core.profiles_service.provision_profile_for_intake(db, existing)
        snapshot = await core.capital_readiness_service.recompute(
            db,
            profile,
            idempotency_key=f"claim:{payload.idempotency_key}",
            expected_snapshot_version=None,
        )
        await db.commit()
        return core.CapitalReadinessClaimResponse(
            intake_id=existing.id,
            profile_id=profile.id,
            created=False,
            communication_locale=profile.communication_locale,
            readiness=core.capital_readiness_service.read_snapshot(
                snapshot, client_safe=True
            ),
        )

    purpose = diagnostic.answers.funding_purpose
    intent = payload.intent or {
        "working_capital": "working_capital",
        "equipment": "equipment",
        "debt_refinance": "refinance_debt",
        "mca_refinance": "refinance_debt",
    }.get(purpose, "not_sure")
    create_payload = core.AdminLeadCreate(
        variant="main_street",
        full_name=(payload.full_name or locked_user.name or client.name).strip(),
        email=client.email or locked_user.email,
        phone=payload.phone,
        business_name=payload.business_name,
        industry=payload.industry,
        intent=intent,
        preferred_language=core.Language(diagnostic.locale),
        capital_readiness_diagnostic=diagnostic,
        secure_room_pin=core._generate_passcode(),
        notify_client=False,
        force_new=False,
    )
    created_response = await core._create_admin_ai_lead_core(
        create_payload,
        request=request,
        user=user,
        db=db,
        commit=False,
        client_override=client,
    )
    intake = await db.get(core.PublicUnderwritingIntake, created_response.intake.id)
    if intake is None:  # pragma: no cover - defensive transactional invariant
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Intake creation failed")
    state = dict(intake.intake_state or {})
    state["capital_readiness_claim"] = {
        "idempotency_key": str(payload.idempotency_key),
        "request_hash": claim_hash,
        "consented_at": core._now().isoformat(),
        "user_id": str(user.id),
        "terms_version": payload.terms_version,
        "privacy_version": payload.privacy_version,
    }
    intake.intake_state = state
    intake.source_kind = "client_capital_readiness"
    intake.source_detail = "Consented Capital Readiness diagnostic"
    profile = await core.profiles_service.provision_profile_for_intake(db, intake)
    snapshot = await core.capital_readiness_service.recompute(
        db,
        profile,
        idempotency_key=f"claim:{payload.idempotency_key}",
        expected_snapshot_version=None,
    )
    await db.commit()
    return core.CapitalReadinessClaimResponse(
        intake_id=intake.id,
        profile_id=profile.id,
        created=True,
        communication_locale=profile.communication_locale,
        readiness=core.capital_readiness_service.read_snapshot(snapshot, client_safe=True),
    )

@core.client_router.get("", response_model=list[core.DealerIntakeRead])
async def list_my_dealer_intakes(user: core.CurrentUser, db: AsyncSession = Depends(core.get_db)) -> list[core.DealerIntakeRead]:
    if user.role != core.Role.CLIENT or user.client is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client account required")
    rows = (
        await db.execute(
            select(core.PublicUnderwritingIntake)
            .where(core.PublicUnderwritingIntake.client_id == user.client.id)
            .order_by(core.PublicUnderwritingIntake.updated_at.desc())
        )
    ).scalars().all()
    return [core.DealerIntakeRead.model_validate(row) for row in rows]

@core.client_router.get("/{intake_id}", response_model=core.DealerIntakeResponse)
async def get_my_dealer_intake(intake_id: UUID, user: core.CurrentUser, db: AsyncSession = Depends(core.get_db)) -> core.DealerIntakeResponse:
    intake = await core._load_client_intake(db, user, intake_id, allow_pending_signing=True)
    return await core._response(db, intake, token=None)

@core.client_router.post("/{intake_id}/chat", response_model=core.DealerIntakeResponse)
async def my_dealer_intake_chat(
    intake_id: UUID,
    payload: core.DealerChatRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    intake = await core._load_client_intake(db, user, intake_id)
    update_data = payload.updates.model_dump(exclude_unset=True) if payload.updates else {}
    core._apply_updates(intake, payload.updates)
    await core._ensure_equipment_evidence_for_update(db, intake, payload.updates)
    core._record_chat_fact(intake, payload.message)
    await core._log_dealer_update_events(db, intake, update_data, request=request, user=user)
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        chat_messages, assistant_message, _paused = await core._client_chat_turn(
            db, intake, payload.message.strip(), actor_name=user.name or intake.full_name, user=user
        )
        messages = chat_messages
    await db.commit()
    intake = await core._load_client_intake(db, user, intake_id, allow_pending_signing=True)
    return await core._response(
        db,
        intake,
        token=None,
        assistant_message=assistant_message,
        messages=messages,
    )

@core.client_router.post("/{intake_id}/chat-actions/{action_id}", response_model=core.IntakeChatActionResult)
async def execute_my_intake_chat_action(
    intake_id: UUID,
    action_id: UUID,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.IntakeChatActionResult:
    intake = await core._load_client_intake(db, user, intake_id)
    action = await core._intake_chat_action(db, intake, action_id)
    if intake.bucket_upload_link is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This intake has no active secure room")
    result = await core.execute_room_action(
        db,
        action=action,
        link=intake.bucket_upload_link,
        download_url_override=(
            f"/api/v1/buckets/client/intakes/{intake.id}/chat-actions/{action.id}/template"
        ),
    )
    await core._log(
        db,
        intake.bucket_id,
        f"chat_action_{result.status}",
        request=request,
        user=user,
        target_type="chat_action",
        target_id=str(action.id),
        detail=f"{action.action_type}: {result.detail}"[:500],
    )
    await db.commit()
    return result

@core.client_router.get("/{intake_id}/chat-actions/{action_id}/template")
async def download_my_intake_chat_template(
    intake_id: UUID,
    action_id: UUID,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> Response:
    intake = await core._load_client_intake(db, user, intake_id)
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
        user=user,
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

@core.client_router.post("/{intake_id}/files/upload-init", response_model=core.BucketFileUploadInitResponse)
async def my_dealer_upload_init(
    intake_id: UUID,
    payload: core.DealerFileUploadInit,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFileUploadInitResponse:
    intake = await core._load_client_intake(db, user, intake_id)
    return await core._start_upload(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email,
                               source_kind="client_room", actor=None)

@core.client_router.post("/{intake_id}/files/complete", response_model=core.BucketFileRead)
async def my_dealer_upload_complete(
    intake_id: UUID,
    payload: core.DealerUploadComplete,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_client_intake(db, user, intake_id)
    return await core._complete_upload(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)

@core.client_router.post("/{intake_id}/requested-documents/pfs", response_model=core.BucketFileRead)
async def my_dealer_submit_pfs(
    intake_id: UUID,
    payload: core.DealerPfsSubmission,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_client_intake(db, user, intake_id)
    return await core._submit_pfs_form(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)

@core.client_router.post("/{intake_id}/requested-documents/debt-schedule", response_model=core.BucketFileRead)
async def my_dealer_submit_debt_schedule(
    intake_id: UUID,
    payload: core.DealerDebtScheduleSubmission,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    intake = await core._load_client_intake(db, user, intake_id)
    return await core._submit_debt_schedule_form(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)

@core.client_router.get("/{intake_id}/financial-statement")
async def read_client_financial_statement(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> dict:
    """The client's own statement, on their own file.

    Scoped through `_load_client_intake`, so a signed-in client reaches their
    file and nobody else's. The staff endpoints are profile-scoped and refuse
    the CLIENT role outright; this is the same data through the door a client
    actually has.
    """
    from app.services import application_profiles as profile_service
    from app.services import financial_statements, pfs_schema

    intake = await core._load_client_intake(db, user, intake_id, allow_pending_signing=True)
    profile = await profile_service.provision_profile_for_intake(db, intake)
    statement = await financial_statements.latest_for_profile(db, profile.id)
    await db.commit()
    return {
        "schema": pfs_schema.describe(),
        "statement_id": str(statement.id) if statement else None,
        "status": statement.status if statement else None,
        "body": (statement.body if statement else None) or pfs_schema.empty_body(),
    }

@core.client_router.put("/{intake_id}/financial-statement")
async def save_client_financial_statement(
    intake_id: UUID,
    payload: core.DealerFinancialStatementSave,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> dict:
    """Save what the client has entered, and file it.

    One button for them, as asked: saving is finishing. The rows are kept either
    way, so a client who spots a wrong figure reopens the page and corrects it
    rather than starting again.
    """
    from app.services import application_profiles as profile_service
    from app.services import financial_statements

    intake = await core._load_client_intake(db, user, intake_id)
    profile = await profile_service.provision_profile_for_intake(db, intake)
    statement = await financial_statements.latest_for_profile(db, profile.id)
    saved = await financial_statements.save_statement(
        db,
        profile,
        body=payload.body,
        statement_date=payload.statement_date,
        status=statement.status if statement else "draft",
        statement=statement,
    )
    if payload.submit:
        req = next(
            (doc for doc in intake.bucket.requested_documents if doc.category == "Personal Financials"),
            None,
        )
        if req is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A personal financial statement is not requested on this file",
            )
        from app.services import dealer_forms_pdf, drafted_forms, pfs_schema

        statement_date = (
            saved.statement_date.isoformat() if saved.statement_date else "not stated"
        )
        applicant = (payload.body or {}).get("applicant") or {}
        label = f"Personal Financial Statement — {applicant.get('name') or user.name or 'applicant'}"
        stored = await drafted_forms.store_form_pdf(
            db,
            bucket_id=intake.bucket_id,
            upload_link_id=intake.bucket_upload_link_id,
            requested_document=req,
            pdf_bytes=dealer_forms_pdf.render_pfs_413_pdf(
                body=payload.body or {}, statement_date=statement_date
            ),
            file_label=label,
            classification="personal_financial_statement",
            key_facts=pfs_schema.key_facts(payload.body or {}, statement_date=statement_date),
            actor_name=user.name or user.email,
            actor_email=user.email,
            summary=f"{label} submitted by the client from their own account.",
        )
        # `submitted_by_user_id` stays null: the client filled this in
        # themselves, and `filled_by_staff` must not claim otherwise just
        # because they happened to be signed in.
        await financial_statements.save_statement(
            db, profile, body=payload.body or {}, status="submitted", statement=saved
        )
        saved.bucket_file_id = stored.id
    await db.commit()
    return {"statement_id": str(saved.id), "submitted": bool(payload.submit)}
