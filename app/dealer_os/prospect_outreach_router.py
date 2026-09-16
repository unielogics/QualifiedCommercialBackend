"""Field Desk Dealer Prospect email, collateral, and suppression API."""

# FastAPI dependencies intentionally use callable defaults.
# ruff: noqa: B008

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.models.dealer_prospect import DealerProspect, DealerProspectActivity
from app.models.prospect_outreach import (
    DRAFT_STATUSES,
    DealerProspectEmailDraft,
    DealerProspectInboundReply,
    EmailSuppression,
    MarketingCollateralAsset,
    MarketingCollateralAssetEvent,
)
from app.schemas.prospect_outreach import (
    EmailSuppressionCreate,
    EmailSuppressionRead,
    MarketingCollateralAction,
    MarketingCollateralEventRead,
    MarketingCollateralHistory,
    MarketingCollateralList,
    MarketingCollateralPatch,
    MarketingCollateralRead,
    MarketingCollateralReorder,
    ProspectDraftAction,
    ProspectEmailDraftCreate,
    ProspectEmailDraftList,
    ProspectEmailDraftPatch,
    ProspectEmailDraftRead,
    ProspectEmailOutboxList,
    ProspectReplyIngest,
    ProspectReplyIngestResult,
    ProspectReplyList,
    ProspectReplyRead,
)
from app.services import prospect_outreach as outreach
from app.services.email import prospect_reply
from app.services.email.user_inbox_sync import decrypt_body

from .services import prospects as prospect_service


def _require_outreach_enabled(request: Request) -> None:
    # Compliance and already-sent secure links must remain usable if an admin
    # pauses the pilot.  The collateral library also stays available so a
    # config admin can validate and approve the exact bundle before agents are
    # allowed into the pipeline; each collateral handler independently enforces
    # ``require_config_admin``.
    path = request.url.path
    collateral_root = "/dealer-os/marketing-collateral"
    if (
        "/prospect-email-unsubscribe/" in path
        or "/prospect-email-bundles/" in path
        or path.endswith(collateral_root)
        or f"{collateral_root}/" in path
    ):
        return
    prospect_service.require_pipeline_enabled()


router = APIRouter(
    prefix="/dealer-os",
    tags=["dealer-prospect-outreach"],
    dependencies=[Depends(_require_outreach_enabled)],
)


def _raise_service(exc: Exception) -> None:
    if isinstance(exc, outreach.OutreachNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, outreach.OutreachConflict):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "draft_conflict", "message": str(exc)},
        ) from exc
    if isinstance(exc, outreach.OutreachBlocked):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": exc.code, "message": exc.detail},
        ) from exc
    raise exc


async def _visible_draft(
    db: AsyncSession, user, draft_id: UUID, *, lock: bool = False
) -> tuple[DealerProspectEmailDraft, DealerProspect]:
    try:
        row = await outreach.load_draft(db, draft_id, lock=lock)
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise
    prospect = await prospect_service.load_visible_prospect(db, user, row.prospect_id)
    return row, prospect


def _collateral_read(row: MarketingCollateralAsset) -> MarketingCollateralRead:
    return MarketingCollateralRead(
        id=row.id,
        assignment=row.assignment,
        logical_key=row.logical_key,
        name=row.name,
        version=row.version,
        sort_order=row.sort_order,
        status=row.status,
        file_name=row.file_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        validation_status=row.validation_status,
        validation_detail=row.validation_detail,
        uploaded_by_user_id=row.uploaded_by_user_id,
        approved_by_user_id=row.approved_by_user_id,
        retired_by_user_id=row.retired_by_user_id,
        approved_at=row.approved_at,
        retired_at=row.retired_at,
        created_at=row.created_at,
        preview_url=f"/api/v1/dealer-os/marketing-collateral/{row.id}/document?disposition=inline",
        download_url=f"/api/v1/dealer-os/marketing-collateral/{row.id}/document?disposition=attachment",
    )


@router.post(
    "/prospects/{prospect_id}/email-drafts",
    response_model=ProspectEmailDraftRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_prospect_email_draft(
    prospect_id: UUID,
    payload: ProspectEmailDraftCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProspectEmailDraftRead:
    prospect = await prospect_service.load_visible_prospect(db, user, prospect_id)
    try:
        row = await outreach.create_draft(db, prospect=prospect, actor=user, payload=payload)
        return await outreach.draft_read(db, row)
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


@router.get(
    "/prospects/{prospect_id}/email-drafts",
    response_model=ProspectEmailDraftList,
)
async def list_prospect_email_drafts(
    prospect_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProspectEmailDraftList:
    await prospect_service.load_visible_prospect(db, user, prospect_id)
    rows = list(
        (
            await db.execute(
                select(DealerProspectEmailDraft)
                .where(DealerProspectEmailDraft.prospect_id == prospect_id)
                .order_by(DealerProspectEmailDraft.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return ProspectEmailDraftList(items=[await outreach.draft_read(db, row) for row in rows])


@router.get(
    "/prospect-email-drafts",
    response_model=ProspectEmailOutboxList,
)
async def list_prospect_email_outbox(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    statuses: list[str] | None = Query(default=None, alias="status"),
    due: str = Query(default="all", pattern="^(all|scheduled|due)$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProspectEmailOutboxList:
    """Authorized shared outbox across every prospect visible to the caller."""
    prospect_service.require_prospect_actor(user)
    selected = list(dict.fromkeys(statuses or []))
    invalid = sorted(set(selected) - set(DRAFT_STATUSES))
    if invalid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "invalid_draft_status", "statuses": invalid},
        )
    conditions = [prospect_service.prospect_access_filter(user)]
    if selected:
        conditions.append(DealerProspectEmailDraft.status.in_(selected))
    if due == "scheduled":
        conditions.append(DealerProspectEmailDraft.auto_send_at.is_not(None))
    elif due == "due":
        conditions.extend(
            [
                DealerProspectEmailDraft.auto_send_at.is_not(None),
                DealerProspectEmailDraft.auto_send_at <= datetime.now(UTC),
            ]
        )
    total = int(
        (
            await db.execute(
                select(func.count(DealerProspectEmailDraft.id))
                .join(DealerProspect, DealerProspect.id == DealerProspectEmailDraft.prospect_id)
                .where(*conditions)
            )
        ).scalar_one()
        or 0
    )
    rows = list(
        (
            await db.execute(
                select(DealerProspectEmailDraft)
                .join(DealerProspect, DealerProspect.id == DealerProspectEmailDraft.prospect_id)
                .where(*conditions)
                .order_by(
                    case(
                        (DealerProspectEmailDraft.status == "pending_review", 0),
                        else_=1,
                    ),
                    DealerProspectEmailDraft.auto_send_at.asc().nullslast(),
                    DealerProspectEmailDraft.created_at.desc(),
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ProspectEmailOutboxList(
        items=[await outreach.draft_read(db, row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/prospects/{prospect_id}/replies", response_model=ProspectReplyList)
async def list_prospect_email_replies(
    prospect_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProspectReplyList:
    await prospect_service.load_visible_prospect(db, user, prospect_id)
    rows = list(
        (
            await db.execute(
                select(DealerProspectInboundReply)
                .where(DealerProspectInboundReply.prospect_id == prospect_id)
                .order_by(
                    DealerProspectInboundReply.received_at.desc().nullslast(),
                    DealerProspectInboundReply.created_at.desc(),
                )
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return ProspectReplyList(
        items=[
            ProspectReplyRead(
                id=row.id,
                draft_id=row.draft_id,
                provider=row.provider,
                provider_message_id=row.provider_message_id,
                from_email=row.from_email,
                subject=row.subject,
                body=decrypt_body(row.body_text_enc, row.encryption_provider),
                received_at=row.received_at,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


@router.get(
    "/prospect-email-drafts/{draft_id}",
    response_model=ProspectEmailDraftRead,
)
async def get_prospect_email_draft(
    draft_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProspectEmailDraftRead:
    row, _ = await _visible_draft(db, user, draft_id)
    return await outreach.draft_read(db, row)


@router.post(
    "/prospect-email-drafts/{draft_id}/edit",
    response_model=ProspectEmailDraftRead,
)
async def start_prospect_email_edit(
    draft_id: UUID,
    user: CurrentUser,
    payload: ProspectDraftAction,
    db: AsyncSession = Depends(get_db),
) -> ProspectEmailDraftRead:
    await _visible_draft(db, user, draft_id)
    try:
        row = await outreach.start_editing(
            db, draft_id, expected_version=payload.expected_version
        )
        return await outreach.draft_read(db, row)
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


@router.patch(
    "/prospect-email-drafts/{draft_id}",
    response_model=ProspectEmailDraftRead,
)
async def patch_prospect_email_draft(
    draft_id: UUID,
    payload: ProspectEmailDraftPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProspectEmailDraftRead:
    await _visible_draft(db, user, draft_id)
    try:
        row = await outreach.edit_draft(
            db,
            draft_id,
            expected_version=payload.expected_version,
            subject=payload.subject,
            body=payload.body,
        )
        return await outreach.draft_read(db, row)
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


@router.post(
    "/prospect-email-drafts/{draft_id}/approve",
    response_model=ProspectEmailDraftRead,
)
async def approve_prospect_email_draft(
    draft_id: UUID,
    user: CurrentUser,
    payload: ProspectDraftAction,
    db: AsyncSession = Depends(get_db),
) -> ProspectEmailDraftRead:
    await _visible_draft(db, user, draft_id)
    try:
        row = await outreach.dispatch_draft(
            db,
            draft_id,
            expected_version=payload.expected_version,
            approved_by_user_id=user.id,
            automatic=False,
        )
        return await outreach.draft_read(db, row)
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


@router.post(
    "/prospect-email-drafts/{draft_id}/cancel",
    response_model=ProspectEmailDraftRead,
)
async def cancel_prospect_email_draft(
    draft_id: UUID,
    user: CurrentUser,
    payload: ProspectDraftAction,
    db: AsyncSession = Depends(get_db),
) -> ProspectEmailDraftRead:
    await _visible_draft(db, user, draft_id)
    try:
        row = await outreach.cancel_draft(
            db, draft_id, expected_version=payload.expected_version
        )
        return await outreach.draft_read(db, row)
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


@router.post(
    "/prospect-email-drafts/{draft_id}/use-secure-bundle",
    response_model=ProspectEmailDraftRead,
)
async def use_secure_bundle_for_prospect_email(
    draft_id: UUID,
    user: CurrentUser,
    payload: ProspectDraftAction,
    db: AsyncSession = Depends(get_db),
) -> ProspectEmailDraftRead:
    await _visible_draft(db, user, draft_id)
    try:
        row = await outreach.select_secure_bundle(
            db,
            draft_id,
            actor_user_id=user.id,
            expected_version=payload.expected_version,
        )
        return await outreach.draft_read(db, row)
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


@router.get("/prospect-email-bundles/{token}", include_in_schema=False)
async def download_prospect_email_bundle(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        draft, assets = await outreach.load_secure_bundle(db, token, lock=True)
    except outreach.OutreachNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Secure bundle not found.") from exc
    except outreach.OutreachBlocked as exc:
        code = (
            status.HTTP_410_GONE
            if exc.code == "secure_bundle_expired"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, {"code": exc.code, "message": exc.detail}) from exc

    output = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for asset in assets:
            base = re.sub(r"[^A-Za-z0-9._ -]+", "_", asset.file_name).strip(" .")
            base = base or f"collateral-{asset.id}.pdf"
            candidate = base
            index = 2
            while candidate.lower() in used:
                stem, dot, suffix = base.rpartition(".")
                candidate = f"{stem or base}-{index}{dot}{suffix}" if dot else f"{base}-{index}"
                index += 1
            used.add(candidate.lower())
            archive.writestr(candidate, bytes(asset.document_bytes))
    if draft.secure_bundle_downloaded_at is None:
        draft.secure_bundle_downloaded_at = datetime.now(UTC)
        db.add(
            DealerProspectActivity(
                prospect_id=draft.prospect_id,
                actor_user_id=None,
                kind="email.secure_bundle_downloaded",
                body="Dealer downloaded the secure collateral bundle.",
                metadata_json={"draft_id": str(draft.id)},
            )
        )
    return Response(
        content=output.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="dealer-information-{draft.prospect_id}.zip"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/marketing-collateral", response_model=MarketingCollateralList)
async def list_marketing_collateral(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    include_retired: bool = False,
    scope: str = Query(default="dealer_outreach", max_length=48),
) -> MarketingCollateralList:
    prospect_service.require_config_admin(user)
    if scope != outreach.COLLATERAL_ASSIGNMENT:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported collateral scope.")
    stmt = select(MarketingCollateralAsset).where(
        MarketingCollateralAsset.assignment == outreach.COLLATERAL_ASSIGNMENT
    )
    if not include_retired:
        stmt = stmt.where(MarketingCollateralAsset.status != "retired")
    rows = list(
        (
            await db.execute(
                stmt.order_by(
                    MarketingCollateralAsset.sort_order,
                    MarketingCollateralAsset.logical_key,
                    MarketingCollateralAsset.version.desc(),
                    MarketingCollateralAsset.id,
                )
            )
        )
        .scalars()
        .all()
    )
    return MarketingCollateralList(items=[_collateral_read(row) for row in rows])


@router.post("/marketing-collateral/reorder", response_model=MarketingCollateralList)
async def reorder_marketing_collateral(
    payload: MarketingCollateralReorder,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MarketingCollateralList:
    prospect_service.require_config_admin(user)
    try:
        rows = await outreach.reorder_collateral(
            db,
            actor_user_id=user.id,
            expected_ids=payload.expected_ids,
            ordered_ids=payload.ordered_ids,
        )
        return MarketingCollateralList(items=[_collateral_read(row) for row in rows])
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


@router.post(
    "/marketing-collateral",
    response_model=MarketingCollateralRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_marketing_collateral(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=180),
    name: str | None = Form(default=None, max_length=180),
    scope: str = Form(default="dealer_outreach", max_length=48),
    sort_order: int = Form(default=0, ge=0, le=100_000),
) -> MarketingCollateralRead:
    prospect_service.require_config_admin(user)
    if scope != outreach.COLLATERAL_ASSIGNMENT:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported collateral scope.")
    selected_name = (title or name or "").strip()
    if not selected_name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Collateral title is required.")
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Only PDF collateral is allowed.")
    max_bytes = outreach.get_settings().prospect_email_max_attachment_bytes
    data = await file.read(max_bytes + 1)
    try:
        row = await outreach.upload_collateral(
            db,
            actor_user_id=user.id,
            name=selected_name,
            file_name=file.filename or "dealer-outreach.pdf",
            data=data,
            sort_order=sort_order,
        )
        return _collateral_read(row)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Another collateral version was uploaded concurrently. Refresh and try again.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


async def _collateral(db: AsyncSession, asset_id: UUID, *, lock: bool = False):
    stmt = select(MarketingCollateralAsset).where(MarketingCollateralAsset.id == asset_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Collateral not found.")
    return row


@router.get(
    "/marketing-collateral/{asset_id}/history",
    response_model=MarketingCollateralHistory,
)
async def marketing_collateral_history(
    asset_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MarketingCollateralHistory:
    prospect_service.require_config_admin(user)
    await _collateral(db, asset_id)
    rows = list(
        (
            await db.execute(
                select(MarketingCollateralAssetEvent)
                .where(MarketingCollateralAssetEvent.asset_id == asset_id)
                .order_by(
                    MarketingCollateralAssetEvent.created_at.desc(),
                    MarketingCollateralAssetEvent.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return MarketingCollateralHistory(
        items=[
            MarketingCollateralEventRead(
                id=row.id,
                asset_id=row.asset_id,
                actor_user_id=row.actor_user_id,
                event_type=row.event_type,
                details=row.details,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


@router.get("/marketing-collateral/{asset_id}/document")
async def preview_marketing_collateral(
    asset_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    disposition: str = Query(default="inline", pattern="^(inline|attachment)$"),
) -> Response:
    prospect_service.require_config_admin(user)
    row = await _collateral(db, asset_id)
    safe_name = row.file_name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    return Response(
        content=bytes(row.document_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{row.sha256}"',
        },
    )


@router.post("/marketing-collateral/{asset_id}/approve", response_model=MarketingCollateralRead)
async def approve_marketing_collateral(
    asset_id: UUID,
    payload: MarketingCollateralAction,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MarketingCollateralRead:
    prospect_service.require_config_admin(user)
    row = await _collateral(db, asset_id, lock=True)
    try:
        return _collateral_read(
            await outreach.approve_collateral(
                db, row, actor_user_id=user.id, sort_order=payload.sort_order
            )
        )
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Another version became active concurrently. Refresh before approving.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        _raise_service(exc)
        raise


@router.post("/marketing-collateral/{asset_id}/restore", response_model=MarketingCollateralRead)
async def restore_marketing_collateral(
    asset_id: UUID,
    payload: MarketingCollateralAction,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MarketingCollateralRead:
    return await approve_marketing_collateral(asset_id, payload, user, db)


@router.patch("/marketing-collateral/{asset_id}", response_model=MarketingCollateralRead)
async def patch_marketing_collateral(
    asset_id: UUID,
    payload: MarketingCollateralPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MarketingCollateralRead:
    """Compatibility endpoint for the admin library's active toggle."""
    prospect_service.require_config_admin(user)
    row = await _collateral(db, asset_id, lock=True)
    if payload.is_active is True:
        row = await outreach.approve_collateral(
            db, row, actor_user_id=user.id, sort_order=payload.sort_order
        )
    elif payload.is_active is False:
        row = await outreach.retire_collateral(db, row, actor_user_id=user.id)
    elif payload.sort_order is not None:
        row = await outreach.update_collateral_order(
            db,
            row,
            actor_user_id=user.id,
            sort_order=payload.sort_order,
        )
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No collateral changes provided.")
    return _collateral_read(row)


@router.post("/marketing-collateral/{asset_id}/retire", response_model=MarketingCollateralRead)
async def retire_marketing_collateral(
    asset_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MarketingCollateralRead:
    prospect_service.require_config_admin(user)
    row = await _collateral(db, asset_id, lock=True)
    return _collateral_read(await outreach.retire_collateral(db, row, actor_user_id=user.id))


@router.get("/email-suppressions", response_model=list[EmailSuppressionRead])
async def list_email_suppressions(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    active_only: bool = True,
) -> list[EmailSuppressionRead]:
    prospect_service.require_config_admin(user)
    stmt = select(EmailSuppression)
    if active_only:
        stmt = stmt.where(EmailSuppression.active.is_(True))
    rows = list(
        (await db.execute(stmt.order_by(EmailSuppression.created_at.desc()).limit(500)))
        .scalars()
        .all()
    )
    return [
        EmailSuppressionRead(
            id=row.id,
            email=row.email_normalized,
            reason=row.reason,
            source=row.source,
            active=row.active,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


@router.post("/email-suppressions", response_model=EmailSuppressionRead)
async def create_email_suppression(
    payload: EmailSuppressionCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EmailSuppressionRead:
    prospect_service.require_config_admin(user)
    row = await outreach.set_suppression(
        db,
        email=str(payload.email),
        reason=payload.reason,
        source="administrative",
        actor_user_id=user.id,
        details=payload.details,
    )
    return EmailSuppressionRead(
        id=row.id,
        email=row.email_normalized,
        reason=row.reason,
        source=row.source,
        active=row.active,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


@router.delete("/email-suppressions/{suppression_id}", response_model=EmailSuppressionRead)
async def revoke_email_suppression(
    suppression_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EmailSuppressionRead:
    prospect_service.require_config_admin(user)
    row = await db.get(EmailSuppression, suppression_id, with_for_update=True)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Suppression not found.")
    await outreach.revoke_suppression(db, row, actor_user_id=user.id)
    return EmailSuppressionRead(
        id=row.id,
        email=row.email_normalized,
        reason=row.reason,
        source=row.source,
        active=row.active,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


async def _apply_unsubscribe(db: AsyncSession, token: str) -> DealerProspectEmailDraft:
    digest = outreach.token_hash(token)
    draft = (
        await db.execute(
            select(DealerProspectEmailDraft).where(
                DealerProspectEmailDraft.unsubscribe_token_hash == digest
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unsubscribe link is invalid.")
    await outreach.set_suppression(
        db,
        email=draft.recipient_email,
        reason="unsubscribe",
        source="one_click",
        details={"draft_id": str(draft.id), "prospect_id": str(draft.prospect_id)},
    )
    prospect = await db.get(DealerProspect, draft.prospect_id, with_for_update=True)
    if prospect is not None:
        prospect.do_not_contact = True
        prospect.do_not_contact_reason = "Email unsubscribe"
        prospect.next_follow_up_at = None
        prospect.last_activity_at = datetime.now(UTC)
        db.add(
            DealerProspectActivity(
                prospect_id=prospect.id,
                actor_user_id=None,
                kind="email.unsubscribed",
                body="Recipient unsubscribed from Dealer Desk email.",
                metadata_json={"draft_id": str(draft.id)},
            )
        )
    return draft


@router.get("/prospect-email-unsubscribe/{token}", include_in_schema=False)
async def unsubscribe_prospect_email_get(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _apply_unsubscribe(db, token)
    return Response(
        content=(
            "<!doctype html><html><body><h1>You are unsubscribed</h1>"
            "<p>Qualified Commercial will not send further Dealer Desk email to this address.</p>"
            "</body></html>"
        ),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/prospect-email-unsubscribe/{token}", include_in_schema=False)
async def unsubscribe_prospect_email_post(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _apply_unsubscribe(db, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/prospect-email-replies/ingest", response_model=ProspectReplyIngestResult)
async def ingest_prospect_email_reply(
    payload: ProspectReplyIngest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProspectReplyIngestResult:
    prospect_service.require_config_admin(user)
    result = await prospect_reply.ingest_reply(
        db,
        provider=payload.provider,
        provider_message_id=payload.provider_message_id,
        from_email=str(payload.from_email),
        to_addresses=[str(value) for value in payload.to_addresses],
        subject=payload.subject,
        body=payload.body,
        in_reply_to=payload.in_reply_to,
        references=payload.references,
        received_at=payload.received_at,
    )
    return ProspectReplyIngestResult(
        matched=result.matched,
        duplicate=result.duplicate,
        prospect_id=result.prospect_id,
        draft_id=result.draft_id,
    )
