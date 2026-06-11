"""Lead-funnel metrics — agent dashboard KPIs.

Powers `GET /agents/me/funnel`. Computes 8 metrics scoped to one
broker (or firm-wide for super-admin):

  leads_this_week       — count(Client created in last 7d)
  contacted             — count(Client where stage in C+)
  intake_completion     — % of clients who completed intake
  prequal_conversion    — % of contacted clients who got >=1 prequal
  lead_to_prequal       — avg days from Client.created_at to first
                          Loan.created_at (loans whose stage moved
                          past PREQUALIFIED at some point)
  prequal_to_funded     — avg days from Loan.created_at to
                          Activity('loan.stage_change' with new=funded)
  stale_lead_count      — Clients in lead/contacted with
                          coalesce(contacted_at, created_at) > 7d ago.
                          NOT updated_at (which would tick on phone /
                          city edits and silently un-stale).
  clients_by_stage      — {stage: count} distribution

Velocity metrics return both `value` and `sample_size` so the UI can
dim the readout when N is small (one funded loan ≠ a meaningful
average). v2 follow-up: bring in real engagement events (call logs,
inbound replies) for stale-lead detection.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ClientStage, LoanStage
from app.models.activity import Activity
from app.models.client import Client
from app.models.loan import Loan
from app.schemas.agent_metrics import FunnelMetricsRead, FunnelStat

log = logging.getLogger(__name__)


# Stages that count as "contacted+" (anything past initial lead).
_CONTACTED_OR_BEYOND: list[str] = [
    ClientStage.CONTACTED,
    ClientStage.VERIFIED,
    ClientStage.READY_FOR_LENDING,
    ClientStage.PROCESSING,
    ClientStage.FUNDED,
]

# Stale-lead window — leads/contacteds with no activity in this
# many days are surfaced. Matches the v1 NBA logic.
_STALE_DAYS = 7


async def compute_funnel(
    db: AsyncSession,
    *,
    broker_id: UUID | None,
    broker_ids: list[UUID] | None = None,
) -> FunnelMetricsRead:
    """Compute the 8 funnel KPIs for one broker (or firm-wide).

    `broker_id=None, broker_ids=None` → firm-wide (super-admin path).
    `broker_id=<uuid>` → scoped to that broker's clients.
    `broker_ids=[]` → empty portfolio.

    Single async session, batched into a few aggregate queries to
    keep latency bounded as a broker's book grows.
    """
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    stale_threshold = now - timedelta(days=_STALE_DAYS)

    # Base scope: filter Client by broker (or unfiltered for super).
    def _scope(stmt):
        if broker_ids is not None:
            if not broker_ids:
                return stmt.where(Client.broker_id.in_([]))
            return stmt.where(Client.broker_id.in_(broker_ids))
        if broker_id is not None:
            return stmt.where(Client.broker_id == broker_id)
        return stmt

    # ── 1. clients_by_stage + total counts (single grouped query) ──
    by_stage_rows = (
        await db.execute(
            _scope(
                select(Client.stage, func.count(Client.id))
                .group_by(Client.stage)
            )
        )
    ).all()
    clients_by_stage: dict[str, int] = {str(stage): n for stage, n in by_stage_rows}
    total_clients = sum(clients_by_stage.values())
    contacted_count = sum(
        n for stage, n in clients_by_stage.items()
        if stage in _CONTACTED_OR_BEYOND
    )

    # ── 2. leads_this_week ──
    leads_this_week = (
        await db.execute(
            _scope(
                select(func.count(Client.id)).where(Client.created_at >= week_ago)
            )
        )
    ).scalar_one()

    # ── 3. intake completion % ──
    intake_completed = (
        await db.execute(
            _scope(
                select(func.count(Client.id)).where(
                    Client.intake_completed_at.is_not(None)
                )
            )
        )
    ).scalar_one()
    intake_pct: float | None = (
        (intake_completed / total_clients) * 100 if total_clients else None
    )

    # ── 4. prequal_conversion % ──
    # = clients with >= 1 prequalified-or-beyond loan / contacted_count
    prequal_clients_stmt = (
        select(func.count(func.distinct(Loan.client_id)))
        .where(Loan.stage != LoanStage.PREQUALIFIED.value)  # post-prequal
    )
    if broker_ids is not None:
        prequal_clients_stmt = prequal_clients_stmt.where(Loan.broker_id.in_(broker_ids))
    elif broker_id is not None:
        prequal_clients_stmt = prequal_clients_stmt.where(Loan.broker_id == broker_id)
    # The "stage != prequalified" filter actually means we count loans
    # that have advanced PAST the initial prequalified state. Use the
    # OR of (in any post-prequal stage) for clarity — anything beyond
    # prequal counts as "successfully progressed".
    prequal_clients = (
        await db.execute(prequal_clients_stmt)
    ).scalar_one()
    prequal_pct: float | None = (
        (prequal_clients / contacted_count) * 100 if contacted_count else None
    )

    # ── 5. lead_to_prequal velocity (avg days) ──
    # For each loan past PREQUALIFIED, days = Loan.created_at - Client.created_at
    velocity_l2p_stmt = (
        select(
            func.avg(
                func.extract("epoch", Loan.created_at - Client.created_at)
                / 86400.0
            ),
            func.count(Loan.id),
        )
        .join(Client, Client.id == Loan.client_id)
        .where(Loan.stage != LoanStage.PREQUALIFIED.value)
    )
    if broker_ids is not None:
        velocity_l2p_stmt = velocity_l2p_stmt.where(Loan.broker_id.in_(broker_ids))
    elif broker_id is not None:
        velocity_l2p_stmt = velocity_l2p_stmt.where(Loan.broker_id == broker_id)
    avg_l2p, n_l2p = (
        await db.execute(velocity_l2p_stmt)
    ).one()

    # ── 6. prequal_to_funded velocity (avg days) ──
    # For each Loan currently FUNDED, find the Activity row recording
    # the stage transition to funded. Days = activity.occurred_at -
    # loan.created_at.
    funded_loans_stmt = (
        select(Loan.id, Loan.created_at)
        .where(Loan.stage == LoanStage.FUNDED.value)
    )
    if broker_ids is not None:
        funded_loans_stmt = funded_loans_stmt.where(Loan.broker_id.in_(broker_ids))
    elif broker_id is not None:
        funded_loans_stmt = funded_loans_stmt.where(Loan.broker_id == broker_id)
    funded_loans = (await db.execute(funded_loans_stmt)).all()

    p2f_diffs: list[float] = []
    for loan_id, created_at in funded_loans:
        # Pick the FIRST stage_change → funded for this loan
        stage_act = (
            await db.execute(
                select(Activity.occurred_at)
                .where(
                    Activity.loan_id == loan_id,
                    Activity.kind == "loan.stage_change",
                )
                .order_by(Activity.occurred_at.asc())
                .limit(20)
            )
        ).all()
        funded_at = None
        for (occurred,) in stage_act:
            funded_at = occurred
            # Stop at the latest one — assumes the chronologically
            # final stage_change for a funded loan IS the funded
            # transition. Activity payload would be more precise
            # but kind alone is sufficient for v1 averages.
        if funded_at is not None:
            p2f_diffs.append((funded_at - created_at).total_seconds() / 86400.0)
    avg_p2f = sum(p2f_diffs) / len(p2f_diffs) if p2f_diffs else None
    n_p2f = len(p2f_diffs)

    # ── 7. stale_lead_count ──
    stale_count = (
        await db.execute(
            _scope(
                select(func.count(Client.id)).where(
                    Client.stage.in_([ClientStage.LEAD, ClientStage.CONTACTED]),
                    func.coalesce(Client.contacted_at, Client.created_at) < stale_threshold,
                )
            )
        )
    ).scalar_one()

    return FunnelMetricsRead(
        leads_this_week=leads_this_week,
        contacted=contacted_count,
        stale_lead_count=stale_count,
        intake_completion=FunnelStat(value=intake_pct, sample_size=total_clients),
        prequal_conversion=FunnelStat(value=prequal_pct, sample_size=contacted_count),
        lead_to_prequal=FunnelStat(
            value=float(avg_l2p) if avg_l2p is not None else None,
            sample_size=int(n_l2p or 0),
        ),
        prequal_to_funded=FunnelStat(
            value=avg_p2f, sample_size=n_p2f,
        ),
        clients_by_stage=clients_by_stage,
    )
