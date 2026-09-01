"""Auth — Clerk owns sessions; backend exposes /me."""

from __future__ import annotations

# FastAPI dependency declarations intentionally use Depends in defaults.
# ruff: noqa: B008

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import ProductAccountType, Role
from app.schemas.common import ORMModel
from app.services.user_access import account_types, has_product_access

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupAttribution(BaseModel):
    source: str = Field(default="public_site", max_length=64)
    page: str | None = Field(default=None, max_length=300)
    program: str | None = Field(default=None, max_length=120)
    vertical: str | None = Field(default=None, max_length=64)
    campaign: str | None = Field(default=None, max_length=120)
    cta: str | None = Field(default=None, max_length=120)


class MeResponse(ORMModel):
    id: str
    clerk_id: str | None
    email: str
    name: str
    role: Role
    # Only ever set for Role.DEALER_PARTNER. Whether this user (and their
    # company) have the required signed contracts is a separate query — see
    # GET /contracts/{contract_type}/status — not a field on this response,
    # since AppShell's gate needs BOTH the individual Platform Access
    # Agreement AND the company's Referral Protection Agreement status.
    referral_partner_company_id: UUID | None = None
    account_types: list[ProductAccountType]
    account_status: str
    can_access_funding: bool
    can_access_audit: bool


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser) -> MeResponse:
    products = account_types(user)
    return MeResponse(
        id=str(user.id),
        clerk_id=user.clerk_id,
        email=user.email,
        name=user.name,
        role=user.role,
        referral_partner_company_id=user.referral_partner_company_id,
        account_types=products,
        account_status=user.account_status,
        can_access_funding=has_product_access(user, ProductAccountType.FUNDING),
        can_access_audit=has_product_access(user, ProductAccountType.AUDIT),
    )


@router.post("/signup-attribution")
async def record_signup_attribution(
    body: SignupAttribution,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """Persist marketing provenance after Clerk creates the Funding login."""

    if user.client is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Funding client profile is not ready")
    source = (body.source.strip().lower() or "public_site")[:32]
    payload = body.model_dump(exclude_none=True)
    payload["source"] = source
    payload["captured_at"] = datetime.now(UTC).isoformat()
    payload["request_ip"] = (
        (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
        or (request.client.host if request.client else None)
    )
    existing = dict(user.client.lead_intake or {})
    existing["signup_attribution"] = payload
    user.client.lead_intake = existing
    user.client.source_channel = "public_site"
    user.client.lead_source = source
    await db.flush()
    return {"recorded": True}
