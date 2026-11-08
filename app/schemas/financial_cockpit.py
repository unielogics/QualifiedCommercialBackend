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

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.loan_repayment import LoanRepaymentConfig, validate_repayment_config


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
    ebitda: dict[str, Any] = Field(default_factory=dict)
    metric_review: dict[str, Any] = Field(default_factory=dict)
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
    originated_on: date | None = None
    paid_down_pct: float | None = None
    origin: str
    editable: bool
    source: str
    draft_pending: bool = False
    draft_family_id: UUID | None = None
    draft_revision: int | None = None

    @model_validator(mode="after")
    def _loan_balance_reduction(self):
        from app.services.loan_progress import paid_down_pct

        self.paid_down_pct = paid_down_pct(self.original_amount, self.balance)
        return self


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

    income_annual: float | None = Field(default=None, allow_inf_nan=False)
    income_note: str | None = Field(default=None, max_length=300)
    debt_service_monthly: float | None = Field(default=None, ge=0)
    debt_service_note: str | None = Field(default=None, max_length=300)


class FinancialPlanWrite(BaseModel):
    """What the desk decided: obligations the new loan pays off, payees that
    are not operating spend, add-backs and overrides to the coverage inputs,
    and the new payment or the loan it comes from (level or interest-only)."""

    expected_updated_at: datetime | None = None
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
    new_loan_repayment_config: LoanRepaymentConfig | None = None
    new_loan_payments_per_year: float | None = Field(default=None, ge=1, le=366, allow_inf_nan=False)
    add_backs: list[AddBackIn] = Field(default_factory=list, max_length=20)
    overrides: PlanOverridesIn | None = None
    #: Which recorded terms the loan fields were adopted from; kept across saves
    #: so the cockpit can say whether the plan still mirrors them.
    terms_ref: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def _repayment_schedule(self):
        if self.new_loan_repayment_config is not None and (not self.new_loan_amount or self.new_loan_rate_pct is None):
            raise ValueError("Flexible repayment requires the new loan amount, rate and maturity")
        if self.new_loan_repayment_config is not None:
            self.new_loan_repayment_config = validate_repayment_config(self.new_loan_repayment_config, self.new_loan_term_months,
                structure="interest_only" if self.new_loan_interest_only else self.new_loan_structure,
                interest_only_months=self.new_loan_interest_only_months, periods_per_year=self.new_loan_payments_per_year or 12,
                amount=self.new_loan_amount, rate_pct=self.new_loan_rate_pct)
        return self

    @field_validator("expected_updated_at")
    @classmethod
    def _aware_plan_timestamp(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Expected plan timestamp must include a timezone")
        return value


class DebtRowPatch(BaseModel):
    """One schedule row edited in place on the coverage card: the figures the
    desk overrides, the statement payee it is linked to, whether it counts,
    and whether it stays. Only the fields sent change."""

    lender: str | None = Field(default=None, min_length=1, max_length=180)
    category: str | None = Field(default=None, max_length=40)
    monthly_payment: float | None = Field(default=None, ge=0, le=1_000_000_000)
    balance: float | None = Field(default=None, ge=0, le=1_000_000_000)
    payoff_amount: float | None = Field(default=None, ge=0, le=1_000_000_000)
    payment_amount: float | None = Field(default=None, ge=0, le=1_000_000_000)
    payment_frequency: Literal["daily", "weekly", "biweekly", "monthly"] | None = None
    #: What the line was approved for, the interest rate (a PERCENT a year; a
    #: factor such as 1.35 goes in `factor_rate`) and whether it is being paid on time.
    original_amount: float | None = Field(default=None, ge=0, le=1_000_000_000)
    rate: float | None = Field(default=None, ge=0, le=999.999)
    factor_rate: float | None = Field(default=None, ge=1, le=5)
    payment_status: Literal["current", "delinquent"] | None = None
    originated_on: date | None = None
    maturity_on: date | None = None
    secured: Literal["secured", "unsecured"] | None = None
    collateral: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)
    #: Asked of an advance and nothing else: modified for lower sales, and what changed.
    mca_modified: bool | None = None
    mca_modification_note: str | None = Field(default=None, max_length=1000)
    count_in_dscr: bool | None = None
    #: The recurring payee (its key) this row is seen as in the statements; "" unlinks.
    vendor_key: str | None = Field(default=None, max_length=60)
    status: Literal["active", "dismissed"] | None = None


class ClosingStipulationItem(BaseModel):
    id: str | None = None
    text: str = Field(min_length=1, max_length=600)
    source: Literal["term_sheet", "desk"] | None = None
    status: Literal["open", "received", "waived"] = "open"
    note: str | None = Field(default=None, max_length=600)
    updated_at: str | None = None
    updated_by: str | None = None
    created_at: str | None = None


class ClosingStipulationsWrite(BaseModel):
    items: list[ClosingStipulationItem] = Field(default_factory=list, max_length=200)


class ClosingStipulationsRead(BaseModel):
    """What must be in hand before funding, once the client has accepted."""

    profile_id: str
    accepted: bool
    accepted_amount: float | None = None
    estimated_close_date: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    open_count: int = 0
    received_count: int = 0
    waived_count: int = 0
    terms_conditions: list[str] = Field(default_factory=list)
    ready_to_close: bool = False


class FinancialPlanRead(BaseModel):
    profile_id: UUID
    record: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    levers: list[dict[str, Any]] = Field(default_factory=list)
    obligations: list[dict[str, Any]] = Field(default_factory=list)
