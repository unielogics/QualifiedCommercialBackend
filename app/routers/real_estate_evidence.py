"""Staff-reviewed property/loan-specific mortgage statement evidence."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.routers.financial_workspace import _profile
from app.schemas.real_estate_evidence import (
    MortgageStatementApply,
    MortgageStatementImport,
    MortgageStatementUnlink,
)
from app.services import real_estate_evidence as evidence

router = APIRouter(prefix="/application-profiles/{profile_id}/financial-workspace/real-estate-evidence", tags=["financial-workspace"])
Db = Annotated[AsyncSession, Depends(get_db)]


async def _staff_profile(db, profile_id, user):
    evidence.require_staff(user)
    return await _profile(db, profile_id, user)


@router.get("")
async def read_evidence(profile_id: UUID, family_id: UUID, user: CurrentUser, db: Db) -> dict:
    profile = await _staff_profile(db, profile_id, user)
    result = await evidence.list_evidence(db, profile, family_id=family_id, actor=user)
    await db.commit()
    return result


@router.post("/imports")
async def propose_import(profile_id: UUID, payload: MortgageStatementImport, user: CurrentUser, db: Db) -> dict:
    profile = await _staff_profile(db, profile_id, user)
    result = await evidence.propose_import(db, profile, **payload.model_dump(), actor=user)
    await db.commit()
    return result


@router.post("/imports/{import_id}/apply")
async def apply_import(profile_id: UUID, import_id: UUID, payload: MortgageStatementApply, user: CurrentUser, db: Db) -> dict:
    profile = await _staff_profile(db, profile_id, user)
    result = await evidence.apply_import(db, profile, import_id, **payload.model_dump(), actor=user)
    await db.commit()
    return result


@router.post("/imports/{import_id}/reject")
async def reject_import(profile_id: UUID, import_id: UUID, user: CurrentUser, db: Db) -> dict:
    profile = await _staff_profile(db, profile_id, user)
    result = await evidence.reject_import(db, profile, import_id, actor=user)
    await db.commit()
    return result


@router.post("/unlink")
async def unlink_evidence(profile_id: UUID, payload: MortgageStatementUnlink, user: CurrentUser, db: Db) -> dict:
    profile = await _staff_profile(db, profile_id, user)
    result = await evidence.unlink_evidence(db, profile, **payload.model_dump(), actor=user)
    await db.commit()
    return result
