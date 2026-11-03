"""The financial cockpit read model: GET /application-profiles/{id}/financial-cockpit.

The sections are dictionaries on purpose. Their rows are wide (a bank month
carries a dozen figures, a DSCR candidate its numerator, basis and status)
and the shapes are pinned by app/tests/test_financial_cockpit.py and mirrored
in the dashboard's src/lib/financialCockpit.ts, which is the contract.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class FinancialCockpitRead(BaseModel):
    profile_id: UUID
    generated_at: datetime
    requested_amount: float | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    bank: dict[str, Any] = Field(default_factory=dict)
    tax: dict[str, Any] = Field(default_factory=dict)
    profit_and_loss: dict[str, Any] | None = None
    balance_sheet: dict[str, Any] | None = None
    expenses: dict[str, Any] = Field(default_factory=dict)
    debt: dict[str, Any] = Field(default_factory=dict)
    dscr: dict[str, Any] = Field(default_factory=dict)
    net_profit: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class AdvanceIn(BaseModel):
    """One merchant cash advance as the client describes it: who funded it, what
    was approved, what is left, how often it debits and how much."""

    id: UUID | None = None
    lender: str = Field(min_length=1, max_length=180)
    original_amount: float | None = Field(default=None, ge=0)
    balance: float | None = Field(default=None, ge=0)
    payment_amount: float | None = Field(default=None, ge=0)
    payment_frequency: Literal["daily", "weekly", "biweekly", "monthly"] | None = None
    factor_rate: float | None = Field(default=None, ge=1, le=3)
    payoff_amount: float | None = Field(default=None, ge=0)
    maturity_on: date | None = None
    notes: str | None = Field(default=None, max_length=1000)
    count_in_dscr: bool = True


class AdvanceRead(AdvanceIn):
    id: UUID
    monthly_payment: float | None = None
    origin: str
    editable: bool
    source: str


class AdvancesWrite(BaseModel):
    rows: list[AdvanceIn] = Field(default_factory=list, max_length=50)


class AdvancesRead(BaseModel):
    profile_id: UUID
    rows: list[AdvanceRead] = Field(default_factory=list)
    origin: str = "admin"


class StatementRereadResult(BaseModel):
    queued: int
    file_ids: list[UUID] = Field(default_factory=list)
