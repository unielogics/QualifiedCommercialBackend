"""/broker/ai-underwriter-leads — route handlers of the AI intake.

Moved out of app/routers/dealer_ai_intake.py, which keeps every helper,
constant, router object and service import: thirty-one test files patch names
there, so those references go through `core` and resolve at call time, and the
old module still exposes these handlers by name. Importing this module is what
registers the routes.
"""

from __future__ import annotations

# ruff: noqa: B008, E501
from typing import Any, Literal
from uuid import UUID

from fastapi import BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, with_loader_criteria

import app.routers.dealer_ai_intake as core

__all__ = [
    "list_broker_dealer_leads",
    "broker_dealer_channel_inbox",
    "broker_mark_channel_seen",
    "create_broker_ai_lead",
    "get_broker_dealer_lead",
    "broker_request_lead_deletion",
    "broker_cancel_lead_deletion",
    "list_broker_lead_notes",
    "create_broker_lead_note",
    "broker_dealer_lead_chat",
    "broker_dealer_lead_upload_init",
    "broker_dealer_lead_upload_complete",
    "broker_submit_lead_pfs",
    "broker_submit_lead_debt_schedule",
    "rerun_broker_dealer_lead_review",
    "broker_dealer_lead_review_progress",
    "broker_request_lead_pfs",
    "broker_request_lead_debt_schedule"
]


@core.broker_router.get("", response_model=core.DealerAILeadListResponse)
async def list_broker_dealer_leads(
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
    q: str | None = None,
    status_filter: str | None = None,
    probability_status: str | None = None,
    archived_filter: Literal["active", "archived", "all"] = "active",
    limit: int = 50,
    offset: int = 0,
) -> core.DealerAILeadListResponse:
    await core._require_dealer_partner(user, db)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    stmt = (
        select(core.PublicUnderwritingIntake)
        .join(core.Bucket, core.PublicUnderwritingIntake.bucket_id == core.Bucket.id)
        .where(
            core.PublicUnderwritingIntake.broker_id == user.id,
            core.PublicUnderwritingIntake.variant == core.DEALER_VARIANT,
            # A broker's own deletion request hides the lead from their own
            # board immediately — the lead stays fully intact and visible to
            # admin (with a pending badge) until admin separately confirms.
            core.PublicUnderwritingIntake.delete_requested_at.is_(None),
        )
        .options(
            selectinload(core.PublicUnderwritingIntake.bucket).selectinload(core.Bucket.requested_documents).selectinload(core.BucketRequestedDocument.template_file),
            selectinload(core.PublicUnderwritingIntake.bucket).selectinload(core.Bucket.files),
            selectinload(core.PublicUnderwritingIntake.bucket).selectinload(core.Bucket.notes),
            selectinload(core.PublicUnderwritingIntake.bucket_upload_link),
            selectinload(core.PublicUnderwritingIntake.latest_review),
            selectinload(core.PublicUnderwritingIntake.broker),
            selectinload(core.PublicUnderwritingIntake.delete_requested_by),
            with_loader_criteria(core.BucketFile, core.BucketFile.deleted_at.is_(None), include_aliases=True),
        )
        .order_by(core.PublicUnderwritingIntake.updated_at.desc())
    )
    closed_lifecycle = core._closed_intake_lifecycle_clause()
    if archived_filter == "active":
        stmt = stmt.where(core.Bucket.archived_at.is_(None), ~closed_lifecycle)
    elif archived_filter == "archived":
        stmt = stmt.where(or_(core.Bucket.archived_at.is_not(None), closed_lifecycle))
    if status_filter and status_filter != "all":
        stmt = stmt.where(core._intake_status_filter_clause(status_filter))
    if q and q.strip():
        stmt = stmt.where(core._intake_search_clause(q))
    rows = list((await db.execute(stmt)).scalars().unique().all())
    if probability_status and probability_status != "all":
        rows = [row for row in rows if str(core._lead_result(row).get("probability_status") or "") == probability_status]
    total = len(rows)
    page = rows[offset:offset + limit]
    underwriting_by_intake = await core._underwriting_statuses_by_intake(
        db, [row.id for row in page]
    )
    profile_hold_intake_ids = await core._profile_hold_intake_ids(
        db,
        [row.id for row in page],
    )
    items = [
        core._lead_row(
            row,
            underwriting_status=underwriting_by_intake.get(row.id),
            effective_profile_hold=row.id in profile_hold_intake_ids,
        )
        for row in page
    ]
    channel_unread = await core._channel_unread_by_intake(db, user=user, intakes=page)
    for item in items:
        item.channel_unread_count = channel_unread.get(item.id, 0)
    return core.DealerAILeadListResponse(items=items, total=total, limit=limit, offset=offset)

@core.broker_router.get("/messages", response_model=core.DealerLeadInboxResponse)
async def broker_dealer_channel_inbox(
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerLeadInboxResponse:
    """Dealer partner's whole communication inbox: one row per owned lead that
    has a team<->partner thread, newest activity first, with this partner's
    unread count. Each row clicks through to that lead's Messages tab in the UI."""
    await core._require_dealer_partner(user, db)
    stmt = (
        select(core.PublicUnderwritingIntake)
        .join(core.Bucket, core.PublicUnderwritingIntake.bucket_id == core.Bucket.id)
        .where(
            core.Bucket.archived_at.is_(None),
            core.PublicUnderwritingIntake.broker_id == user.id,
            core.PublicUnderwritingIntake.variant == core.DEALER_VARIANT,
            core.PublicUnderwritingIntake.delete_requested_at.is_(None),
        )
        .options(selectinload(core.PublicUnderwritingIntake.bucket).selectinload(core.Bucket.notes))
    )
    intakes = list((await db.execute(stmt)).scalars().unique().all())
    return await core._build_channel_inbox(db, user, intakes)

@core.broker_router.post("/{intake_id}/messages/seen", response_model=core.ChannelSeenResponse)
async def broker_mark_channel_seen(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.ChannelSeenResponse:
    """Dealer partner opened the Messages tab on this lead — clear its unread."""
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    seen_at = await core._mark_channel_seen(db, user, intake.id)
    return core.ChannelSeenResponse(intake_id=intake.id, seen_at=seen_at)

@core.broker_router.post("", response_model=core.DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def create_broker_ai_lead(
    payload: core.BrokerLeadCreate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Dealer partner creates an AI-underwriter lead ON BEHALF of their own
    client. Dealer-variant only. Reuses the same creation helpers as the
    public /start and admin-create flows. No terms/throttle."""
    await core._require_dealer_partner(user, db)

    if not payload.force_new:
        existing = await core._latest_active_intake_by_email(db, str(payload.email), variant=core.DEALER_VARIANT)
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
            "at": core._now().isoformat(),
        },
        "on_behalf_of_client": True,
    }

    adapter = core.DealerIntakeStart(
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        business_name=payload.business_name,
    )
    client = await core._find_or_create_client(db, adapter)
    bucket, link = await core._create_bucket_for_intake(db, client, adapter, request)

    if isinstance(client.lead_intake, dict):
        client.lead_intake = {**client.lead_intake, "created_by_broker": str(user.id)}

    token = core._new_public_token()
    intake_state: dict[str, Any] = {
        "messages": [],
        "source": "dealer_ai_intake",
        "broker_provenance": provenance,
    }

    intake = core.PublicUnderwritingIntake(
        source_kind="dealer_partner",
        source_actor_name=(user.name or user.email or "")[:200],
        source_user_id=user.id,
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        broker_id=user.id,
        token_hash=core._hash_token(token),
        variant=core.DEALER_VARIANT,
        full_name=payload.full_name.strip(),
        email=client.email or core._normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        preferred_language=payload.preferred_language,
        asset_rows=[],
        intake_state=intake_state,
    )
    db.add(intake)
    await core._log(
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
    welcome_text = core._admin_created_welcome(core.DEALER_VARIANT)
    db.add(core._persist_admin_welcome_message(bucket.id, welcome_text))
    await db.commit()
    intake = await core._load_broker_dealer_lead(db, user, intake.id)

    email_note = ""
    if payload.notify_client:
        record = await core._record_resume_email(
            intake, token=token, request=request, reason="broker_created", db=db, sender_user_id=user.id,
        )
        await db.commit()
        intake = await core._load_broker_dealer_lead(db, user, intake.id)
        email_note = (
            " A secure login link was emailed to the client."
            if record.get("ok")
            else " Email delivery is unavailable; share the resume link manually."
        )

    return await core._response(
        db,
        intake,
        token=token,
        public_path="/dealer-ai-underwriter",
        include_management=False,
        admin_thread=True,
        thread_user=user,
        assistant_message=welcome_text + email_note,
    )

@core.broker_router.get("/{intake_id}", response_model=core.DealerIntakeResponse)
async def get_broker_dealer_lead(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    return await core._response(db, intake, token=None, include_management=False, admin_thread=True, thread_user=user)

@core.broker_router.post("/{intake_id}/request-deletion", response_model=core.DealerIntakeResponse)
async def broker_request_lead_deletion(
    intake_id: UUID,
    payload: core.RequestLeadDeletionRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Flags the broker's own lead for deletion. Destroys nothing — only
    hides it from THIS broker's own list (list_broker_dealer_leads filters
    delete_requested_at IS NULL); admin still sees the full lead, with a
    pending-deletion badge, until admin separately confirms."""
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    intake.delete_requested_at = core._now()
    intake.delete_requested_by_user_id = user.id
    await core._log(
        db, intake.bucket_id, "dealer_ai_lead_deletion_requested_by_broker", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id), detail=payload.reason,
    )
    await db.commit()
    intake = await core._load_broker_dealer_lead(db, user, intake.id)
    return await core._response(db, intake, token=None, include_management=False, admin_thread=True, thread_user=user)

@core.broker_router.post("/{intake_id}/cancel-deletion-request", response_model=core.DealerIntakeResponse)
async def broker_cancel_lead_deletion(
    intake_id: UUID,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Lets a broker undo their own accidental deletion request before admin
    acts on it — fully reversible, no confirmation needed."""
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    intake.delete_requested_at = None
    intake.delete_requested_by_user_id = None
    await core._log(
        db, intake.bucket_id, "dealer_ai_lead_deletion_request_cancelled_by_broker", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id),
    )
    await db.commit()
    intake = await core._load_broker_dealer_lead(db, user, intake.id)
    return await core._response(db, intake, token=None, include_management=False, admin_thread=True, thread_user=user)

@core.broker_router.get("/{intake_id}/notes", response_model=list[core.BucketNoteRead])
async def list_broker_lead_notes(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> list[core.BucketNoteRead]:
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    notes = sorted((n for n in intake.bucket.notes if n.visibility == "admin"), key=lambda n: n.created_at)
    # The lead was authorised above; whoever may read the note may see what was
    # pasted into it.
    images = await core.inline_images.hydrate(db, "bucket_note", [str(note.id) for note in notes])
    return [
        core.BucketNoteRead.model_validate(note).model_copy(
            update={"images": images.get(str(note.id), [])}
        )
        for note in notes
    ]

@core.broker_router.post("/{intake_id}/notes", response_model=core.BucketNoteRead, status_code=status.HTTP_201_CREATED)
async def create_broker_lead_note(
    intake_id: UUID,
    payload: core.DealerLeadNoteCreate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketNoteRead:
    """Same shared thread as create_admin_lead_note — both routers write/read
    the identical BucketNote rows (visibility='admin'), which is what makes
    this a shared admin<->broker thread with no separate join table."""
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    note = core.BucketNote(
        bucket_id=intake.bucket_id,
        author_name=user.name,
        author_role=user.role,
        visibility="admin",
        content=payload.content,
    )
    db.add(note)
    intake.last_message_at = core._now()
    await db.flush()
    attached = await core.inline_images.attach(
        db,
        image_ids=payload.image_ids,
        subject_kind="bucket_note",
        subject_id=str(note.id),
        user_id=user.id,
    )
    await core._log(db, intake.bucket_id, "dealer_ai_lead_note_created", request=request, user=user, target_type="note", target_id=str(note.id))
    await core.file_events.emit(
        db,
        intake_id=intake.id,
        kind="message.sent",
        visibility=core.file_events.VISIBILITY_TEAM,
        title=f"Note from the partner {user.name or user.email}",
        actor=user,
        target_type="note",
        target_id=note.id,
    )
    await db.commit()
    await db.refresh(note)
    return core.BucketNoteRead.model_validate(note).model_copy(
        update={"images": [core.inline_images.serialize(image) for image in attached]}
    )

@core.broker_router.post("/{intake_id}/chat", response_model=core.DealerIntakeResponse)
async def broker_dealer_lead_chat(
    intake_id: UUID,
    payload: core.DealerChatRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Dealer-partner chat with the underwriting AI on their own lead, in the
    same private internal thread (audience='admin') the admin cockpit uses —
    the client never sees this thread either way."""
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    assistant_message = None
    if payload.message and payload.message.strip():
        if intake.variant == core.FUNDING_VARIANT:
            await core._refresh_dscr_pricing(db)
        context_fn = core._context_fn_for(intake)
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **context_fn(intake)}
        chat_messages, _, _ = await core.create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="admin",
            message=payload.message.strip(),
            actor_name=user.name or "Dealer partner",
            user=user,
            intake_id=intake.id,
        )
        if chat_messages:
            assistant_message = chat_messages[-1].content
            await core._merge_thread_borrower_facts(db, intake, chat_messages[-1])
        core._record_chat_fact(intake, payload.message, source="broker_chat")
    await db.commit()
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    return await core._response(
        db,
        intake,
        token=None,
        include_management=False,
        assistant_message=assistant_message,
        admin_thread=True,
        thread_user=user,
    )

@core.broker_router.post("/{intake_id}/files/upload-init", response_model=core.BucketFileUploadInitResponse)
async def broker_dealer_lead_upload_init(
    intake_id: UUID,
    payload: core.DealerFileUploadInit,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFileUploadInitResponse:
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    return await core._start_upload(db, intake, payload, request, actor_name=user.name or "Dealer partner", actor_email=user.email,
                               source_kind="dealer_partner", actor=user)

@core.broker_router.post("/{intake_id}/files/complete", response_model=core.BucketFileRead)
async def broker_dealer_lead_upload_complete(
    intake_id: UUID,
    payload: core.DealerUploadComplete,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    return await core._complete_upload(db, intake, payload, request, actor_name=user.name or "Dealer partner", actor_email=user.email)

@core.broker_router.post("/{intake_id}/requested-documents/pfs", response_model=core.BucketFileRead)
async def broker_submit_lead_pfs(
    intake_id: UUID,
    payload: core.DealerPfsSubmission,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    """Dealer partner fills out the on-screen PFS on behalf of their client —
    same fallback available to the client themselves, usable here so a
    broker can close out the checklist without waiting on the client."""
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    return await core._submit_pfs_form(db, intake, payload, request, actor_name=user.name or "Dealer partner", actor_email=user.email)

@core.broker_router.post("/{intake_id}/requested-documents/debt-schedule", response_model=core.BucketFileRead)
async def broker_submit_lead_debt_schedule(
    intake_id: UUID,
    payload: core.DealerDebtScheduleSubmission,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    return await core._submit_debt_schedule_form(db, intake, payload, request, actor_name=user.name or "Dealer partner", actor_email=user.email)

@core.broker_router.post("/{intake_id}/run-review", response_model=core.ReviewRunStartResponse)
async def rerun_broker_dealer_lead_review(
    intake_id: UUID,
    request: Request,
    background: BackgroundTasks,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.ReviewRunStartResponse:
    """Same async run-review + polling pattern as the admin endpoint, scoped
    to the broker's own lead."""
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    core._throttle_or_429(
        core._ADMIN_REVIEW_LAST_BY_INTAKE,
        str(intake.id),
        core._ADMIN_REVIEW_MIN_INTERVAL_SECONDS,
        "A review was just re-run for this lead. Please wait a moment before running another.",
    )
    review = await core._create_queued_review(
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
    background.add_task(core._run_review_background, review.id, intake.id)
    return core.ReviewRunStartResponse(review_id=review.id, status="queued")

@core.broker_router.get("/{intake_id}/review-progress", response_model=core.ReviewProgressResponse)
async def broker_dealer_lead_review_progress(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
    review_id: UUID | None = None,
) -> core.ReviewProgressResponse:
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    if review_id is not None:
        review = await db.get(core.BucketAIReview, review_id)
        if review is None or review.bucket_id != intake.bucket_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    else:
        review = (
            await db.execute(
                select(core.BucketAIReview)
                .where(core.BucketAIReview.bucket_id == intake.bucket_id)
                .order_by(core.BucketAIReview.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if review is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No review found")
    progress = review.progress if isinstance(review.progress, dict) else {}
    return core.ReviewProgressResponse(
        review_id=review.id,
        status=review.status,
        stage=str(progress.get("stage") or review.status),
        label=str(progress.get("label") or ""),
        percent=int(progress.get("percent") or (100 if review.status in {"completed", "failed"} else 0)),
        files_total=int(progress.get("files_total") or 0),
        files_done=int(progress.get("files_done") or 0),
        error=review.error,
    )

@core.broker_router.post("/{intake_id}/request-pfs", response_model=core.BucketRequestedDocumentRead)
async def broker_request_lead_pfs(
    intake_id: UUID,
    payload: core.RequestPfsOrDebtScheduleRequest,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketRequestedDocument:
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    return await core._request_pfs(db, intake, payload.owner_name, actor=user)

@core.broker_router.post("/{intake_id}/request-debt-schedule", response_model=core.BucketRequestedDocumentRead)
async def broker_request_lead_debt_schedule(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketRequestedDocument:
    await core._require_dealer_partner(user, db)
    intake = await core._load_broker_dealer_lead(db, user, intake_id)
    return await core._request_debt_schedule(db, intake, actor=user)
