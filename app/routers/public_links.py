"""One address for a client's secure link.

Nine kinds of link reach a client today, each with its own path. This route
takes any token and says which screen it opens, so the dashboard's
``/link/{token}`` can send the client there. It answers existence only: the
destination screen still enforces expiry, revocation, PINs and passcodes
exactly as before, so nothing here widens what a token can do.

Plaintext tokens (rooms, shares, HUD) are looked up as stored; the rest are
stored as a SHA-256 hex digest and looked up by it.
"""

from __future__ import annotations

import hashlib
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.application_profile import ApplicationVerificationInvitation
from app.models.bucket import BucketPublicShare, BucketShare, BucketUploadLink
from app.models.financial_form_link import FinancialFormLink
from app.models.hud_share_link import HudShareLink
from app.models.production_package import ProductionPackageShareLink
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.submission_batch import LenderSubmission

router = APIRouter(prefix="/public/links", tags=["public-links"])

PublicLinkKind = Literal[
    "room",
    "share",
    "public_share",
    "hud_share",
    "intake",
    "form",
    "production_package",
    "lender_submission",
    "bank_verification",
]


class PublicLinkResolution(BaseModel):
    kind: PublicLinkKind
    path: str


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def intake_path(intake: PublicUnderwritingIntake, token: str) -> str:
    """The public screen an intake resumes on, by its variant — the same choice
    the resume email makes."""

    from app.routers import dealer_ai_intake as intake_routes

    if intake.variant == intake_routes.FUNDING_VARIANT:
        base = intake_routes.FUNDING_PUBLIC_PATH
    elif intake.variant == intake_routes.MCA_VARIANT:
        base = intake_routes.MCA_PUBLIC_PATH
    else:
        base = intake_routes._dealer_route_public_path(intake)
    return f"{base}?token={quote(token, safe='')}"


def form_path(link: FinancialFormLink, token: str) -> str:
    encoded = quote(token, safe="")
    return f"/forms/worksheet/{encoded}" if link.kind == "worksheet" else f"/forms/{link.kind}/{encoded}"


async def _one(db: AsyncSession, statement):
    return (await db.execute(statement)).scalars().first()


async def resolve_public_link(db: AsyncSession, token: str) -> PublicLinkResolution | None:
    """Which screen a token opens, or None when no kind knows it.

    The order is the order of how often each kind is sent; a token matches at
    most one table because every kind mints its own random value.
    """

    token = token.strip()
    if not token or len(token) > 200:
        return None
    encoded = quote(token, safe="")
    digest = sha256_hex(token)

    if await _one(db, select(BucketUploadLink.id).where(BucketUploadLink.token == token)):
        return PublicLinkResolution(kind="room", path=f"/buckets/request/{encoded}")
    intake = await _one(db, select(PublicUnderwritingIntake).where(PublicUnderwritingIntake.token_hash == digest))
    if intake is not None:
        return PublicLinkResolution(kind="intake", path=intake_path(intake, token))
    form = await _one(db, select(FinancialFormLink).where(FinancialFormLink.token_hash == digest))
    if form is not None:
        return PublicLinkResolution(kind="form", path=form_path(form, token))
    if await _one(db, select(BucketShare.id).where(BucketShare.token == token)):
        return PublicLinkResolution(kind="share", path=f"/buckets/share/{encoded}")
    if await _one(db, select(BucketPublicShare.id).where(BucketPublicShare.token == token)):
        return PublicLinkResolution(kind="public_share", path=f"/buckets/public-share/{encoded}")
    if await _one(db, select(ProductionPackageShareLink.id).where(ProductionPackageShareLink.token_hash == digest)):
        return PublicLinkResolution(kind="production_package", path=f"/production-package/link/{encoded}")
    if await _one(db, select(LenderSubmission.id).where(LenderSubmission.token_hash == digest)):
        return PublicLinkResolution(kind="lender_submission", path=f"/lender-submissions/{encoded}")
    if await _one(db, select(ApplicationVerificationInvitation.id).where(ApplicationVerificationInvitation.token_hash == digest)):
        # That screen reads its token from the URL fragment, so it never reaches a server log.
        return PublicLinkResolution(kind="bank_verification", path=f"/application-verification#t={encoded}")
    if await _one(db, select(HudShareLink.id).where(HudShareLink.token == token)):
        return PublicLinkResolution(kind="hud_share", path=f"/hud/share/{encoded}")
    return None


@router.get("/{token}", response_model=PublicLinkResolution)
async def resolve_link(token: str, db: AsyncSession = Depends(get_db)) -> PublicLinkResolution:
    resolved = await resolve_public_link(db, token)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link is not recognised.")
    return resolved
