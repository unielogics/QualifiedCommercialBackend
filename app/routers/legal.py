"""Legal acceptance router.

Two endpoints:
  - POST /legal/accept   — record that the current user accepted the
                           supplied document versions. Captures the
                           request's IP + User-Agent for audit.
  - GET  /legal/acceptance — return the user's most recent acceptance
                           (used by the UI to know whether to re-prompt
                           when the Effective Date is bumped).
  - GET  /legal/acknowledgment — what the first-login acknowledgment screen
                           shows a team login: the versions in force, the
                           documents, the latest row, and the linked
                           company's signed agreement. /auth/me carries the
                           flag that decides whether the screen renders.
"""

# ruff: noqa: B008

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.models.legal_acceptance import LegalAcceptance
from app.schemas.common import ORMModel
from app.services import user_acknowledgment as ack

router = APIRouter(prefix="/legal", tags=["legal"])


class AcceptRequest(BaseModel):
    terms_version: str = Field(min_length=1, max_length=32)
    privacy_version: str = Field(min_length=1, max_length=32)
    # Funding/AI/Communications Disclosure was added in the v1.0 (2026-05-19)
    # deploy. Optional so older clients can still POST {terms, privacy} —
    # they just won't get the disclosure column populated.
    disclosure_version: str | None = Field(default=None, max_length=32)


class AcceptanceRead(ORMModel):
    id: UUID
    user_id: UUID
    terms_version: str
    privacy_version: str
    disclosure_version: str | None
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
        disclosure_version=body.disclosure_version,
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return AcceptanceRead.model_validate(row)


class DocumentRead(BaseModel):
    key: str
    title: str
    version: str
    url: str


class CompanyAgreementRead(BaseModel):
    company_name: str
    title: str
    contract_number: str
    signed_at: datetime | None


class AcknowledgmentRead(BaseModel):
    status: str
    current: dict[str, str]
    documents: list[DocumentRead]
    latest: AcceptanceRead | None
    company_agreement: CompanyAgreementRead | None


@router.get("/acknowledgment", response_model=AcknowledgmentRead)
async def acknowledgment(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> AcknowledgmentRead:
    """What the acknowledgment screen shows the current user. The apps POST
    back exactly the `current` versions handed to them, so they never compare
    versions themselves."""
    latest = await ack.latest_acceptance(db, user.id)
    docs = ack.documents(get_settings().frontend_app_url)
    company = await ack.company_agreement_on_file(db, user)
    return AcknowledgmentRead(
        status=ack.acknowledgment_status(user, latest),
        current=ack.current_versions(),
        documents=[DocumentRead(**d) for d in docs],
        latest=AcceptanceRead.model_validate(latest) if latest else None,
        company_agreement=CompanyAgreementRead(
            company_name=company.company_name, title=company.title,
            contract_number=company.contract_number, signed_at=company.signed_at,
        ) if company else None,
    )


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
