from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.schemas.financial_debt_actions import FinancialDebtAction, FinancialDebtDocumentImport
from app.services import application_profiles as profiles
from app.services.financial_debt_actions import apply_debt_action

router = APIRouter(prefix="/application-profiles/{profile_id}/financial-workspace/debts", tags=["financial-workspace"])


@router.post("/actions")
async def reconcile_debt(profile_id: UUID, payload: FinancialDebtAction, user: CurrentUser,
                         db: AsyncSession = Depends(get_db)) -> dict:  # noqa: B008
    from app.routers.application_profiles import _require_statement_staff
    from app.services import financial_workspace

    _require_statement_staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    if payload.file_id is not None:
        evidence = await profiles.evidence_state(db, profile)
        if payload.file_id not in {file.id for file in evidence.files}:
            raise HTTPException(404, "Document not found on this file")
    snapshot = await financial_workspace.load_workspace(db, profile, kind="debt_schedule", family_id=payload.family_id, actor=user)
    body = apply_debt_action(snapshot["body"], payload.model_dump(mode="json", exclude_unset=True), actor_id=str(user.id))
    result = await financial_workspace.save_draft(db, profile, kind="debt_schedule", family_id=UUID(snapshot["family_id"]),
                                                 body=body, expected_revision=payload.expected_revision, actor=user)
    await profiles.log_profile_action(db, profile, user, f"financial_debt.{payload.op}", "Updated a financial obligation draft",
                                     target_type="financial_debt", target_id=payload.row_id)
    await db.commit()
    return result


@router.post("/{row_id}/document-imports")
async def read_loan_document_terms(profile_id: UUID, row_id: UUID, payload: FinancialDebtDocumentImport,
                                  user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:  # noqa: B008
    from app.routers.application_profiles import _require_statement_staff
    from app.services.financial_debt_document_imports import propose_document_import

    _require_statement_staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    result = await propose_document_import(db, profile, row_id=row_id, actor=user, **payload.model_dump())
    await db.commit()
    return result
