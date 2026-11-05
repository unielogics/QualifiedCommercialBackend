from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FinancialDebtAction(BaseModel):
    family_id: UUID | None = None
    expected_revision: int = Field(ge=0)
    op: Literal["delete", "restore", "duplicate", "link_document", "unlink_document", "select_payoff"]
    row_id: UUID
    target_row_id: UUID | None = None
    file_id: UUID | None = None
    role: Literal["agreement", "payoff", "supporting"] | None = None
    payoff_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    payoff_date: date | None = None


class FinancialDebtDocumentImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family_id: UUID | None = None
    expected_revision: int = Field(ge=0)
    file_id: UUID
    role: Literal["agreement", "payoff", "supporting"] = "agreement"

