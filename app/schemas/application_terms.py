from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.schemas.financial_cockpit import FinancialPlanRead, FinancialPlanWrite
from app.schemas.loan_repayment import LoanRepaymentConfig, validate_repayment_config

FunderType = Literal[
    "bank",
    "credit_union",
    "private_fund",
    "private_capital",
    "family_office",
    "balance_sheet",
    "warehouse",
    "table_funder",
    "other",
]
RepaymentFrequency = Literal["daily", "weekly", "biweekly", "monthly", "custom"]
DebtServiceTreatment = Literal["additive", "refinance"]


class LoanTypeOption(BaseModel):
    value: str
    label: str
    description: str | None = None


class ClientTermsWrite(BaseModel):
    expected_version: int = Field(ge=0)
    loan_type: str = Field(min_length=2, max_length=64)
    amount: float = Field(gt=0, le=1_000_000_000)
    apr_pct: float = Field(ge=0, le=100)
    term_months: int = Field(ge=1, le=480)
    funder_type: FunderType
    funder_name: str | None = Field(default=None, max_length=160)
    repayment_frequency: RepaymentFrequency
    custom_payments_per_year: float | None = Field(default=None, ge=1, le=366, allow_inf_nan=False)
    custom_repayment_label: str | None = Field(default=None, max_length=80)
    debt_service_treatment: DebtServiceTreatment = "additive"
    retained_annual_debt_service: float | None = Field(default=None, ge=0, le=1_000_000_000)
    #: amortizing | simple_interest | interest_only; None means the house
    #: default for the term (simple interest under three years, amortizing from
    #: three years). interest_only_months: interest-only months before principal.
    loan_structure: Literal["amortizing", "simple_interest", "interest_only"] | None = None
    interest_only_months: int | None = Field(default=None, ge=0, le=480)
    repayment_config: LoanRepaymentConfig | None = None
    expiration_days: int = Field(ge=1, le=180)
    closing_estimate_days: int = Field(ge=0, le=180)
    co_brand_enabled: bool = True
    sponsor_name: str | None = Field(default="UrChoice", max_length=160)
    client_note: str | None = Field(default=None, max_length=1200)
    conditions: list[str] = Field(default_factory=list, max_length=12)
    # Optional atomic save of the unsaved Financials scenario alongside these
    # versioned loan terms. Existing terms-only callers retain their behavior.
    financial_plan: FinancialPlanWrite | None = None

    @field_validator("loan_type", "funder_name", "custom_repayment_label", "sponsor_name", "client_note", mode="before")
    @classmethod
    def trim_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        trimmed = value.strip()
        return trimmed or None

    @field_validator("conditions")
    @classmethod
    def clean_conditions(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            text = str(value).strip()
            if not text or text in cleaned:
                continue
            if len(text) > 240:
                raise ValueError("Each condition must be 240 characters or fewer")
            cleaned.append(text)
        return cleaned

    @field_validator("custom_payments_per_year")
    @classmethod
    def _stored_cadence_precision(cls, value):
        if value is not None and abs(value - round(value, 3)) > 1e-9:
            raise ValueError("Custom payments per year supports up to three decimal places; the saved cadence must match the quote exactly")
        return value

    @model_validator(mode="after")
    def validate_custom_schedule(self) -> ClientTermsWrite:
        if self.repayment_frequency == "custom" and not self.custom_payments_per_year:
            raise ValueError("Custom repayment requires payments per year")
        if self.repayment_frequency != "custom":
            self.custom_payments_per_year = None
            self.custom_repayment_label = None
        if self.co_brand_enabled:
            self.sponsor_name = self.sponsor_name or "UrChoice"
        else:
            self.sponsor_name = None
        if self.debt_service_treatment == "refinance" and self.retained_annual_debt_service is None:
            raise ValueError("Refinance terms require the annual debt service that will remain after payoff (enter 0 for a full payoff)")
        if self.debt_service_treatment != "refinance":
            self.retained_annual_debt_service = None
        from app.services.application_terms import CADENCE_PERIODS_PER_YEAR

        if self.repayment_config is not None:
            self.repayment_config = validate_repayment_config(self.repayment_config, self.term_months,
                structure=self.loan_structure, interest_only_months=self.interest_only_months,
                periods_per_year=self.custom_payments_per_year if self.repayment_frequency == "custom" else CADENCE_PERIODS_PER_YEAR[self.repayment_frequency],
                amount=self.amount, rate_pct=self.apr_pct)
        return self


class ClientTermsCalculation(BaseModel):
    periodic_payment: float | None = None
    payment_count: int | None = None
    payments_per_year: float | None = None
    annual_debt_service: float | None = None
    total_repayment: float | None = None
    financing_cost: float | None = None
    dscr_before: float | None = None
    dscr_after: float | None = None
    cash_flow_value: float | None = None
    cash_flow_label: str
    current_annual_debt_service: float | None = None
    annual_property_carrying_costs: float | None = None
    projected_annual_debt_service: float | None = None
    dscr_method: Literal["business", "real_estate"]
    dscr_status: Literal["ready", "needs_evidence"]
    dscr_explanation: str
    source: str
    #: The structure the payment stands on, the interest-only months first and
    #: the payment during them (the periodic payment above is the one after).
    loan_structure: str | None = None
    interest_only_months: int = 0
    interest_only_payment: float | None = None
    structure_label: str | None = None
    repayment_config: dict | None = None
    schedule: dict | None = None
    balloon_events: list[dict] = Field(default_factory=list)
    maturity_balance: float | None = None
    first_year_debt_service: float | None = None
    max_annual_debt_service: float | None = None


class ClientTermsRead(BaseModel):
    profile_id: UUID
    term_sheet_id: UUID | None = None
    version: int = 0
    status: Literal["not_started", "draft", "issued"] = "not_started"
    loan_type: str | None = None
    loan_type_label: str | None = None
    amount: float | None = None
    apr_pct: float | None = None
    term_months: int | None = None
    funder_type: FunderType | None = None
    funder_name: str | None = None
    repayment_frequency: RepaymentFrequency = "monthly"
    custom_payments_per_year: float | None = None
    custom_repayment_label: str | None = None
    debt_service_treatment: DebtServiceTreatment = "additive"
    retained_annual_debt_service: float | None = None
    loan_structure: Literal["amortizing", "simple_interest", "interest_only"] | None = None
    interest_only_months: int | None = None
    repayment_config: dict | None = None
    expiration_days: int | None = None
    closing_estimate_days: int | None = None
    co_brand_enabled: bool = True
    sponsor_name: str | None = "UrChoice"
    client_note: str | None = None
    conditions: list[str] = Field(default_factory=list)
    issued_at: datetime | None = None
    expires_on: date | None = None
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None
    client_email: EmailStr | None = None
    direct_client_contact_suppressed: bool = False
    loan_type_options: list[LoanTypeOption] = Field(default_factory=list)
    calculation: ClientTermsCalculation
    #: What the Financials tab says: the plan in words, coverage today and
    #: with the plan, and whether the plan's loan mirrors these terms.
    plan: dict[str, Any] | None = None
    financial_plan: FinancialPlanRead | None = None


class ClientTermsEmailRequest(BaseModel):
    expected_version: int = Field(ge=1)
    delivery_key: UUID
    to_emails: list[EmailStr] = Field(min_length=1, max_length=10)
    cc_emails: list[EmailStr] = Field(default_factory=list, max_length=10)
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=5000)

    @field_validator("to_emails", "cc_emails")
    @classmethod
    def unique_emails(cls, values: list[EmailStr]) -> list[EmailStr]:
        seen: set[str] = set()
        result: list[EmailStr] = []
        for value in values:
            key = str(value).lower()
            if key not in seen:
                result.append(value)
                seen.add(key)
        return result


class ClientTermsEmailResult(BaseModel):
    sent: bool
    filename: str
    message_id: str | None = None
    detail: str | None = None
