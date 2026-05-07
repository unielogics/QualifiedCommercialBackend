"""FastAPI app entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (
    ai,
    ai_feedback,
    ai_tasks,
    auth,
    brokers,
    calendar,
    clients,
    credit,
    documents,
    email_drafts,
    fred,
    intake,
    legal,
    loan_participants,
    loan_summary,
    loan_workspace,
    loans,
    messages,
    meta,
    prequal,
    rates,
    reports,
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
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is unset — AI orchestrator will fail when invoked.")


@app.get("/", tags=["health"])
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "qcbackend", "env": settings.app_env}


# Mount all routers under /api/v1
api_prefix = "/api/v1"
for r in [
    meta.router,
    auth.router,
    loans.router,
    loan_participants.router,
    loan_summary.router,
    loan_workspace.router,
    clients.router,
    brokers.router,
    documents.router,
    messages.router,
    ai_tasks.router,
    ai.router,
    ai_feedback.router,
    calendar.router,
    credit.router,
    intake.router,
    rates.router,
    fred.router,
    reports.router,
    search.router,
    settings_router.router,
    users.router,
    email_drafts.router,
    legal.router,
    prequal.router,
]:
    app.include_router(r, prefix=api_prefix)
