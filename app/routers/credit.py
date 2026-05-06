"""Soft pull endpoints — Module 4 (mobile) + Apply flow gate."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.config import get_settings
from app.constants import SOFT_PULL_EXPIRING_SOON_DAYS, SOFT_PULL_VALIDITY_DAYS
from app.db import get_db
from app.deps import CurrentUser
from app.enums import CreditPullStatus, Role
from app.models.client import Client
from app.models.credit_pull import CreditPull
from app.schemas.credit import CreditPullRead, CreditPullRequest
from app.services import isoftpull_client
from app.services import isoftpull_report_parser
from app.services.isoftpull_session import IsoftpullSessionError, get_session

router = APIRouter(prefix="/credit", tags=["credit"])
log = logging.getLogger(__name__)


def _client_id_for(user) -> str | None:
    return user.client.id if user.client else None


def _to_read(row: CreditPull) -> CreditPullRead:
    """Attach the derived expiry fields to a CreditPullRead."""
    days: int | None = None
    is_expired = False
    expiring_soon = False
    if row.expires_at is not None:
        delta = row.expires_at - datetime.now(timezone.utc)
        days = max(0, delta.days)
        is_expired = delta.total_seconds() <= 0
        expiring_soon = (not is_expired) and days <= SOFT_PULL_EXPIRING_SOON_DAYS

    base = CreditPullRead.model_validate(row).model_dump()
    base.update(
        is_expired=is_expired,
        days_until_expiry=days,
        expiring_soon=expiring_soon,
    )
    return CreditPullRead(**base)


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
    return _to_read(row) if row else None


@router.post("/pull", response_model=CreditPullRead)
async def initiate_pull(
    payload: CreditPullRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> CreditPullRead:
    cid = _client_id_for(user)
    if cid is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client profile required")
    if not payload.fcra_consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "FCRA consent required")

    # Idempotency: if a PENDING pull was just created (within 60s) we
    # don't want to fire a second bureau request — that doubles the cost
    # AND stamps a duplicate consumer-facing soft pull.
    recent_cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
    recent_pending_stmt = (
        select(CreditPull)
        .where(CreditPull.client_id == cid)
        .where(CreditPull.status == CreditPullStatus.PENDING)
        .where(CreditPull.created_at > recent_cutoff)
        .order_by(CreditPull.created_at.desc())
        .limit(1)
    )
    if (await db.execute(recent_pending_stmt)).scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A credit pull is already in flight — try again in a minute.",
        )

    # FCRA paper trail row. We persist only the last 4 of SSN; the full
    # 9-digit number is forwarded to iSoftPull below and never written to
    # the database or logged. phone / email are derived from the User /
    # Client records, not collected here.
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
        last4_ssn=payload.ssn[-4:],
        fcra_consent=True,
    )
    db.add(pull)
    await db.flush()

    settings = get_settings()
    private_key = settings.isoftpull_private_key or settings.isoftpull_api_key
    public_key = settings.isoftpull_public_key

    if private_key:
        try:
            result = await isoftpull_client.pull(
                public_key=public_key,
                private_key=private_key,
                base_url=settings.isoftpull_api_url,
                applicant=isoftpull_client.ApplicantPayload(
                    legal_first_name=payload.legal_first_name,
                    legal_last_name=payload.legal_last_name,
                    street=payload.street,
                    city=payload.city,
                    state=payload.state,
                    zip=payload.zip,
                    dob=payload.dob.isoformat(),
                    ssn=payload.ssn,  # full 9 digits, used in flight only
                ),
                timeout_seconds=settings.isoftpull_timeout_seconds,
                max_retries=settings.isoftpull_max_retries,
            )
        except isoftpull_client.IsoftpullValidationError as exc:
            pull.status = CreditPullStatus.REVOKED
            pull.notes = f"validation: {exc}"
            await db.flush()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except isoftpull_client.IsoftpullDeniedError as exc:
            pull.status = CreditPullStatus.REVOKED
            pull.notes = f"denied: {exc}"
            await db.flush()
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except isoftpull_client.IsoftpullRateLimitedError as exc:
            pull.status = CreditPullStatus.REVOKED
            pull.notes = f"rate_limited: {exc}"
            await db.flush()
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
        except isoftpull_client.IsoftpullTransportError as exc:
            pull.status = CreditPullStatus.REVOKED
            pull.notes = f"transport: {exc}"
            await db.flush()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Credit bureau is temporarily unavailable — please retry.",
            ) from exc
        except Exception as exc:  # noqa: BLE001 — broad on purpose
            # Catch-all so a programming or transient infra error doesn't
            # leak as an opaque 500 to the mobile app. We still log the
            # full traceback for ops and stamp the row as REVOKED so the
            # 60s dedup window doesn't stick.
            log.exception("Unexpected error during iSoftPull bureau call")
            pull.status = CreditPullStatus.REVOKED
            pull.notes = f"unexpected: {type(exc).__name__}: {exc}"
            await db.flush()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Credit pull failed — please retry. Our team has been notified.",
            ) from exc

        try:
            # FICO sourcing — three-tier fallback:
            #   1. Full Feed JSON (preferred). Once iSoftPull enables it on
            #      the API token, result.fico is populated and we use that.
            #   2. Scrape the report viewer HTML using a server-side login.
            #      Bridge while Full Feed is off — see services/
            #      isoftpull_session.py + isoftpull_report_parser.py.
            #   3. Intelligence-passed proxy (720). Only if both above fail.
            INTELLIGENCE_PASSED_PROXY_FICO = 720
            effective_fico = result.fico
            fico_source = "full_feed" if result.fico is not None else None

            scraped = None
            if effective_fico is None and result.report_link:
                scraped = await isoftpull_report_parser.fetch_and_parse(result.report_link)
                if scraped and scraped.best_score is not None:
                    effective_fico = scraped.best_score
                    fico_source = f"scraped:{scraped.best_score_model}"

            if effective_fico is None and result.intelligence_passed:
                effective_fico = INTELLIGENCE_PASSED_PROXY_FICO
                fico_source = "intelligence-proxy"

            # Persist both the verbatim iSoftPull JSON AND the structured
            # report we scraped. We stash the parsed report under
            # bureau_response['parsed_report'] so we don't need a schema
            # migration; the column is already JSONB. The summary is
            # rebuildable from parsed_report so we don't store it.
            persisted_response = dict(result.raw or {})
            if scraped is not None:
                persisted_response["parsed_report"] = scraped.to_dict()
            pull.fico = effective_fico
            pull.bureau_response = persisted_response
            pull.status = CreditPullStatus.COMPLETED
            pull.pulled_at = result.pulled_at
            pull.expires_at = result.pulled_at + timedelta(days=SOFT_PULL_VALIDITY_DAYS)

            note_parts: list[str] = []
            if result.provider_pull_id:
                note_parts.append(f"isoftpull_id={result.provider_pull_id}")
            if result.intelligence_name:
                note_parts.append(
                    f"intelligence={result.intelligence_name}:{'pass' if result.intelligence_passed else 'fail'}"
                )
            if fico_source:
                note_parts.append(f"fico_source={fico_source}")
            if scraped:
                detail = []
                if scraped.fico_8 is not None:
                    detail.append(f"fico8={scraped.fico_8}")
                if scraped.fico_2 is not None:
                    detail.append(f"fico2={scraped.fico_2}")
                if scraped.vantage_4 is not None:
                    detail.append(f"vantage4={scraped.vantage_4}")
                if detail:
                    note_parts.append("scraped:" + ",".join(detail))
            if note_parts:
                pull.notes = "; ".join(note_parts)

            if user.client:
                user.client.fico = effective_fico
            await db.flush()
            await db.refresh(pull)
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to persist credit pull result")
            pull.status = CreditPullStatus.REVOKED
            pull.notes = f"persist_error: {type(exc).__name__}: {exc}"
            await db.flush()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Credit pull succeeded but failed to save — please retry.",
            ) from exc
        return _to_read(pull)

    # No iSoftPull credentials configured — fail loudly rather than fake a
    # score. The simulator gate would interpret a fabricated FICO as real
    # eligibility, leading to misleading rate quotes.
    pull.status = CreditPullStatus.REVOKED
    pull.notes = "iSoftPull credentials not configured"
    await db.flush()
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Credit pull service is not configured. Set ISOFTPULL_PRIVATE_KEY in the backend environment.",
    )


# ── Operator report viewer (proxied through the server-side iSoftPull session) ──

def _allowed_to_view_report(viewer, pull: CreditPull) -> bool:
    """Operators can view any pull on their book; clients cannot view at all.

    Borrowers should not see this — they get the simulator unlock and the
    intelligence verdict. The HTML report is an internal artifact for
    underwriters/brokers/admins only.
    """
    if viewer.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        return True
    if viewer.role == Role.BROKER and viewer.broker:
        return True  # broker scoping by client_id can be tightened later
    return False


@router.get("/pulls/{pull_id}/report", response_class=HTMLResponse)
async def report_view(
    pull_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Stream the iSoftPull report HTML through the backend's authenticated
    session. Operator-only — borrowers cannot reach this endpoint.

    Bridge: while the iSoftPull API token has Full Feed disabled, this is
    how operators inspect the underlying report. Goes away when Full Feed
    is enabled and we render the parsed JSON in our own UI instead.
    """
    pull = await db.get(CreditPull, pull_id)
    if pull is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Credit pull not found")
    if not _allowed_to_view_report(user, pull):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized")

    # Pull the report link out of the stored bureau response.
    report_link: str | None = None
    raw = pull.bureau_response or {}
    reports = raw.get("reports") or {}
    for bureau in ("experian", "transunion", "equifax"):
        node = reports.get(bureau) or {}
        if isinstance(node, dict) and node.get("status") == "success":
            link = node.get("link")
            if isinstance(link, str):
                report_link = link
                break
    if not report_link:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No report link on file for this pull"
        )

    try:
        session = get_session()
        resp = await session.fetch(report_link)
    except IsoftpullSessionError as exc:
        log.warning("iSoftPull session unavailable for report proxy: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Report viewer temporarily unavailable.",
        ) from exc

    if resp.status_code != 200:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"iSoftPull returned {resp.status_code} for the report.",
        )
    return HTMLResponse(content=resp.text, status_code=200)


# ── Structured report + summary endpoints ─────────────────────────────────

from app.services.credit_summary import summarize as _summarize_report
from app.services.isoftpull_report_parser import ScrapedReport


def _scraped_from_pull(pull: CreditPull) -> ScrapedReport | None:
    """Reconstruct ScrapedReport from the JSONB blob stored at pull time.

    Returns None if the pull was made before scraping was wired up, or if
    the scraper missed (e.g. iSoftPull session was offline). Callers
    should treat None as "no parsed report on file."
    """
    raw = pull.bureau_response or {}
    parsed = raw.get("parsed_report")
    if not isinstance(parsed, dict):
        return None
    # Inflate the dataclasses. We can be lenient — anything missing comes
    # back as the default, which is what summarize() expects.
    from app.services.isoftpull_report_parser import (
        AddressRecord, CreditScore, EmploymentRecord, IdentityRisk,
        Inquiry, TradeAccount,
    )

    def _l(items, cls):
        return [cls(**x) for x in (items or []) if isinstance(x, dict)]

    risk_d = parsed.get("identity_risk") or {}
    return ScrapedReport(
        personal_info=parsed.get("personal_info") or {},
        addresses=_l(parsed.get("addresses"), AddressRecord),
        employment=_l(parsed.get("employment"), EmploymentRecord),
        scores=_l(parsed.get("scores"), CreditScore),
        identity_risk=IdentityRisk(
            ofac=risk_d.get("ofac") or {},
            mla=risk_d.get("mla") or {},
            fraud_shield=risk_d.get("fraud_shield") or {},
        ),
        inquiries=_l(parsed.get("inquiries"), Inquiry),
        trade_accounts=_l(parsed.get("trade_accounts"), TradeAccount),
        public_records=parsed.get("public_records") or [],
        collections=parsed.get("collections") or [],
        fico_8=parsed.get("fico_8"),
        fico_2=parsed.get("fico_2"),
        vantage_4=parsed.get("vantage_4"),
        best_score=parsed.get("best_score"),
        best_score_model=parsed.get("best_score_model"),
        raw_html_length=parsed.get("raw_html_length") or 0,
    )


def _viewer_can_see_pull(viewer, pull: CreditPull) -> bool:
    """Operator: any pull. Borrower: their own client_id only."""
    if viewer.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER):
        return True
    if viewer.role == Role.CLIENT and viewer.client and viewer.client.id == pull.client_id:
        return True
    return False


@router.get("/pulls/{pull_id}/parsed")
async def parsed_report(
    pull_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Operator-only structured report. Full ScrapedReport dump — every
    field iSoftPull surfaced, ready to render in the super-admin UI."""
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator-only")
    pull = await db.get(CreditPull, pull_id)
    if pull is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Credit pull not found")
    scraped = _scraped_from_pull(pull)
    if scraped is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No parsed report on file for this pull.",
        )
    return scraped.to_dict()


@router.get("/pulls/{pull_id}/summary")
async def credit_summary(
    pull_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Borrower-safe summary of the credit pull.

    Returns headline FICO + tier, a few positive/neutral/warn bullets
    derived from tradelines, and the SKUs the borrower currently
    qualifies for. Borrowers see their own pull; operators see any.
    """
    pull = await db.get(CreditPull, pull_id)
    if pull is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Credit pull not found")
    if not _viewer_can_see_pull(user, pull):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized")
    scraped = _scraped_from_pull(pull)
    if scraped is None:
        # No parsed report — fall back to a minimal summary built off the
        # FICO that's stored on the row (intelligence proxy or otherwise).
        return {
            "fico": pull.fico,
            "fico_model": None,
            "tier": None,
            "tier_max_ltv": None,
            "bullets": [],
            "available_products": [],
            "blocked_products": [],
            "fraud_flag": None,
            "note": "Parsed report unavailable — using stored FICO only.",
        }
    summary = _summarize_report(scraped)
    # Convert dataclasses to dicts for JSON
    return {
        "fico": summary.fico,
        "fico_model": summary.fico_model,
        "tier": summary.tier,
        "tier_max_ltv": summary.tier_max_ltv,
        "bullets": [
            {"kind": b.kind, "label": b.label, "detail": b.detail} for b in summary.bullets
        ],
        "aggregates": {
            "open_count": summary.aggregates.open_count,
            "closed_count": summary.aggregates.closed_count,
            "derogatory_count": summary.aggregates.derogatory_count,
            "total_balance": summary.aggregates.total_balance,
            "total_credit_limit": summary.aggregates.total_credit_limit,
            "total_monthly_payment": summary.aggregates.total_monthly_payment,
            "revolving_balance": summary.aggregates.revolving_balance,
            "revolving_credit_limit": summary.aggregates.revolving_credit_limit,
            "revolving_utilization": summary.aggregates.revolving_utilization,
            "has_mortgage": summary.aggregates.has_mortgage,
            "oldest_account_opened": summary.aggregates.oldest_account_opened.isoformat()
                if summary.aggregates.oldest_account_opened else None,
            "by_type": summary.aggregates.by_type,
        },
        "recent_inquiries_6mo": summary.recent_inquiries_6mo,
        "available_products": summary.available_products,
        "blocked_products": summary.blocked_products,
        "fraud_flag": summary.fraud_flag,
    }
