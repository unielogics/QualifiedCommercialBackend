"""Soft pull endpoints — Module 4 (mobile) + Apply flow gate."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.constants import SOFT_PULL_EXPIRING_SOON_DAYS
from app.db import get_db
from app.deps import CurrentUser
from app.enums import CreditPullStatus, Role
from app.models.credit_pull import CreditPull
from app.schemas.billing import CreditPullAccessRead
from app.schemas.credit import CreditPullRead, CreditPullRequest
from app.services import credit_pull_core
from app.services.payment_authorization import (
    client_has_completed_payment_authorization,
    require_payment_authorized_for_credit,
)
from app.services.isoftpull_session import IsoftpullSessionError, get_session

router = APIRouter(prefix="/credit", tags=["credit"])
log = logging.getLogger(__name__)


def _role_value(user) -> str:
    return str(getattr(user.role, "value", user.role))


def _has_role(user, role: Role) -> bool:
    return _role_value(user) == role.value


def _has_any_role(user, roles: tuple[Role, ...]) -> bool:
    return _role_value(user) in {role.value for role in roles}


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
async def current(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    client_id: str | None = None,
) -> CreditPullRead | None:
    """Most-recent valid credit pull for a client.

    Resolution rules:
      • client_id="self" or omitted → caller's own client (borrower app).
      • client_id=<uuid> + operator role (super_admin / loan_exec /
        broker) → that borrower's most-recent pull. Broker access is
        scoped to clients owned by their broker_id. Lets operators see
        the pulled FICO + parsed report on the loan Credit panel.
      • client_id=<uuid> + borrower role mismatch → 403.
    """
    if _has_role(user, Role.CLIENT):
        # Normal app surfaces poll /credit/current to decide whether to show
        # locked or unlocked credit UI. Missing payment authorization should
        # not break app rendering; the actual bureau pull and credit summary
        # endpoints remain protected below.
        authorized = await client_has_completed_payment_authorization(db, user)
        if not authorized:
            log.info(
                "GET /credit/current user=%s role=%s -> null (payment authorization required)",
                user.email,
                user.role,
            )
            return None
    target_cid: str | None
    if client_id and client_id != "self":
        from uuid import UUID
        try:
            UUID(client_id)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "client_id must be a UUID or 'self'")
        if _has_role(user, Role.CLIENT):
            own = _client_id_for(user)
            if str(own) != client_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Borrowers can only fetch their own credit")
            target_cid = own
        elif _has_role(user, Role.BROKER):
            # Scope check — broker can only see pulls for their own
            # clients. Same gate the rest of the agent surface uses.
            from app.models.client import Client as _Client
            row = (
                await db.execute(
                    select(_Client).where(_Client.id == client_id)
                )
            ).scalar_one_or_none()
            broker = getattr(user, "broker", None)
            if row is None or broker is None or row.broker_id != broker.id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
            target_cid = client_id
        else:
            # super_admin / loan_exec — firm-wide read.
            target_cid = client_id
    else:
        target_cid = _client_id_for(user)

    if target_cid is None:
        log.info("GET /credit/current user=%s role=%s target=None -> null", user.email, user.role)
        return None
    stmt = (
        select(CreditPull)
        .where(CreditPull.client_id == target_cid)
        .where(CreditPull.status == CreditPullStatus.COMPLETED)
        .where(CreditPull.expires_at > datetime.now(timezone.utc))
        .order_by(CreditPull.pulled_at.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    log.info(
        "GET /credit/current user=%s role=%s target_cid=%s -> %s",
        user.email, user.role, target_cid,
        f"pull_id={row.id} fico={row.fico}" if row else "null (no valid pull)",
    )
    return _to_read(row) if row else None


@router.get("/pull-access", response_model=CreditPullAccessRead)
async def pull_access(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CreditPullAccessRead:
    if not _has_role(user, Role.CLIENT):
        return CreditPullAccessRead(
            role=_role_value(user),
            requires_payment_authorization=False,
            payment_authorized=True,
            can_run_credit=True,
        )
    authorized = await client_has_completed_payment_authorization(db, user)
    has_client = user.client is not None
    return CreditPullAccessRead(
        role=_role_value(user),
        requires_payment_authorization=True,
        payment_authorized=authorized,
        can_run_credit=authorized and has_client,
        reason_code=None if authorized and has_client else "payment_authorization_required",
        message=None if authorized and has_client else "Complete the payment pre-authorization before activating credit features.",
    )


@router.post("/pull", response_model=CreditPullRead)
async def initiate_pull(
    payload: CreditPullRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> CreditPullRead:
    cid = _client_id_for(user)
    if cid is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client profile required")
    await require_payment_authorized_for_credit(db, user)
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

    # Bureau-pull core is shared with the admin/lead-triggered path
    # (app/services/credit_pull_core.py) so both callers use the exact same
    # FICO-fallback chain, parsing, and CreditPull persistence.
    try:
        pull = await credit_pull_core.run_soft_pull(
            db,
            client=user.client,
            applicant=credit_pull_core.SoftPullApplicant(
                legal_first_name=payload.legal_first_name,
                legal_last_name=payload.legal_last_name,
                dob=payload.dob,
                street=payload.street,
                city=payload.city,
                state=payload.state,
                zip=payload.zip,
                # SSN is optional — when None, iSoftPull tries to match on
                # name+address+DOB. The frontend only asks for SSN if THIS
                # attempt comes back as no-hit.
                ssn=payload.ssn,
            ),
            actor=user,
        )
    except credit_pull_core.SoftPullDenied as exc:
        if exc.code in ("no_hit_provide_ssn", "bureau_freeze"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.message) from exc
    except credit_pull_core.SoftPullValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except credit_pull_core.SoftPullRateLimited as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except credit_pull_core.SoftPullUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return _to_read(pull)


# ── Operator report viewer (proxied through the server-side iSoftPull session) ──

def _allowed_to_view_report(viewer, pull: CreditPull) -> bool:
    """Operators can view any pull on their book; clients cannot view at all.

    Borrowers should not see this — they get the simulator unlock and the
    intelligence verdict. The HTML report is an internal artifact for
    underwriters/brokers/admins only.
    """
    if _has_any_role(viewer, (Role.SUPER_ADMIN, Role.LOAN_EXEC)):
        return True
    if _has_role(viewer, Role.BROKER) and viewer.broker:
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
    if _has_any_role(viewer, (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER)):
        return True
    if _has_role(viewer, Role.CLIENT) and viewer.client and viewer.client.id == pull.client_id:
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
    if not _has_any_role(user, (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER)):
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
    await require_payment_authorized_for_credit(db, user)
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
