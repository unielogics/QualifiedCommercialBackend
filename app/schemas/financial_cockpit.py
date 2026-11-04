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
    #: The terms the package and the listing speak of (recorded term sheet,
    #: else the plan's loan) with the schedule and the principal at risk;
    #: see financial_cockpit.proposed_terms_block.
    proposed_terms: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)


class DebtScheduleVoidWrite(BaseModel):
    """The desk's declaration that the business carries no debt, so the
    standing debt schedule requirement is set aside on this file."""

    reason: str | None = Field(default=None, max_length=2000)


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
    #: The detected keys folded into this account (itself included).
    members: list[str] = Field(default_factory=list)
    #: Accounts the statements list beside this one (savings, empty shares):
    #: {last4, label, ending_balance}. Seen and named, never accounts of their own.
    sub_accounts: list[dict] = Field(default_factory=list)


class BankAccountsRead(BaseModel):
    profile_id: UUID
    accounts: list[BankAccountRead] = Field(default_factory=list)
    counted: list[str] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)


class BankAccountsWrite(BaseModel):
    counted: list[str] = Field(default_factory=list, max_length=20)
    #: Detected keys the desk declares to be the same account: {key: the account it belongs to}.
    aliases: dict[str, str] | None = Field(default=None)


class AddBackIn(BaseModel):
    """One amount the desk adds back to the income available for debt service."""

    id: str | None = Field(default=None, max_length=60)
    kind: Literal["owner_salary", "interest_refinanced", "depreciation", "one_time_expense", "rent_to_owner", "other"] = "other"
    label: str | None = Field(default=None, max_length=120)
    amount_monthly: float = Field(gt=0)
    note: str | None = Field(default=None, max_length=300)
    source: str | None = Field(default=None, max_length=40)


class PlanOverridesIn(BaseModel):
    """Figures the desk sets outright, each with the reason."""

    income_annual: float | None = Field(default=None, ge=0)
    income_note: str | None = Field(default=None, max_length=300)
    debt_service_monthly: float | None = Field(default=None, ge=0)
    debt_service_note: str | None = Field(default=None, max_length=300)


class FinancialPlanWrite(BaseModel):
    """What the desk decided: obligations the new loan pays off, payees that
    are not operating spend, add-backs and overrides to the coverage inputs,
    and the new payment or the loan it comes from (level or interest-only)."""

    retire: list[str] = Field(default_factory=list, max_length=60)
    exclude: list[str] = Field(default_factory=list, max_length=60)
    new_payment_monthly: float | None = Field(default=None, ge=0)
    new_payment_source: Literal["manual", "term_sheet", "offer", "manual_terms", "recorded_terms"] | None = None
    new_loan_amount: float | None = Field(default=None, ge=0)
    new_loan_rate_pct: float | None = Field(default=None, ge=0, le=100)
    new_loan_term_months: int | None = Field(default=None, ge=1, le=600)
    new_loan_interest_only: bool = False
    #: amortizing | simple_interest | interest_only; defaults by term (simple
    #: interest under three years, amortizing from three years).
    new_loan_structure: Literal["amortizing", "simple_interest", "interest_only"] | None = None
    #: Months of the term that are interest-only before principal starts.
    new_loan_interest_only_months: int | None = Field(default=None, ge=0, le=600)
    add_backs: list[AddBackIn] = Field(default_factory=list, max_length=20)
    overrides: PlanOverridesIn | None = None
    #: Which recorded terms the loan fields were adopted from; kept across saves
    #: so the cockpit can say whether the plan still mirrors them.
    terms_ref: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=600)


class FinancialPlanRead(BaseModel):
    profile_id: UUID
    record: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    levers: list[dict[str, Any]] = Field(default_factory=list)
    obligations: list[dict[str, Any]] = Field(default_factory=list)
