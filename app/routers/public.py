"""Public, UNauthenticated endpoints for the marketing site (QCWeb).

Two surfaces, both intentionally auth-free (the public site has no
logged-in user):

  * GET  /public/fred/series   — read-only last-N-day FRED series for
                                 the program-page rate charts. Mirrors
                                 the authed /fred/series shape but never
                                 triggers a refresh and exposes nothing
                                 beyond the already-public index values.
  * POST /public/investor-inquiry — full-screen "For Investors" form;
                                 emails the lead to franco@ and logs an
                                 Activity row so nothing is lost even if
                                 mail delivery is unconfigured.

Kept deliberately small + defensive (length caps, consent gate, a
best-effort per-IP throttle). No DB writes other than the Activity log.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.activity import Activity
from app.routers.fred import _build_summary, _current_spreads
from app.schemas.fred import FredSeriesSummary
from app.services import fred as fred_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])

INVESTOR_INBOX = "franco@qualifiedcommercial.com"
SUPPORT_INBOX = "support@qualifiedcommercial.com"

# Best-effort in-memory throttle (single-instance deploy — see scheduler
# note in app/services/scheduler.py). Maps client IP → last submit ts.
_LAST_SUBMIT: dict[str, float] = {}
_THROTTLE_SECONDS = 20.0


@router.get("/fred/series", response_model=list[FredSeriesSummary])
async def public_fred_series(
    db: AsyncSession = Depends(get_db),
    days: int = 30,
) -> list[FredSeriesSummary]:
    """Unauthenticated bundled FRED summary for the public program-page
    rate charts. Read-only; returns whatever the daily refresh has
    populated (empty list-friendly if the table is bare)."""
    requested_days = max(1, min(days, 90))
    deepest = max(requested_days, 30)
    spreads = await _current_spreads(db)
    out: list[FredSeriesSummary] = []
    for series_id in fred_service.SERIES_IDS:
        history = await fred_service.get_history(db, series_id, days=deepest)
        out.append(
            _build_summary(series_id, history, spreads.get(series_id), requested_days)
        )
    return out


class InvestorInquiry(BaseModel):
    investor_type: str = Field(min_length=1, max_length=80)
    funding_to_deploy: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4000)
    full_name: str = Field(min_length=1, max_length=160)
    phone: str = Field(min_length=3, max_length=40)
    consent: bool


class InvestorInquiryResult(BaseModel):
    ok: bool


@router.post("/investor-inquiry", response_model=InvestorInquiryResult)
async def investor_inquiry(
    payload: InvestorInquiry,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> InvestorInquiryResult:
    """Public 'For Investors' lead form. Emails franco@ and logs an
    Activity row. Consent is mandatory."""
    if payload.consent is not True:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Consent to be contacted is required.",
        )

    ip = (request.client.host if request.client else "?") or "?"
    now = time.monotonic()
    last = _LAST_SUBMIT.get(ip)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Please wait a moment before submitting again.",
        )
    _LAST_SUBMIT[ip] = now

    subject = f"New investor inquiry — {payload.full_name}"
    mail_body = (
        f"Name: {payload.full_name}\n"
        f"Phone: {payload.phone}\n"
        f"Investor type: {payload.investor_type}\n"
        f"Funding to deploy: {payload.funding_to_deploy}\n"
        f"Subject: {payload.title}\n\n"
        f"{payload.body}\n"
    )

    sent = False
    try:
        from app.services.email.gmail_client import gmail_config, send_message

        cfg = gmail_config()
        if cfg is not None:
            send_message(cfg, to=INVESTOR_INBOX, subject=subject, body=mail_body)
            sent = True
        else:
            log.warning("investor-inquiry: gmail not configured — lead logged only")
    except Exception:
        log.exception("investor-inquiry: email send failed — lead still logged")

    # Always persist the lead so it's never lost, regardless of mail state.
    db.add(
        Activity(
            loan_id=None,
            actor_id=None,
            actor_label="public",
            kind="investor.inquiry",
            summary=f"Investor inquiry from {payload.full_name} ({payload.investor_type})",
            payload={
                "full_name": payload.full_name,
                "phone": payload.phone,
                "investor_type": payload.investor_type,
                "funding_to_deploy": payload.funding_to_deploy,
                "title": payload.title,
                "body": payload.body,
                "emailed": sent,
            },
        )
    )
    await db.flush()
    return InvestorInquiryResult(ok=True)


class SupportInquiry(BaseModel):
    """Public /support contact form. Same defensive shape as the
    investor inquiry — length caps + mandatory consent — plus an
    explicit `email` field so the inbox can simply hit Reply (the
    investor flow gates on phone instead)."""

    full_name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=5, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    topic: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4000)
    consent: bool


class SupportInquiryResult(BaseModel):
    ok: bool


@router.post("/support-inquiry", response_model=SupportInquiryResult)
async def support_inquiry(
    payload: SupportInquiry,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SupportInquiryResult:
    """Public /support contact form on qualifiedcommercial.com. Mirrors
    `/public/investor-inquiry`: emails support@ via the existing Gmail
    relay (best-effort), always logs an Activity row so the inquiry is
    never lost when mail is misconfigured, enforces a 20-second per-IP
    throttle to deter abuse."""
    if payload.consent is not True:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Consent to be contacted is required.",
        )
    # Lightweight email sanity (no pydantic[email] dep — the recipient
    # is a human, format errors surface on the reply path).
    if "@" not in payload.email or "." not in payload.email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A valid email is required so we can reply.",
        )

    ip = (request.client.host if request.client else "?") or "?"
    now = time.monotonic()
    last = _LAST_SUBMIT.get(ip)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Please wait a moment before submitting again.",
        )
    _LAST_SUBMIT[ip] = now

    subject = f"New support inquiry — {payload.full_name}"
    mail_body = (
        f"Name: {payload.full_name}\n"
        f"Email: {payload.email}\n"
        f"Phone: {payload.phone or '(not provided)'}\n"
        f"Topic: {payload.topic}\n"
        f"Subject: {payload.title}\n\n"
        f"{payload.body}\n"
    )

    sent = False
    try:
        from app.services.email.gmail_client import gmail_config, send_message

        cfg = gmail_config()
        if cfg is not None:
            send_message(cfg, to=SUPPORT_INBOX, subject=subject, body=mail_body)
            sent = True
        else:
            log.warning("support-inquiry: gmail not configured — lead logged only")
    except Exception:
        log.exception("support-inquiry: email send failed — lead still logged")

    db.add(
        Activity(
            loan_id=None,
            actor_id=None,
            actor_label="public",
            kind="support.inquiry",
            summary=f"Support inquiry from {payload.full_name} ({payload.topic})",
            payload={
                "full_name": payload.full_name,
                "email": payload.email,
                "phone": payload.phone,
                "topic": payload.topic,
                "title": payload.title,
                "body": payload.body,
                "emailed": sent,
            },
        )
    )
    await db.flush()
    return SupportInquiryResult(ok=True)
