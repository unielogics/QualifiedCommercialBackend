"""Super-admin diagnostics + dev test-injection endpoints.

Two surfaces today:

  * `GET /admin/connect-lender/health` — read-only probe of every link
    in the Connect-Lender + lender-email chain so the operator can see
    at a glance which piece (if any) is preventing a clean run.
    Driven by the Super Admin → Lenders page.

  * `POST /admin/dev/inject-lender-email` — dev-only mock-inbox
    injector. Gated by `USE_FAKE_INBOX=True` so a misconfigured prod
    can't accept synthetic inbound mail. Lets the operator drop in
    sample files (eg .eml content) and watch them flow through the
    lender thread without setting up Gmail Pub/Sub.

Both endpoints are super-admin only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser, require_role
from app.enums import LoanStage, Role
from app.models.ai_usage_event import AIUsageEvent
from app.models.capital_partner_application import (
    APPLICATION_STATUSES,
    CapitalPartnerApplication,
)
from app.models.lender import Lender
from app.models.loan import Loan
from app.models.loan_scenario import LoanScenario
from app.models.user import User
from app.services.lender_thread import LenderThreadError, inject_inbound_lender_email
from app.services.ai.usage import load_ai_spend_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_super_admin(user) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


# ---------------------------------------------------------------------------
# Connect Lender — health probe
# ---------------------------------------------------------------------------

class HealthCheck(BaseModel):
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str


class ConnectLenderHealth(BaseModel):
    overall: str  # "ok" | "warn" | "fail"
    checks: list[HealthCheck]
    eligible_loan_count: int
    active_lender_count: int
    connected_loan_count: int
    gmail_can_send: bool


class GmailTestResult(BaseModel):
    ok: bool
    tier: str  # "token" | "profile" | "labels" | "(not_configured)"
    note: str
    service_account_email: str | None = None
    delegated_user: str | None = None


class AIUsageBucket(BaseModel):
    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class AIUsageSummary(BaseModel):
    day_start: datetime
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost_usd: float
    alert_level: str
    daily_warning_usd: float
    daily_critical_usd: float
    avg_client_file_warning_usd: float
    avg_client_file_critical_usd: float
    avg_cost_per_client_usd: float
    avg_cost_per_loan_file_usd: float
    master_enabled: bool
    chat_enabled: bool
    automations_enabled: bool
    document_scanning_enabled: bool
    summaries_enabled: bool
    lender_ai_enabled: bool
    by_category: list[AIUsageBucket]
    by_feature: list[AIUsageBucket]
    by_client: list[AIUsageBucket]
    by_loan_file: list[AIUsageBucket]
    top_calls: list[dict[str, Any]]


def _today_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _money(value: Any) -> float:
    return round(float(value or 0), 6)


@router.get("/ai-usage/today", response_model=AIUsageSummary)
async def ai_usage_today(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AIUsageSummary:
    _require_super_admin(user)
    spend_settings = await load_ai_spend_settings(db)
    start = _today_start()

    total_row = (
        await db.execute(
            select(
                func.count(AIUsageEvent.id),
                func.coalesce(func.sum(AIUsageEvent.input_tokens), 0),
                func.coalesce(func.sum(AIUsageEvent.output_tokens), 0),
                func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0),
            ).where(AIUsageEvent.created_at >= start)
        )
    ).one()

    async def buckets(column) -> list[AIUsageBucket]:
        rows = (
            await db.execute(
                select(
                    column,
                    func.count(AIUsageEvent.id),
                    func.coalesce(func.sum(AIUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(AIUsageEvent.output_tokens), 0),
                    func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0),
                )
                .where(AIUsageEvent.created_at >= start)
                .group_by(column)
                .order_by(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).desc())
            )
        ).all()
        return [
            AIUsageBucket(
                key=str(key or "unscoped"),
                calls=int(calls or 0),
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                estimated_cost_usd=_money(cost),
            )
            for key, calls, input_tokens, output_tokens, cost in rows
        ]

    distinct_clients = int((
        await db.execute(
            select(func.count(func.distinct(AIUsageEvent.client_id))).where(
                AIUsageEvent.created_at >= start,
                AIUsageEvent.client_id.is_not(None),
            )
        )
    ).scalar_one() or 0)
    distinct_loans = int((
        await db.execute(
            select(func.count(func.distinct(AIUsageEvent.loan_id))).where(
                AIUsageEvent.created_at >= start,
                AIUsageEvent.loan_id.is_not(None),
            )
        )
    ).scalar_one() or 0)
    total_cost = _money(total_row[3])
    avg_client = _money(total_cost / distinct_clients) if distinct_clients else 0.0
    avg_loan = _money(total_cost / distinct_loans) if distinct_loans else 0.0
    alert_level = "ok"
    if (
        spend_settings.daily_critical_usd > 0
        and total_cost >= spend_settings.daily_critical_usd
    ) or (
        spend_settings.avg_client_file_critical_usd > 0
        and avg_loan >= spend_settings.avg_client_file_critical_usd
    ):
        alert_level = "critical"
    elif (
        spend_settings.daily_warning_usd > 0
        and total_cost >= spend_settings.daily_warning_usd
    ) or (
        spend_settings.avg_client_file_warning_usd > 0
        and avg_loan >= spend_settings.avg_client_file_warning_usd
    ):
        alert_level = "warning"

    top_rows = (
        await db.execute(
            select(AIUsageEvent)
            .where(AIUsageEvent.created_at >= start)
            .order_by(AIUsageEvent.estimated_cost_usd.desc())
            .limit(20)
        )
    ).scalars().all()

    return AIUsageSummary(
        day_start=start,
        total_calls=int(total_row[0] or 0),
        total_input_tokens=int(total_row[1] or 0),
        total_output_tokens=int(total_row[2] or 0),
        total_estimated_cost_usd=total_cost,
        alert_level=alert_level,
        daily_warning_usd=spend_settings.daily_warning_usd,
        daily_critical_usd=spend_settings.daily_critical_usd,
        avg_client_file_warning_usd=spend_settings.avg_client_file_warning_usd,
        avg_client_file_critical_usd=spend_settings.avg_client_file_critical_usd,
        avg_cost_per_client_usd=avg_client,
        avg_cost_per_loan_file_usd=avg_loan,
        master_enabled=spend_settings.master_enabled,
        chat_enabled=spend_settings.chat_enabled,
        automations_enabled=spend_settings.automations_enabled,
        document_scanning_enabled=spend_settings.document_scanning_enabled,
        summaries_enabled=spend_settings.summaries_enabled,
        lender_ai_enabled=spend_settings.lender_ai_enabled,
        by_category=await buckets(AIUsageEvent.category),
        by_feature=await buckets(AIUsageEvent.feature),
        by_client=(await buckets(AIUsageEvent.client_id))[:20],
        by_loan_file=(await buckets(AIUsageEvent.loan_id))[:20],
        top_calls=[
            {
                "id": str(row.id),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "feature": row.feature,
                "category": row.category,
                "model": row.model,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "estimated_cost_usd": _money(row.estimated_cost_usd),
                "user_id": str(row.user_id) if row.user_id else None,
                "broker_id": str(row.broker_id) if row.broker_id else None,
                "client_id": str(row.client_id) if row.client_id else None,
                "loan_id": str(row.loan_id) if row.loan_id else None,
                "thread_id": str(row.thread_id) if row.thread_id else None,
                "metadata": row.metadata_json,
            }
            for row in top_rows
        ],
    )


@router.get("/connect-lender/health", response_model=ConnectLenderHealth)
async def connect_lender_health(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectLenderHealth:
    """Diagnostic probe — answers 'why is Connect Lender not working?'.

    Returns a list of named checks the UI renders as traffic lights.
    The overall status is the worst of the individual statuses
    (fail > warn > ok)."""
    _require_super_admin(user)
    settings = get_settings()
    checks: list[HealthCheck] = []

    # 1. Caller role (always ok at this point since the gate above passed —
    # we surface it anyway so the UI can show 'super_admin verified').
    checks.append(
        HealthCheck(
            name="Super admin role",
            status="ok",
            detail=f"Authenticated as {user.email} ({user.role.value if hasattr(user.role, 'value') else user.role}).",
        )
    )

    # 2. Active lender count — the dropdown is empty without these.
    lender_total = (await db.execute(select(func.count(Lender.id)))).scalar_one() or 0
    lender_active = (
        await db.execute(select(func.count(Lender.id)).where(Lender.is_active.is_(True)))
    ).scalar_one() or 0
    if lender_active == 0:
        checks.append(
            HealthCheck(
                name="Active lenders",
                status="fail",
                detail="No active lenders in the roster. Add one above before connecting.",
            )
        )
    elif lender_active < 3:
        checks.append(
            HealthCheck(
                name="Active lenders",
                status="warn",
                detail=f"{lender_active} active / {lender_total} total. Consider adding more options.",
            )
        )
    else:
        checks.append(
            HealthCheck(
                name="Active lenders",
                status="ok",
                detail=f"{lender_active} active / {lender_total} total.",
            )
        )

    # 3. Connectable loans (stage = PREQUALIFIED or COLLECTING_DOCS, lender_id null).
    connectable = (
        await db.execute(
            select(func.count(Loan.id))
            .where(Loan.lender_id.is_(None))
            .where(Loan.stage.in_([LoanStage.PREQUALIFIED, LoanStage.COLLECTING_DOCS]))
        )
    ).scalar_one() or 0
    connected = (
        await db.execute(
            select(func.count(Loan.id)).where(Loan.lender_id.is_not(None))
        )
    ).scalar_one() or 0
    if connectable == 0 and connected == 0:
        checks.append(
            HealthCheck(
                name="Loans eligible for connection",
                status="warn",
                detail="No loans in PREQUALIFIED or COLLECTING_DOCS, and none connected. "
                "Pipeline may be empty or all loans are past the connect stage.",
            )
        )
    else:
        checks.append(
            HealthCheck(
                name="Loans eligible for connection",
                status="ok",
                detail=f"{connectable} connectable, {connected} already connected.",
            )
        )

    # 4. Gmail config — required for outbound (and eventually inbound) mail.
    sa_path = settings.gmail_service_account_path
    delegated = settings.gmail_delegated_user
    gmail_can_send = False
    if not sa_path or not delegated:
        checks.append(
            HealthCheck(
                name="Gmail outbound",
                status="warn",
                detail=(
                    "Gmail not configured (GMAIL_SERVICE_ACCOUNT_PATH / "
                    "GMAIL_DELEGATED_USER unset). Connect Lender still works; "
                    "outbound mail will be drafted but not sent until configured."
                ),
            )
        )
    elif not Path(sa_path).expanduser().exists():
        checks.append(
            HealthCheck(
                name="Gmail outbound",
                status="fail",
                detail=f"GMAIL_SERVICE_ACCOUNT_PATH={sa_path!r} does not exist on disk.",
            )
        )
    else:
        gmail_can_send = True
        checks.append(
            HealthCheck(
                name="Gmail outbound",
                status="ok",
                detail=f"Service-account JSON at {sa_path}; delegated user {delegated}. "
                f"Hit POST /admin/gmail/test for a live connectivity probe.",
            )
        )

    # 5. Gmail inbound — mock vs real.
    if settings.use_fake_inbox:
        checks.append(
            HealthCheck(
                name="Gmail inbound",
                status="warn",
                detail=(
                    "USE_FAKE_INBOX=True — inbound mail is in mock mode. "
                    "Use /admin/dev/inject-lender-email to seed test messages."
                ),
            )
        )
    else:
        checks.append(
            HealthCheck(
                name="Gmail inbound",
                status="ok",
                detail="Real inbound path active (Pub/Sub consumer must be running).",
            )
        )

    # 6. Bedrock provider — the lender-thread mini-summary + instruct_ai
    # reply both fall back to deterministic text when disabled, so this is
    # only a warning.
    if not settings.ai_provider_enabled:
        checks.append(
            HealthCheck(
                name="AI summaries",
                status="warn",
                detail="Bedrock provider disabled — thread summaries fall back to deterministic text.",
            )
        )
    else:
        checks.append(
            HealthCheck(
                name="AI summaries",
                status="ok",
                detail=f"Bedrock provider enabled; using {settings.bedrock_model_light}.",
            )
        )

    overall = "ok"
    if any(c.status == "fail" for c in checks):
        overall = "fail"
    elif any(c.status == "warn" for c in checks):
        overall = "warn"

    return ConnectLenderHealth(
        overall=overall,
        checks=checks,
        eligible_loan_count=connectable,
        active_lender_count=lender_active,
        connected_loan_count=connected,
        gmail_can_send=gmail_can_send,
    )


@router.post("/gmail/test", response_model=GmailTestResult)
async def gmail_test(user: CurrentUser) -> GmailTestResult:
    """Live connectivity probe: acquire a Gmail access token via the
    service account, then call users.getProfile to confirm domain-wide
    delegation works for the delegated user. Super-admin only.

    Returns success at the highest tier reached, so the operator can see
    exactly where things fall over (key parse → token mint → DWD profile)."""
    _require_super_admin(user)
    settings = get_settings()
    if not settings.gmail_service_account_path or not settings.gmail_delegated_user:
        return GmailTestResult(
            ok=False,
            tier="(not_configured)",
            note="GMAIL_SERVICE_ACCOUNT_PATH and/or GMAIL_DELEGATED_USER are unset.",
            service_account_email=None,
            delegated_user=settings.gmail_delegated_user or None,
        )
    if not Path(settings.gmail_service_account_path).expanduser().exists():
        return GmailTestResult(
            ok=False,
            tier="(not_configured)",
            note=f"Service-account JSON not found at {settings.gmail_service_account_path}.",
            delegated_user=settings.gmail_delegated_user,
        )

    from app.services.email.gmail_client import (
        acquire_token,
        get_profile,
        gmail_config,
        explain_http_error,
    )

    cfg = gmail_config()
    if cfg is None:
        return GmailTestResult(
            ok=False,
            tier="(not_configured)",
            note="gmail_config() returned None — service account path empty after expansion.",
        )

    # Tier 1 — token
    try:
        info = acquire_token(cfg)
    except Exception as exc:  # noqa: BLE001
        return GmailTestResult(
            ok=False,
            tier="token",
            note=f"Token acquisition failed: {exc}",
            delegated_user=settings.gmail_delegated_user,
        )
    sa_email = info.get("service_account_email")

    # Tier 2 — getProfile (proves DWD)
    try:
        profile = get_profile(cfg)
    except Exception as exc:  # noqa: BLE001 — googleapiclient.HttpError or anything else
        return GmailTestResult(
            ok=False,
            tier="profile",
            note=f"getProfile failed: {explain_http_error(exc)}",
            service_account_email=sa_email,
            delegated_user=settings.gmail_delegated_user,
        )

    return GmailTestResult(
        ok=True,
        tier="profile",
        note=f"Authenticated as {profile.get('emailAddress')} "
        f"({profile.get('messagesTotal', 0)} msgs in mailbox).",
        service_account_email=sa_email,
        delegated_user=settings.gmail_delegated_user,
    )


# ---------------------------------------------------------------------------
# Dev-only mock-inbox injector
# ---------------------------------------------------------------------------

class InjectLenderEmailPayload(BaseModel):
    loan_id: UUID
    from_email: str = Field(min_length=3, max_length=320)
    subject: str = Field(default="", max_length=512)
    body: str = Field(min_length=1)


class InjectLenderEmailResponse(BaseModel):
    message_id: UUID
    loan_id: UUID
    sent_at: str
    note: str


@router.post("/dev/inject-lender-email", response_model=InjectLenderEmailResponse)
async def inject_lender_email(
    payload: InjectLenderEmailPayload,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> InjectLenderEmailResponse:
    """Simulate an inbound email from the connected lender. Writes a
    `Message(from_role=LENDER)` row and an `Activity(kind='email.inbound')`
    entry — same shape the real Pub/Sub consumer will produce.

    Gated behind `USE_FAKE_INBOX=True` so accidental usage in prod is
    rejected. Super-admin only."""
    _require_super_admin(user)
    settings = get_settings()
    # Hard gate: even if USE_FAKE_INBOX is True, the dev injector must never
    # run in production. Real lender threads / customers must not be polluted
    # with synthetic inbound messages.
    if (settings.app_env or "").lower() == "production":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Dev injector is disabled in production (APP_ENV=production).",
        )
    if not settings.use_fake_inbox:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "USE_FAKE_INBOX is False — this endpoint is dev-only and disabled.",
        )
    try:
        msg = await inject_inbound_lender_email(
            db,
            loan_id=payload.loan_id,
            from_email=payload.from_email,
            subject=payload.subject or "(no subject)",
            body=payload.body,
        )
    except LenderThreadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return InjectLenderEmailResponse(
        message_id=msg.id,
        loan_id=msg.loan_id,
        sent_at=msg.sent_at.isoformat(),
        note=f"Injected {len(payload.body)}-char body from {payload.from_email}.",
    )


# ---------------------------------------------------------------------------
# System-wide simulator runs — operator (super-admin + loan-exec) only
# ---------------------------------------------------------------------------
#
# Loan scenarios are otherwise only reachable per-loan via
# /loans/{loan_id}/scenarios. This is the cross-user firm-wide list that
# backs the Simulate page's runs table for operators.


class AdminLoanScenarioRead(BaseModel):
    id: UUID
    name: str
    discount_points: float
    loan_amount: float | None
    base_rate: float | None
    ltv: float | None
    recalc_snapshot: dict[str, Any] | None
    created_at: datetime
    loan_id: UUID
    loan_deal_id: str | None
    loan_address: str | None
    created_by_name: str | None
    created_by_email: str | None


@router.get("/loan-scenarios", response_model=list[AdminLoanScenarioRead])
async def list_all_loan_scenarios(
    _: User = Depends(require_role(Role.SUPER_ADMIN, Role.LOAN_EXEC)),
    db: AsyncSession = Depends(get_db),
) -> list[AdminLoanScenarioRead]:
    rows = (
        await db.execute(
            select(
                LoanScenario,
                Loan.deal_id,
                Loan.address,
                User.name,
                User.email,
            )
            .outerjoin(Loan, LoanScenario.loan_id == Loan.id)
            .outerjoin(User, LoanScenario.created_by == User.id)
            .order_by(LoanScenario.created_at.desc())
        )
    ).all()
    return [
        AdminLoanScenarioRead(
            id=s.id,
            name=s.name,
            discount_points=float(s.discount_points or 0),
            loan_amount=float(s.loan_amount) if s.loan_amount is not None else None,
            base_rate=float(s.base_rate) if s.base_rate is not None else None,
            ltv=float(s.ltv) if s.ltv is not None else None,
            recalc_snapshot=s.recalc_snapshot,
            created_at=s.created_at,
            loan_id=s.loan_id,
            loan_deal_id=deal_id,
            loan_address=address,
            created_by_name=name,
            created_by_email=email,
        )
        for (s, deal_id, address, name, email) in rows
    ]


# ---------------------------------------------------------------------------
# Capital partner applications — review queue + decision endpoints.
#
# Public submissions land in `capital_partner_applications` via
# POST /public/capital-partner-application (see app/routers/public.py).
# Super-admin reviews from QCDashboard /admin/capital-partner-applications.
# Approval optionally promotes the row into a real `lenders` entry so
# the team can immediately route deals to the new partner.
# ---------------------------------------------------------------------------


class CapitalPartnerAppListRow(BaseModel):
    id: UUID
    company_name: str
    contact_name: str
    contact_email: str
    loan_types: list[str]
    geographic_states: list[str]
    monthly_origination_band: str | None
    status: str
    reviewed_at: datetime | None
    promoted_lender_id: UUID | None
    created_at: datetime

    class Config:
        from_attributes = True


class CapitalPartnerAppRead(BaseModel):
    id: UUID
    # Company
    company_name: str
    legal_entity_type: str | None
    formation_state: str | None
    ein: str | None
    years_in_business: int | None
    website: str | None
    # Lending appetite
    loan_types: list[str]
    loan_size_min: int | None
    loan_size_max: int | None
    geographic_states: list[str]
    asset_classes: list[str]
    # Capital & volume
    capital_source: str | None
    aum_band: str | None
    monthly_origination_band: str | None
    # Underwriting box
    max_ltv: float | None
    max_ltc: float | None
    min_dscr: float | None
    min_fico: int | None
    rate_range: str | None
    # Contact
    contact_name: str
    contact_title: str | None
    contact_email: str
    contact_phone: str | None
    submission_email: str | None
    submission_portal_url: str | None
    average_response_time: str | None
    notes: str | None
    # Review state
    status: str
    review_notes: str | None
    reviewed_by_id: UUID | None
    reviewed_at: datetime | None
    promoted_lender_id: UUID | None
    # Audit
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CapitalPartnerDecisionPayload(BaseModel):
    decision: str  # "approved" | "denied"
    review_notes: str | None = Field(default=None, max_length=4000)
    # Approval-only: when true, copy the application into a real
    # `lenders` table row so the team can immediately route deals.
    promote_to_lender: bool = False


def _to_lender_products(loan_types: list[str]) -> list[str]:
    """Loose normalisation of the application's free-form loan_types
    list to the dropdown values used by the existing Lender.products
    field. Unknown entries are passed through (operator can edit
    later in the lender roster)."""
    keep = []
    for t in loan_types or []:
        if not isinstance(t, str):
            continue
        s = t.strip().lower().replace("&", "and").replace(" ", "_").replace("-", "_")
        if s and s not in keep:
            keep.append(s)
    return keep


@router.get(
    "/capital-partner-applications",
    response_model=list[CapitalPartnerAppListRow],
)
async def list_capital_partner_applications(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = None,
) -> list[CapitalPartnerAppListRow]:
    """Lender-application queue. Defaults to newest-first; pass
    `?status_filter=pending` to narrow the list."""
    _require_super_admin(user)
    stmt = select(CapitalPartnerApplication).order_by(
        CapitalPartnerApplication.created_at.desc()
    )
    if status_filter:
        if status_filter not in APPLICATION_STATUSES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Unknown status_filter: {status_filter!r}",
            )
        stmt = stmt.where(CapitalPartnerApplication.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [CapitalPartnerAppListRow.model_validate(r) for r in rows]


@router.get(
    "/capital-partner-applications/{app_id}",
    response_model=CapitalPartnerAppRead,
)
async def get_capital_partner_application(
    app_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CapitalPartnerAppRead:
    """Full detail view for a single application."""
    _require_super_admin(user)
    row = (
        await db.execute(
            select(CapitalPartnerApplication).where(
                CapitalPartnerApplication.id == app_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return CapitalPartnerAppRead.model_validate(row)


@router.post(
    "/capital-partner-applications/{app_id}/decision",
    response_model=CapitalPartnerAppRead,
)
async def decide_capital_partner_application(
    app_id: UUID,
    payload: CapitalPartnerDecisionPayload,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CapitalPartnerAppRead:
    """Approve or deny an application. On approval, optionally promote
    the row into a real `lenders` entry so the team can route deals
    immediately. Returns the updated application."""
    _require_super_admin(user)
    if payload.decision not in ("approved", "denied"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "decision must be 'approved' or 'denied'.",
        )
    row = (
        await db.execute(
            select(CapitalPartnerApplication).where(
                CapitalPartnerApplication.id == app_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    if row.status != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Application is already {row.status}. Re-deciding is not supported.",
        )

    row.status = payload.decision
    row.review_notes = payload.review_notes
    row.reviewed_by_id = user.id
    row.reviewed_at = datetime.now(tz=row.created_at.tzinfo)

    if payload.decision == "approved" and payload.promote_to_lender:
        # Only promote if a lender with the same name doesn't already
        # exist — operator can manually deduplicate later.
        existing = (
            await db.execute(
                select(Lender).where(
                    func.lower(Lender.name) == row.company_name.strip().lower()
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            lender = Lender(
                name=row.company_name.strip(),
                submission_email=row.submission_email or row.contact_email,
                contact_name=row.contact_name,
                contact_email=row.contact_email,
                contact_phone=row.contact_phone,
                contact_title=row.contact_title,
                products=_to_lender_products(list(row.loan_types or [])),
                notes=(
                    f"Auto-promoted from capital partner application {row.id}.\n"
                    + (row.notes or "")
                ).strip(),
                is_active=True,
            )
            db.add(lender)
            await db.flush()
            await db.refresh(lender)
            row.promoted_lender_id = lender.id
        else:
            row.promoted_lender_id = existing.id

    await db.flush()
    await db.refresh(row)
    return CapitalPartnerAppRead.model_validate(row)
