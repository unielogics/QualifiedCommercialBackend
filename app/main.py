"""FastAPI app entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal
from app.dealer_os import crm_router as dealer_os_crm_router
from app.dealer_os import router as dealer_os_router
from app.routers import (
    admin as admin_router,
)
from app.routers import (
    agent_tasks,
    agents,
    agreements,
    ai,
    ai_agents,
    ai_feedback,
    ai_preview,
    ai_tasks,
    ai_voice,
    analysis,
    application_profiles,
    production_packages,
    auth,
    billing,
    brokers,
    buckets,
    calendar,
    client_access,
    clients,
    closing_costs,
    communications,
    contracts,
    credit,
    deal_chat,
    deal_secretary,
    dealer_ai_intake,
    deals,
    devices,
    documents,
    email_drafts,
    fix_flip,
    fred,
    inbox,
    intake,
    legal,
    lender_packages,
    lenders,
    lending_admin,
    loan_participants,
    loan_summary,
    loan_workspace,
    loans,
    me,
    messages,
    meta,
    notifications,
    operator_files,
    prequal,
    rates,
    regional_managers,
    reports,
    search,
    users,
)
from app.routers import (
    google as google_router,
)
from app.routers import (
    pipeline as pipeline_router,
)
from app.routers import (
    public as public_router,
)
from app.routers import (
    settings as settings_router,
)
from app.routers import (
    sms as sms_router,
)
from app.routers import (
    webhooks as webhooks_router,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
log = logging.getLogger("qc")

app = FastAPI(
    title="Qualified Commercial API",
    version="0.1.0",
    description=(
        "Center of Truth backend for the AI-driven brokerage underwriting platform. "
        "See docs/ARCHITECTURE.md for locked-in constraints."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Let browser fetch() read the download filename the server sets (dealer-named
    # package.zip / intelligence.pdf); without this the header is hidden cross-origin.
    expose_headers=["Content-Disposition"],
)


@app.on_event("startup")
async def startup() -> None:
    if not settings.clerk_secret_key:
        log.warning(
            "CLERK_SECRET_KEY is unset — running in DEV auth mode (every request "
            "treated as a seeded super_admin). Set CLERK_SECRET_KEY before prod."
        )
    if not settings.ai_provider_enabled:
        log.warning(
            "Bedrock AI is disabled — set BEDROCK_ENABLED=true for IAM auth "
            "or AWS_BEARER_TOKEN_BEDROCK for bearer-token auth."
        )

    # Start the in-process APScheduler. See app/services/scheduler.py
    # for the SINGLE-INSTANCE assumption — must move to AWS EventBridge
    # before scaling out to multiple backend instances.
    from app.services.scheduler import start_scheduler
    start_scheduler()
    from app.services.communication_events import broker as communication_event_broker
    await communication_event_broker.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    from app.services.communication_events import broker as communication_event_broker
    await communication_event_broker.stop()
    from app.services.scheduler import shutdown_scheduler
    shutdown_scheduler()


@app.get("/", tags=["health"])
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "qcbackend", "env": settings.app_env}


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness: process is up. Does not touch dependencies."""
    return {"status": "ok", "service": "qcbackend", "env": settings.app_env}


@app.get("/ready", tags=["health"])
async def ready() -> dict[str, str]:
    """Readiness: process is up and can reach Postgres."""
    async with SessionLocal() as db:
        await db.execute(text("SELECT 1"))
    return {"status": "ready", "service": "qcbackend", "database": "ok"}


# Mount all routers under /api/v1
api_prefix = "/api/v1"
for r in [
    dealer_os_router.router,
    dealer_os_crm_router.router,
    meta.router,
    auth.router,
    client_access.router,
    admin_router.router,
    loans.router,
    loan_participants.router,
    loan_summary.router,
    loan_workspace.router,
    loan_workspace.public_router,  # /public/hud/{token} — no auth
    clients.router,
    communications.router,
    deals.router,
    deal_chat.router,
    agent_tasks.router,
    brokers.router,
    contracts.router,
    agreements.router,
    buckets.router,
    dealer_ai_intake.router,
    dealer_ai_intake.funding_router,
    dealer_ai_intake.client_router,
    dealer_ai_intake.admin_router,
    dealer_ai_intake.broker_router,
    dealer_ai_intake.mca_router,
    documents.router,
    messages.router,
    notifications.router,
    operator_files.router,
    ai_tasks.router,
    ai.router,
    ai_agents.router,
    ai_voice.router,
    ai_feedback.router,
    ai_preview.router,
    analysis.router,
    application_profiles.router,
    production_packages.router,
    production_packages.public_router,
    analysis.property_router,
    analysis.public_address_router,
    calendar.router,
    billing.router,
    credit.router,
    intake.router,
    rates.router,
    regional_managers.router,
    fred.router,
    google_router.router,
    reports.router,
    search.router,
    settings_router.router,
    users.router,
    email_drafts.router,
    inbox.router,
    legal.router,
    lender_packages.router,
    lenders.router,
    closing_costs.router,
    lending_admin.router,
    prequal.router,
    devices.router,
    me.router,
    agents.router,
    deal_secretary.router,
    fix_flip.router,
    pipeline_router.router,
    public_router.router,
    sms_router.router,
    webhooks_router.router,
]:
    app.include_router(r, prefix=api_prefix)
