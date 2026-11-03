"""The financial cockpit read model: GET /application-profiles/{id}/financial-cockpit.

The sections are dictionaries on purpose. Their rows are wide (a bank month
carries a dozen figures, a DSCR candidate its numerator, basis and status)
and the shapes are pinned by app/tests/test_financial_cockpit.py and mirrored
in the dashboard's src/lib/financialCockpit.ts, which is the contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
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
