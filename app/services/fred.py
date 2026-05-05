"""FRED (Federal Reserve Economic Data) — daily index pull.

Pulls the four "must-have commercial bases" from the St. Louis Fed API and
upserts them into `fred_observations`. The dashboard widget combines each
index with its spread (`lender_spreads`) into:

    Index Rate (FRED) + Lender Spread (your business rule) = Estimated Interest Rate

Series tracked:
  - DGS10  : 10-Year Treasury        — Long-term fixed (DSCR 30-yr)
  - SOFR   : Secured Overnight Rate  — Bridge / floating
  - DPRIME : Bank Prime Loan Rate    — SBA 7(a), Fix & Flip / Ground Up
  - DGS5   : 5-Year Treasury         — 5-year hybrid / fixed

Triggered by the operator team's morning cron (POST /admin/fred/refresh).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.fred_observation import FredObservation

log = logging.getLogger(__name__)

# Single source of truth — both the refresh job and the /fred/series endpoint
# iterate this list. Add a new series here once and the whole pipeline picks
# it up (the spread row needs to be added separately via the spreads admin).
SERIES_IDS = ("DGS10", "SOFR", "DPRIME", "DGS5")

SERIES_LABELS: dict[str, str] = {
    "DGS10": "10-Year Treasury",
    "SOFR": "Secured Overnight Financing Rate",
    "DPRIME": "Bank Prime Loan Rate",
    "DGS5": "5-Year Treasury",
}

SERIES_DESCRIPTIONS: dict[str, str] = {
    "DGS10": "Long-term fixed-rate benchmark — drives DSCR 30-yr pricing.",
    "SOFR": "Overnight floating benchmark — drives bridge / floating-rate pricing.",
    "DPRIME": "Bank prime — drives SBA 7(a) and small-balance bank loans.",
    "DGS5": "5-year benchmark — drives 5-year hybrid / fixed products.",
}

# Default product → series mapping for the dashboard widget. Edit these by
# changing the spread on the corresponding series_id (the widget pulls them
# both together).
PRODUCT_SERIES_MAP: dict[str, str] = {
    "fix_and_flip": "DPRIME",
    "ground_up": "DPRIME",
    "dscr": "DGS10",
    "bridge": "SOFR",
}


class FredFetchError(Exception):
    """Raised when the FRED HTTP call fails or returns an unexpected payload."""


async def fetch_series(series_id: str, days_back: int = 35) -> list[tuple[date, float | None]]:
    """Fetch the most recent `days_back` observations for a series.

    Defaults to 35 so we always have a 30-day window even after weekends/
    holidays where the series doesn't update. Returns [(date, value), ...]
    sorted oldest → newest. Raises FredFetchError on any non-2xx response.
    """
    settings = get_settings()
    if not settings.fred_api_key:
        raise FredFetchError(
            "FRED_API_KEY is not configured — set it in .env before refreshing."
        )

    end = date.today()
    start = end - timedelta(days=days_back)
    params = {
        "series_id": series_id,
        "api_key": settings.fred_api_key,
        "file_type": "json",
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
        "sort_order": "asc",
    }
    url = f"{settings.fred_api_url}/series/observations"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code >= 400:
            raise FredFetchError(f"FRED {series_id} returned {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()

    observations = payload.get("observations") or []
    out: list[tuple[date, float | None]] = []
    for obs in observations:
        try:
            d = date.fromisoformat(obs["date"])
        except (KeyError, ValueError):
            continue
        raw = obs.get("value")
        # FRED uses "." for "no value" — store NULL so we can distinguish
        # "data point exists" from "data point missing".
        if raw is None or raw == "." or raw == "":
            value: float | None = None
        else:
            try:
                value = float(raw)
            except ValueError:
                value = None
        out.append((d, value))
    return out


async def upsert_observations(
    db: AsyncSession, series_id: str, observations: list[tuple[date, float | None]]
) -> int:
    """Bulk upsert (series_id, observation_date) → value. Returns row count."""
    if not observations:
        return 0
    now = datetime.now(timezone.utc)
    rows = [
        {
            "series_id": series_id,
            "observation_date": d,
            "value": v,
            "created_at": now,
            "updated_at": now,
        }
        for d, v in observations
    ]
    stmt = pg_insert(FredObservation).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_fred_series_date",
        set_={"value": stmt.excluded.value, "updated_at": now},
    )
    await db.execute(stmt)
    await db.flush()
    return len(rows)


async def refresh_all(db: AsyncSession) -> dict[str, Any]:
    """Pull every series in SERIES_IDS, upsert results. Returns a summary
    dict suitable for returning straight from the refresh endpoint."""
    summary: dict[str, Any] = {"series": {}, "errors": {}}
    for series_id in SERIES_IDS:
        try:
            obs = await fetch_series(series_id)
            inserted = await upsert_observations(db, series_id, obs)
            summary["series"][series_id] = {
                "rows": inserted,
                "latest_date": obs[-1][0].isoformat() if obs else None,
                "latest_value": obs[-1][1] if obs else None,
            }
        except FredFetchError as exc:
            log.warning("FRED refresh failed for %s: %s", series_id, exc)
            summary["errors"][series_id] = str(exc)
    return summary


async def get_history(
    db: AsyncSession, series_id: str, days: int
) -> list[FredObservation]:
    """Read the last N days of observations from the DB (no network)."""
    cutoff = date.today() - timedelta(days=days)
    rows = (
        await db.execute(
            select(FredObservation)
            .where(
                FredObservation.series_id == series_id,
                FredObservation.observation_date >= cutoff,
            )
            .order_by(FredObservation.observation_date.asc())
        )
    ).scalars().all()
    return list(rows)
