"""Shared, non-inferred use-of-funds budget contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

UseOfFundsCategory = Literal[
    "real_estate", "equipment", "working_capital", "inventory",
    "debt_refinance", "closing_fees", "other",
]
USE_OF_FUNDS_CATEGORIES = (
    "real_estate", "equipment", "working_capital", "inventory",
    "debt_refinance", "closing_fees", "other",
)


class UseOfFundsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    category: UseOfFundsCategory
    label: str = Field(min_length=1, max_length=160)
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2, allow_inf_nan=False)

    @field_validator("amount", mode="before")
    @classmethod
    def _not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Amount must be a monetary value, not a boolean")
        return value


class UseOfFundsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[UseOfFundsItem] = Field(max_length=50)
    expected_revision: int = Field(ge=0)

    @field_validator("items")
    @classmethod
    def _unique_ids(cls, rows: list[UseOfFundsItem]) -> list[UseOfFundsItem]:
        if len({row.id for row in rows}) != len(rows):
            raise ValueError("Each use-of-funds item needs a unique stable ID")
        return rows


class UseOfFundsRead(BaseModel):
    profile_id: UUID
    can_edit: bool = False
    items: list[UseOfFundsItem]
    requested_amount: float | None
    requested_amount_source: str | None
    total: float
    category_totals: dict[str, float]
    unallocated_amount: float | None
    complete: bool
    real_estate_equipment_amount: float
    real_estate_equipment_pct: float | None
    revision: int
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None
    source: Literal["profile", "legacy_dealer", "none"] = "none"
    warnings: list[str] = Field(default_factory=list)
