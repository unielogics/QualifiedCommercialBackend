"""FastAPI app entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal
from app.routers import (
    admin as admin_router,
    agent_tasks,
    agents,
    ai,
    ai_agents,
    ai_feedback,
    ai_voice,
    ai_preview,
    ai_tasks,
    analysis,
    auth,
    billing,
    brokers,
    buckets,
    calendar,
    clients,
    closing_costs,
    credit,
    deal_chat,
    dealer_ai_intake,
    fix_flip,
    deal_secretary,
    deals,
    devices,
    documents,
    email_drafts,
    fred,
    intake,
    legal,
    lender_packages,
    lenders,
    lending_admin,
    loan_participants,
    me,
    loan_summary,
    loan_workspace,
    loans,
    messages,
    meta,
    notifications,
    pipeline as pipeline_router,
    prequal,
    public as public_router,
    rates,
    regional_managers,
    reports,
    webhooks as webhooks_router,
    search,
    settings as settings_router,
    users,
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


@app.on_event("shutdown")
async def shutdown() -> None:
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
    meta.router,
    auth.router,
    admin_router.router,
    loans.router,
    loan_participants.router,
    loan_summary.router,
    loan_workspace.router,
    loan_workspace.public_router,  # /public/hud/{token} — no auth
    clients.router,
    deals.router,
    deal_chat.router,
    agent_tasks.router,
    brokers.router,
    buckets.router,
    dealer_ai_intake.router,
    dealer_ai_intake.client_router,
    documents.router,
    messages.router,
    notifications.router,
    ai_tasks.router,
    ai.router,
    ai_agents.router,
    ai_voice.router,
    ai_feedback.router,
    ai_preview.router,
    analysis.router,
    analysis.property_router,
    calendar.router,
    billing.router,
    credit.router,
    intake.router,
    rates.router,
    regional_managers.router,
    fred.router,
    reports.router,
    search.router,
    settings_router.router,
    users.router,
    email_drafts.router,
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
    webhooks_router.router,
]:
    app.include_router(r, prefix=api_prefix)
