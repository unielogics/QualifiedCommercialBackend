"""Staff-reviewed funding costs, reserves and investor participation."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Money = Annotated[Decimal, Field(ge=0, le=999_999_999_999, decimal_places=2, allow_inf_nan=False)]


class DealCostRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    label: str = Field(min_length=1, max_length=180)
    category: Literal["closing_cost", "escrow_reserve", "referral", "origination", "consulting", "servicing", "management", "other"]
    payer: Literal["borrower", "investor", "sponsor"] = "borrower"
    method: Literal["fixed", "percent"] = "fixed"
    value: Annotated[Decimal, Field(ge=0, le=999_999_999_999, decimal_places=4, allow_inf_nan=False)] | None = None
    basis: Literal["loan_amount", "investor_funding", "outstanding_principal", "interest"] = "loan_amount"
    timing: Literal["closing", "monthly", "annual"] = "closing"
    settlement: Literal["withheld", "separate"] = "separate"
    enabled: bool = True
    recipient: str | None = Field(default=None, max_length=180)
    recipient_role: Literal["platform", "lender", "third_party", "escrow"] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    reserve_release: Literal["unknown", "maturity"] = "unknown"
    origin: Literal["manual", "legacy_setting", "program_default", "production_arrangement"] = "manual"
    source_ref: str | None = Field(default=None, max_length=180)
    legacy_field: Literal["forecast_fee_points", "forecast_consulting_fee"] | None = None

    @field_validator("value", mode="before")
    @classmethod
    def not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("Enter a numeric amount or rate")
        return value

    @model_validator(mode="after")
    def valid_cost(self):
        if self.recipient_role is None:
            self.recipient_role = "escrow" if self.category == "escrow_reserve" else "platform" if self.category in {"origination", "consulting", "servicing", "management"} else "third_party"
        if self.category == "origination" and self.timing != "closing":
            raise ValueError("Origination costs are one-time closing charges")
        if self.category == "origination" and self.payer == "borrower" and self.recipient_role == "platform" and self.method == "percent" and self.basis != "loan_amount":
            raise ValueError("Platform origination points use the gross loan amount")
        if self.method == "fixed" and self.value is not None and self.value != self.value.quantize(Decimal(".01")):
            raise ValueError("Fixed dollar costs support two decimal places")
        if self.method == "percent" and self.value is not None and self.value > 100:
            raise ValueError("Percentage cannot exceed 100")
        if self.settlement == "withheld" and (self.payer == "sponsor" or self.payer == "investor" and self.timing == "closing"):
            raise ValueError("Sponsor costs and investor closing costs must be paid separately")
        if self.category == "escrow_reserve" and self.timing != "closing":
            raise ValueError("Reserves are closing held assets, not recurring fees")
        if self.legacy_field:
            required = ("origination", "percent") if self.legacy_field == "forecast_fee_points" else ("consulting", "fixed")
            if (self.category, self.method) != required or self.payer != "borrower" or self.timing != "closing" or self.basis != "loan_amount":
                raise ValueError("Legacy fee projections require the original borrower closing-fee basis")
        return self


class DealBudgetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    funding_mode: Literal["single_investor", "multiple_investors"] = "single_investor"
    investor_funding_amount: Money | None = None
    minimum_investor_contribution: Money | None = None
    costs_confirmed: bool = False
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    rows: list[DealCostRow] = Field(default_factory=list, max_length=100)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("investor_funding_amount", "minimum_investor_contribution", mode="before")
    @classmethod
    def not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("Enter a monetary amount")
        return value

    @model_validator(mode="after")
    def unique_rows(self):
        if len({row.id for row in self.rows}) != len(self.rows):
            raise ValueError("Cost rows require distinct IDs")
        linked = [row.legacy_field for row in self.rows if row.legacy_field]
        if len(set(linked)) != len(linked):
            raise ValueError("A recorded fee may be included only once")
        for amount in (self.investor_funding_amount, self.minimum_investor_contribution):
            if amount is not None and amount <= 0:
                raise ValueError("Funding and minimum contributions must be positive when supplied")
        return self


class DealBudgetLoan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Money | None = None
    rate_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    term_months: int | None = Field(default=None, ge=1, le=480)
    structure: Literal["amortizing", "simple_interest", "interest_only"] | None = None
    interest_only_months: int | None = Field(default=0, ge=0, le=480)
    repayment_config: dict[str, Any] | None = None
    payments_per_year: float | None = Field(default=12, ge=1, le=366, allow_inf_nan=False)


class DealBudgetPreview(DealBudgetPatch):
    loan: DealBudgetLoan | None = None


class DealBudgetRead(BaseModel):
    profile_id: UUID
    revision: int = 0
    funding_mode: Literal["single_investor", "multiple_investors"] = "single_investor"
    investor_funding_amount: Money | None = None
    minimum_investor_contribution: Money | None = None
    costs_confirmed: bool = False
    currency: str = "USD"
    rows: list[DealCostRow] = Field(default_factory=list)
    note: str | None = None
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None
    can_edit: bool = False
    suggested_rows: list[DealCostRow] = Field(default_factory=list)
    calculation: dict[str, Any] = Field(default_factory=dict)
