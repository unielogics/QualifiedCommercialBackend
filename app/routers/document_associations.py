"""Staff review of one document's association with the existing application."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.services import application_profiles, document_associations

router = APIRouter(prefix="/application-profiles/{profile_id}", tags=["document-associations"])
Db = Annotated[AsyncSession, Depends(get_db)]


class DocumentAssociationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["accept", "deny", "reset"]
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)
    source_stamp: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)


@router.get("/document-associations")
async def read_associations(profile_id: UUID, user: CurrentUser, db: Db):
    if user.role not in {Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.BROKER, Role.LOAN_EXEC, Role.FIELD_REP}:
        raise HTTPException(403, "Document identity review requires a staff account")
    profile = await application_profiles.load_profile(db, profile_id, user)
    return await document_associations.read(db, profile)


@router.post("/documents/{file_id}/association")
async def review_association(profile_id: UUID, file_id: UUID, payload: DocumentAssociationWrite, user: CurrentUser, db: Db):
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    profile = await application_profiles.load_profile(db, profile_id, user)
    result = await document_associations.save(db, profile, user, file_id=file_id, **payload.model_dump())
    await db.commit()
    return result
