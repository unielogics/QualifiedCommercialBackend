"""Legal acceptance router.

Two endpoints:
  - POST /legal/accept   — record that the current user accepted the
                           supplied document versions. Captures the
                           request's IP + User-Agent for audit.
  - GET  /legal/acceptance — return the user's most recent acceptance
                           (used by the UI to know whether to re-prompt
                           when the Effective Date is bumped).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.models.legal_acceptance import LegalAcceptance
from app.schemas.common import ORMModel

router = APIRouter(prefix="/legal", tags=["legal"])


class AcceptRequest(BaseModel):
    terms_version: str = Field(min_length=1, max_length=32)
    privacy_version: str = Field(min_length=1, max_length=32)


class AcceptanceRead(ORMModel):
    id: UUID
    user_id: UUID
    terms_version: str
    privacy_version: str
    ip_address: str | None
    user_agent: str | None
    created_at: datetime


def _client_ip(request: Request) -> str | None:
    """Extract the real client IP behind common proxies (CloudFront / ALB /
    Nginx / Cloudflare). Falls back to request.client.host."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # x-forwarded-for is comma-separated — first entry is the original client.
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else None


@router.post("/accept", response_model=AcceptanceRead, status_code=201)
async def accept(
    body: AcceptRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AcceptanceRead:
    """Record a legal-document acceptance for the current user.

    Idempotent-ish: every call writes a fresh row. Calling this on every
    sign-in is intentional — duplicates are cheap and the latest row is
    the one the UI cares about. (We may de-dup by version later.)
    """
    row = LegalAcceptance(
        user_id=user.id,
        terms_version=body.terms_version,
        privacy_version=body.privacy_version,
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return AcceptanceRead.model_validate(row)


@router.get("/acceptance", response_model=AcceptanceRead | None)
async def latest_acceptance(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> AcceptanceRead | None:
    """Most recent acceptance for the current user, or null if they
    haven't accepted yet."""
    row = (
        await db.execute(
            select(LegalAcceptance)
            .where(LegalAcceptance.user_id == user.id)
            .order_by(LegalAcceptance.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return AcceptanceRead.model_validate(row) if row else None
