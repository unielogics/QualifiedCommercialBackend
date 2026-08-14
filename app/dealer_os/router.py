"""Dealer OS API — everything under /api/v1/dealer-os/* (isolation contract).

Stream 1 surface: team console CRUD + the per-dealer Targets & Settings
endpoints (AI propose / admin override, override-always-wins). Engines,
ledger, plan, forecast, messaging land in Streams 2-5 on this same router.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role

from .deps import load_dealer, require_team, require_team_or_dealer, resolve_dealer_scope
from .models import (
    DealerAddback,
    DealerAlert,
    DealerBusiness,
    DealerCashEvent,
    DealerFinancialPeriod,
    DealerMessage,
    DealerMetricLineage,
    DealerMetricSnapshot,
    DealerMetricTarget,
    DealerPlanAction,
    DealerSession,
    DealerSourceConnection,
)
from .schemas import (
    AddbackRead,
    AlertRead,
    CashEventPatch,
    CashEventRead,
    CashImport,
    CashImportResult,
    DealerCreate,
    DealerListItem,
    DealerRead,
    DealerUpdate,
    ForecastRead,
    GlobalAlertRead,
    HealthRead,
    LenderPackageRead,
    MessageCreate,
    MessageRead,
    PathsRead,
    PeriodRead,
    PeriodUpsert,
    PlanActionCreate,
    PlanActionRead,
    PlanActionUpdate,
    SessionCreate,
    SessionRead,
    SnapshotRead,
    TargetOverride,
    TargetRead,
)
from .services.engines import recompute_snapshot
from .services.forecast import compute_forecast
from .services.normalize import classify_event, period_of, rebuild_periods
from .services.paths import compute_ladder, compute_paths
from .services.targets import propose_targets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dealer-os", tags=["dealer-os"])


@router.get("/dealers", response_model=list[DealerListItem])
async def list_dealers(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[DealerListItem]:
    # Team sees the whole book; a DEALER login sees only businesses linked to it
    # (dealer_user_id) — this is what powers the self-serve "My business" view.
    require_team_or_dealer(user)
    stmt = select(DealerBusiness).order_by(DealerBusiness.created_at.desc())
    if user.role == Role.DEALER:
        stmt = stmt.where(DealerBusiness.dealer_user_id == user.id)
    dealers = (await db.execute(stmt)).scalars().all()
    if not dealers:
        return []
    ids = [d.id for d in dealers]
    # latest snapshot per dealer + open alert counts, batched
    snaps = (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id.in_(ids))
            .order_by(DealerMetricSnapshot.dealer_id, DealerMetricSnapshot.as_of.desc())
        )
    ).scalars().all()
    latest: dict[UUID, DealerMetricSnapshot] = {}
    for s in snaps:
        latest.setdefault(s.dealer_id, s)
    alerts = dict(
        (
            await db.execute(
                select(DealerAlert.dealer_id, func.count())
                .where(DealerAlert.dealer_id.in_(ids), DealerAlert.resolved_at.is_(None))
                .group_by(DealerAlert.dealer_id)
            )
        ).all()
    )
    out: list[DealerListItem] = []
    for d in dealers:
        item = DealerListItem.model_validate(d)
        snap = latest.get(d.id)
        if snap is not None:
            item.score = float(snap.score) if snap.score is not None else None
            item.tier = snap.tier
        item.open_alerts = int(alerts.get(d.id, 0))
        out.append(item)
    return out


@router.post("/dealers", response_model=DealerRead, status_code=status.HTTP_201_CREATED)
async def create_dealer(payload: DealerCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DealerBusiness:
    require_team(user)
    dealer = DealerBusiness(**payload.model_dump(), owner_user_id=user.id)
    db.add(dealer)
    await db.flush()
    # Every dealer starts with the uploads source active and a full set of
    # AI-proposed targets, so the cockpit is never empty.
    db.add(DealerSourceConnection(dealer_id=dealer.id, kind="uploads", status="active"))
    await propose_targets(db, dealer)
    await db.commit()
    await db.refresh(dealer)
    return dealer


@router.get("/dealers/{dealer_id}", response_model=DealerRead)
async def get_dealer(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DealerBusiness:
    require_team_or_dealer(user)
    return await resolve_dealer_scope(db, user, dealer_id)


@router.patch("/dealers/{dealer_id}", response_model=DealerRead)
async def update_dealer(
    dealer_id: UUID, payload: DealerUpdate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerBusiness:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(dealer, k, v)
    await db.commit()
    await db.refresh(dealer)
    return dealer


def _target_read(t: DealerMetricTarget) -> TargetRead:
    r = TargetRead.model_validate(t)
    r.effective_value = float(t.effective_value) if t.effective_value is not None else None
    return r


@router.get("/dealers/{dealer_id}/targets", response_model=list[TargetRead])
async def list_targets(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[TargetRead]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows = (
        await db.execute(
            select(DealerMetricTarget)
            .where(DealerMetricTarget.dealer_id == dealer.id)
            .order_by(DealerMetricTarget.metric_key)
        )
    ).scalars().all()
    return [_target_read(t) for t in rows]


@router.post("/dealers/{dealer_id}/targets/propose", response_model=list[TargetRead])
async def repropose_targets(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[TargetRead]:
    """Refresh AI proposals. Never touches admin overrides — an overridden row
    keeps its admin_value and simply carries the newer suggestion beside it."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    rows = await propose_targets(db, dealer)
    await db.commit()
    return [_target_read(t) for t in rows]


@router.put("/dealers/{dealer_id}/targets", response_model=TargetRead)
async def override_target(
    dealer_id: UUID, payload: TargetOverride, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TargetRead:
    """Set (or clear, with admin_value=null) the admin override. Override wins."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    row = (
        await db.execute(
            select(DealerMetricTarget).where(
                DealerMetricTarget.dealer_id == dealer.id,
                DealerMetricTarget.metric_key == payload.metric_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown metric for this dealer — propose targets first")
    row.admin_value = payload.admin_value
    row.admin_set_by_user_id = user.id
    row.admin_set_at = datetime.now(timezone.utc)
    row.status = "overridden" if payload.admin_value is not None else "ai_proposed"
    await db.commit()
    await db.refresh(row)
    return _target_read(row)


# --- Stream 2: ingestion & normalization -----------------------------------


@router.post(
    "/dealers/{dealer_id}/cash-events/import",
    response_model=CashImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_cash_events(
    dealer_id: UUID, payload: CashImport, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> CashImportResult:
    """Bulk-import statement/CSV lines. Each row is AI-classified via the
    normalization rules, then the affected monthly periods are rebuilt."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    periods: set[date] = set()
    for row in payload.rows:
        category, flags = classify_event(row.description, row.amount)
        period = period_of(row.occurred_on)
        periods.add(period)
        db.add(
            DealerCashEvent(
                dealer_id=dealer.id,
                period=period,
                occurred_on=row.occurred_on,
                description=row.description,
                amount=row.amount,
                category=category,
                flags=flags,
                invoice_date=row.invoice_date,
                due_date=row.due_date,
                categorized_by="ai",
                source="upload",
            )
        )
    await db.flush()
    touched = await rebuild_periods(db, dealer.id, periods)
    # Best-effort engine refresh — never fail the ingest on engine errors.
    try:
        await recompute_snapshot(db, dealer.id)
    except Exception:
        logger.exception("dealer-os: snapshot recompute failed after cash import for %s", dealer.id)
    await db.commit()
    return CashImportResult(imported=len(payload.rows), periods=touched)


@router.get("/dealers/{dealer_id}/cash-events", response_model=list[CashEventRead])
async def list_cash_events(
    dealer_id: UUID,
    user: CurrentUser,
    period: date | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> list[DealerCashEvent]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerCashEvent).where(DealerCashEvent.dealer_id == dealer.id)
    if period is not None:
        q = q.where(DealerCashEvent.period == period)
    q = q.order_by(DealerCashEvent.occurred_on.asc()).limit(limit)
    return (await db.execute(q)).scalars().all()


@router.patch("/dealers/{dealer_id}/cash-events/{event_id}", response_model=CashEventRead)
async def patch_cash_event(
    dealer_id: UUID,
    event_id: UUID,
    payload: CashEventPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerCashEvent:
    """Admin recategorization. Moving a line to owner_personal/one_time also
    seeds a candidate add-back (once per source event)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    event = (
        await db.execute(
            select(DealerCashEvent).where(
                DealerCashEvent.id == event_id, DealerCashEvent.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cash event not found for this dealer")
    if payload.category is not None:
        event.category = payload.category
    if payload.flags is not None:
        event.flags = payload.flags
    event.categorized_by = "admin"

    if payload.category in ("owner_personal", "one_time"):
        existing = (
            await db.execute(select(DealerAddback).where(DealerAddback.source_event_id == event.id))
        ).scalar_one_or_none()
        if existing is None:
            amt = abs(float(event.amount))
            is_owner = payload.category == "owner_personal"
            db.add(
                DealerAddback(
                    dealer_id=dealer.id,
                    title=event.description[:200],
                    monthly_amount=amt if is_owner else None,
                    annual_amount=amt * 12 if is_owner else amt,
                    status="candidate",
                    evidence=f"Flagged from statement line {event.occurred_on}",
                    source_event_id=event.id,
                )
            )
    await rebuild_periods(db, dealer.id, {event.period})
    await db.commit()
    await db.refresh(event)
    return event


@router.get("/dealers/{dealer_id}/periods", response_model=list[PeriodRead])
async def list_periods(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerFinancialPeriod]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerFinancialPeriod)
                .where(DealerFinancialPeriod.dealer_id == dealer.id)
                .order_by(DealerFinancialPeriod.period.asc())
            )
        )
        .scalars()
        .all()
    )


@router.put("/dealers/{dealer_id}/periods/{period}", response_model=PeriodRead)
async def upsert_period(
    dealer_id: UUID,
    period: date,
    payload: PeriodUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerFinancialPeriod:
    """Manual month upsert — source becomes 'manual' and manual wins: later
    event-driven rebuilds only recompute deposits/withdrawals, never the
    manually entered balance/EBITDA/revenue fields."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    fp = (
        await db.execute(
            select(DealerFinancialPeriod).where(
                DealerFinancialPeriod.dealer_id == dealer.id,
                DealerFinancialPeriod.period == period,
            )
        )
    ).scalar_one_or_none()
    if fp is None:
        fp = DealerFinancialPeriod(dealer_id=dealer.id, period=period)
        db.add(fp)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(fp, k, v)
    fp.source = "manual"
    # Best-effort engine refresh — never fail the ingest on engine errors.
    try:
        await recompute_snapshot(db, dealer.id)
    except Exception:
        logger.exception("dealer-os: snapshot recompute failed after period upsert for %s", dealer.id)
    await db.commit()
    await db.refresh(fp)
    return fp


# --- Stream 3: engines, lineage & alerts -----------------------------------


@router.post("/dealers/{dealer_id}/recompute", response_model=SnapshotRead)
async def recompute_dealer(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerMetricSnapshot:
    """Force a fresh metric snapshot (with lineage + alerts) for the dealer."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    snapshot = await recompute_snapshot(db, dealer.id)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


@router.get("/dealers/{dealer_id}/health", response_model=HealthRead)
async def dealer_health(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> HealthRead:
    """Cockpit read: latest snapshot + targets + unresolved alerts + lineage size."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    snapshot = (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id == dealer.id)
            .order_by(DealerMetricSnapshot.as_of.desc(), DealerMetricSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    targets = (
        await db.execute(
            select(DealerMetricTarget)
            .where(DealerMetricTarget.dealer_id == dealer.id)
            .order_by(DealerMetricTarget.metric_key)
        )
    ).scalars().all()
    alerts = (
        await db.execute(
            select(DealerAlert)
            .where(DealerAlert.dealer_id == dealer.id, DealerAlert.resolved_at.is_(None))
            .order_by(DealerAlert.created_at.desc())
        )
    ).scalars().all()
    lineage_count = 0
    if snapshot is not None:
        lineage_count = (
            await db.execute(
                select(func.count())
                .select_from(DealerMetricLineage)
                .where(DealerMetricLineage.snapshot_id == snapshot.id)
            )
        ).scalar_one()
    return HealthRead(
        snapshot=SnapshotRead.model_validate(snapshot) if snapshot is not None else None,
        targets=[_target_read(t) for t in targets],
        alerts=[AlertRead.model_validate(a) for a in alerts],
        lineage_count=int(lineage_count),
    )


@router.post("/dealers/{dealer_id}/alerts/{alert_id}/resolve", response_model=AlertRead)
async def resolve_alert(
    dealer_id: UUID, alert_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerAlert:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    alert = (
        await db.execute(
            select(DealerAlert).where(DealerAlert.id == alert_id, DealerAlert.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found for this dealer")
    if alert.resolved_at is None:
        alert.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(alert)
    return alert


# --- Stream 4: plan, forecast & funding paths --------------------------------


async def _load_plan_action(db: AsyncSession, dealer_id: UUID, action_id: UUID) -> DealerPlanAction:
    action = (
        await db.execute(
            select(DealerPlanAction).where(
                DealerPlanAction.id == action_id, DealerPlanAction.dealer_id == dealer_id
            )
        )
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan action not found for this dealer")
    return action


async def _latest_snapshot_metrics(db: AsyncSession, dealer_id: UUID) -> dict:
    """Latest snapshot's metrics dict, or 400 with a clear next step."""
    snapshot = (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id == dealer_id)
            .order_by(DealerMetricSnapshot.as_of.desc(), DealerMetricSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No metric snapshot exists for this dealer yet — import financials or "
            "POST /dealers/{id}/recompute first",
        )
    return snapshot.metrics or {}


@router.get("/dealers/{dealer_id}/plan", response_model=list[PlanActionRead])
async def list_plan_actions(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPlanAction]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerPlanAction)
                .where(DealerPlanAction.dealer_id == dealer.id)
                .order_by(DealerPlanAction.sort.asc(), DealerPlanAction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/dealers/{dealer_id}/plan", response_model=PlanActionRead, status_code=status.HTTP_201_CREATED
)
async def create_plan_action(
    dealer_id: UUID, payload: PlanActionCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerPlanAction:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    action = DealerPlanAction(dealer_id=dealer.id, **payload.model_dump())
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


@router.patch("/dealers/{dealer_id}/plan/{action_id}", response_model=PlanActionRead)
async def update_plan_action(
    dealer_id: UUID,
    action_id: UUID,
    payload: PlanActionUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPlanAction:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    action = await _load_plan_action(db, dealer.id, action_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(action, k, v)
    await db.commit()
    await db.refresh(action)
    return action


@router.delete("/dealers/{dealer_id}/plan/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan_action(
    dealer_id: UUID, action_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    action = await _load_plan_action(db, dealer.id, action_id)
    await db.delete(action)
    await db.commit()


@router.post("/dealers/{dealer_id}/plan/publish", response_model=list[PlanActionRead])
async def publish_plan(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPlanAction]:
    """Publish the whole plan to the dealer portal (sets published=true on all)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    actions = (
        (
            await db.execute(
                select(DealerPlanAction)
                .where(DealerPlanAction.dealer_id == dealer.id)
                .order_by(DealerPlanAction.sort.asc(), DealerPlanAction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    for a in actions:
        a.published = True
    await db.commit()
    return actions


@router.get("/dealers/{dealer_id}/forecast", response_model=ForecastRead)
async def dealer_forecast(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ForecastRead:
    """12-month baseline vs plan-adjusted projection from the latest snapshot."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    metrics = await _latest_snapshot_metrics(db, dealer.id)
    open_actions = (
        (
            await db.execute(
                select(DealerPlanAction).where(
                    DealerPlanAction.dealer_id == dealer.id, DealerPlanAction.status != "done"
                )
            )
        )
        .scalars()
        .all()
    )
    plan_actions = [
        {"category": a.category, "status": a.status, "due_on": a.due_on} for a in open_actions
    ]
    return ForecastRead(**compute_forecast(metrics, plan_actions))


@router.get("/dealers/{dealer_id}/paths", response_model=PathsRead)
async def dealer_paths(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PathsRead:
    """Funding-path readiness + credit-ladder position from the latest snapshot."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    metrics = await _latest_snapshot_metrics(db, dealer.id)
    target_rows = (
        (
            await db.execute(
                select(DealerMetricTarget).where(DealerMetricTarget.dealer_id == dealer.id)
            )
        )
        .scalars()
        .all()
    )
    targets = {
        t.metric_key: (float(t.effective_value) if t.effective_value is not None else None)
        for t in target_rows
    }
    return PathsRead(
        paths=compute_paths(metrics, targets), ladder=compute_ladder(metrics, targets)
    )


# --- Stream 5: messaging, sessions, global alerts & lender package -----------


@router.get("/dealers/{dealer_id}/messages", response_model=list[MessageRead])
async def list_messages(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerMessage]:
    """Full thread, oldest first. Team sees internal notes; a DEALER login
    only ever gets internal=false rows."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerMessage).where(DealerMessage.dealer_id == dealer.id)
    if user.role == Role.DEALER:
        q = q.where(DealerMessage.internal.is_(False))
    return (
        (await db.execute(q.order_by(DealerMessage.created_at.asc()))).scalars().all()
    )


@router.post(
    "/dealers/{dealer_id}/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED
)
async def create_message(
    dealer_id: UUID, payload: MessageCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerMessage:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    # A dealer can never author an internal note, whatever the payload says.
    internal = False if user.role == Role.DEALER else payload.internal
    message = DealerMessage(
        dealer_id=dealer.id,
        author_user_id=user.id,
        author_name=user.name,
        body=payload.body,
        internal=internal,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


@router.get("/dealers/{dealer_id}/sessions", response_model=list[SessionRead])
async def list_sessions(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerSession]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerSession)
                .where(DealerSession.dealer_id == dealer.id)
                .order_by(DealerSession.starts_at.asc())
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/dealers/{dealer_id}/sessions", response_model=SessionRead, status_code=status.HTTP_201_CREATED
)
async def create_session(
    dealer_id: UUID, payload: SessionCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerSession:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    session = DealerSession(dealer_id=dealer.id, created_by_user_id=user.id, **payload.model_dump())
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.delete("/dealers/{dealer_id}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    dealer_id: UUID, session_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    session = (
        await db.execute(
            select(DealerSession).where(
                DealerSession.id == session_id, DealerSession.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found for this dealer")
    await db.delete(session)
    await db.commit()


@router.get("/alerts", response_model=list[GlobalAlertRead])
async def list_global_alerts(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[GlobalAlertRead]:
    """Global team view: every unresolved alert across all dealers, newest first."""
    require_team(user)
    rows = (
        await db.execute(
            select(DealerAlert, DealerBusiness.name)
            .join(DealerBusiness, DealerBusiness.id == DealerAlert.dealer_id)
            .where(DealerAlert.resolved_at.is_(None))
            .order_by(DealerAlert.created_at.desc())
        )
    ).all()
    out: list[GlobalAlertRead] = []
    for alert, dealer_name in rows:
        item = GlobalAlertRead(
            **AlertRead.model_validate(alert).model_dump(),
            dealer_id=alert.dealer_id,
            dealer_name=dealer_name,
        )
        out.append(item)
    return out


@router.get("/dealers/{dealer_id}/lender-package", response_model=LenderPackageRead)
async def lender_package(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> LenderPackageRead:
    """One-call JSON bundle powering the print-ready lender report. Sections
    that need a snapshot (forecast, paths) come back None — never 400 — so a
    brand-new dealer still renders a partial package."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    snapshot = (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id == dealer.id)
            .order_by(DealerMetricSnapshot.as_of.desc(), DealerMetricSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    target_rows = (
        (
            await db.execute(
                select(DealerMetricTarget)
                .where(DealerMetricTarget.dealer_id == dealer.id)
                .order_by(DealerMetricTarget.metric_key)
            )
        )
        .scalars()
        .all()
    )
    periods = (
        (
            await db.execute(
                select(DealerFinancialPeriod)
                .where(DealerFinancialPeriod.dealer_id == dealer.id)
                .order_by(DealerFinancialPeriod.period.asc())
            )
        )
        .scalars()
        .all()
    )
    addbacks = (
        (
            await db.execute(
                select(DealerAddback)
                .where(DealerAddback.dealer_id == dealer.id)
                .order_by(DealerAddback.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    plan = (
        (
            await db.execute(
                select(DealerPlanAction)
                .where(DealerPlanAction.dealer_id == dealer.id)
                .order_by(DealerPlanAction.sort.asc(), DealerPlanAction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    forecast: ForecastRead | None = None
    paths: PathsRead | None = None
    if snapshot is not None:
        metrics = snapshot.metrics or {}
        open_actions = [
            {"category": a.category, "status": a.status, "due_on": a.due_on}
            for a in plan
            if a.status != "done"
        ]
        forecast = ForecastRead(**compute_forecast(metrics, open_actions))
        targets_map = {
            t.metric_key: (float(t.effective_value) if t.effective_value is not None else None)
            for t in target_rows
        }
        paths = PathsRead(
            paths=compute_paths(metrics, targets_map), ladder=compute_ladder(metrics, targets_map)
        )

    return LenderPackageRead(
        dealer=DealerRead.model_validate(dealer),
        snapshot=SnapshotRead.model_validate(snapshot) if snapshot is not None else None,
        targets=[_target_read(t) for t in target_rows],
        periods=[PeriodRead.model_validate(p) for p in periods],
        addbacks=[AddbackRead.model_validate(a) for a in addbacks],
        plan=[PlanActionRead.model_validate(a) for a in plan],
        forecast=forecast,
        paths=paths,
    )
