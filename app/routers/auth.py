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
from app.services import user_acknowledgment as ack
from app.services.user_access import account_types, console_keys, console_links, has_product_access
from app.services.user_phone import needs_phone

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupAttribution(BaseModel):
    source: str = Field(default="public_site", max_length=64)
    page: str | None = Field(default=None, max_length=300)
    program: str | None = Field(default=None, max_length=120)
    vertical: str | None = Field(default=None, max_length=64)
    campaign: str | None = Field(default=None, max_length=120)
    cta: str | None = Field(default=None, max_length=120)


class ConsoleLink(BaseModel):
    key: str
    label: str
    url: str


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
    account_types: list[str]
    account_status: str
    # An operator's own contact details. The Production Package names the
    # relationship manager and their phone on both agreements.
    phone: str | None = None
    title: str | None = None
    # The one-time gate both apps show a rep, an underwriter or a super admin
    # with no mobile on file. Computed here so the two apps cannot disagree.
    needs_phone: bool = False
    # The platform-document acknowledgment every console shows a team login
    # once, and once more after a version bump. Computed here so the three
    # apps cannot disagree.
    needs_acknowledgment: bool = False
    can_access_funding: bool
    can_access_audit: bool
    # The consoles this login may sign in to — Funding, Field Desk, Audit —
    # with their URLs. The frontends render the switcher from it and show an
    # entry notice when their own key is absent. For operator roles it is
    # advisory (which sign-ins to offer); the server-enforced boundaries stay
    # the dealer-OS rep/team guards and the external product boundary.
    consoles: list[ConsoleLink] = Field(default_factory=list)


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> MeResponse:
    products = account_types(user)
    effective_account_types = sorted(
        {*console_keys(user), *(product.value for product in products)}
    )
    latest = await ack.latest_acceptance(db, user.id)
    return MeResponse(
        id=str(user.id),
        clerk_id=user.clerk_id,
        email=user.email,
        name=user.name,
        role=user.role,
        referral_partner_company_id=user.referral_partner_company_id,
        account_types=effective_account_types,
        account_status=user.account_status,
        phone=user.phone,
        title=user.title,
        needs_phone=needs_phone(user),
        needs_acknowledgment=ack.needs_acknowledgment(user, latest),
        can_access_funding=has_product_access(user, ProductAccountType.FUNDING),
        can_access_audit=has_product_access(user, ProductAccountType.AUDIT),
        consoles=[ConsoleLink(**c) for c in console_links(user)],
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
