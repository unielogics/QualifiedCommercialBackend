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
    #: The plan (record, evaluation, levers); see app/services/financial_plan.py.
    plan: dict[str, Any] = Field(default_factory=dict)
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
    #: The statement payee this advance is (cockpit payee key), when known.
    payee_key: str | None = Field(default=None, max_length=60)


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


class BankAccountRead(BaseModel):
    key: str
    label: str
    months: list[str] = Field(default_factory=list)
    accepted_months: list[str] = Field(default_factory=list)
    counted: bool = True
    latest_month: str | None = None


class BankAccountsRead(BaseModel):
    profile_id: UUID
    accounts: list[BankAccountRead] = Field(default_factory=list)
    counted: list[str] = Field(default_factory=list)


class BankAccountsWrite(BaseModel):
    counted: list[str] = Field(default_factory=list, max_length=20)


class FinancialPlanWrite(BaseModel):
    """What the desk decided: obligations the new loan pays off, payees that
    are not operating spend, and the new payment or the loan it comes from."""

    retire: list[str] = Field(default_factory=list, max_length=60)
    exclude: list[str] = Field(default_factory=list, max_length=60)
    new_payment_monthly: float | None = Field(default=None, ge=0)
    new_payment_source: Literal["manual", "term_sheet", "offer", "manual_terms"] | None = None
    new_loan_amount: float | None = Field(default=None, ge=0)
    new_loan_rate_pct: float | None = Field(default=None, ge=0, le=100)
    new_loan_term_months: int | None = Field(default=None, ge=1, le=600)
    note: str | None = Field(default=None, max_length=600)


class FinancialPlanRead(BaseModel):
    profile_id: UUID
    record: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    levers: list[dict[str, Any]] = Field(default_factory=list)
    obligations: list[dict[str, Any]] = Field(default_factory=list)
