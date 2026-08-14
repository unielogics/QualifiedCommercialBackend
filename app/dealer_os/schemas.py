"""Pydantic schemas for the Dealer OS API."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DealerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    industry: str = "auto_dealer"
    notes: str | None = None


class DealerUpdate(BaseModel):
    name: str | None = None
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    industry: str | None = None
    status: str | None = None
    notes: str | None = None


class DealerRead(ORM):
    id: UUID
    name: str
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    industry: str
    status: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class DealerListItem(ORM):
    id: UUID
    name: str
    city: str | None = None
    state: str | None = None
    status: str
    created_at: datetime
    # rollups filled by the router (no snapshot may exist yet)
    score: float | None = None
    tier: str | None = None
    open_alerts: int = 0


class TargetRead(ORM):
    id: UUID
    metric_key: str
    ai_proposed_value: float | None = None
    ai_rationale: str | None = None
    ai_proposed_at: datetime | None = None
    admin_value: float | None = None
    admin_set_at: datetime | None = None
    status: str
    effective_value: float | None = None


class TargetOverride(BaseModel):
    metric_key: str = Field(min_length=1, max_length=48)
    admin_value: float | None = None  # None clears the override back to the AI proposal


# --- Stream 2: ingestion & normalization -----------------------------------


class CashEventRow(BaseModel):
    occurred_on: date
    description: str = Field(min_length=1, max_length=320)
    amount: float
    invoice_date: date | None = None
    due_date: date | None = None


class CashImport(BaseModel):
    rows: list[CashEventRow] = Field(max_length=5000)


class CashImportResult(BaseModel):
    imported: int
    periods: int


class CashEventRead(ORM):
    id: UUID
    period: date
    occurred_on: date
    description: str
    amount: float
    category: str
    flags: dict | None = None
    categorized_by: str | None = None
    source: str


class CashEventPatch(BaseModel):
    category: str | None = None
    flags: dict | None = None


class PeriodRead(ORM):
    id: UUID
    period: date
    revenue: float | None = None
    net_income: float | None = None
    ebitda_reported: float | None = None
    ebitda_adjusted: float | None = None
    ebitda_bankable: float | None = None
    debt_service: float | None = None
    deposits: float | None = None
    withdrawals: float | None = None
    ending_balance: float | None = None
    low_balance: float | None = None
    avg_daily_balance: float | None = None
    nsf_count: int
    liquidity: dict | None = None
    source: str
    reconciled: bool


class PeriodUpsert(BaseModel):
    revenue: float | None = None
    net_income: float | None = None
    ebitda_reported: float | None = None
    ebitda_adjusted: float | None = None
    ebitda_bankable: float | None = None
    debt_service: float | None = None
    deposits: float | None = None
    withdrawals: float | None = None
    ending_balance: float | None = None
    low_balance: float | None = None
    avg_daily_balance: float | None = None
    nsf_count: int | None = None
    reconciled: bool | None = None


# --- Stream 3: engines, lineage & alerts -----------------------------------


class SnapshotRead(ORM):
    id: UUID
    as_of: date
    metrics: dict
    score: float | None = None
    tier: str | None = None


class AlertRead(ORM):
    id: UUID
    kind: str
    severity: str
    message: str
    ref_kind: str | None = None
    ref_id: UUID | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class HealthRead(BaseModel):
    snapshot: SnapshotRead | None = None
    targets: list[TargetRead] = []
    alerts: list[AlertRead] = []
    lineage_count: int = 0
