"""Staff banking reconciliation for a scoped application file."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.services import application_profiles as profiles
from app.services import debt_payee_links

router = APIRouter(prefix="/application-profiles/{profile_id}/banking", tags=["application-banking"])
Db = Annotated[AsyncSession, Depends(get_db)]


class PayeeLinkWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_key: str | None = Field(max_length=60)


@router.get("/debt-payees")
async def read_debt_payees(profile_id: UUID, user: CurrentUser, db: Db) -> dict:
    from app.routers.application_profiles import _require_statement_staff

    _require_statement_staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    return await debt_payee_links.inventory(db, profile)


@router.put("/debt-payees/{row_id}")
async def write_debt_payee(profile_id: UUID, row_id: UUID, payload: PayeeLinkWrite, user: CurrentUser, db: Db) -> dict:
    from app.routers.application_profiles import _require_statement_staff

    _require_statement_staff(user)
    profile = await profiles.load_profile(db, profile_id, user)
    result = await debt_payee_links.link_payee(db, profile, row_id, payload.vendor_key, actor=user)
    await profiles.log_profile_action(db, profile, user, "banking.debt_payee_linked", "Updated an obligation's observed bank payee",
                                     target_type="financial_debt", target_id=row_id, metadata={"vendor_key": payload.vendor_key})
    await db.commit()
    return result
