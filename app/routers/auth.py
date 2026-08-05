"""Auth — Clerk owns sessions; backend exposes /me."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.deps import CurrentUser
from app.enums import Role
from app.schemas.common import ORMModel

router = APIRouter(prefix="/auth", tags=["auth"])


class MeResponse(ORMModel):
    id: str
    clerk_id: str
    email: str
    name: str
    role: Role
    # Only ever set for Role.DEALER_PARTNER. Whether this user (and their
    # company) have the required signed contracts is a separate query — see
    # GET /contracts/{contract_type}/status — not a field on this response,
    # since AppShell's gate needs BOTH the individual Platform Access
    # Agreement AND the company's Referral Protection Agreement status.
    referral_partner_company_id: UUID | None = None


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser) -> MeResponse:
    return MeResponse(
        id=str(user.id),
        clerk_id=user.clerk_id,
        email=user.email,
        name=user.name,
        role=user.role,
        referral_partner_company_id=user.referral_partner_company_id,
    )
