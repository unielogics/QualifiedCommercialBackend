"""Pipeline batch summary endpoints (Phase 6).

The agent Leads pipeline needs per-row AI state + current blocker +
next-follow-up + missing-items + handoff status badges. Fetching
those one client at a time would be N+1 hell, so this endpoint
returns a batch summary keyed by client_id.

Per the plan's pipeline protection rule, this is consumed only by
the agent Leads view. The funding-mode pipeline keeps its existing
behavior on /pipeline/deal-secretary-summary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.agent_task import AgentTask
from app.models.ai_task_assignment import AITaskAssignment
from app.models.client import Client
from app.models.client_requirement_status import ClientRequirementStatus
from app.models.deal import Deal
from app.models.loan import Loan

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


AIStateLiteral = Literal["deployed", "paused", "draft_first", "human_only", "idle"]


class PipelineClientSummary(BaseModel):
    client_id: UUID
    ai_state: AIStateLiteral
    current_blocker: str | None = None
    next_follow_up_at: datetime | None = None
    human_needed: bool = False
    missing_items_count: int = 0
    handoff_status: Literal["none", "requested", "packet_built", "promoted"] = "none"
    funding_status: str | None = None
    ready_for_lending_eligible: bool = False
    deals_count: int = 0
    loans_count: int = 0
    open_tasks_count: int = 0
    last_activity_at: datetime | None = None


@router.get("/client-summary", response_model=list[PipelineClientSummary])
async def client_summary(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    client_ids: str = Query(..., description="Comma-separated client UUIDs"),
) -> list[PipelineClientSummary]:
    """Batch summary for pipeline cards. Returns one row per visible
    client_id. CLIENT role is forbidden — pipeline is operator-only.
    BROKER is restricted to their own clients (via _scope semantics).
    """
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Pipeline is operator-only")

    try:
        ids = [UUID(s.strip()) for s in client_ids.split(",") if s.strip()]
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid client_id list: {e}")
    if not ids:
        return []

    # Visibility — BROKER only sees their own clients. Filter the
    # requested ids against what the user is allowed to see.
    visible_stmt = select(Client.id).where(Client.id.in_(ids))
    if user.role == Role.BROKER:
        if user.broker is None:
            return []
        visible_stmt = visible_stmt.where(Client.broker_id == user.broker.id)
    visible_ids = {row[0] for row in (await db.execute(visible_stmt)).all()}
    if not visible_ids:
        return []
    visible_list = list(visible_ids)

    # Missing-items per client: count CRS rows with status in
    # {missing, asked} regardless of scope (client / deal / loan).
    missing_q = (
        select(
            ClientRequirementStatus.client_id,
            func.count(ClientRequirementStatus.id),
        )
        .where(
            ClientRequirementStatus.client_id.in_(visible_list),
            ClientRequirementStatus.status.in_(["missing", "asked"]),
        )
        .group_by(ClientRequirementStatus.client_id)
    )
    missing_by_client: dict[UUID, int] = {
        row[0]: int(row[1]) for row in (await db.execute(missing_q)).all()
    }

    # Deals + loans counts.
    deals_q = (
        select(Deal.client_id, func.count(Deal.id))
        .where(Deal.client_id.in_(visible_list))
        .group_by(Deal.client_id)
    )
    deals_by_client: dict[UUID, int] = {
        row[0]: int(row[1]) for row in (await db.execute(deals_q)).all()
    }
    loans_q = (
        select(Loan.client_id, func.count(Loan.id))
        .where(Loan.client_id.in_(visible_list))
        .group_by(Loan.client_id)
    )
    loans_by_client: dict[UUID, int] = {
        row[0]: int(row[1]) for row in (await db.execute(loans_q)).all()
    }

    # next_follow_up_at — earliest scheduled next_run_at across all
    # active assignments owned by this client (joined through CRS).
    next_run_q = (
        select(
            ClientRequirementStatus.client_id,
            func.min(AITaskAssignment.next_run_at),
        )
        .join(
            AITaskAssignment,
            AITaskAssignment.client_requirement_status_id == ClientRequirementStatus.id,
        )
        .where(
            ClientRequirementStatus.client_id.in_(visible_list),
            AITaskAssignment.deleted_at.is_(None),
            AITaskAssignment.next_run_at.is_not(None),
        )
        .group_by(ClientRequirementStatus.client_id)
    )
    next_run_by_client: dict[UUID, datetime | None] = {
        row[0]: row[1] for row in (await db.execute(next_run_q)).all()
    }

    # Handoff status — pick the most aggressive across deals (promoted
    # wins, then requested, then none).
    _HANDOFF_RANK = {"none": 0, "requested": 1, "packet_built": 2, "promoted": 3}
    deals_full = (
        await db.execute(select(Deal).where(Deal.client_id.in_(visible_list)))
    ).scalars().all()
    handoff_by_client: dict[UUID, str] = {}
    for d in deals_full:
        cur = handoff_by_client.get(d.client_id, "none")
        if _HANDOFF_RANK.get(d.handoff_status, 0) > _HANDOFF_RANK.get(cur, 0):
            handoff_by_client[d.client_id] = d.handoff_status

    # AgentTask open count per client (Phase 7).
    tasks_q = (
        select(AgentTask.client_id, func.count(AgentTask.id))
        .where(
            AgentTask.client_id.in_(visible_list),
            AgentTask.status.in_(["open", "in_progress", "waiting"]),
        )
        .group_by(AgentTask.client_id)
    )
    tasks_by_client: dict[UUID, int] = {
        row[0]: int(row[1]) for row in (await db.execute(tasks_q)).all()
    }

    # Funding status — pick the latest loan stage per client.
    loans_full = (
        await db.execute(
            select(Loan)
            .where(Loan.client_id.in_(visible_list))
            .order_by(Loan.created_at.desc())
        )
    ).scalars().all()
    funding_by_client: dict[UUID, str] = {}
    for l in loans_full:
        if l.client_id not in funding_by_client:
            funding_by_client[l.client_id] = str(l.stage)

    out: list[PipelineClientSummary] = []
    for cid in visible_list:
        missing = missing_by_client.get(cid, 0)
        # human_needed when there are missing items AND no AI is
        # actively scheduled in the near future. Cheap heuristic:
        # missing > 0 AND next_follow_up_at is None.
        next_at = next_run_by_client.get(cid)
        human_needed = missing > 0 and next_at is None
        out.append(
            PipelineClientSummary(
                client_id=cid,
                ai_state="deployed" if next_at is not None else ("idle" if missing == 0 else "human_only"),
                current_blocker=None,  # Reserved — Phase 7+ fills from AgentTask.
                next_follow_up_at=next_at,
                human_needed=human_needed,
                missing_items_count=missing,
                handoff_status=handoff_by_client.get(cid, "none"),  # type: ignore[arg-type]
                funding_status=funding_by_client.get(cid),
                ready_for_lending_eligible=(
                    deals_by_client.get(cid, 0) > 0
                    and handoff_by_client.get(cid, "none") == "none"
                ),
                deals_count=deals_by_client.get(cid, 0),
                loans_count=loans_by_client.get(cid, 0),
                open_tasks_count=tasks_by_client.get(cid, 0),
                last_activity_at=None,  # Reserved — keep payload tight.
            )
        )
    return out


__all__ = ["router", "PipelineClientSummary"]
