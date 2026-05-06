"""FRED router — feeds the dashboard 'Today's Market Rates' widget.

Three surfaces:
  - GET  /fred/series                — bundled summary for all tracked series
  - GET  /fred/series/{series_id}    — single series with full history
  - POST /admin/fred/refresh         — trigger the daily pull (super-admin)
  - GET  /lender-spreads             — current spread per series
  - POST /lender-spreads             — upsert (creates an audit-trail row)

The refresh endpoint is the cron target — wire AWS EventBridge / system cron
to hit it once each morning. Manual triggers from super-admin are also fine.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser, require_role
from app.enums import Role
from app.models.fred_observation import FredObservation
from app.models.lender_spread import LenderSpread
from app.models.user import User
from app.schemas.fred import (
    FredObservationOut,
    FredRefreshResult,
    FredSeriesSummary,
    LenderSpreadRead,
    LenderSpreadUpsert,
)
from app.services import fred as fred_service

router = APIRouter(tags=["fred"])


# ── Helpers ────────────────────────────────────────────────────────────


async def _current_spreads(db: AsyncSession) -> dict[str, LenderSpread]:
    """Return the most-recent spread row per series_id (the 'active' spread)."""
    rows = (
        await db.execute(
            select(LenderSpread).order_by(
                LenderSpread.series_id.asc(), LenderSpread.created_at.desc()
            )
        )
    ).scalars().all()
    out: dict[str, LenderSpread] = {}
    for r in rows:
        if r.series_id not in out:
            out[r.series_id] = r
    return out


def _delta_bps(latest: float | None, previous: float | None) -> int | None:
    if latest is None or previous is None:
        return None
    return round((latest - previous) * 100)


def _build_summary(
    series_id: str,
    history_full: list[FredObservation],
    spread: LenderSpread | None,
    requested_days: int = 30,
) -> FredSeriesSummary:
    """Build a series summary from a deep history slice.

    `history_full` is whatever rows the caller decided to load (typically
    up to 100 days back). We slice 7-day and `requested_days`-day windows
    out of that single load — no extra DB calls per window.
    """
    today = date.today()
    history_7 = [h for h in history_full if (today - h.observation_date).days <= 7]
    history_30 = [h for h in history_full if (today - h.observation_date).days <= 30]
    history_window = [
        h for h in history_full if (today - h.observation_date).days <= requested_days
    ]
    valid = [h for h in history_full if h.value is not None]
    latest = valid[-1] if valid else None
    previous = valid[-2] if len(valid) >= 2 else None
    spread_bps = spread.spread_bps if spread else 0
    latest_value = float(latest.value) if latest else None
    estimated = (latest_value + spread_bps / 100.0) if latest_value is not None else None

    def _to_out(rows: list[FredObservation]) -> list[FredObservationOut]:
        return [
            FredObservationOut(
                date=h.observation_date,
                value=float(h.value) if h.value is not None else None,
            )
            for h in rows
        ]

    return FredSeriesSummary(
        series_id=series_id,
        label=fred_service.SERIES_LABELS.get(series_id, series_id),
        description=fred_service.SERIES_DESCRIPTIONS.get(series_id, ""),
        current_value=latest_value,
        current_date=latest.observation_date if latest else None,
        previous_value=float(previous.value) if previous else None,
        delta_bps=_delta_bps(latest_value, float(previous.value) if previous else None),
        spread_bps=spread_bps,
        estimated_rate=estimated,
        history_7d=_to_out(history_7),
        history_30d=_to_out(history_30),
        history=_to_out(history_window),
        history_days=requested_days,
    )


# ── Series endpoints ───────────────────────────────────────────────────


@router.get("/fred/series", response_model=list[FredSeriesSummary])
async def list_series(
    _: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = 30,
) -> list[FredSeriesSummary]:
    """Bundled summary for every tracked FRED series.

    `days` controls the `history` window returned in each card (1..90,
    default 30). The fixed `history_7d` / `history_30d` fields are always
    populated regardless of `days` — the dashboard's small chart and the
    explorer's bigger chart can read whichever window they need from a
    single bundled call.
    """
    requested_days = max(1, min(days, 90))
    # Load enough rows to cover the longest possible window once per series,
    # then slice in _build_summary for each window.
    deepest = max(requested_days, 30)
    spreads = await _current_spreads(db)
    out: list[FredSeriesSummary] = []
    for series_id in fred_service.SERIES_IDS:
        history = await fred_service.get_history(db, series_id, days=deepest)
        out.append(_build_summary(series_id, history, spreads.get(series_id), requested_days))
    return out


@router.get("/fred/series/{series_id}", response_model=FredSeriesSummary)
async def get_single_series(
    series_id: str,
    _: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = 30,
) -> FredSeriesSummary:
    series_id = series_id.upper()
    if series_id not in fred_service.SERIES_IDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Series {series_id} not tracked")
    requested_days = max(1, min(days, 90))
    history = await fred_service.get_history(db, series_id, days=max(requested_days, 30))
    spreads = await _current_spreads(db)
    return _build_summary(series_id, history, spreads.get(series_id), requested_days)


@router.post(
    "/admin/fred/refresh",
    response_model=FredRefreshResult,
    status_code=status.HTTP_200_OK,
)
async def refresh_fred(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> FredRefreshResult:
    """Cron target. Pulls all tracked series from FRED and upserts.

    Wire this to AWS EventBridge (or your system cron) to fire once each
    morning. Idempotent — re-running the same day just refreshes the
    most-recent observations in place.
    """
    summary = await fred_service.refresh_all(db)
    return FredRefreshResult(**summary)


# ── Lender spreads ─────────────────────────────────────────────────────


@router.get("/lender-spreads", response_model=list[LenderSpreadRead])
async def list_spreads(
    _: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[LenderSpreadRead]:
    """Active spread per series (the most-recent row for each series_id)."""
    current = await _current_spreads(db)
    return [LenderSpreadRead.model_validate(s) for s in current.values()]


@router.post(
    "/lender-spreads",
    response_model=LenderSpreadRead,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_spread(
    body: LenderSpreadUpsert,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> LenderSpreadRead:
    """Append a new spread row. The most-recent row per series wins; older
    rows form the audit trail."""
    series_id = body.series_id.upper()
    if series_id not in fred_service.SERIES_IDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Series {series_id} is not tracked. Tracked: {', '.join(fred_service.SERIES_IDS)}",
        )
    row = LenderSpread(
        series_id=series_id,
        spread_bps=body.spread_bps,
        notes=body.notes,
        created_by=user.id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return LenderSpreadRead.model_validate(row)


@router.get("/lender-spreads/{series_id}/history", response_model=list[LenderSpreadRead])
async def spread_history(
    series_id: str,
    _: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[LenderSpreadRead]:
    """All historical spread rows for a series, newest first (audit trail)."""
    series_id = series_id.upper()
    rows = (
        await db.execute(
            select(LenderSpread)
            .where(LenderSpread.series_id == series_id)
            .order_by(LenderSpread.created_at.desc())
        )
    ).scalars().all()
    return [LenderSpreadRead.model_validate(r) for r in rows]
