"""/admin/ai-underwriter-leads — route handlers of the AI intake.

Moved out of app/routers/dealer_ai_intake.py, which keeps every helper,
constant, router object and service import: thirty-one test files patch names
there, so those references go through `core` and resolve at call time, and the
old module still exposes these handlers by name. Importing this module is what
registers the routes.
"""

from __future__ import annotations

# ruff: noqa: B008, E501
import asyncio
import hashlib
import html
import logging
import secrets
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, with_loader_criteria

import app.routers.dealer_ai_intake as core

__all__ = [
    "list_dealer_ai_leads",
    "download_admin_dealer_intelligence_pdf",
    "get_whats_new",
    "mark_whats_new_seen",
    "list_dealer_partners",
    "assign_lead_partner",
    "get_intake_notification_routing",
    "update_intake_notification_routing",
    "admin_dealer_channel_inbox",
    "get_dealer_ai_lead",
    "admin_request_lead_deletion",
    "admin_cancel_lead_deletion",
    "admin_restore_lead",
    "apply_intake_legal_hold",
    "get_intake_retention_status",
    "release_intake_legal_hold",
    "admin_confirm_lead_deletion",
    "list_admin_lead_notes",
    "create_admin_lead_note",
    "update_lead_outcome_status",
    "update_lead_language",
    "update_lead_contact",
    "create_admin_ai_lead",
    "admin_mark_channel_seen",
    "create_admin_ai_lead_from_bucket",
    "request_lead_credit_authorization",
    "admin_request_lead_pfs",
    "admin_request_lead_debt_schedule",
    "request_lead_contract",
    "get_lead_contract_status",
    "get_lead_credit_status",
    "get_lead_program_fit",
    "get_lead_dscr_potential",
    "run_lead_credit_pull",
    "prepare_banker_submission",
    "rerun_dealer_ai_lead_review",
    "dealer_ai_lead_review_progress",
    "dealer_ai_lead_chat",
    "get_dealer_ai_client_thread",
    "reply_dealer_ai_client_thread",
    "retry_dealer_ai_client_thread_sms",
    "resume_dealer_ai_client_thread",
    "dealer_ai_lead_upload_init",
    "dealer_ai_lead_upload_complete",
    "admin_submit_lead_pfs",
    "admin_submit_lead_debt_schedule",
    "dealer_ai_lead_ingest_from_drive",
    "create_dealer_ai_executive_summary",
    "create_dealer_ai_prequalification",
    "create_dealer_ai_lender_packet",
    "generate_package_readiness_documents",
    "download_dealer_ai_package_zip",
    "preview_dealer_ai_vendor_email",
    "send_dealer_ai_vendor_email"
]


@core.admin_router.get("", response_model=core.DealerAILeadListResponse)
async def list_dealer_ai_leads(
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
    q: str | None = None,
    status_filter: str | None = None,
    probability_status: str | None = None,
    variant_filter: str | None = None,
    archived_filter: Literal["active", "archived", "all"] = "active",
    partner_user_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> core.DealerAILeadListResponse:
    core._require_super_admin(user)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    stmt = (
        select(core.PublicUnderwritingIntake)
        .join(core.Bucket, core.PublicUnderwritingIntake.bucket_id == core.Bucket.id)
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
        stmt = stmt.where(
            core.Bucket.archived_at.is_(None),
            ~closed_lifecycle,
        )
    elif archived_filter == "archived":
        # "Archived" is the operator's historical-work queue.  It includes
        # explicit bucket archives and lifecycle-closed files, but only the
        # former receives a Restore action.
        stmt = stmt.where(
            or_(core.Bucket.archived_at.is_not(None), closed_lifecycle)
        )
    if status_filter and status_filter != "all":
        stmt = stmt.where(core._intake_status_filter_clause(status_filter))
    if partner_user_id is not None:
        stmt = stmt.where(core.PublicUnderwritingIntake.broker_id == partner_user_id)
    if variant_filter and variant_filter != "all":
        if variant_filter == "dealer":
            stmt = stmt.where(core.PublicUnderwritingIntake.variant == core.DEALER_VARIANT)
        elif variant_filter == "real_estate":
            stmt = stmt.where(core.PublicUnderwritingIntake.variant == core.FUNDING_VARIANT)
        else:
            stmt = stmt.where(core.PublicUnderwritingIntake.variant == variant_filter)
    if q and q.strip():
        stmt = stmt.where(core._intake_search_clause(q))
    rows = list((await db.execute(stmt)).scalars().unique().all())
    if probability_status and probability_status != "all":
        rows = [
            row for row in rows
            if str(core._lead_result(row).get("probability_status") or "") == probability_status
        ]
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
    # NEW badges: unseen client/broker activity per lead for THIS admin,
    # against their per-lead seen cursors (default window: last 7 days for
    # leads never opened).
    from app.models.admin_activity import AdminActivitySeen
    from app.services.admin_activity import unseen_counts_by_bucket

    seen_rows = (
        await db.execute(
            select(AdminActivitySeen).where(
                AdminActivitySeen.user_id == user.id,
                AdminActivitySeen.intake_id.in_([row.id for row in page] or [UUID(int=0)]),
            )
        )
    ).scalars().all()
    seen_by_intake = {row.intake_id: row.seen_at for row in seen_rows if row.intake_id}
    counts = await unseen_counts_by_bucket(
        db,
        seen_by_intake=seen_by_intake,
        bucket_to_intake={row.bucket_id: row.id for row in page},
        default_since=core._now() - timedelta(days=7),
    )
    channel_unread = await core._channel_unread_by_intake(db, user=user, intakes=page)
    for item in items:
        item.unseen_activity_count = counts.get(item.bucket_id, 0)
        item.channel_unread_count = channel_unread.get(item.id, 0)
    return core.DealerAILeadListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )

@core.admin_router.get("/{intake_id}/intelligence.pdf")
async def download_admin_dealer_intelligence_pdf(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> Response:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
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
    is_re = intake.variant == core.FUNDING_VARIANT
    if is_re:
        await core._refresh_dscr_pricing(db)
    pdf_bytes = await asyncio.to_thread(
        core.render_dealer_intelligence_pdf,
        intake=intake,
        files=files,
        missing_docs=missing_docs,
        result=latest_result,
        # Internal-only sections — never passed on the public/borrower exports.
        program_fit=(
            None
            if is_re
            else core._compute_main_street_fit(intake)
            if intake.variant == core.MAIN_STREET_VARIANT
            else core._compute_loan_program_fit(intake)
        ),
        dscr_potential=core._compute_dscr_potential(intake) if is_re else None,
        credit=core._credit_pull_state(intake) or None,
        internal=True,
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

@core.admin_router.get("/whats-new", response_model=core.WhatsNewResponse)
async def get_whats_new(
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.WhatsNewResponse:
    """Recent client/broker platform activity (uploads, messages, forms,
    bookings, new intakes) across all leads, with this admin's unseen count
    against their feed cursor."""
    core._require_super_admin(user)
    from app.models.admin_activity import AdminActivitySeen
    from app.services.admin_activity import client_activity_rows

    since = core._now() - timedelta(days=7)
    items = await client_activity_rows(db, since=since, limit=60)
    cursor = (
        await db.execute(
            select(AdminActivitySeen).where(
                AdminActivitySeen.user_id == user.id,
                AdminActivitySeen.intake_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    feed_seen_at = cursor.seen_at if cursor else None
    unseen = sum(1 for item in items if feed_seen_at is None or item["created_at"] > feed_seen_at)
    return core.WhatsNewResponse(
        items=[core.WhatsNewItem(**{key: value for key, value in item.items() if key != "bucket_id"}) for item in items],
        unseen_count=unseen,
        feed_seen_at=feed_seen_at,
    )

@core.admin_router.post("/whats-new/seen", response_model=core.WhatsNewResponse)
async def mark_whats_new_seen(
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.WhatsNewResponse:
    """Advance this admin's feed cursor to now (Mark all seen)."""
    core._require_super_admin(user)
    from app.models.admin_activity import AdminActivitySeen

    cursor = (
        await db.execute(
            select(AdminActivitySeen).where(
                AdminActivitySeen.user_id == user.id,
                AdminActivitySeen.intake_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if cursor is None:
        db.add(AdminActivitySeen(user_id=user.id, intake_id=None, seen_at=core._now()))
    else:
        cursor.seen_at = core._now()
    await db.commit()
    return await core.get_whats_new(user, db)

@core.admin_router.get("/dealer-partners", response_model=list[core.DealerPartnerOption])
async def list_dealer_partners(
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> list[core.DealerPartnerOption]:
    """Dealer partners the team can assign a file to / start a conversation with.
    Registered before GET /{intake_id} so the literal path isn't captured."""
    core._require_super_admin(user)
    rows = (
        await db.execute(
            select(core.User)
            .where(
                core.User.role == core.Role.DEALER_PARTNER,
                core.User.deleted_at.is_(None),
                core.User.account_status == "active",
            )
            .order_by(core.User.name)
        )
    ).scalars().all()
    return [core.DealerPartnerOption(id=r.id, name=r.name or r.email, email=r.email) for r in rows]

@core.admin_router.patch("/{intake_id}/assign-partner", response_model=core.DealerIntakeResponse)
async def assign_lead_partner(
    intake_id: UUID,
    payload: core.AssignPartnerRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Assign (or clear) the dealer partner on a file so the team's messages on
    it reach that partner's channel. Setting None detaches the partner."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    if payload.broker_user_id is not None and intake.variant != core.DEALER_VARIANT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only auto-industry dealer intake files can be assigned to an Auto Dealer Agent.",
        )
    if payload.broker_user_id is None:
        intake.broker_id = None
    else:
        partner = await core._load_dealer_partner_user(db, payload.broker_user_id)
        intake.broker_id = partner.id
    # The file's agent seat follows the partner (services/file_team).
    _profile = await core.profiles_service.find_profile(db, intake_id=intake.id)
    if _profile is not None:
        from app.services import file_team as _file_team

        await _file_team.refresh_agent_seat(db, _profile, actor=user)
    await core._log(
        db, intake.bucket_id, "dealer_ai_lead_partner_assigned", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id),
        detail=str(payload.broker_user_id) if payload.broker_user_id else "detached",
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake.id)
    return await core._response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)

@core.admin_router.get(
    "/{intake_id}/notification-routing",
    response_model=core.IntakeNotificationRoutingRead,
)
async def get_intake_notification_routing(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.IntakeNotificationRoutingRead:
    core._require_intake_operator(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    candidates = await core._intake_notification_candidates(db, intake)
    return core._notification_routing_read(intake, candidates)

@core.admin_router.put(
    "/{intake_id}/notification-routing",
    response_model=core.IntakeNotificationRoutingRead,
)
async def update_intake_notification_routing(
    intake_id: UUID,
    payload: core.IntakeNotificationRoutingUpdate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.IntakeNotificationRoutingRead:
    core._require_intake_operator(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    # Serialize routing edits with relationship/assignment changes. Candidate
    # validation and persistence must describe the same intake version.
    intake = (
        await db.execute(
            select(core.PublicUnderwritingIntake)
            .where(core.PublicUnderwritingIntake.id == intake.id)
            .with_for_update()
        )
    ).scalar_one()
    await db.refresh(
        intake,
        attribute_names=[
            "intake_state",
            "assigned_underwriter_user_id",
            "source_user_id",
            "broker_id",
            "referral_partner_company_id",
        ],
    )
    candidates = await core._intake_notification_candidates(db, intake)
    eligible_ids = {candidate.user_id for candidate in candidates}
    for event_key, rule in payload.events.items():
        if rule.enabled and (rule.email_enabled or rule.in_app_enabled) and not (
            rule.to_user_ids or rule.cc_user_ids
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Choose at least one recipient for {core.INTAKE_NOTIFICATION_EVENT_DEFINITIONS[event_key][0]}.",
            )
        unauthorized = (set(rule.to_user_ids) | set(rule.cc_user_ids)) - eligible_ids
        if unauthorized:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                (
                    f"{core.INTAKE_NOTIFICATION_EVENT_DEFINITIONS[event_key][0]} contains "
                    "a recipient who is no longer involved in or authorized for this file."
                ),
            )

    now = core._now()
    state = core._intake_state(intake)
    state[core.INTAKE_NOTIFICATION_ROUTING_STATE_KEY] = {
        "version": core.INTAKE_NOTIFICATION_ROUTING_VERSION,
        "events": {
            event_key: payload.events[event_key].model_dump(mode="json")
            for event_key in core.INTAKE_NOTIFICATION_EVENT_DEFINITIONS
        },
        "updated_at": now.isoformat(),
        "updated_by_user_id": str(user.id),
    }
    intake.intake_state = state
    await core._log(
        db,
        intake.bucket_id,
        "dealer_ai_notification_routing_updated",
        request=request,
        user=user,
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail="Per-event AI Intake notification recipients and CC routing updated",
    )
    await db.commit()
    return core._notification_routing_read(intake, candidates)

@core.admin_router.get("/messages", response_model=core.DealerLeadInboxResponse)
async def admin_dealer_channel_inbox(
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerLeadInboxResponse:
    """Internal team's dealer-lead message inbox across every lead. Registered
    before GET /{intake_id} so the literal path isn't captured as an intake_id."""
    core._require_super_admin(user)
    stmt = (
        select(core.PublicUnderwritingIntake)
        .join(core.Bucket, core.PublicUnderwritingIntake.bucket_id == core.Bucket.id)
        .where(core.Bucket.archived_at.is_(None))
        .options(selectinload(core.PublicUnderwritingIntake.bucket).selectinload(core.Bucket.notes))
    )
    intakes = list((await db.execute(stmt)).scalars().unique().all())
    return await core._build_channel_inbox(db, user, intakes)

@core.admin_router.get("/{intake_id}", response_model=core.DealerIntakeResponse)
async def get_dealer_ai_lead(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    await core._mark_lead_seen(db, user, intake.id)
    return await core._response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)

@core.admin_router.post("/{intake_id}/request-deletion", response_model=core.DealerIntakeResponse)
async def admin_request_lead_deletion(
    intake_id: UUID,
    payload: core.RequestLeadDeletionRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Admin flags a lead for archival review.

    This is the same non-destructive request flag available to a broker and is
    needed for admin-created/self-serve leads. It only enables the separate
    archive confirmation endpoint; evidence is never deleted.
    """
    core._require_intake_operator(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    intake.delete_requested_at = core._now()
    intake.delete_requested_by_user_id = user.id
    await core._log(
        db, intake.bucket_id, "dealer_ai_lead_deletion_requested_by_admin", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id), detail=payload.reason,
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake.id)
    return await core._response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)

@core.admin_router.post("/{intake_id}/cancel-deletion-request", response_model=core.DealerIntakeResponse)
async def admin_cancel_lead_deletion(
    intake_id: UUID,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Admin retracts a pending deletion request (their own, or a broker's)
    without destroying anything — fully reversible."""
    core._require_intake_operator(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    intake.delete_requested_at = None
    intake.delete_requested_by_user_id = None
    await core._log(
        db, intake.bucket_id, "dealer_ai_lead_deletion_request_cancelled_by_admin", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id),
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake.id)
    return await core._response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)

@core.admin_router.post("/{intake_id}/restore", response_model=core.DealerAILeadRow)
async def admin_restore_lead(
    intake_id: UUID,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerAILeadRow:
    """Restore a soft-archived AI Intake and its existing bucket/files.

    This is intentionally separate from hard-delete recovery: archived rows
    and their objects still exist and are safe to restore in place.
    """
    core._require_intake_operator(user)
    intake = (
        await db.execute(
            select(core.PublicUnderwritingIntake)
            .where(core.PublicUnderwritingIntake.id == intake_id)
            .options(
                selectinload(core.PublicUnderwritingIntake.bucket).selectinload(core.Bucket.requested_documents).selectinload(core.BucketRequestedDocument.template_file),
                selectinload(core.PublicUnderwritingIntake.bucket).selectinload(core.Bucket.files),
                selectinload(core.PublicUnderwritingIntake.latest_review),
                selectinload(core.PublicUnderwritingIntake.broker),
                selectinload(core.PublicUnderwritingIntake.delete_requested_by),
                with_loader_criteria(core.BucketFile, core.BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if intake is None or intake.bucket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI lead not found")
    profile_hold = intake.id in (
        await core._profile_hold_intake_ids(db, [intake.id])
    )
    if intake.bucket.archived_at is None:
        underwriting_by_intake = await core._underwriting_statuses_by_intake(
            db, [intake.id]
        )
        return core._lead_row(
            intake,
            underwriting_status=underwriting_by_intake.get(intake.id),
            effective_profile_hold=profile_hold,
        )

    await core._verify_files_before_restore(
        [
            file
            for file in intake.bucket.files
            if file.status == "uploaded" and file.deleted_at is None
        ]
    )

    state = dict(intake.intake_state or {})
    archive_meta = state.pop("_archive", {})
    prior_status = archive_meta.get("bucket_status") if isinstance(archive_meta, dict) else None
    intake.intake_state = state
    intake.archived_at = None
    intake.archived_by_user_id = None
    intake.bucket.archived_at = None
    intake.bucket.status = (
        prior_status
        if isinstance(prior_status, str) and prior_status and prior_status != "archived"
        else "collecting_documents"
    )
    await core._log(
        db,
        intake.bucket_id,
        "dealer_ai_lead_restored",
        request=request,
        user=user,
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=intake.business_name or intake.full_name,
    )
    await db.commit()
    underwriting_by_intake = await core._underwriting_statuses_by_intake(db, [intake.id])
    return core._lead_row(
        intake,
        underwriting_status=underwriting_by_intake.get(intake.id),
        effective_profile_hold=profile_hold,
    )

@core.admin_router.post(
    "/{intake_id}/legal-hold",
    response_model=core.RetentionStatusRead,
)
async def apply_intake_legal_hold(
    intake_id: UUID,
    payload: core.LegalHoldChange,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.RetentionStatusRead:
    core._require_governance_admin(user)
    intake = await db.get(core.PublicUnderwritingIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI Intake file not found")
    already_held = intake.legal_hold
    await core.retention_holds.set_legal_hold(
        db,
        intake,
        core.PublicUnderwritingIntake,
        enabled=True,
    )
    await core._log(
        db,
        intake.bucket_id,
        "retention_legal_hold_applied",
        request=request,
        user=user,
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=("Already held; " if already_held else "") + payload.reason.strip(),
    )
    await db.commit()
    await db.refresh(intake)
    return core.RetentionStatusRead(
        **await core.retention_holds.effective_status_read(
            db,
            intake,
            resource_type="ai_intake",
        )
    )

@core.admin_router.get(
    "/{intake_id}/retention",
    response_model=core.RetentionStatusRead,
)
async def get_intake_retention_status(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.RetentionStatusRead:
    """Reload the intake's effective, graph-wide retention status."""

    core._require_governance_admin(user)
    intake = await db.get(core.PublicUnderwritingIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI Intake file not found")
    return core.RetentionStatusRead(
        **await core.retention_holds.effective_status_read(
            db,
            intake,
            resource_type="ai_intake",
        )
    )

@core.admin_router.post(
    "/{intake_id}/legal-hold/release",
    response_model=core.RetentionStatusRead,
)
async def release_intake_legal_hold(
    intake_id: UUID,
    payload: core.LegalHoldChange,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.RetentionStatusRead:
    core._require_governance_admin(user)
    intake = await db.get(core.PublicUnderwritingIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI Intake file not found")
    was_held = intake.legal_hold
    await core.retention_holds.set_legal_hold(
        db,
        intake,
        core.PublicUnderwritingIntake,
        enabled=False,
    )
    await core._log(
        db,
        intake.bucket_id,
        "retention_legal_hold_released",
        request=request,
        user=user,
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=("Was not held; " if not was_held else "") + payload.reason.strip(),
    )
    await db.commit()
    await db.refresh(intake)
    return core.RetentionStatusRead(
        **await core.retention_holds.effective_status_read(
            db,
            intake,
            resource_type="ai_intake",
        )
    )

@core.admin_router.post("/{intake_id}/confirm-deletion", status_code=status.HTTP_204_NO_CONTENT)
async def admin_confirm_lead_deletion(
    intake_id: UUID,
    payload: core.ConfirmLeadDeletionRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> None:
    """Compatibility endpoint for removing a file from active work.

    The operation is intentionally a reversible archive. It never deletes S3
    objects or bucket-scoped underwriting history. The exact-name check still
    protects against archiving the wrong file, and the record remains visible
    through the archived filter and restore endpoint.
    """
    core._require_intake_operator(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    confirm_target = " ".join((intake.business_name or intake.full_name or "").split())
    if payload.confirm_name.casefold() != confirm_target.casefold():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Confirmation name does not match this AI Intake file",
        )
    archived_at = core._now()
    state = dict(intake.intake_state or {})
    state["_archive"] = {
        "bucket_status": intake.bucket.status,
        "archived_at": archived_at.isoformat(),
        "archived_by_user_id": str(user.id),
    }
    intake.intake_state = state
    intake.archived_at = archived_at
    intake.archived_by_user_id = user.id
    intake.bucket.archived_at = archived_at
    intake.bucket.status = "archived"
    revoked_access_count = await core.bucket_archive.revoke_external_access(
        db,
        intake.bucket_id,
    )
    await core.intake_retention.extend_intake_retention(
        db,
        intake,
        anchor=archived_at,
    )

    archive_event = core.AIAuditEvent(
        event_type="intake_archived",
        actor_type="user",
        actor_id=user.id,
        client_id=intake.client_id,
        loan_id=(
            await db.execute(
                select(core.ApplicationProfile.loan_id).where(
                    core.ApplicationProfile.intake_id == intake.id
                )
            )
        ).scalar_one_or_none(),
        payload={
            "intake_id": str(intake.id),
            "bucket_id": str(intake.bucket_id),
            "variant": intake.variant,
            "business_name": intake.business_name,
            "full_name": intake.full_name,
            "email": intake.email,
            "phone": intake.phone,
            "status": intake.status,
            "confirmed_name": payload.confirm_name,
            "requested_by_user_id": str(intake.delete_requested_by_user_id) if intake.delete_requested_by_user_id else None,
            "requested_at": intake.delete_requested_at.isoformat() if intake.delete_requested_at else None,
            "actor_email": user.email,
            "ip": core._client_ip(request),
            "files": [
                {"id": str(file.id), "name": file.file_name, "size_bytes": file.size_bytes}
                for file in intake.bucket.files
            ],
        },
    )
    intake.delete_requested_at = None
    intake.delete_requested_by_user_id = None
    db.add(archive_event)
    await core._log(
        db,
        intake.bucket_id,
        "dealer_ai_lead_archived",
        request=request,
        user=user,
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=(
            f"{confirm_target} | external access revoked={revoked_access_count}"
        ),
    )
    await db.commit()

@core.admin_router.get("/{intake_id}/notes", response_model=list[core.BucketNoteRead])
async def list_admin_lead_notes(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> list[core.BucketNoteRead]:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
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

@core.admin_router.post("/{intake_id}/notes", response_model=core.BucketNoteRead, status_code=status.HTTP_201_CREATED)
async def create_admin_lead_note(
    intake_id: UUID,
    payload: core.DealerLeadNoteCreate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketNoteRead:
    """Internal note on the shared admin <-> dealer-partner thread for this
    lead. visibility="admin" keeps it invisible to the client and to any
    bucket vendor/share viewer — confirmed no other surface reads visibility
    == "admin" notes."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
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
        title=f"Note to the partner channel from {user.name or user.email}",
        actor=user,
        target_type="note",
        target_id=note.id,
    )
    await db.commit()
    await db.refresh(note)
    return core.BucketNoteRead.model_validate(note).model_copy(
        update={"images": [core.inline_images.serialize(image) for image in attached]}
    )

@core.admin_router.patch("/{intake_id}/outcome-status", response_model=core.DealerIntakeResponse)
async def update_lead_outcome_status(
    intake_id: UUID,
    payload: core.OutcomeStatusUpdate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Admin-only: sets the firm's loan decision on this lead (submitted /
    closed / denied). No broker-accessible write path exists to this field —
    the loan outcome is the firm's call, not the referring partner's."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    previous_outcome = intake.outcome_status
    intake.outcome_status = payload.outcome_status
    if payload.outcome_status in {"closed", "denied"}:
        await core.intake_retention.extend_intake_retention(
            db,
            intake,
            anchor=core._now(),
        )
    await core._log(
        db, intake.bucket_id, "dealer_ai_lead_outcome_status_changed", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id), detail=payload.outcome_status,
    )
    if payload.outcome_status != previous_outcome:
        await core.file_events.emit(
            db,
            intake_id=intake.id,
            kind="status.changed",
            visibility=core.file_events.VISIBILITY_CLIENT,
            title=core._OUTCOME_EVENT_TITLES.get(payload.outcome_status, f"File {payload.outcome_status}"),
            actor=user,
            target_type="public_underwriting_intake",
            target_id=intake.id,
            meta={"from": previous_outcome, "to": payload.outcome_status},
        )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake.id)
    return await core._response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)

@core.admin_router.patch("/{intake_id}/language", response_model=core.DealerIntakeResponse)
async def update_lead_language(
    intake_id: UUID,
    payload: core.LanguageUpdate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Admin-only: corrects the client's language preference after the fact.
    Mirrors update_lead_outcome_status — once set (by the client's own pick,
    or an admin/broker's pick at lead-creation time), only an admin can
    change it. No broker-accessible write path exists to this field."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    intake.preferred_language = payload.preferred_language
    linked_profile = (
        await db.execute(
            select(core.ApplicationProfile)
            .where(core.ApplicationProfile.intake_id == intake.id)
            .order_by(
                core.ApplicationProfile.updated_at.desc(),
                core.ApplicationProfile.created_at.desc(),
                core.ApplicationProfile.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if linked_profile is not None:
        linked_profile.communication_locale = payload.preferred_language.value
        linked_profile.communication_locale_source = "staff_selection"
        linked_profile.communication_locale_updated_at = core._now()
        linked_profile.communication_locale_updated_by_user_id = user.id
    await core._log(
        db, intake.bucket_id, "dealer_ai_lead_language_changed", request=request, user=user,
        target_type="public_underwriting_intake", target_id=str(intake.id), detail=payload.preferred_language,
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake.id)
    return await core._response(db, intake, token=None, include_management=True, admin_thread=True, thread_user=user)

@core.admin_router.patch("/{intake_id}/contact", response_model=core.DealerIntakeResponse)
async def update_lead_contact(
    intake_id: UUID,
    payload: core.AdminLeadContactUpdate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Admin-only correction of the person, entity, and request summary.

    These fields belong to the intake. The linked Client record is not changed
    because one client can own multiple files with different entity contacts.
    """
    core._require_governance_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    values = payload.model_dump(exclude_unset=True)
    if "full_name" in values and values["full_name"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Principal name is required")
    if "email" in values and values["email"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Email is required")
    for field, value in values.items():
        setattr(intake, field, str(value) if field == "email" and value is not None else value)
    if {"business_name", "full_name"}.intersection(values):
        bucket = await db.get(core.Bucket, intake.bucket_id)
        if bucket is not None and bucket.name_sync_mode == "linked":
            linked_name = (intake.business_name or intake.full_name or "Application").strip()[:180]
            bucket.name = linked_name
            bucket.client_name = linked_name
    changed = ", ".join(sorted(values)) or "No fields changed"
    await core._log(
        db,
        intake.bucket_id,
        "dealer_ai_lead_contact_updated",
        request=request,
        user=user,
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Updated fields: {changed}",
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake.id)
    return await core._response(
        db, intake, token=None, include_management=True, admin_thread=True, thread_user=user
    )

@core.admin_router.post("", response_model=core.DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_ai_lead(
    payload: core.AdminLeadCreate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Super-admin creates an AI-underwriter lead on behalf of a client."""
    core._require_governance_admin(user)
    return await core._create_admin_ai_lead_core(
        payload,
        request=request,
        user=user,
        db=db,
    )

@core.admin_router.post("/{intake_id}/messages/seen", response_model=core.ChannelSeenResponse)
async def admin_mark_channel_seen(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.ChannelSeenResponse:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    seen_at = await core._mark_channel_seen(db, user, intake.id)
    return core.ChannelSeenResponse(intake_id=intake.id, seen_at=seen_at)

@core.admin_router.post("/from-bucket/{bucket_id}", response_model=core.DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_ai_lead_from_bucket(
    bucket_id: UUID,
    payload: core.AdminLeadFromBucketCreate,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Convert an EXISTING Bucket (already collecting files some other way) into an
    AI-underwriter lead, so the admin can audit those files with the AI review and
    build a lender package — without a second, empty bucket being created and
    without re-uploading anything. Reuses the same client find-or-create + intake
    construction as create_admin_ai_lead, but skips _create_bucket_for_intake /
    _create_bucket_for_funding_review entirely: the bucket, its BucketFile rows,
    and any BucketRequestedDocument checklist it already has stay exactly as-is.
    File association is automatic (_active_files just filters bucket.files)."""
    core._require_super_admin(user)
    if payload.variant not in core._ADMIN_VARIANT_CONSTANTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"variant must be one of {', '.join(sorted(core._ADMIN_VARIANT_CONSTANTS))}",
        )
    is_re = payload.variant == "real_estate"
    is_ms = payload.variant == "main_street"
    variant_const = core._ADMIN_VARIANT_CONSTANTS[payload.variant]
    # Normalized, never rejected: an unrecognized value becomes "other"/"not_sure"
    # rather than failing a conversion the operator is part-way through. The
    # bucket already exists here, so unlike the create path these only steer the
    # AI context and the program screen, not a document package.
    ms_intent = core.normalize_intent(payload.intent) if is_ms else None
    ms_industry = core.normalize_industry(payload.industry) if is_ms else None

    bucket = (
        await db.execute(
            select(core.Bucket)
            .where(core.Bucket.id == bucket_id)
            .options(selectinload(core.Bucket.upload_links))
        )
    ).scalar_one_or_none()
    if bucket is None or bucket.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket not found")

    # Duplicate policy: unless force_new, surface the existing lead for this bucket
    # rather than creating a second intake on top of the same files.
    if not payload.force_new:
        existing = (
            await db.execute(
                select(core.PublicUnderwritingIntake).where(core.PublicUnderwritingIntake.bucket_id == bucket_id)
            )
        ).scalars().first()
        if existing is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "message": "An AI-underwriter lead already exists for this bucket.",
                    "intake_id": str(existing.id),
                },
            )

    provenance = {
        "created_by_admin": {
            "user_id": str(user.id),
            "name": user.name,
            "email": user.email,
            "at": core._now().isoformat(),
        },
        "on_behalf_of_client": True,
        "converted_from_bucket_id": str(bucket.id),
    }

    if is_re:
        adapter = core.FundingReviewStart(
            full_name=payload.full_name, email=payload.email, phone=payload.phone,
            investor_name=payload.business_name,
        )
        client = await core._find_or_create_funding_client(db, adapter)
    else:
        adapter = core.DealerIntakeStart(
            full_name=payload.full_name, email=payload.email, phone=payload.phone,
            business_name=payload.business_name,
        )
        client = await core._find_or_create_client(db, adapter)

    if isinstance(client.lead_intake, dict):
        client.lead_intake = {**client.lead_intake, "created_by_admin": str(user.id)}

    # Reuse the bucket's existing active upload link (if any) rather than minting a
    # new one — bucket_upload_link_id is nullable, so a bucket with no client-facing
    # link at all (e.g. a purely admin-populated audit bucket) is fine too.
    link = next(
        (upload_link for upload_link in bucket.upload_links if upload_link.status == "active"),
        None,
    )

    token = core._new_public_token()
    intake = core.PublicUnderwritingIntake(
        source_kind="bucket_conversion",
        source_actor_name=(user.name or user.email or "")[:200],
        source_user_id=user.id,
        source_detail=f"From document room {bucket.id}"[:200],
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id if link else None,
        token_hash=core._hash_token(token),
        variant=variant_const,
        full_name=payload.full_name.strip(),
        email=client.email or core._normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        preferred_language=payload.preferred_language,
        intake_state={
            "messages": [],
            "source": "bucket_conversion",
            "admin_provenance": provenance,
            **(
                {"main_street_details": {"intent": ms_intent, "industry": ms_industry}}
                if is_ms
                else {}
            ),
        },
    )
    db.add(intake)
    await db.flush()

    # Warm the per-file analysis cache cheaply (placeholder inserts, no model calls)
    # so the scheduler drain starts analyzing before the admin even opens the lead.
    from app.services.bucket_ai import enqueue_file_analysis

    for file in core._active_files(bucket):
        await enqueue_file_analysis(db, file)

    await core._log(
        db,
        bucket.id,
        "ai_lead_created_from_bucket",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Admin converted bucket '{bucket.name}' into a {payload.variant} lead for {intake.email}",
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake.id)

    email_note = ""
    # Main Street has no public client room yet — dealer, real estate and MCA each
    # have a bespoke intake app and nothing equivalent exists for operating
    # businesses. Emailing the client the dealer room would drop them into a
    # floorplan questionnaire, so the link is withheld and the operator is told
    # why. The lead itself is fully workable from the dashboard.
    if payload.notify_client and is_ms:
        email_note = (
            " No login link was sent: operating-business leads have no client-facing"
            " room yet, so work this file from the dashboard."
        )
    elif payload.notify_client:
        if is_re:
            record = await core._record_resume_email(
                intake, token=token, request=request, reason="admin_created",
                public_path=core.FUNDING_PUBLIC_PATH, review_label="real estate funding review",
                room_label="real estate funding review file", db=db, sender_user_id=user.id,
            )
        else:
            record = await core._record_resume_email(
                intake, token=token, request=request, reason="admin_created", db=db, sender_user_id=user.id,
            )
        await db.commit()
        intake = await core._load_admin_dealer_lead(db, intake.id)
        email_note = (
            " A secure login link was emailed to the client."
            if record.get("ok")
            else " Email delivery is unavailable; share the resume link manually."
        )

    return await core._response(
        db,
        intake,
        token=token,
        # None for Main Street: it has no client room yet, and the dealer room
        # would ask an operating business floorplan and collateral questions.
        public_path=(
            core.FUNDING_PUBLIC_PATH if is_re
            else None if is_ms
            else core.MCA_PUBLIC_PATH if payload.variant == "mca_refinance"
            else "/dealer-ai-underwriter"
        ),
        include_management=True,
        admin_thread=True,
        thread_user=user,
        assistant_message=(
            "Lead created from the existing bucket — its files are already attached. "
            "Run the AI review when ready." + email_note
        ),
    )

@core.admin_router.post("/{intake_id}/credit-authorization", response_model=core.BucketRequestedDocumentRead)
async def request_lead_credit_authorization(
    intake_id: UUID,
    payload: core.AdminCreditAuthorizationRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketRequestedDocument:
    """Admin requests the client sign a credit-authorization form on this
    lead — identical code path for dealer and real-estate leads. Idempotent:
    if one is already requested on this bucket, returns it (updating the
    template/text in place if the client hasn't signed yet — once signed,
    the request is immutable so an in-flight signature can't be silently
    invalidated by a wording change) rather than creating a duplicate
    checklist item."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)

    if payload.template_file_id is not None:
        template_file = await db.get(core.BucketFile, payload.template_file_id)
        if template_file is None or template_file.bucket_id != intake.bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Template file does not belong to this lead's bucket")

    existing = core._credit_authorization_doc(intake)
    if existing is not None:
        if existing.status != "uploaded" and (payload.template_file_id is not None or payload.document_text is not None):
            existing.template_file_id = payload.template_file_id
            existing.signature_document_text = payload.document_text
            await db.commit()
            await db.refresh(existing)
        return existing

    doc = core.BucketRequestedDocument(
        bucket_id=intake.bucket_id,
        name=core._CREDIT_AUTH_DOC_NAME,
        category="compliance",
        description="Sign to authorize a soft credit inquiry for this file.",
        required=True,
        is_custom=True,
        requires_signature=True,
        signature_kind="credit_authorization",
        template_file_id=payload.template_file_id,
        signature_document_text=payload.document_text,
    )
    db.add(doc)
    await core._log(
        db,
        intake.bucket_id,
        "credit_authorization_requested",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Admin requested credit authorization for {intake.email}",
    )
    await db.commit()
    await db.refresh(doc)
    return doc

@core.admin_router.post("/{intake_id}/request-pfs", response_model=core.BucketRequestedDocumentRead)
async def admin_request_lead_pfs(
    intake_id: UUID,
    payload: core.RequestPfsOrDebtScheduleRequest,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketRequestedDocument:
    """Admin requests a PFS on this lead — either the baseline PFS (already
    auto-created for most leads) or a second/later owner's PFS. Unlike
    credit-authorization, there is no signature/template step: this just
    ensures the requested-document exists so the client can upload a real
    file or use the on-screen fill-in-online form. Dealer leads only —
    PFS/debt-schedule are not a real-estate concept on this platform."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    core._require_dealer_intake(intake)
    return await core._request_pfs(db, intake, payload.owner_name, actor=user)

@core.admin_router.post("/{intake_id}/request-debt-schedule", response_model=core.BucketRequestedDocumentRead)
async def admin_request_lead_debt_schedule(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketRequestedDocument:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    core._require_dealer_intake(intake)
    return await core._request_debt_schedule(db, intake, actor=user)

@core.admin_router.post("/{intake_id}/contracts", response_model=core.BucketRequestedDocumentRead)
async def request_lead_contract(
    intake_id: UUID,
    payload: core.AdminContractRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketRequestedDocument:
    """Admin requests one of the 3 client-facing contract types be signed on
    this lead, via the existing requested-document/chat sign flow — see
    app/routers/contracts.py's module docstring for why PLATFORM_ACCESS and
    REFERRAL_PROTECTION are NOT routed here. Idempotent per contract_type,
    same immutable-once-signed semantics as credit-authorization."""
    core._require_super_admin(user)
    if payload.contract_type not in core._CONTRACT_SIGNATURE_KIND:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This contract type is not requestable on a lead")
    intake = await core._load_admin_dealer_lead(db, intake_id)

    from app.services import contract_templates as tpl

    field_values = {**core._contract_field_values_from_lead(intake, payload.contract_type), **payload.field_values}
    rendered = tpl.render_contract_document(payload.contract_type, field_values)
    document_text = rendered.plain_text

    existing = core._contract_doc(intake, payload.contract_type)
    if existing is not None:
        if existing.status != "uploaded":
            existing.signature_document_text = document_text
            await db.commit()
            await db.refresh(existing)
        return existing

    doc = core.BucketRequestedDocument(
        bucket_id=intake.bucket_id,
        name=core._CONTRACT_DOC_NAME[payload.contract_type],
        category="compliance",
        description="Sign to execute this engagement agreement.",
        required=True,
        is_custom=True,
        requires_signature=True,
        signature_kind=core._CONTRACT_SIGNATURE_KIND[payload.contract_type],
        signature_document_text=document_text,
    )
    db.add(doc)
    await core._log(
        db,
        intake.bucket_id,
        "contract_requested",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"Admin requested {payload.contract_type.value} for {intake.email}",
    )
    await db.commit()
    await db.refresh(doc)
    return doc

@core.admin_router.get("/{intake_id}/contracts", response_model=list[core.AdminContractRequestStatus])
async def get_lead_contract_status(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> list[core.AdminContractRequestStatus]:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    results: list[core.AdminContractRequestStatus] = []
    for contract_type in core._CONTRACT_SIGNATURE_KIND:
        doc = core._contract_doc(intake, contract_type)
        results.append(
            core.AdminContractRequestStatus(
                contract_type=contract_type,
                requested=doc is not None,
                signed=bool(doc and doc.status == "uploaded"),
                requested_document_id=doc.id if doc else None,
            )
        )
    return results

@core.admin_router.get("/{intake_id}/credit-status", response_model=core.LeadCreditStatusResponse)
async def get_lead_credit_status(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.LeadCreditStatusResponse:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    doc = core._credit_authorization_doc(intake)
    if doc is None:
        return core.LeadCreditStatusResponse(authorization_requested=False, authorization_signed=False)

    credit_state = core._credit_pull_state(intake)
    return core.LeadCreditStatusResponse(
        authorization_requested=True,
        authorization_signed=doc.status == "uploaded",
        requested_document_id=doc.id,
        pull_id=UUID(credit_state["pull_id"]) if credit_state and credit_state.get("pull_id") else None,
        fico=credit_state.get("fico") if credit_state else None,
        pulled_at=datetime.fromisoformat(credit_state["pulled_at"]) if credit_state and credit_state.get("pulled_at") else None,
        expires_at=datetime.fromisoformat(credit_state["expires_at"]) if credit_state and credit_state.get("expires_at") else None,
    )

@core.admin_router.get("/{intake_id}/program-fit", response_model=core.LeadProgramFitResponse)
async def get_lead_program_fit(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.LeadProgramFitResponse:
    """Admin-only read of the deterministic program-fit screen (all 14
    programs in PROGRAM_LABELS) — dealer leads only, recomputed live from the
    current key_metrics/dealer_details rather than relying on the possibly-
    stale intake_state snapshot, so the admin always sees the latest figures
    without needing a chat turn or re-run first."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    if intake.variant == core.FUNDING_VARIANT:
        return core.LeadProgramFitResponse(computed=False)
    fit = core._compute_loan_program_fit(intake)
    profile = (
        await db.execute(
            select(core.ApplicationProfile).where(core.ApplicationProfile.intake_id == intake.id)
        )
    ).scalar_one_or_none()
    if profile is None:
        return core.LeadProgramFitResponse(computed=True, programs=fit)
    from app.services.application_programs import get_program_readiness

    normalized = await get_program_readiness(db, profile)
    # Readiness can pin the first automatic selection and materialize stable
    # requirement rows. Persist those deterministic first-read writes here too.
    await db.commit()
    return core.LeadProgramFitResponse(
        computed=True,
        programs=fit,
        candidates=[candidate.model_dump(mode="json") for candidate in normalized.candidates],
        readiness=normalized.model_dump(mode="json"),
    )

@core.admin_router.get("/{intake_id}/dscr-potential", response_model=core.LeadDscrPotentialResponse)
async def get_lead_dscr_potential(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.LeadDscrPotentialResponse:
    """Deterministic DSCR/LTV/max-loan math for real-estate leads only —
    dealer leads have the program-fit screen instead."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    if intake.variant != core.FUNDING_VARIANT:
        return core.LeadDscrPotentialResponse(computed=False)
    await core._refresh_dscr_pricing(db)
    potential = core._compute_dscr_potential(intake)
    return core.LeadDscrPotentialResponse(computed=bool(potential.get("computed")), potential=potential)

@core.admin_router.post("/{intake_id}/credit-pull", response_model=core.LeadCreditStatusResponse)
async def run_lead_credit_pull(
    intake_id: UUID,
    payload: core.AdminCreditPullRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.LeadCreditStatusResponse:
    """Admin runs a soft credit pull on a lead's client, gated on the client
    having SIGNED the credit-authorization requested-document first. Identical
    path for dealer and real-estate leads — the pull is keyed to
    intake.client_id, which every lead has. Deliberately skips the Stripe
    payment-authorization gate the borrower self-serve endpoint requires
    (POST /credit/pull) — this is an admin underwriting-audit action, and
    consent here is the signed authorization document itself."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    if intake.client_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This lead has no linked client")

    doc = core._credit_authorization_doc(intake)
    if doc is None or doc.status != "uploaded":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The client must sign the credit authorization before a pull can run.",
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
            actor=user,
        )
    except credit_pull_core.SoftPullDenied as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": exc.code, "message": exc.message}) from exc
    except credit_pull_core.SoftPullValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except credit_pull_core.SoftPullRateLimited as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except credit_pull_core.SoftPullUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    # Compact cross-reference on the intake — the AI context functions and
    # package PDF read this instead of re-querying CreditPull, and it's the
    # ONLY place credit data touches PublicUnderwritingIntake (never added to
    # _CLIENT_SAFE_INTAKE_STATE_KEYS — bureau data stays admin/AI-only).
    state = core._intake_state(intake)
    state["credit_pull"] = {
        "pull_id": str(pull.id),
        "fico": pull.fico,
        "pulled_at": pull.pulled_at.isoformat() if pull.pulled_at else None,
        "expires_at": pull.expires_at.isoformat() if pull.expires_at else None,
    }
    intake.intake_state = state
    await db.commit()

    return core.LeadCreditStatusResponse(
        authorization_requested=True,
        authorization_signed=True,
        requested_document_id=doc.id,
        pull_id=pull.id,
        fico=pull.fico,
        pulled_at=pull.pulled_at,
        expires_at=pull.expires_at,
    )

@core.admin_router.post("/{intake_id}/prepare-banker-submission", response_model=core.PrepareBankerSubmissionResponse)
async def prepare_banker_submission(
    intake_id: UUID,
    payload: core.PrepareBankerSubmissionRequest,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.PrepareBankerSubmissionResponse:
    """Assembles the normalized JSON payload an admin would hand to the
    banker's intake system -- shared borrower/entity fields, computed
    key_metrics/program-fit, and (if supplied) transient sensitive
    identifiers. Stateless: nothing in this request or response is
    persisted -- no DB write, no intake_state mutation, no logging of the
    identifiers. The real outbound POST to the banker's own API is future
    work pending that integration's spec; this returns the assembled
    payload for admin review/download today."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    if intake.variant == core.FUNDING_VARIANT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Banker submission payloads are dealer leads only")
    from app.services.banker_submission import build_banker_payload

    built = build_banker_payload(
        intake,
        key_metrics=core._key_metrics(intake),
        program_fit=core._compute_loan_program_fit(intake),
        entity_structure=core._entity_structure(intake),
        owners=core._dealer_details(intake).get("owners") or [],
        sensitive_identifiers=payload.identifiers.model_dump(),
    )
    return core.PrepareBankerSubmissionResponse(payload=built)

@core.admin_router.post("/{intake_id}/run-review", response_model=core.ReviewRunStartResponse)
async def rerun_dealer_ai_lead_review(
    intake_id: UUID,
    request: Request,
    background: BackgroundTasks,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.ReviewRunStartResponse:
    """Kick off an admin re-run of the AI review on the lead's latest uploads.
    Returns immediately with a review_id; the heavy pass runs in the background
    and the UI polls GET /{intake_id}/review-progress. Cooldown-throttled."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    core._throttle_or_429(
        core._ADMIN_REVIEW_LAST_BY_INTAKE,
        str(intake.id),
        core._ADMIN_REVIEW_MIN_INTERVAL_SECONDS,
        "A review was just re-run for this lead. Please wait a moment before running another.",
    )
    is_funding = intake.variant == core.FUNDING_VARIANT
    review = await core._create_queued_review(
        db,
        intake,
        request=request,
        actor_name=user.name or "Super admin",
        actor_email=user.email,
        actor_role="super_admin",
        log_event="funding_review_ai_review_rerun" if is_funding else "dealer_ai_review_rerun",
        detail="Admin re-run over latest uploads",
        requested_by_user_id=user.id,
    )
    background.add_task(core._run_review_background, review.id, intake.id)
    return core.ReviewRunStartResponse(review_id=review.id, status="queued")

@core.admin_router.get("/{intake_id}/review-progress", response_model=core.ReviewProgressResponse)
async def dealer_ai_lead_review_progress(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
    review_id: UUID | None = None,
) -> core.ReviewProgressResponse:
    """Poll the live progress of a lead's most recent (or a specific) AI review."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
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

@core.admin_router.post("/{intake_id}/chat", response_model=core.DealerIntakeResponse)
async def dealer_ai_lead_chat(
    intake_id: UUID,
    payload: core.DealerChatRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DealerIntakeResponse:
    """Admin chat with the underwriting AI on a lead, in a PRIVATE internal
    thread (audience='admin') the client never sees."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    assistant_message = None
    if payload.message and payload.message.strip():
        # Refresh the product AI context first so the internal thread reasons
        # over current deterministic figures (dscr_potential, program fit)
        # rather than the snapshot from the last client-side sync.
        if intake.variant == core.FUNDING_VARIANT:
            await core._refresh_dscr_pricing(db)
        context_fn = core._context_fn_for(intake)
        intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **context_fn(intake)}
        chat_messages, _, _ = await core.create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="admin",
            message=payload.message.strip(),
            actor_name=user.name or "Super admin",
            user=user,
            intake_id=intake.id,
        )
        if chat_messages:
            assistant_message = chat_messages[-1].content
            await core._merge_thread_borrower_facts(db, intake, chat_messages[-1])
        # Persist the operator's stated facts (requested amount, credit tier, use
        # of funds, …) so the next review/summary sees them — the client path
        # already records into chat_facts; the admin path did not until now.
        core._record_chat_fact(intake, payload.message, source="admin_chat")
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._response(
        db,
        intake,
        token=None,
        include_management=True,
        assistant_message=assistant_message,
        admin_thread=True,
        thread_user=user,
    )

@core.admin_router.get("/{intake_id}/client-thread", response_model=core.ClientThreadResponse)
async def get_dealer_ai_client_thread(
    intake_id: UUID,
    response: Response,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.ClientThreadResponse:
    """Return the CLIENT-visible (uploader) conversation for this lead so the
    super-admin can see what the borrower and their AI have exchanged. This is a
    different thread from the private admin cockpit chat (audience='admin')."""
    core._require_super_admin(user)
    response.headers["Cache-Control"] = "private, no-store"
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._client_thread_response(db, intake)

@core.admin_router.post("/{intake_id}/client-thread/reply", response_model=core.ClientThreadResponse)
async def reply_dealer_ai_client_thread(
    intake_id: UUID,
    payload: core.DealerChatRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.ClientThreadResponse:
    """Post a message ON BEHALF of the operator INTO the client's (uploader)
    thread, attributed as the underwriter, so the borrower gets a human reply.
    The message IS visible to the client (unlike the private admin thread).

    The AI does not answer this message, and it stands down in this conversation
    for the takeover window so the underwriter and the borrower can talk without
    it stepping in — the same rule as a Live Chat takeover on a loan. The window
    lapses on its own; `/client-thread/resume` hands the conversation back early.
    An audit-log entry records the admin who authored it."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    if not (payload.message and payload.message.strip()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A message is required")
    if intake.bucket_upload_link is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This lead has no client upload link to reply into")
    attribution = f"Underwriter — {user.name}" if user.name else "Underwriter"
    portal_message = await core.create_human_message(
        db,
        bucket=intake.bucket,
        audience="uploader",
        message=payload.message.strip(),
        actor_name=attribution,
        user=user,
        upload_link=intake.bucket_upload_link,
    )
    if payload.also_sms:
        profile_id = (
            await db.execute(
                select(core.ApplicationProfile.id).where(core.ApplicationProfile.intake_id == intake.id)
            )
        ).scalar_one_or_none()
        await core.send_sms_checked(
            db,
            to_phone=intake.phone,
            body=payload.message.strip(),
            require_consent_kind="transactional",
            client_id=intake.client_id,
            profile_id=profile_id,
            intake_id=intake.id,
            portal_message_id=portal_message.id,
            context="intake_client_reply",
        )
    paused_until = core.engagement.pause(intake.bucket_upload_link)
    await core._log(
        db,
        intake.bucket_id,
        "dealer_ai_admin_replied_to_client",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"On-behalf reply into client thread by {user.name or user.email} · AI paused until {paused_until.isoformat()}",
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._client_thread_response(db, intake, paused_until=paused_until)

@core.admin_router.post(
    "/{intake_id}/client-thread/{message_id}/sms-retry",
    response_model=core.ClientThreadResponse,
)
async def retry_dealer_ai_client_thread_sms(
    intake_id: UUID,
    message_id: UUID,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.ClientThreadResponse:
    """Retry only the SMS transport for an operator-authored portal message."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    message = (
        await db.execute(
            select(core.BucketAIMessage).where(
                core.BucketAIMessage.id == message_id,
                core.BucketAIMessage.bucket_id == intake.bucket_id,
                core.BucketAIMessage.audience == "uploader",
                core.BucketAIMessage.sender_kind == "operator",
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client message not found")
    prior = (
        await db.execute(
            select(core.SmsMessage)
            .where(core.SmsMessage.portal_message_id == message.id)
            .order_by(core.SmsMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior is not None and prior.status in {"sent", "delivered"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "This message was already sent by SMS")
    profile_id = (
        await db.execute(select(core.ApplicationProfile.id).where(core.ApplicationProfile.intake_id == intake.id))
    ).scalar_one_or_none()
    await core.send_sms_checked(
        db,
        to_phone=intake.phone,
        body=message.content,
        require_consent_kind="transactional",
        client_id=intake.client_id,
        profile_id=profile_id,
        intake_id=intake.id,
        portal_message_id=message.id,
        context="intake_client_retry",
    )
    await core._log(
        db,
        intake.bucket_id,
        "dealer_ai_admin_retried_client_sms",
        request=request,
        user=user,
        actor_role="underwriter",
        target_type="bucket_ai_message",
        target_id=str(message.id),
        detail=f"SMS delivery retried by {user.name or user.email}",
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._client_thread_response(db, intake)

@core.admin_router.post("/{intake_id}/client-thread/resume", response_model=core.ClientThreadResponse)
async def resume_dealer_ai_client_thread(
    intake_id: UUID,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.ClientThreadResponse:
    """End the takeover early and let the assistant answer the borrower again.

    The window lapses by itself, so this is only for handing the conversation
    back before it does."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    if intake.bucket_upload_link is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This lead has no client upload link")
    core.engagement.resume(intake.bucket_upload_link)
    await core._log(
        db,
        intake.bucket_id,
        "dealer_ai_client_thread_ai_resumed",
        request=request,
        user=user,
        actor_role="super_admin",
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"AI resumed on the client thread by {user.name or user.email}",
    )
    await db.commit()
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._client_thread_response(db, intake)

@core.admin_router.post("/{intake_id}/files/upload-init", response_model=core.BucketFileUploadInitResponse)
async def dealer_ai_lead_upload_init(
    intake_id: UUID,
    payload: core.DealerFileUploadInit,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFileUploadInitResponse:
    """Admin file upload into the lead's bucket (attaches to the upload link and
    runs zip extraction on complete, like the client path)."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._start_upload(db, intake, payload, request, actor_name=user.name or "Super admin", actor_email=user.email,
                               source_kind="internal_upload", actor=user)

@core.admin_router.post("/{intake_id}/files/complete", response_model=core.BucketFileRead)
async def dealer_ai_lead_upload_complete(
    intake_id: UUID,
    payload: core.DealerUploadComplete,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._complete_upload(db, intake, payload, request, actor_name=user.name or "Super admin", actor_email=user.email)

@core.admin_router.post("/{intake_id}/requested-documents/pfs", response_model=core.BucketFileRead)
async def admin_submit_lead_pfs(
    intake_id: UUID,
    payload: core.DealerPfsSubmission,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    """Admin fills out the on-screen PFS on behalf of the client — same
    fallback the client sees on the public/logged-in pages, usable here so an
    admin can close out the checklist without waiting on the client."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._submit_pfs_form(db, intake, payload, request, actor_name=user.name or "Super admin", actor_email=user.email)

@core.admin_router.post("/{intake_id}/requested-documents/debt-schedule", response_model=core.BucketFileRead)
async def admin_submit_lead_debt_schedule(
    intake_id: UUID,
    payload: core.DealerDebtScheduleSubmission,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.BucketFile:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    return await core._submit_debt_schedule_form(db, intake, payload, request, actor_name=user.name or "Super admin", actor_email=user.email)

@core.admin_router.post("/{intake_id}/files/ingest-from-drive", response_model=core.DriveIngestResponse)
async def dealer_ai_lead_ingest_from_drive(
    intake_id: UUID,
    payload: core.DriveIngestRequest,
    request: Request,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.DriveIngestResponse:
    """Ingest operator-selected Google Drive files into the lead's bucket so the
    AI learns from them.

    Each Drive file is downloaded via the operator's own OAuth grant (drive.file
    scope — only picker-/app-granted files are visible), stored into the bucket's
    S3 prefix as a BucketFile(status='uploaded'), and queued for per-file AI
    analysis (enqueue_file_analysis) exactly like a normal upload — so the next
    review composes from a warm cache. Best-effort per file: an unavailable /
    oversized / duplicate file is skipped and reported, never aborting the batch."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)

    from app.services.bucket_ai import enqueue_file_analysis
    from app.services.google import google_oauth_client
    from app.services.google.drive_client import download_file_bytes

    _INGEST_MAX_BYTES = 25 * 1024 * 1024  # per-file cap for AI ingest
    _, prefix, _ = core._bucket_storage_config()
    actor_name = user.name or "Super admin"
    actor_email = user.email

    items: list[core.DriveIngestItemResult] = []
    ingested = 0
    skipped = 0
    # De-dupe against ids already ingested in this request.
    seen_ids: set[str] = set()
    # S3 keys written this request — used to best-effort clean up orphans if the
    # batch fails before/at commit (S3 puts are not transactional with the DB).
    put_keys: list[str] = []
    log = logging.getLogger(__name__)

    def _cleanup_put_objects() -> None:
        bucket, _p, _k = core._bucket_storage_config()
        for key in put_keys:
            try:
                core._s3_client().delete_object(Bucket=bucket, Key=key)
            except Exception:  # noqa: BLE001
                log.warning("drive ingest: orphan cleanup failed key=%s", key)

    try:
        for raw_id in payload.drive_file_ids:
            file_id = (raw_id or "").strip()
            if not file_id or file_id in seen_ids:
                continue
            seen_ids.add(file_id)
            try:
                got = await download_file_bytes(db, user.id, file_id, max_bytes=_INGEST_MAX_BYTES)
            except (
                google_oauth_client.GoogleNotConnected,
                google_oauth_client.GoogleScopeMissing,
                google_oauth_client.GoogleTokenRevoked,
            ) as exc:
                # A credential failure mid-batch is terminal for the remaining
                # files, but files already stored this request are kept: commit
                # them (below), and if NONE were ingested yet, surface a 409 so
                # the operator knows to reconnect. This never orphans S3 — every
                # put so far has a matching row about to be committed.
                if ingested == 0:
                    _cleanup_put_objects()
                    if isinstance(exc, google_oauth_client.GoogleScopeMissing):
                        raise HTTPException(status.HTTP_409_CONFLICT, "Reconnect Google with Drive access to import files.") from exc
                    raise HTTPException(status.HTTP_409_CONFLICT, "Connect your Google account (Settings → Connections) before importing from Drive.") from exc
                items.append(core.DriveIngestItemResult(drive_file_id=file_id, status="skipped", reason="google_disconnected"))
                skipped += 1
                break
            if got is None:
                items.append(core.DriveIngestItemResult(drive_file_id=file_id, status="skipped", reason="unavailable_or_too_large"))
                skipped += 1
                continue
            fname, data, ctype = got
            content_type = core._sanitize_upload_content_type(ctype)
            safe = core._safe_filename(fname)
            try:
                core.upload_validation.validate_upload_bytes(
                    data,
                    file_name=fname,
                    content_type=content_type,
                )
            except core.upload_validation.PasswordProtectedPDF:
                items.append(
                    core.DriveIngestItemResult(
                        drive_file_id=file_id,
                        file_name=fname,
                        status="skipped",
                        reason=core.upload_validation.PASSWORD_PROTECTED_PDF_CODE,
                    )
                )
                skipped += 1
                continue
            # Content-level idempotency: hash the downloaded bytes and skip if a
            # byte-identical active file is already in the bucket. Using the hash
            # (not name+size) avoids both a false "already there" on a coincidental
            # name+size collision AND the multiple-rows crash a name+size lookup
            # could hit. .first() (not one_or_none) is safe against dupes.
            content_hash = hashlib.sha256(data).hexdigest()
            existing = (
                await db.execute(
                    select(core.BucketFile.id).where(
                        core.BucketFile.bucket_id == intake.bucket_id,
                        core.BucketFile.content_hash == content_hash,
                        core.BucketFile.deleted_at.is_(None),
                    ).limit(1)
                )
            ).first()
            if existing is not None:
                items.append(core.DriveIngestItemResult(drive_file_id=file_id, file_name=fname, status="skipped", reason="already_in_bucket"))
                skipped += 1
                continue

            new_id = uuid4()
            s3_key = f"{prefix}/uploads/{intake.bucket_id}/{new_id}-drive-{safe}"
            try:
                core._put_bucket_object(s3_key, content_type, data)
            except Exception:  # noqa: BLE001
                log.exception("drive ingest: S3 put failed intake=%s file=%s", intake_id, file_id)
                items.append(core.DriveIngestItemResult(drive_file_id=file_id, file_name=fname, status="skipped", reason="storage_error"))
                skipped += 1
                continue
            put_keys.append(s3_key)
            bucket_file = core.BucketFile(
                id=new_id,
                bucket_id=intake.bucket_id,
                requested_document_id=None,
                upload_link_id=intake.bucket_upload_link_id,
                file_name=fname,
                s3_key=s3_key,
                content_type=content_type,
                size_bytes=len(data),
                content_hash=content_hash,
                uploaded_by_name=actor_name,
                uploaded_by_email=actor_email,
                status="uploaded",
            )
            db.add(bucket_file)
            await db.flush()
            # Queue per-file analysis so the review composes from a warm cache.
            try:
                await enqueue_file_analysis(db, bucket_file)
            except Exception:  # noqa: BLE001
                log.exception("drive ingest: enqueue analysis failed file=%s", bucket_file.id)
            items.append(core.DriveIngestItemResult(drive_file_id=file_id, file_name=fname, status="ingested"))
            ingested += 1

        await core._log(
            db,
            intake.bucket_id,
            "underwriting_drive_files_ingested",
            request=request,
            actor_name=actor_name,
            actor_email=actor_email,
            actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
            target_type="bucket",
            target_id=str(intake.bucket_id),
            detail=f"Ingested {ingested} Drive file(s), skipped {skipped}",
        )
        await db.commit()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — any failure after S3 writes: roll back + GC orphans.
        await db.rollback()
        _cleanup_put_objects()
        log.exception("drive ingest: batch failed intake=%s — rolled back + cleaned %d object(s)", intake_id, len(put_keys))
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Drive import failed; no files were imported.",
        ) from None
    return core.DriveIngestResponse(ingested=ingested, skipped=skipped, items=items)

@core.admin_router.post("/{intake_id}/executive-summary", response_model=core.PublicUnderwritingArtifactRead)
async def create_dealer_ai_executive_summary(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.PublicUnderwritingArtifactRead:
    generated = await core.generate_package_readiness_documents(intake_id, user, db)
    return generated.executive_summary

@core.admin_router.post("/{intake_id}/prequalification", response_model=core.PublicUnderwritingArtifactRead)
async def create_dealer_ai_prequalification(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.PublicUnderwritingArtifactRead:
    """Admin manual draft/redraft of the borrower prequalification — always
    creates a fresh artifact (unlike the chat auto-trigger's idempotent
    get-or-create), since an admin clicking this expects a regenerate.
    Real-estate only: the schema/instructions are DSCR-specific."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    if intake.variant != core.FUNDING_VARIANT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Prequalification is only available for real estate leads")
    artifact = await core._create_prequalification_artifact(db, intake, user)
    await db.commit()
    artifact = await core._latest_artifact(db, intake_id, "prequalification") or artifact
    return core._artifact_read(artifact)

@core.admin_router.post("/{intake_id}/lender-packet", response_model=core.PublicUnderwritingArtifactRead)
async def create_dealer_ai_lender_packet(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.PublicUnderwritingArtifactRead:
    generated = await core.generate_package_readiness_documents(intake_id, user, db)
    return generated.lender_packet

@core.admin_router.post(
    "/{intake_id}/package-readiness/generate",
    response_model=core.PackageReadinessGenerationRead,
)
async def generate_package_readiness_documents(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.PackageReadinessGenerationRead:
    """Regenerate the two current underwriting PDFs as one audited action.

    Both PDFs are immutable artifact snapshots and are also published into the
    connected bucket. A new run supersedes (but never destroys) the prior
    bucket versions, so the current file list stays clean while the artifact
    history remains available for audit and preview.
    """

    core._require_intake_operator(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    # Serialize regeneration clicks for this file. Without this lock, two
    # browsers could each publish a "current" pair before either sees the
    # other's BucketFile rows.
    await db.execute(
        select(core.PublicUnderwritingIntake.id)
        .where(core.PublicUnderwritingIntake.id == intake.id)
        .with_for_update()
    )
    previous_summary = await core._latest_artifact(db, intake.id, "executive_summary")
    previous_packet = await core._latest_artifact(db, intake.id, "lender_packet")
    generation_id = uuid4()
    generated_at = core._now()
    created_s3_keys: list[str] = []
    commit_attempted = False
    try:
        (
            source_files,
            source_metadata,
            context_snapshot,
            financials,
            input_fingerprint,
        ) = await core._package_generation_inputs(
            db,
            intake,
        )
        summary = await core._create_executive_summary_artifact(
            db,
            intake,
            user,
            financials=financials,
            context_snapshot=context_snapshot,
            source_snapshot_metadata=source_metadata,
            cleanup_s3_keys=created_s3_keys,
        )
        packet = await core._create_lender_packet_artifact(
            db,
            intake,
            user,
            executive_summary=summary,
            files_snapshot=source_files,
            financials=financials,
            source_snapshot_metadata=source_metadata,
            cleanup_s3_keys=created_s3_keys,
        )

        summary_file, superseded_summary_ids = await core._publish_package_artifact_to_bucket(
            db,
            intake=intake,
            artifact=summary,
            previous_artifact=previous_summary,
            generation_id=generation_id,
            generated_at=generated_at,
            user=user,
        )
        packet_file, superseded_packet_ids = await core._publish_package_artifact_to_bucket(
            db,
            intake=intake,
            artifact=packet,
            previous_artifact=previous_packet,
            generation_id=generation_id,
            generated_at=generated_at,
            user=user,
        )
        await core._log(
            db,
            intake.bucket_id,
            "underwriting_package_readiness_generated",
            user=user,
            actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
            target_type="public_underwriting_intake",
            target_id=str(intake.id),
            detail=(
                "Generated executive summary and lender package PDFs "
                f"as package version {generation_id}"
            ),
        )
        # Build the full response while the transaction is still open. This
        # keeps database/read failures inside the rollback + S3 cleanup path;
        # after commit there is no fallible enrichment that could falsely tell
        # the operator generation failed even though the PDFs were published.
        artifacts = [
            item
            for item in await core._management_artifacts(db, intake.id)
            if item.artifact_type in core._PACKAGE_READINESS_TYPES
        ]
        current = {item.id: item for item in artifacts}
        summary = current.get(summary.id, summary)
        packet = current.get(packet.id, packet)
        response = core.PackageReadinessGenerationRead(
            generation_id=generation_id,
            generated_at=generated_at,
            executive_summary=core._artifact_read(
                summary,
                current_input_fingerprint=input_fingerprint,
                active_bucket_file_ids={summary_file.id, packet_file.id},
            ),
            lender_packet=core._artifact_read(
                packet,
                current_input_fingerprint=input_fingerprint,
                active_bucket_file_ids={summary_file.id, packet_file.id},
            ),
            bucket_files=[
                core._package_bucket_file_read(summary_file),
                core._package_bucket_file_read(packet_file),
            ],
            superseded_bucket_file_ids=[
                *superseded_summary_ids,
                *superseded_packet_ids,
            ],
            artifact_history=[
                core._artifact_read(
                    item,
                    current_input_fingerprint=input_fingerprint,
                    active_bucket_file_ids={summary_file.id, packet_file.id},
                )
                for item in artifacts
            ],
        )
        commit_attempted = True
        await db.commit()
    except Exception:
        await db.rollback()
        if not commit_attempted:
            for key in created_s3_keys:
                await asyncio.to_thread(core._delete_s3_object, key)
        else:
            # A lost commit acknowledgement is ambiguous: the rows may already
            # be durable. Never delete bytes that a committed artifact/file can
            # reference; orphan reconciliation is safer than broken packages.
            core.log.exception(
                "Package commit outcome is ambiguous for intake %s; retaining %d stored PDFs",
                intake.id,
                len(created_s3_keys),
            )
        raise

    return response

@core.admin_router.get("/{intake_id}/package.zip")
async def download_dealer_ai_package_zip(
    intake_id: UUID,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> Response:
    """Bundle the full shipping package as a single ZIP — see build_package_zip_bytes."""
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    filename, payload = await core.build_package_zip_bytes(db, intake, user)
    files = [
        file
        for file in core._active_files(intake.bucket)
        if not core._is_package_readiness_output(file)
    ]
    await core._log(
        db,
        intake.bucket_id,
        "underwriting_package_zip_downloaded",
        user=user,
        actor_role=user.role.value if hasattr(user.role, "value") else str(user.role),
        target_type="public_underwriting_intake",
        target_id=str(intake.id),
        detail=f"{filename} ({round(len(payload) / 1024)} KB, {len(files)} docs)",
    )
    await db.commit()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@core.admin_router.post("/{intake_id}/vendor-email/preview", response_model=core.VendorEmailPreviewResponse)
async def preview_dealer_ai_vendor_email(
    intake_id: UUID,
    payload: core.VendorEmailPreviewRequest,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.VendorEmailPreviewResponse:
    core._require_super_admin(user)
    intake = await core._load_admin_dealer_lead(db, intake_id)
    summary_artifact, current_packet = await core._fresh_package_artifact_pair(db, intake)
    packet_artifact = current_packet if payload.include_lender_packet else None
    draft = await core._generate_management_json(
        db,
        intake,
        user,
        purpose="vendor_email",
        extra={
            "requested_recipients": [str(email) for email in payload.to_emails],
            "cc_emails": [str(email) for email in payload.cc_emails],
            "executive_summary": summary_artifact.body_json,
            "lender_packet_title": packet_artifact.title if packet_artifact else None,
        },
    )
    subject = payload.subject or str(draft.get("subject") or f"Qualified Commercial review: {intake.business_name or intake.full_name}")
    body = payload.body or str(draft.get("body") or summary_artifact.body_text or "")
    await db.commit()
    return core.VendorEmailPreviewResponse(
        subject=subject[:512],
        body=body,
        to_emails=[str(email) for email in payload.to_emails],
        cc_emails=[str(email) for email in payload.cc_emails],
        executive_summary=core._artifact_read(summary_artifact),
        lender_packet=core._artifact_read(packet_artifact) if packet_artifact else None,
    )

@core.admin_router.post("/{intake_id}/vendor-email/send", response_model=core.VendorEmailSendResponse)
async def send_dealer_ai_vendor_email(
    intake_id: UUID,
    payload: core.VendorEmailSendRequest,
    user: core.CurrentUser,
    db: AsyncSession = Depends(core.get_db),
) -> core.VendorEmailSendResponse:
    core._require_super_admin(user)
    if not payload.to_emails:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one vendor email is required")
    intake = await core._load_admin_dealer_lead(db, intake_id)
    # Serialize package sends for this intake until the durable dispatch row is
    # committed.  A concurrent retry then observes ``dispatching`` instead of
    # crossing the provider boundary a second time.
    await db.execute(
        select(core.PublicUnderwritingIntake.id)
        .where(core.PublicUnderwritingIntake.id == intake.id)
        .with_for_update()
    )
    summary_artifact, current_packet = await core._fresh_package_artifact_pair(db, intake)
    # The lender packet is attached when explicitly requested (attach_lender_packet)
    # or, for backward-compat, via the legacy include_lender_packet flag.
    want_packet = payload.attach_lender_packet if payload.attach_lender_packet is not None else payload.include_lender_packet
    packet_artifact = current_packet if want_packet else None
    cc_emails = [str(email).lower().strip() for email in payload.cc_emails if str(email).strip()]
    requested_recipients = [
        str(email).lower().strip()
        for email in payload.to_emails
        if str(email).strip()
    ]
    ambiguous_dispatches = list(
        (
            await db.execute(
                select(core.PublicUnderwritingIntakeEmailSend).where(
                    core.PublicUnderwritingIntakeEmailSend.intake_id == intake.id,
                    core.PublicUnderwritingIntakeEmailSend.executive_summary_artifact_id
                    == summary_artifact.id,
                    core.PublicUnderwritingIntakeEmailSend.lender_packet_artifact_id
                    == (packet_artifact.id if packet_artifact else None),
                    core.PublicUnderwritingIntakeEmailSend.ses_status == "dispatching",
                    core.PublicUnderwritingIntakeEmailSend.subject
                    == payload.subject.strip(),
                )
            )
        )
        .scalars()
        .all()
    )
    if any(
        row.to_emails == [email]
        and row.cc_emails == cc_emails
        and row.body.startswith(payload.body.strip())
        for row in ambiguous_dispatches
        for email in requested_recipients
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This exact lender-package delivery is already in progress or has an uncertain provider outcome. Review the send history before trying again.",
        )
    sends: list[core.PublicUnderwritingIntakeEmailSend] = []
    completed_statuses: list[tuple[core.PublicUnderwritingIntakeEmailSend, str]] = []
    access_ids: list[UUID] = []
    _MAX_ATTACH = 8 * 1024 * 1024  # per-file cap
    # Aggregate RAW cap for the whole message. Base64 inflates attachments ~1.37x,
    # so 15 MiB raw ≈ ~20.5 MB encoded — a real buffer under Gmail's 25MB ceiling
    # once MIME headers, the HTML body, and multipart boundaries are added. A stack
    # of sub-8MB files can't build one oversized message that the provider rejects
    # for EVERY recipient. Files that would push past the total are noted, not attached.
    _MAX_TOTAL_ATTACH = 15 * 1024 * 1024
    attachments: list[tuple[str, bytes, str]] = []
    attachment_note = ""
    total_attach_bytes = 0

    def _try_attach(name: str, data: bytes, ctype: str, *, too_big_note: str) -> None:
        """Append an attachment if it fits both the per-file and running aggregate
        caps; otherwise record a note (never silently drop, never overflow)."""
        nonlocal total_attach_bytes, attachment_note
        if len(data) > _MAX_ATTACH or total_attach_bytes + len(data) > _MAX_TOTAL_ATTACH:
            attachment_note += too_big_note
            return
        attachments.append((name, data, ctype))
        total_attach_bytes += len(data)

    # Lender packet PDF.
    if packet_artifact and packet_artifact.s3_key:
        try:
            _try_attach(
                f"{core._safe_filename(packet_artifact.title)}.pdf",
                await core._s3_bytes(packet_artifact.s3_key),
                "application/pdf",
                too_big_note="\n\nThe underwriting packet is available through the secure vendor bucket because the PDF is too large for email.",
            )
        except Exception as exc:  # noqa: BLE001
            attachment_note += f"\n\nThe underwriting packet is available through the secure vendor bucket. Attachment fallback reason: {exc}"
    # Executive summary PDF (legacy markdown artifacts fall back to text).
    if payload.attach_executive_summary:
        if summary_artifact.s3_key:
            try:
                _try_attach(
                    f"{core._safe_filename(summary_artifact.title or 'executive-summary')}.pdf",
                    await core._s3_bytes(summary_artifact.s3_key),
                    "application/pdf",
                    too_big_note="\n\nThe executive summary is available through the secure vendor bucket (too large to attach).",
                )
            except Exception as exc:  # noqa: BLE001
                attachment_note += f"\n\nThe executive summary is available through the secure vendor bucket. Attachment fallback reason: {exc}"
        elif summary_artifact.body_text:
            _try_attach(
                f"{core._safe_filename(summary_artifact.title or 'executive-summary')}.txt",
                summary_artifact.body_text.encode("utf-8"),
                "text/plain; charset=utf-8",
                too_big_note="\n\nThe executive summary is available through the secure vendor bucket (too large to attach).",
            )
    # Full shipping package ZIP (built on the fly).
    if payload.attach_package_zip:
        try:
            zip_name, zip_bytes = await core.build_package_zip_bytes(db, intake, user)
            _try_attach(
                zip_name,
                zip_bytes,
                "application/zip",
                too_big_note="\n\nThe full package ZIP is too large to email — use the secure bucket access below or the Download ZIP button.",
            )
        except Exception:  # noqa: BLE001
            core.log.exception("vendor-email: package zip build failed intake=%s", intake_id)
            attachment_note += "\n\nThe full package ZIP could not be attached; use the secure bucket access below."
    # Google Drive attachments (downloaded via the sender's OAuth grant). Skip any
    # over the per-file OR aggregate size cap and note them rather than failing the
    # whole send. max_bytes is enforced inside download_file_bytes so oversized
    # files are never fully buffered in memory.
    if payload.drive_file_ids:
        from app.services.google.drive_client import download_file_bytes

        for file_id in payload.drive_file_ids[:10]:
            try:
                got = await download_file_bytes(db, user.id, file_id, max_bytes=_MAX_ATTACH)
            except Exception:  # noqa: BLE001 — not connected / revoked
                got = None
            if got is None:
                attachment_note += f"\n\nA Google Drive file ({file_id}) could not be attached (unavailable or too large)."
                continue
            fname, data, ctype = got
            _try_attach(
                fname, data, ctype,
                too_big_note=f"\n\n'{fname}' was not attached to keep the email under the size limit; it's in the secure vendor bucket.",
            )

    # Bucket access blurb. "passcode" creates ONE no-login share for the bucket and
    # embeds the link + one-time passcode in every recipient's body (same access for
    # all). "login" keeps the per-recipient Clerk vendor-login link. "none" omits it.
    passcode_blurb = ""
    passcode_blurb_redacted = ""  # persisted variant with the one-time code masked
    if payload.bucket_access == "passcode":
        share_passcode = core._generate_passcode()
        first_recipient = str(payload.to_emails[0]) if payload.to_emails else "Vendor"
        share = core.BucketShare(
            bucket_id=intake.bucket_id,
            token=secrets.token_urlsafe(32),
            recipient_name=first_recipient[:180],  # column is String(180)
            recipient_email=first_recipient[:320] if payload.to_emails else None,
            passcode_hash=core._hash_passcode(share_passcode),
            can_preview=payload.can_preview,
            can_download=payload.can_download,
            can_add_notes=payload.can_add_notes,
            can_view_ai_summary=payload.can_view_ai_summary,
            can_use_ai_chat=payload.can_use_ai_chat,
            can_view_ai_tasks=payload.can_view_ai_tasks,
            can_propose_tasks=payload.can_propose_tasks,
        )
        # Freeze only lender-safe files. Raw merchant terms contain desk-only
        # economics, and later unrelated uploads must not leak through this link.
        share.files = await core._lender_package_bucket_files(db, intake)
        db.add(share)
        await db.flush()
        share_url = core._public_url(f"/buckets/share/{share.token}")
        passcode_blurb = (
            "\n\nSecure file access (no login required):\n"
            f"{share_url}\nAccess code: {share_passcode}\n"
            "Open the link and enter the access code to view the file package."
        )
        # The one-time passcode must NOT be persisted at rest (parity with the
        # hash-only BucketShare design); the stored send-row body masks it.
        passcode_blurb_redacted = (
            "\n\nSecure file access (no login required):\n"
            f"{share_url}\nAccess code: (sent to recipient; not stored)\n"
            "Open the link and enter the access code to view the file package."
        )

    for idx, raw_email in enumerate(payload.to_emails):
        email = str(raw_email).lower().strip()
        access = None
        if payload.bucket_access == "login":
            access = await core._prepare_vendor_access(db, intake, email, payload)
            access_ids.append(access.id)
            vendor_link = core._public_url(f"/vendor/buckets?bucket={intake.bucket_id}")
            access_blurb = (
                "\n\nSecure bucket access:\n"
                + vendor_link
                + "\n\nQualified Commercial has enabled vendor access for this bucket. Please log in with the invited vendor email to view the file package."
            )
            access_blurb_for_record = access_blurb
        else:
            access_blurb = passcode_blurb  # passcode share (same for all) or "" for none
            access_blurb_for_record = passcode_blurb_redacted  # masks the one-time code at rest
        body = payload.body.strip() + access_blurb + attachment_note
        # Persisted copy never carries the live one-time passcode.
        body_for_record = payload.body.strip() + access_blurb_for_record + attachment_note
        html_body = "<br>".join(html.escape(line) for line in body.splitlines())
        # Each To recipient gets their own message (separate secure-bucket access
        # link). CC only ONCE — on the first message — so a CC'd colleague isn't
        # copied N times (once per To recipient). The send row still records the
        # intended cc list for audit.
        send_cc = cc_emails if idx == 0 else []
        send_row = core.PublicUnderwritingIntakeEmailSend(
            intake_id=intake.id,
            executive_summary_artifact_id=summary_artifact.id,
            lender_packet_artifact_id=packet_artifact.id if packet_artifact else None,
            to_emails=[email],
            cc_emails=cc_emails,
            subject=payload.subject.strip(),
            body=body_for_record,
            vendor_access_ids=[str(access.id)] if access is not None else None,
            ses_status="dispatching",
            ses_message_ids=None,
            ses_error=None,
            sent_by_user_id=user.id,
        )
        db.add(send_row)
        sends.append(send_row)
        # Persist the exact access grant/share and a fail-closed dispatch ledger
        # before calling Gmail/SES. If the process dies after provider
        # acceptance, retries see ``dispatching`` and do not silently duplicate
        # the lender email.
        await db.commit()

        try:
            # The pre-provider commit released the first lock. Reacquire it and
            # hold it through freshness validation, provider handoff, and the
            # durable outcome commit. Package generation takes this same lock,
            # eliminating the last regenerate-vs-send TOCTOU window.
            await db.execute(
                select(core.PublicUnderwritingIntake.id)
                .where(core.PublicUnderwritingIntake.id == intake.id)
                .with_for_update()
            )
            fresh_summary, fresh_packet = await core._fresh_package_artifact_pair(db, intake)
            if (
                fresh_summary.id != summary_artifact.id
                or fresh_packet.id != current_packet.id
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "The package was regenerated while this email was being prepared. Review and send the current PDF pair.",
                )
        except HTTPException:
            send_row.ses_status = "failed"
            send_row.ses_error = "package_changed_before_provider_handoff"
            for completed_row, completed_status in completed_statuses:
                completed_row.ses_status = completed_status
            await db.commit()
            raise

        try:
            # Send from the operator's connected Gmail when available, else
            # firm SES. This is intentionally after the durable dispatch row.
            result = await core.send_as_user(
                db,
                user.id,
                to_emails=[email],
                cc_emails=send_cc,
                subject=payload.subject.strip(),
                body_text=body,
                body_html=f"<p>{html_body}</p>",
                attachments=attachments or None,
            )
        except Exception as exc:  # noqa: BLE001
            # An exception does not prove the provider rejected the request; it
            # can occur after acceptance while parsing the response. Leave this
            # recipient fail-closed as ``dispatching`` so an automatic retry
            # cannot create a duplicate, while finalizing prior recipients.
            send_row.ses_error = "transport_exception_provider_outcome_uncertain"
            for completed_row, completed_status in completed_statuses:
                completed_row.ses_status = completed_status
            await db.commit()
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "The lender email provider outcome is uncertain. The dispatch attempt and secure access were preserved; review the send history before retrying.",
            ) from exc

        # Keep the row in ``dispatching`` through the whole recipient batch so
        # a concurrent retry cannot resend recipient one while recipient two is
        # still being processed. The message id/error below is nevertheless
        # committed immediately as the durable provider outcome.
        send_row.ses_message_ids = [result.message_id] if result.message_id else None
        send_row.ses_error = result.error
        completed_statuses.append(
            (send_row, result.detail[:40] if result.ok else "failed")
        )
        # Commit the provider outcome on its own. A later activity-log failure
        # must not roll back the recipient's working access link or erase a
        # provider-accepted message id.
        await db.commit()
        try:
            await core._log(
                db,
                intake.bucket_id,
                "underwriting_vendor_email_sent"
                if result.ok
                else "underwriting_vendor_email_failed",
                user=user,
                actor_role=(
                    user.role.value if hasattr(user.role, "value") else str(user.role)
                ),
                target_type="public_underwriting_email",
                target_id=email,
                detail=result.detail if result.ok else result.error or result.detail,
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            core.log.exception(
                "vendor-email: provider outcome persisted but activity log failed intake=%s recipient=%s",
                intake.id,
                email,
            )

    for row, completed_status in completed_statuses:
        row.ses_status = completed_status
    await db.commit()
    for row in sends:
        await db.refresh(row)
    return core.VendorEmailSendResponse(email_sends=[core._email_send_read(row) for row in sends], vendor_access_ids=access_ids)
