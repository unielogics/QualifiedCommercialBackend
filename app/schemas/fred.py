"""Pydantic shapes for the FRED + lender-spread endpoints."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class FredObservationOut(BaseModel):
    """A single (date, value) point — the unit the dashboard sparkline plots."""
    date: date
    value: float | None


class LenderSpreadRead(ORMModel):
    id: UUID
    series_id: str
    spread_bps: int
    notes: str | None
    created_by: UUID | None
    created_at: datetime


class LenderSpreadUpsert(BaseModel):
    series_id: str = Field(min_length=1, max_length=32)
    spread_bps: int = Field(ge=-1000, le=2000, description="Basis points to add to the index")
    notes: str | None = Field(default=None, max_length=2000)


class FredSeriesSummary(BaseModel):
    """One row per FRED series — drives the dashboard widget cards.

    `estimated_rate` is the customer-facing number:
        estimated_rate = current_value + (spread_bps / 100)
    Both inputs are exposed so the UI can render the breakdown
    ("Index 4.35% + Spread 2.15% = Est. 6.50%").
    """
    series_id: str
    label: str
    description: str
    current_value: float | None
    current_date: date | None
    previous_value: float | None
    delta_bps: int | None  # current_value vs previous_value, in bps
    spread_bps: int
    estimated_rate: float | None
    history_7d: list[FredObservationOut]
    history_30d: list[FredObservationOut]


class FredRefreshResult(BaseModel):
    series: dict[str, dict]
    errors: dict[str, str]
