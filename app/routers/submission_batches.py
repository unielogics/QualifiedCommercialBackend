"""Underwriter-approved application submissions and isolated recipient access."""

# ruff: noqa: B008

from __future__ import annotations

import asyncio
import io
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.bucket import BucketFile
from app.models.lender import Lender
from app.models.submission_batch import (
    LenderOffering,
    LenderSubmission,
    LenderSubmissionEvent,
    SubmissionBatch,
)
from app.schemas.submission_batch import (
    LenderOfferingCreate,
    LenderOfferingRead,
    SubmissionApproval,
    SubmissionBatchCreate,
    SubmissionBatchRead,
)
from app.services import application_profiles as profiles
from app.services import submission_batches as service
from app.services.program_rules import ProgramRuleError, validate_rules

router = APIRouter(tags=["application-lender-submissions"])
public_router = APIRouter(prefix="/public/lender-submissions", tags=["lender-submission-access"])


@router.get("/lenders/{lender_id}/offerings", response_model=list[LenderOfferingRead])
async def offerings(lender_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    service.require_underwriter(user)
    return list((await db.scalars(select(LenderOffering).where(LenderOffering.lender_id == lender_id).order_by(LenderOffering.version.desc()))).all())


@router.post("/lenders/{lender_id}/offerings", response_model=LenderOfferingRead, status_code=201)
async def create_offering(lender_id: UUID, payload: LenderOfferingCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "Super Admin access is required to confirm lender requirements")
    lender = (await db.scalars(select(Lender).where(Lender.id == lender_id).with_for_update())).first()
    if lender is None:
        raise HTTPException(404, "Lender not found")
    try:
        validate_rules(payload.fit_rules, enforce_supported_fields=True)
    except ProgramRuleError as exc:
        raise HTTPException(422, str(exc)) from exc
    for file_id in payload.official_form_file_ids:
        file = await db.get(BucketFile, file_id)
        if file is None or file.deleted_at is not None or file.status != "uploaded":
            raise HTTPException(422, "An official lender form is unavailable")
    previous = list((await db.scalars(select(LenderOffering).where(LenderOffering.lender_id == lender_id, LenderOffering.program_key == payload.program_key).order_by(LenderOffering.version.desc()))).all())
    for row in previous:
        if row.status == "active":
            row.status = "retired"
    await db.flush()
    row = LenderOffering(lender_id=lender_id, version=max((r.version for r in previous), default=0) + 1, created_by_user_id=user.id, **payload.model_dump(mode="json"))
    db.add(row)
    await db.flush()
    return row


@router.get("/application-profiles/{profile_id}/lender-options")
async def lender_options(profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    service.require_underwriter(user)
    profile = await profiles.load_profile(db, profile_id, user)
    source = await service.source_state(db, profile)
    selected = {s.program_key for s in source["readiness"].selections}
    lenders = list((await db.scalars(select(Lender).where(Lender.is_active.is_(True)).order_by(Lender.name))).all())
    result = []
    for lender in lenders:
        all_offerings = await service.active_offerings(db, lender.id, selected)
        offering = next((o for o in all_offerings if o.program_key in selected), next((o for o in all_offerings if o.program_key == "*"), None))
        fit = service.fit_for_offering(offering, source["context"], selected)
        requirements = []
        for definition in (offering.requirements if offering else []) or []:
            state = source["states"].get(service.lender_requirement_key(lender.id, definition["requirement_key"]))
            requirements.append({**definition, "status": state.status if state else "missing"})
        result.append({"id": str(lender.id), "name": lender.name, "submission_email": lender.submission_email,
            "fit": fit, "offering_id": str(offering.id) if offering else None,
            "configuration_complete": bool(offering and offering.checklist_confirmed and (not offering.official_forms_required or offering.official_form_file_ids)),
            "requirements": requirements,
            "offerings": [LenderOfferingRead.model_validate(o).model_dump(mode="json") for o in all_offerings]})
    return {"profile_id": str(profile.id), "lenders": result}


@router.post("/application-profiles/{profile_id}/lender-collection")
async def collect_lender_requirements(profile_id: UUID, payload: SubmissionBatchCreate, background: BackgroundTasks, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    service.require_underwriter(user)
    profile = await profiles.load_profile(db, profile_id, user)
    source = await service.source_state(db, profile)
    selected = {s.program_key for s in source["readiness"].selections}
    for target in payload.targets:
        lender = await db.get(Lender, target.lender_id)
        if not lender or not lender.is_active:
            raise HTTPException(422, "A selected lender is unavailable")
        offering = await service.selected_offering(db, target, selected)
        await service.materialize_lender_requirements(db, profile, offering, lender)
    await db.commit()
    from app.routers.application_collection import deliver_due_profile
    background.add_task(deliver_due_profile, profile.id)
    return {"status": "collection_tasks_created", "lender_count": len(payload.targets)}


@router.post("/application-profiles/{profile_id}/submission-batches", response_model=SubmissionBatchRead, status_code=201)
async def prepare(profile_id: UUID, payload: SubmissionBatchCreate, background: BackgroundTasks, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    service.require_underwriter(user)
    profile = await profiles.load_profile(db, profile_id, user)
    batch = await service.prepare_batch(db, profile, user, payload)
    result = await service.read_batch(db, batch)
    await db.commit()
    from app.routers.application_collection import deliver_due_profile
    background.add_task(deliver_due_profile, profile.id)
    return result


@router.get("/application-profiles/{profile_id}/submission-batches", response_model=list[SubmissionBatchRead])
async def batches(profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    service.require_underwriter(user)
    await profiles.load_profile(db, profile_id, user)
    rows = list((await db.scalars(select(SubmissionBatch).where(SubmissionBatch.profile_id == profile_id).order_by(SubmissionBatch.created_at.desc()).limit(30))).all())
    return [await service.read_batch(db, row) for row in rows]


async def load_batch(db, profile_id, batch_id, user):
    service.require_underwriter(user)
    profile = await profiles.load_profile(db, profile_id, user)
    batch = await db.get(SubmissionBatch, batch_id)
    if batch is None or batch.profile_id != profile.id:
        raise HTTPException(404, "Submission batch not found")
    return profile, batch


@router.get("/application-profiles/{profile_id}/submission-batches/{batch_id}", response_model=SubmissionBatchRead)
async def batch_read(profile_id: UUID, batch_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    _, batch = await load_batch(db, profile_id, batch_id, user)
    return await service.read_batch(db, batch)


@router.post("/application-profiles/{profile_id}/submission-batches/{batch_id}/approve", response_model=SubmissionBatchRead)
async def approve(profile_id: UUID, batch_id: UUID, payload: SubmissionApproval, background: BackgroundTasks, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    profile, batch = await load_batch(db, profile_id, batch_id, user)
    result = await service.approve_batch(db, profile, batch, user, payload.submission_ids)
    await db.commit()
    background.add_task(service.drain_delivery_queue)
    return result


async def load_child(db, profile_id, batch_id, submission_id, user):
    _, batch = await load_batch(db, profile_id, batch_id, user)
    row = await db.get(LenderSubmission, submission_id)
    if row is None or row.batch_id != batch.id:
        raise HTTPException(404, "Lender submission not found")
    return row


@router.post("/application-profiles/{profile_id}/submission-batches/{batch_id}/{submission_id}/revoke")
async def revoke(profile_id: UUID, batch_id: UUID, submission_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    row = await load_child(db, profile_id, batch_id, submission_id, user)
    row.revoked_at = service.now()
    row.status = "revoked"
    service.event(db, row, "revoked", user.id)
    return {"status": "revoked"}


@router.get("/application-profiles/{profile_id}/submission-batches/{batch_id}/{submission_id}/events")
async def events(profile_id: UUID, batch_id: UUID, submission_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    row = await load_child(db, profile_id, batch_id, submission_id, user)
    items = list((await db.scalars(select(LenderSubmissionEvent).where(LenderSubmissionEvent.submission_id == row.id).order_by(LenderSubmissionEvent.occurred_at))).all())
    return [{"event": item.event, "occurred_at": item.occurred_at, "detail": item.detail} for item in items]


@router.get("/application-profiles/{profile_id}/submission-batches/{batch_id}/{submission_id}/export")
async def export(profile_id: UUID, batch_id: UUID, submission_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    row = await load_child(db, profile_id, batch_id, submission_id, user)
    preview = bool(row.blockers)
    data = await service.archive_bytes(db, row, preview=preview)
    service.event(db, row, "internal_export", user.id, preview=preview)
    return StreamingResponse(io.BytesIO(data), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="lender-package-{row.id}{"-preview" if preview else ""}.zip"', "Cache-Control": "no-store"})


class DeliveryReconciliation(BaseModel):
    status: Literal["delivered", "confirmed_not_sent"]
    source_reference: str = Field(min_length=5, max_length=1000)


@router.post("/application-profiles/{profile_id}/submission-batches/{batch_id}/{submission_id}/reconcile")
async def reconcile(profile_id: UUID, batch_id: UUID, submission_id: UUID, payload: DeliveryReconciliation, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    row = await load_child(db, profile_id, batch_id, submission_id, user)
    if row.status not in service.SENT | {"delivery_unknown"}:
        raise HTTPException(409, "This submission has no provider delivery to reconcile")
    if payload.status == "delivered":
        row.delivered_at = service.now()
        if row.status not in {"viewed", "downloaded"}:
            row.status = "delivered"
    elif row.status == "delivery_unknown":
        row.status = "failed"
        row.last_error = "Reconciled as not sent; eligible for retry"
    else:
        raise HTTPException(409, "A confirmed send cannot be marked unsent")
    service.event(db, row, "delivery_reconciled", user.id, status=payload.status, source_reference=payload.source_reference)
    return {"status": row.status}


@public_router.get("/{token}")
async def public_read(token: str, response: Response, db: AsyncSession = Depends(get_db)):
    row = await service.public_submission(db, token)
    if row.viewed_at is None:
        row.viewed_at = service.now()
        if row.status != "downloaded":
            row.status = "viewed"
        service.event(db, row, "viewed")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {"lender_name": row.lender_name, "expires_at": row.expires_at, "version": row.version, "manifest": row.manifest}


@public_router.get("/{token}/export")
async def public_export(token: str, db: AsyncSession = Depends(get_db)):
    row = await service.public_submission(db, token)
    data = await service.archive_bytes(db, row)
    row.downloaded_at = service.now()
    row.status = "downloaded"
    service.event(db, row, "downloaded")
    await db.commit()
    return StreamingResponse(io.BytesIO(data), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="financing-review-package.zip"', "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@public_router.get("/{token}/files/{file_id}")
async def public_file(token: str, file_id: UUID, db: AsyncSession = Depends(get_db)):
    row = await service.public_submission(db, token)
    snapshot = next((item for item in row.manifest.get("files", []) if item["id"] == str(file_id)), None)
    if snapshot is None:
        raise HTTPException(404, "Document is not part of this lender package")
    file = await db.get(BucketFile, file_id)
    if file is None or file.deleted_at or file.status != "uploaded":
        raise HTTPException(410, "Document is no longer available")
    raw = await asyncio.to_thread(service._read_file, file, snapshot)
    service.event(db, row, "file_downloaded", file_id=str(file.id))
    row.downloaded_at = service.now()
    row.status = "downloaded"
    return StreamingResponse(io.BytesIO(raw), media_type="application/octet-stream", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(service._safe_name(file.file_name, str(file.id))), "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
