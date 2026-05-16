from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ClosingCostTierRead(ORMModel):
    id: UUID
    from_amount: float | None = None
    to_amount: float | None = None
    percentage: float = 0
    minimum_dollar: float = 0
    sort_order: int = 0

    created_at: datetime
    updated_at: datetime


class ClosingCostTierCreate(BaseModel):
    from_amount: float | None = Field(default=None, ge=0)
    to_amount: float | None = Field(default=None, ge=0)
    percentage: float = Field(default=0, ge=0, le=1)
    minimum_dollar: float = Field(default=0, ge=0)
    sort_order: int = 0


class ClosingCostTierUpdate(BaseModel):
    from_amount: float | None = Field(default=None, ge=0)
    to_amount: float | None = Field(default=None, ge=0)
    percentage: float | None = Field(default=None, ge=0, le=1)
    minimum_dollar: float | None = Field(default=None, ge=0)
    sort_order: int | None = None


class ClosingCostTiersReplace(BaseModel):
    """Bulk replace — the Excel-grid Save sends the whole table."""

    tiers: list[ClosingCostTierCreate] = Field(default_factory=list)
