"""Soft pull endpoints — Module 4 (mobile) + Apply flow gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import SOFT_PULL_VALIDITY_DAYS
from app.db import get_db
from app.deps import CurrentUser
from app.enums import CreditPullStatus
from app.models.credit_pull import CreditPull
from app.schemas.credit import CreditPullRead, CreditPullRequest

router = APIRouter(prefix="/credit", tags=["credit"])


def _client_id_for(user) -> str | None:
    return user.client.id if user.client else None


@router.get("/current", response_model=CreditPullRead | None)
async def current(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> CreditPullRead | None:
    cid = _client_id_for(user)
    if cid is None:
        return None
    stmt = (
        select(CreditPull)
        .where(CreditPull.client_id == cid)
        .where(CreditPull.status == CreditPullStatus.COMPLETED)
        .where(CreditPull.expires_at > datetime.now(timezone.utc))
        .order_by(CreditPull.pulled_at.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    return CreditPullRead.model_validate(row) if row else None


@router.post("/pull", response_model=CreditPullRead)
async def initiate_pull(
    payload: CreditPullRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> CreditPullRead:
    cid = _client_id_for(user)
    if cid is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client profile required")
    if not payload.fcra_consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "FCRA consent required")

    pull = CreditPull(
        client_id=cid,
        status=CreditPullStatus.PENDING,
        legal_first_name=payload.legal_first_name,
        legal_last_name=payload.legal_last_name,
        dob=payload.dob,
        street=payload.street,
        city=payload.city,
        state=payload.state,
        zip=payload.zip,
        phone=payload.phone,
        email=payload.email,
        last4_ssn=payload.last4_ssn,
        fcra_consent=True,
    )
    db.add(pull)
    await db.flush()

    settings = get_settings()
    if settings.isoftpull_api_key:
        # TODO: real iSoftpull call. Synchronous for now (small payload):
        # import httpx
        # r = await httpx.AsyncClient(...).post(settings.isoftpull_api_url + "/pulls", ...)
        # pull.fico = r.json()["fico"]; pull.bureau_response = r.json()
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "iSoftpull integration is wired in code but the live HTTP call is not enabled "
            "yet (will be turned on when keys are provided + endpoint contract confirmed).",
        )

    # Dev-only short-circuit so the mobile flow can be exercised end-to-end:
    pull.fico = 712
    pull.status = CreditPullStatus.COMPLETED
    pull.pulled_at = datetime.now(timezone.utc)
    pull.expires_at = pull.pulled_at + timedelta(days=SOFT_PULL_VALIDITY_DAYS)
    pull.notes = "DEV mode — synthetic 712. Wire iSoftpull keys to enable real pulls."
    if user.client:
        user.client.fico = 712
    await db.flush()
    await db.refresh(pull)
    return CreditPullRead.model_validate(pull)
