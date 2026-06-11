"""Operator dashboard reports.

Aggregates loans into the headline KPIs the desktop dashboard renders. Kept
intentionally simple — every value is computed from the current Loan rows
in scope (so role gating still applies). Delta fields are reserved for a
future "trailing 30/90-day" comparison; today they're returned as None.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import LOAN_STAGE_ORDER, LoanStage, Role
from app.models.loan import Loan
from app.scoping import scope_loan_query

router = APIRouter(prefix="/reports", tags=["reports"])


class StageBreakdown(BaseModel):
    stage: LoanStage
    count: int
    value: float


class TypeBreakdown(BaseModel):
    type: str
    count: int
    value: float


class DashboardReport(BaseModel):
    funded_ytd: float
    funded_ytd_delta: float | None = None
    pipeline_value: float
    pipeline_count: int
    avg_close_days: int | None = None
    avg_close_delta: int | None = None
    pull_through: float | None = None  # 0..1
    pull_through_delta: float | None = None
    by_stage: list[StageBreakdown]
    by_type: list[TypeBreakdown]


def _scope(user, stmt):
    return scope_loan_query(user, stmt)


@router.get("/dashboard", response_model=DashboardReport)
async def dashboard_report(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DashboardReport:
    rows = (await db.execute(_scope(user, select(Loan)))).scalars().all()

    today = date.today()
    year_start = date(today.year, 1, 1)

    funded_ytd = sum(
        float(r.amount)
        for r in rows
        if r.stage == LoanStage.FUNDED and r.close_date and r.close_date >= year_start
    )

    pipeline_loans = [r for r in rows if r.stage != LoanStage.FUNDED]
    pipeline_value = sum(float(r.amount) for r in pipeline_loans)
    pipeline_count = len(pipeline_loans)

    # Pull-through = funded / (funded + active). Cheap proxy for now;
    # production should use a 90-day rolling cohort.
    funded_count = sum(1 for r in rows if r.stage == LoanStage.FUNDED)
    total_lifecycle = funded_count + pipeline_count
    pull_through = (funded_count / total_lifecycle) if total_lifecycle > 0 else None

    # Avg close days = mean(close_date - created_at) for funded loans this year
    closes_in_days: list[int] = []
    for r in rows:
        if r.stage == LoanStage.FUNDED and r.close_date and r.created_at:
            delta = (r.close_date - r.created_at.date()).days
            if delta > 0:
                closes_in_days.append(delta)
    avg_close_days = round(sum(closes_in_days) / len(closes_in_days)) if closes_in_days else None

    # Stage breakdown — preserve canonical 6-stage order
    by_stage: list[StageBreakdown] = []
    for stage in LOAN_STAGE_ORDER:
        items = [r for r in rows if r.stage == stage]
        by_stage.append(
            StageBreakdown(
                stage=stage,
                count=len(items),
                value=sum(float(r.amount) for r in items),
            )
        )

    # Type breakdown — only loan types actually present
    type_buckets: dict[str, list[Loan]] = {}
    for r in rows:
        type_buckets.setdefault(
            str(r.type.value if hasattr(r.type, "value") else r.type), []
        ).append(r)
    by_type = [
        TypeBreakdown(type=k, count=len(v), value=sum(float(r.amount) for r in v))
        for k, v in sorted(type_buckets.items(), key=lambda kv: -sum(float(r.amount) for r in kv[1]))
    ]

    return DashboardReport(
        funded_ytd=funded_ytd,
        pipeline_value=pipeline_value,
        pipeline_count=pipeline_count,
        avg_close_days=avg_close_days,
        pull_through=pull_through,
        by_stage=by_stage,
        by_type=by_type,
    )
