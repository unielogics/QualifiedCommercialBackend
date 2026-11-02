"""POST /capital/public/access-request: the public "Request an introduction" form.

No account, no offering information in either direction. The row feeds the
desk's queue (Marketplace > Members > Access requests) and an email goes to
the issuer's notify list. Consent is mandatory, the honeypot drops bots
quietly, and one IP may submit once every 30 seconds.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.activity import Activity
from app.services.payment_authorization import client_ip

from . import schemas as S
from .models import CpAccessRequest
from .services import emails
from .services.members import issuer_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/capital/public", tags=["capital-public"])

_LAST_SUBMIT: dict[str, float] = {}
_THROTTLE_SECONDS = 30.0
#: Where requests go when the issuer has not set a notify list yet.
FALLBACK_DESK_INBOX = "franco@qualifiedcommercial.com"


@router.post("/access-request", response_model=S.AccessRequestResult, status_code=status.HTTP_201_CREATED)
async def create_access_request(
    payload: S.AccessRequestIn,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> S.AccessRequestResult:
    if payload.website:
        # A bot filled the hidden field. Say yes and store nothing.
        return S.AccessRequestResult(ok=True, id=None)
    if payload.consent is not True:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Consent to be contacted is required.")

    ip = client_ip(request) or "?"
    now = time.monotonic()
    last = _LAST_SUBMIT.get(ip)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Please wait a moment before submitting again.")
    _LAST_SUBMIT[ip] = now

    row = CpAccessRequest(
        investing_as=payload.investing_as,
        entity_name=(payload.entity_name or "").strip() or None,
        capital_band=payload.capital_band.strip(),
        source=payload.source.strip(),
        full_name=payload.full_name.strip(),
        email=payload.email,
        phone=payload.phone,
        note=(payload.note or "").strip() or None,
        locale=payload.locale,
        ip_address=ip[:64],
        user_agent=(request.headers.get("user-agent") or "")[:400] or None,
    )
    db.add(row)
    await db.flush()

    settings_row = await issuer_settings(db)
    recipients = [e for e in (settings_row.notify_emails or []) if e] or [FALLBACK_DESK_INBOX]
    sent = await emails.notify_desk(
        db,
        recipients=recipients,
        subject=f"Capital partner introduction request: {row.full_name}",
        body=emails.access_request_desk_body(row),
        template_key="cp.access_request",
    )
    row.emailed = sent > 0

    db.add(
        Activity(
            loan_id=None,
            actor_id=None,
            actor_label="public",
            kind="capital_partner.access_request",
            summary=f"Capital partner introduction request from {row.full_name} ({row.investing_as})",
            payload={
                "access_request_id": str(row.id),
                "full_name": row.full_name,
                "email": row.email,
                "phone": row.phone,
                "investing_as": row.investing_as,
                "entity_name": row.entity_name,
                "capital_band": row.capital_band,
                "source": row.source,
                "locale": row.locale,
                "emailed": row.emailed,
            },
        )
    )
    await db.flush()
    return S.AccessRequestResult(ok=True, id=row.id)
