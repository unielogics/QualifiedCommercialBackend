"""Agent dashboard endpoints — funnel + Next Best Actions.

Two endpoints (alembic 0024 + agent_metrics):

  GET /agents/me/funnel        — 8 funnel KPIs (FunnelMetricsRead)
  GET /agents/me/next-actions  — ranked, capped list of NBA rows

Scope rule (mirrored across both):
  - BROKER → user.broker.id (their book only)
  - SUPER_ADMIN / LOAN_EXEC → broker_id=None (firm-wide)
  - CLIENT → 403 (this is an operator surface)

Sibling to /me/broker-settings — keeps personal settings (me.py) and
agent metrics (this file) in separate routers so the surfaces don't
bloat each other.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.schemas.agent_metrics import FunnelMetricsRead, NextActionRead
from app.services.lead_funnel import compute_funnel
from app.services.next_actions import compute_next_actions

router = APIRouter(prefix="/agents", tags=["agents"])
log = logging.getLogger(__name__)


def _broker_scope_or_403(user) -> "UUID | None":
    """Resolves the calling user to a broker_id (None = firm-wide).
    Raises 403 for borrowers — these endpoints are operator-only."""
    if user.role == Role.CLIENT:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Agent metrics is operator-only"
        )
    if user.role == Role.BROKER:
        if user.broker is None:
            # Edge case — broker user without a Broker row. Treat as
            # firm-wide rather than 500-ing; super-admin can fix the
            # data later.
            log.warning("agent metrics: BROKER user %s has no broker row", user.id)
            return None
        return user.broker.id
    return None  # SUPER_ADMIN, LOAN_EXEC see firm-wide


@router.get("/me/funnel", response_model=FunnelMetricsRead)
async def get_my_funnel(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> FunnelMetricsRead:
    """Lead-funnel KPIs for the dashboard. Scoped by role."""
    broker_id = _broker_scope_or_403(user)
    return await compute_funnel(db, broker_id=broker_id)


@router.get("/me/next-actions", response_model=list[NextActionRead])
async def get_my_next_actions(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[NextActionRead]:
    """Ranked Next Best Actions. Returns up to 8 rows.

    Per-client dedup keeps the highest-priority action when one
    client has multiple signals (stale lead + overdue doc → one
    row). Null-loan `pending_task` items have no client_id and ride
    alongside without affecting dedup.
    """
    broker_id = _broker_scope_or_403(user)
    return await compute_next_actions(db, broker_id=broker_id)
