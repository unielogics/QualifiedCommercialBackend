from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

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
    custom_payments_per_year: int | None = Field(default=None, ge=1, le=365)
    custom_repayment_label: str | None = Field(default=None, max_length=80)
    debt_service_treatment: DebtServiceTreatment = "additive"
    retained_annual_debt_service: float | None = Field(default=None, ge=0, le=1_000_000_000)
    expiration_days: int = Field(ge=1, le=180)
    closing_estimate_days: int = Field(ge=0, le=180)
    co_brand_enabled: bool = True
    sponsor_name: str | None = Field(default="UrChoice", max_length=160)
    client_note: str | None = Field(default=None, max_length=1200)
    conditions: list[str] = Field(default_factory=list, max_length=12)

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
    custom_payments_per_year: int | None = None
    custom_repayment_label: str | None = None
    debt_service_treatment: DebtServiceTreatment = "additive"
    retained_annual_debt_service: float | None = None
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
