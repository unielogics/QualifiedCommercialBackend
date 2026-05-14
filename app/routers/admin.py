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
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import LoanStage, Role
from app.models.lender import Lender
from app.models.loan import Loan
from app.services.lender_thread import LenderThreadError, inject_inbound_lender_email

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
        checks.append(
            HealthCheck(
                name="Gmail outbound",
                status="ok",
                detail=f"Service-account JSON at {sa_path}; delegated user {delegated}.",
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

    # 6. Anthropic key — the lender-thread mini-summary + instruct_ai
    # reply both fall back to deterministic text when absent, so this is
    # only a warning.
    if not settings.anthropic_api_key:
        checks.append(
            HealthCheck(
                name="AI summaries",
                status="warn",
                detail="ANTHROPIC_API_KEY unset — thread summaries fall back to deterministic text.",
            )
        )
    else:
        checks.append(
            HealthCheck(
                name="AI summaries",
                status="ok",
                detail=f"Anthropic key present; using {settings.anthropic_model_light}.",
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
