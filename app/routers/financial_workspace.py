"""Scoped staff workspace: drafts do not change filed financial versions."""

import re
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.schemas.financial_workspace import (
    DebtBatchApply,
    DebtBatchCreate,
    DebtBatchRefresh,
    DebtBatchRetry,
    FinancialDraftWrite,
    FinancialFamilyCreate,
    FinancialImportApply,
    FinancialImportCreate,
    FinancialKind,
    FinancialVersionRestore,
    FinancialVersionSave,
)
from app.services import application_profiles as profiles
from app.services import financial_workspace as workspace

router = APIRouter(prefix="/application-profiles/{profile_id}/financial-workspace", tags=["financial-workspace"])
Db = Annotated[AsyncSession, Depends(get_db)]


async def _batch_profile(db, profile_id, user):
    from app.services.financial_debt_batch_imports import _staff

    _staff(user)
    return await profiles.load_profile(db, profile_id, user)


@router.post("/debt-batch-imports")
async def create_debt_batch(profile_id: UUID, payload: DebtBatchCreate, background: BackgroundTasks, user: CurrentUser, db: Db) -> dict:
    from app.services import financial_debt_batch_imports as batches

    profile = await _batch_profile(db, profile_id, user)
    result = await batches.create(db, profile, **payload.model_dump(), actor=user)
    await db.commit()
    if result["status"] in {"queued", "processing"}:
        background.add_task(batches.run_batch, UUID(result["id"]))
    return result


@router.get("/debt-batch-imports")
async def list_debt_batches(profile_id: UUID, user: CurrentUser, db: Db, family_id: UUID | None = None) -> dict:
    from app.services import financial_debt_batch_imports as batches

    profile = await _batch_profile(db, profile_id, user)
    return await batches.list_reviews(db, profile, family_id=family_id)


@router.get("/debt-batch-imports/{batch_id}")
async def read_debt_batch(profile_id: UUID, batch_id: UUID, user: CurrentUser, db: Db) -> dict:
    from app.services import financial_debt_batch_imports as batches

    profile = await _batch_profile(db, profile_id, user)
    return await batches.get(db, profile, batch_id)


@router.post("/debt-batch-imports/{batch_id}/retry")
async def retry_debt_batch(profile_id: UUID, batch_id: UUID, payload: DebtBatchRetry, background: BackgroundTasks, user: CurrentUser, db: Db) -> dict:
    from app.services import financial_debt_batch_imports as batches

    profile = await _batch_profile(db, profile_id, user)
    result = await batches.retry(db, profile, batch_id, **payload.model_dump(), actor=user)
    await db.commit()
    if result["status"] == "queued":
        background.add_task(batches.run_batch, batch_id)
    return result


@router.post("/debt-batch-imports/{batch_id}/refresh")
async def refresh_debt_batch(profile_id: UUID, batch_id: UUID, payload: DebtBatchRefresh, background: BackgroundTasks, user: CurrentUser, db: Db) -> dict:
    from app.services import financial_debt_batch_imports as batches

    profile = await _batch_profile(db, profile_id, user)
    result = await batches.refresh(db, profile, batch_id, **payload.model_dump(), actor=user)
    await db.commit()
    if result["status"] in {"queued", "processing"}:
        background.add_task(batches.run_batch, UUID(result["id"]))
    return result


@router.post("/debt-batch-imports/{batch_id}/apply")
async def apply_debt_batch(profile_id: UUID, batch_id: UUID, payload: DebtBatchApply, user: CurrentUser, db: Db) -> dict:
    from app.services import financial_debt_batch_imports as batches

    profile = await _batch_profile(db, profile_id, user)
    result = await batches.apply(db, profile, batch_id, **payload.model_dump(), actor=user)
    await profiles.log_profile_action(db, profile, user, "financial_debt.batch_applied", "Applied reviewed batch debt sources to the working draft", target_type="financial_import", target_id=batch_id)
    await db.commit()
    return result


class DebtCandidateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family_id: UUID | None = None
    expected_revision: int = Field(ge=0)
    candidate_key: str = Field(min_length=1, max_length=100)
    action: Literal["include", "disable", "restore", "link_existing"]
    target_row_id: UUID | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)


class DebtIntakePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family_id: UUID | None = None
    expected_revision: int = Field(ge=0)
    mode: Literal["documents", "banking"]


class DebtIntakeApply(DebtIntakePreview):
    selected_ids: list[str] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)


@router.post("/debt-intake/proposals")
async def preview_debt_intake(profile_id: UUID, payload: DebtIntakePreview, user: CurrentUser, db: Db) -> dict:
    from app.services import financial_debt_intake

    profile = await _profile(db, profile_id, user)
    result = await financial_debt_intake.proposals(db, profile, **payload.model_dump(), actor=user)
    await db.commit()
    return result


@router.post("/debt-intake/apply")
async def apply_debt_intake(profile_id: UUID, payload: DebtIntakeApply, user: CurrentUser, db: Db) -> dict:
    from app.services import financial_debt_intake

    profile = await _profile(db, profile_id, user)
    result = await financial_debt_intake.apply(db, profile, **payload.model_dump(), actor=user)
    await profiles.log_profile_action(db, profile, user, "financial_debt.intake_applied", "Applied reviewed source proposals to the working debt schedule", target_type="financial_form", target_id=UUID(result["family_id"]), metadata={"mode": payload.mode, "selected_count": len(payload.selected_ids)})
    await db.commit()
    return result


@router.get("/debt-review")
async def read_debt_review(profile_id: UUID, user: CurrentUser, db: Db, family_id: UUID | None = None) -> dict:
    from app.services import financial_debt_review

    profile = await _profile(db, profile_id, user)
    result = await financial_debt_review.read_review(db, profile, family_id=family_id, actor=user)
    await db.commit()
    return result


@router.post("/debt-review/actions")
async def act_on_debt_candidate(profile_id: UUID, payload: DebtCandidateAction, user: CurrentUser, db: Db) -> dict:
    from app.services import financial_debt_review

    profile = await _profile(db, profile_id, user)
    result = await financial_debt_review.act(db, profile, **payload.model_dump(), actor=user)
    await profiles.log_profile_action(db, profile, user, "financial_debt.observed_candidate_reviewed", "Reviewed an observed payment against the working debt schedule", target_type="financial_form", target_id=UUID(result["workspace"]["family_id"]), metadata={"candidate_key": payload.candidate_key, "action": payload.action})
    await db.commit()
    return result


async def _profile(db, profile_id, user):
    if user.role in {Role.CLIENT, Role.DEALER, Role.VENDOR, Role.LENDER}:
        raise HTTPException(403, "Financial workspace requires a staff account")
    return await profiles.load_profile(db, profile_id, user)


@router.get("/evidence/{file_id}/image-preview")
async def preview_source_image(profile_id: UUID, file_id: UUID, user: CurrentUser, db: Db,
                               expected_content_hash: Annotated[str | None, Query(pattern=r"^[a-fA-F0-9]{64}$")] = None) -> Response:
    from app.routers.application_profiles import _can_review_manual_bank_evidence
    from app.services.financial_source_images import scoped_image_preview

    if not _can_review_manual_bank_evidence(user):
        raise HTTPException(403, "Evidence review role required")
    profile = await profiles.load_profile(db, profile_id, user)
    raw, content_hash = await scoped_image_preview(db, profile, file_id, expected_content_hash=expected_content_hash)
    return Response(raw, media_type="image/jpeg", headers={"Cache-Control": "no-store",
        "Content-Disposition": 'inline; filename="loan-image-preview.jpg"', "X-Source-Content-Hash": content_hash})


@router.get("")
async def read_workspace(profile_id: UUID, kind: FinancialKind, user: CurrentUser, db: Db, family_id: UUID | None = None) -> dict:
    profile = await _profile(db, profile_id, user)
    result = await workspace.load_workspace(db, profile, kind=kind, family_id=family_id, actor=user)
    await db.commit()
    return result


@router.put("/draft")
async def write_draft(profile_id: UUID, payload: FinancialDraftWrite, user: CurrentUser, db: Db) -> dict:
    profile = await _profile(db, profile_id, user)
    result = await workspace.save_draft(db, profile, **payload.model_dump(), actor=user)
    await db.commit()
    return result


@router.post("/families")
async def create_family(profile_id: UUID, payload: FinancialFamilyCreate, user: CurrentUser, db: Db) -> dict:
    profile = await _profile(db, profile_id, user)
    result = await workspace.create_family(db, profile, **payload.model_dump(), actor=user)
    await db.commit()
    return result


@router.post("/versions")
async def save_version(profile_id: UUID, payload: FinancialVersionSave, user: CurrentUser, db: Db) -> dict:
    profile = await _profile(db, profile_id, user)
    result = await workspace.publish_version(db, profile, **payload.model_dump(), actor=user)
    saved = result["saved_version"]
    await profiles.log_profile_action(db, profile, user, "financial_form.version_saved", f"Saved {workspace.LABELS[payload.kind]} v{saved['version_number']}", target_type="financial_version", target_id=UUID(saved["id"]), metadata={"kind": payload.kind, "family_id": result["family_id"]})
    await db.commit()
    return result


@router.get("/versions/{version_id}/pdf")
async def read_version_pdf(profile_id: UUID, version_id: UUID, user: CurrentUser, db: Db) -> Response:
    profile = await _profile(db, profile_id, user)
    content, filename = await workspace.version_pdf(db, profile, version_id)
    filename = filename.replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "-", filename) or "financial-version.pdf"
    return Response(content, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}', "Cache-Control": "no-store"})


class FinancialVersionShareWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_days: int = Field(default=7, ge=1, le=30)


@router.post("/versions/{version_id}/share")
async def share_version_pdf(profile_id: UUID, version_id: UUID, user: CurrentUser, db: Db,
                            payload: FinancialVersionShareWrite | None = None) -> dict:
    from app.services.financial_version_shares import create_share

    profile = await _profile(db, profile_id, user)
    result = await create_share(db, profile, version_id, ttl_days=payload.ttl_days if payload else 7)
    await profiles.log_profile_action(db, profile, user, "financial_form.version_shared", "Created a read-only link to a saved financial PDF",
                                     target_type="financial_version", target_id=version_id,
                                     metadata={"share_id": result["id"], "expires_at": result["expires_at"]})
    await db.commit()
    return result


@router.post("/versions/{version_id}/restore")
async def restore_version(profile_id: UUID, version_id: UUID, payload: FinancialVersionRestore, user: CurrentUser, db: Db) -> dict:
    profile = await _profile(db, profile_id, user)
    result = await workspace.restore_version(db, profile, version_id, **payload.model_dump(), actor=user)
    await db.commit()
    return result


@router.post("/imports")
async def create_import(profile_id: UUID, payload: FinancialImportCreate, user: CurrentUser, db: Db) -> dict:
    profile = await _profile(db, profile_id, user)
    result = await workspace.propose_import(db, profile, **payload.model_dump(), actor=user)
    await db.commit()
    return result


@router.post("/imports/{import_id}/apply")
async def apply_import(profile_id: UUID, import_id: UUID, payload: FinancialImportApply, user: CurrentUser, db: Db) -> dict:
    profile = await _profile(db, profile_id, user)
    result = await workspace.apply_import(db, profile, import_id, **payload.model_dump(), actor=user)
    await db.commit()
    return result
