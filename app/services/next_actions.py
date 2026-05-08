"""Next Best Actions — daily action queue for the agent dashboard.

Scans the agent's clients + loans + docs + AITasks and produces a
ranked list of ~5-8 actions worth doing today. Backs the "Next Best
Actions" card on AgentHomeView and (later) the mobile agent home.

Four sources merge into one ranked feed:

  call_lead     — stale clients (lead/contacted, no contact > 3d)
  chase_doc     — loans with documents in just_late / week_late /
                  escalating scenarios
  closing_prep  — loans whose close_date is within 7 days AND have
                  open required docs
  pending_task  — AITasks in PENDING for the broker's loans (or
                  firm-wide null-loan tasks)

Ranking: priority desc → kind weight → freshness desc. Cap at 8.

Per-client dedup: when one client has multiple client-scoped
signals (stale lead AND overdue doc on their loan), keep ONE row —
whichever has the highest priority. Null-client `pending_task`
items bypass dedup and ride alongside.

No caching in v1 (see plan). Re-computed on every request — joins
are bounded to one broker's book.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    AITaskPriority,
    AITaskStatus,
    ClientStage,
    DocStatus,
    LoanStage,
)
from app.models.ai_task import AITask
from app.models.client import Client
from app.models.document import Document
from app.models.loan import Loan
from app.schemas.agent_metrics import NextActionRead

log = logging.getLogger(__name__)


_MAX_NBA = 8

# Sort weight: lower is "more attention-worthy" within the same
# priority bucket. Tuple compares (priority_rank, kind_rank).
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
_KIND_RANK = {
    "call_lead": 0,      # human signal — top of mind
    "closing_prep": 1,   # money is moving — second
    "chase_doc": 2,      # routine collection — third
    "pending_task": 3,   # operator queue — last (already in inbox)
}


async def compute_next_actions(
    db: AsyncSession,
    *,
    broker_id: UUID | None,
) -> list[NextActionRead]:
    """Scan the four sources, dedupe by client, rank, cap at 8."""
    now = datetime.now(timezone.utc)
    today = date.today()

    items: list[NextActionRead] = []

    # ── Source 1: call_lead — stale clients in lead/contacted ──
    stale_threshold = now - timedelta(days=3)
    very_stale_threshold = now - timedelta(days=7)
    stale_stmt = select(Client).where(
        Client.stage.in_([ClientStage.LEAD, ClientStage.CONTACTED])
    )
    if broker_id is not None:
        stale_stmt = stale_stmt.where(Client.broker_id == broker_id)
    stale_clients = (await db.execute(stale_stmt)).scalars().all()
    for c in stale_clients:
        last_touch = c.contacted_at or c.created_at
        if last_touch is None or last_touch > stale_threshold:
            continue
        days = (now - last_touch).days
        priority = "high" if last_touch < very_stale_threshold else "medium"
        items.append(NextActionRead(
            id=f"call_lead:{c.id}",
            kind="call_lead",
            priority=priority,
            title=f"Call {c.name}",
            subtitle=(
                f"{c.stage.value if hasattr(c.stage, 'value') else c.stage} — "
                f"no contact in {days}d"
            ),
            target_type="client",
            target_id=c.id,
            deeplink=f"/clients/{c.id}",
            created_at=last_touch,
            client_id=c.id,
            loan_id=None,
        ))

    # ── Source 2: chase_doc — overdue REQUESTED docs on broker's loans ──
    # Use the existing scenario classifier so the priority gradient
    # matches what the chat-side reminders use.
    from app.services.doc_collection_ai import classify as _classify
    from app.services.loan_intake_automation import (
        _coerce_settings,
        _DEFAULT_FIRST_DAYS,
    )
    from app.models.app_settings import AppSettings

    loans_stmt = select(Loan)
    if broker_id is not None:
        loans_stmt = loans_stmt.where(Loan.broker_id == broker_id)
    loans = (await db.execute(loans_stmt)).scalars().all()
    loan_by_id = {ln.id: ln for ln in loans}
    if loans:
        loan_ids = [ln.id for ln in loans]
        docs = (
            await db.execute(
                select(Document).where(
                    Document.loan_id.in_(loan_ids),
                    Document.status == DocStatus.REQUESTED,
                    Document.requested_on.is_not(None),
                )
            )
        ).scalars().all()
        # Read settings once to find per-item due_offset_days fallbacks.
        settings_row = (
            await db.execute(select(AppSettings).limit(1))
        ).scalar_one_or_none()
        settings = _coerce_settings(settings_row)

        for d in docs:
            ln = loan_by_id.get(d.loan_id)
            if ln is None:
                continue
            # Due date with the same precedence as the cron uses.
            if d.due_date is not None:
                effective_due = d.due_date
            else:
                checklist = settings.checklists.get(str(ln.type))
                offset = checklist.first_reminder_days if checklist else _DEFAULT_FIRST_DAYS
                if checklist:
                    for item in checklist.docs or []:
                        if item.name == d.checklist_key or item.name == d.name:
                            offset = item.due_offset_days
                            break
                effective_due = d.requested_on + timedelta(days=offset)
            days_until = (effective_due - today).days
            scenario = _classify(days_until)
            if scenario not in ("just_late", "week_late", "escalating"):
                continue
            priority = (
                "high" if scenario in ("week_late", "escalating") else "medium"
            )
            items.append(NextActionRead(
                id=f"chase_doc:{d.id}",
                kind="chase_doc",
                priority=priority,
                title=f"Chase {d.name}",
                subtitle=f"{ln.deal_id} — {abs(days_until)}d overdue ({scenario})",
                target_type="loan",
                target_id=ln.id,
                deeplink=f"/loans/{ln.id}#workflow",
                created_at=now - timedelta(days=abs(days_until)),
                client_id=ln.client_id,
                loan_id=ln.id,
            ))

    # ── Source 3: closing_prep — loans closing soon with open docs ──
    soon = today + timedelta(days=7)
    for ln in loans:
        if ln.close_date is None or ln.close_date > soon or ln.close_date < today:
            continue
        if ln.stage == LoanStage.FUNDED:
            continue
        # Has any open required doc?
        open_docs = (
            await db.execute(
                select(Document).where(
                    Document.loan_id == ln.id,
                    Document.status == DocStatus.REQUESTED,
                ).limit(1)
            )
        ).scalars().first()
        if open_docs is None:
            continue
        days_to_close = (ln.close_date - today).days
        items.append(NextActionRead(
            id=f"closing_prep:{ln.id}",
            kind="closing_prep",
            priority="high",
            title=f"Close prep — {ln.deal_id}",
            subtitle=f"Closes in {days_to_close}d, open docs outstanding",
            target_type="loan",
            target_id=ln.id,
            deeplink=f"/loans/{ln.id}#workflow",
            created_at=now,
            client_id=ln.client_id,
            loan_id=ln.id,
        ))

    # ── Source 4: pending_task — AITask PENDING for broker's loans ──
    task_stmt = select(AITask).where(AITask.status == AITaskStatus.PENDING)
    if broker_id is not None:
        task_stmt = task_stmt.where(
            or_(
                AITask.loan_id.is_(None),
                AITask.loan_id.in_(
                    select(Loan.id).where(Loan.broker_id == broker_id)
                ),
            )
        )
    tasks = (await db.execute(task_stmt.limit(20))).scalars().all()
    for t in tasks:
        prio_str = (
            "high" if t.priority == AITaskPriority.HIGH
            else "medium" if t.priority == AITaskPriority.MEDIUM
            else "low"
        )
        ln = loan_by_id.get(t.loan_id) if t.loan_id else None
        items.append(NextActionRead(
            id=f"pending_task:{t.id}",
            kind="pending_task",
            priority=prio_str,
            title=t.title or "AI Task",
            subtitle=(t.summary or "").split("\n", 1)[0][:140],
            target_type="ai_task",
            target_id=t.id,
            deeplink=f"/ai-inbox?task={t.id}",
            created_at=t.created_at,
            client_id=ln.client_id if ln else None,
            loan_id=t.loan_id,
        ))

    # ── Per-client dedup (highest-priority kind wins per client) ──
    by_client: dict[UUID, NextActionRead] = {}
    standalone: list[NextActionRead] = []
    for it in items:
        if it.client_id is None:
            # Null-client items (firm-wide pending_task) ride alongside
            # without competing for the per-client slot.
            standalone.append(it)
            continue
        prev = by_client.get(it.client_id)
        if prev is None or _rank(it) < _rank(prev):
            by_client[it.client_id] = it
    merged = list(by_client.values()) + standalone

    # ── Sort + cap ──
    merged.sort(key=_rank)
    return merged[:_MAX_NBA]


def _rank(item: NextActionRead) -> tuple[int, int, float]:
    """Lower tuple = higher priority. Sort key for the merged list."""
    return (
        _PRIORITY_RANK.get(item.priority, 9),
        _KIND_RANK.get(item.kind, 9),
        # Negative timestamp so newer signals beat older within ties.
        -(item.created_at.timestamp() if item.created_at else 0),
    )
