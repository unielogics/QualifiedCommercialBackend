"""One versioned operating disclosure and its separately reviewed proposals."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, get_args
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = "operating-plan-v1"
Cadence = Literal["weekly", "biweekly", "semi_monthly", "monthly", "quarterly", "annual", "one_time"]
ExpenseCategory = Literal["rent", "salaries", "payroll_taxes", "insurance", "utilities", "marketing", "software",
    "professional_fees", "supplies", "repairs", "transportation", "cogs", "merchant_processing", "other",
    "loan_payments", "income_taxes", "owner_draws", "transfers", "depreciation_amortization"]
ActionKind = Literal["price_change", "product_cost_change", "revenue_change", "expense_reduction", "expense_increase",
                     "processing_cost_change", "marketing_investment", "one_time_adjustment"]
CategoryConfirmation = Literal["confirmed", "not_applicable", "needs_help"]
ReviewDecision = Literal["proposed", "approved", "rejected"]
BaselineTreatment = Literal["unreconciled", "included_in_baseline", "incremental_to_baseline", "outside_operating_earnings"]
RateBasis = Literal["all_in", "processor_markup", "unknown"]
CostBasis = Literal["total", "per_unit"]

REVIEW_FIELDS = ("status", "reviewed_by_user_id", "reviewed_at", "review_reason", "review_source_stamp",
                 "baseline_treatment", "baseline_reconciliation_note")


class _Form(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def no_boolean_numbers(cls, value, info):
        if isinstance(value, bool) and info.field_name not in {"essential", "excluded", "no_operating_expenses_confirmed", "client_attestation"}:
            raise ValueError("Use a number or an explicit unknown, not a boolean")
        # A cleared worksheet amount or optional link is unknown, never zero.
        # Required text and boolean fields retain their normal validation.
        if isinstance(value, str) and not value.strip() and type(None) in get_args(cls.model_fields[info.field_name].annotation):
            return None
        return value


class ExpenseRow(_Form):
    id: UUID = Field(default_factory=uuid4)
    category: ExpenseCategory = "other"
    label: str = Field(default="", max_length=240)
    payee: str = Field(default="", max_length=240)
    current_amount: float | None = Field(default=None, ge=0, le=1e15)
    cost_basis: CostBasis = "total"
    unit_label: str = Field(default="", max_length=120)
    current_units_per_period: float | None = Field(default=None, ge=0, le=1e15)
    current_unit_cost: float | None = Field(default=None, ge=0, le=1e15)
    current_fixed_amount: float | None = Field(default=None, ge=0, le=1e15)
    cadence: Cadence = "monthly"
    essential: bool = True
    source_file_ids: list[UUID] = Field(default_factory=list, max_length=100)
    source_group_key: str = Field(default="", max_length=240)
    source_candidate_key: str = Field(default="", max_length=240)
    bank_vendor_key: str | None = Field(default=None, max_length=200)
    notes: str = Field(default="", max_length=4000)
    excluded: bool = False
    duplicate_of: UUID | None = None


class AdjustmentRow(_Form):
    id: UUID = Field(default_factory=uuid4)
    kind: ActionKind = "expense_reduction"
    label: str = Field(default="", max_length=240)
    driver_key: str = Field(default="", max_length=200)
    product_name: str = Field(default="", max_length=240)
    expense_row_id: UUID | None = None
    current_amount: float | None = Field(default=None, ge=0, le=1e15)
    proposed_amount: float | None = Field(default=None, ge=0, le=1e15)
    cost_basis: CostBasis = "total"
    unit_label: str = Field(default="", max_length=120)
    current_units_per_period: float | None = Field(default=None, ge=0, le=1e15)
    proposed_units_per_period: float | None = Field(default=None, ge=0, le=1e15)
    current_fixed_amount: float | None = Field(default=None, ge=0, le=1e15)
    proposed_fixed_amount: float | None = Field(default=None, ge=0, le=1e15)
    cadence: Cadence = "monthly"
    units_per_period: float | None = Field(default=None, gt=0, le=1e15)
    current_unit_price: float | None = Field(default=None, ge=0, le=1e15)
    proposed_unit_price: float | None = Field(default=None, ge=0, le=1e15)
    current_unit_cost: float | None = Field(default=None, ge=0, le=1e15)
    proposed_unit_cost: float | None = Field(default=None, ge=0, le=1e15)
    contribution_margin_pct: float | None = Field(default=None, ge=0, le=100)
    revenue_return_multiple: float | None = Field(default=None, ge=0, le=1e15)
    implementation_cost: float | None = Field(default=None, ge=0, le=1e15)
    implementation_cost_cadence: Literal["one_time", "monthly", "annual"] = "one_time"
    effective_date: date | None = None
    explanation: str = Field(default="", max_length=4000)
    source_file_ids: list[UUID] = Field(default_factory=list, max_length=100)
    source_note: str = Field(default="", max_length=4000)
    owner_name: str = Field(default="", max_length=180)
    merchant_source_key: str | None = Field(default=None, max_length=240)
    merchant_offer_id: UUID | None = None
    processing_volume: float | None = Field(default=None, ge=0, le=1e15)
    processing_transaction_count: int | None = Field(default=None, ge=0, le=1_000_000_000)
    current_processing_rate_pct: float | None = Field(default=None, ge=0, le=100)
    proposed_processing_rate_pct: float | None = Field(default=None, ge=0, le=100)
    current_processing_fixed_fee: float | None = Field(default=None, ge=0, le=1e15)
    proposed_processing_fixed_fee: float | None = Field(default=None, ge=0, le=1e15)
    current_processing_per_transaction_fee: float | None = Field(default=None, ge=0, le=1e15)
    proposed_processing_per_transaction_fee: float | None = Field(default=None, ge=0, le=1e15)
    current_processing_rate_basis: RateBasis = "unknown"
    proposed_processing_rate_basis: RateBasis = "unknown"
    excluded: bool = False
    duplicate_of: UUID | None = None
    status: ReviewDecision = "proposed"
    reviewed_by_user_id: UUID | None = None
    reviewed_at: datetime | None = None
    review_reason: str = Field(default="", max_length=2000)
    review_source_stamp: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    baseline_treatment: BaselineTreatment = "unreconciled"
    baseline_reconciliation_note: str = Field(default="", max_length=2000)

    @field_validator("effective_date", "reviewed_at", mode="before")
    @classmethod
    def empty_dates(cls, value):
        return None if value == "" else value

    @field_validator("reviewed_at")
    @classmethod
    def aware_review(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Review timestamp must include a timezone")
        return value


class ReleasedCashFlowAllocation(_Form):
    id: UUID = Field(default_factory=uuid4)
    label: str = Field(default="", max_length=240)
    amount_monthly: float | None = Field(default=None, ge=0, le=1e15)
    expense_row_id: UUID | None = None
    notes: str = Field(default="", max_length=4000)


class ReleasedCashFlow(_Form):
    source_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    allocations: list[ReleasedCashFlowAllocation] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def distinct_rows(self):
        if len({row.id for row in self.allocations}) != len(self.allocations):
            raise ValueError("Each cash-flow allocation must have its own stable ID")
        return self


class OperatingPlanBody(_Form):
    schema_version: Literal["operating-plan-v1"] = SCHEMA_VERSION
    business_name: str = Field(default="", max_length=255)
    period_start: date | None = None
    period_end: date | None = None
    projection_start: date | None = None
    currency: str = Field(default="USD", pattern=r"^(?:[A-Z]{3})?$")
    basis: Literal["cash", "accrual", "unknown"] = "unknown"
    no_operating_expenses_confirmed: bool = False
    client_attestation: bool = False
    notes: str = Field(default="", max_length=10000)
    expenses: list[ExpenseRow] = Field(default_factory=list, max_length=500)
    category_confirmations: dict[ExpenseCategory, CategoryConfirmation] = Field(default_factory=dict)
    adjustments: list[AdjustmentRow] = Field(default_factory=list, max_length=300)
    released_cash_flow: ReleasedCashFlow = Field(default_factory=ReleasedCashFlow)
    field_sources: dict[str, Any] = Field(default_factory=dict)

    @field_validator("period_start", "period_end", "projection_start", mode="before")
    @classmethod
    def empty_dates(cls, value):
        return None if value == "" else value

    @field_validator("currency", mode="before")
    @classmethod
    def normalized_currency(cls, value):
        return str(value or "").strip().upper()

    @model_validator(mode="after")
    def coherent_rows(self):
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("Reporting end must follow reporting start")
        for collection in (self.expenses, self.adjustments):
            rows = {row.id: row for row in collection}
            if len(rows) != len(collection):
                raise ValueError("Each row must have its own stable ID")
            for row in collection:
                if row.duplicate_of is not None:
                    target = rows.get(row.duplicate_of)
                    if target is None or target.id == row.id or target.excluded or target.duplicate_of:
                        raise ValueError("Choose another active row as the duplicate's surviving row")
                    row.excluded = True
        if self.no_operating_expenses_confirmed and any(not row.excluded for row in self.expenses):
            raise ValueError("A no-expenses confirmation cannot coexist with active expense rows")
        active_categories = {row.category for row in self.expenses if not row.excluded}
        if any(decision == "not_applicable" and category in active_categories
               for category, decision in self.category_confirmations.items()):
            raise ValueError("A category marked not applicable cannot contain active expenses")
        known_expenses = {row.id for row in self.expenses}
        if any(row.expense_row_id and row.expense_row_id not in known_expenses for row in self.adjustments):
            raise ValueError("An adjustment must refer to an expense in this operating plan")
        if any(row.expense_row_id and row.expense_row_id not in known_expenses for row in self.released_cash_flow.allocations):
            raise ValueError("A cash-flow allocation must refer to an expense in this operating plan")
        return self


EXPENSE_FIELDS = tuple(ExpenseRow.model_fields)
ADJUSTMENT_FIELDS = tuple(AdjustmentRow.model_fields)


def empty_body():
    return OperatingPlanBody().model_dump(mode="json")


def validate_body(raw):
    try:
        return OperatingPlanBody.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        errors = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        raise HTTPException(422, detail={"message": "Review the operating plan fields", "errors": errors}) from exc


def action_missing(row):
    required = ["label", "driver_key", "effective_date", "explanation", "source_note", "implementation_cost"]
    kind = row["kind"]
    if kind == "price_change":
        required += ["units_per_period", "current_unit_price", "proposed_unit_price"]
    elif kind == "product_cost_change":
        required += ["units_per_period", "current_unit_cost", "proposed_unit_cost"]
    elif kind == "processing_cost_change":
        required += ["processing_volume", "current_processing_rate_pct", "proposed_processing_rate_pct"]
    elif kind == "marketing_investment":
        required += ["product_name", "expense_row_id", "current_amount", "proposed_amount",
                     "revenue_return_multiple", "contribution_margin_pct"]
    elif kind in {"expense_reduction", "expense_increase"} and row.get("cost_basis") == "per_unit":
        required += ["unit_label", "current_units_per_period", "proposed_units_per_period", "current_unit_cost",
                     "proposed_unit_cost", "current_fixed_amount", "proposed_fixed_amount"]
    else:
        required += ["current_amount", "proposed_amount"]
        if kind == "revenue_change":
            required.append("contribution_margin_pct")
    return [key for key in required if row.get(key) is None or row.get(key) == ""]


def expense_missing(row):
    fields = ["label"]
    fields += ["unit_label", "current_units_per_period", "current_unit_cost", "current_fixed_amount"] if row.get("cost_basis") == "per_unit" else ["current_amount"]
    missing = [key for key in fields if row.get(key) is None or row.get(key) == ""]
    if row.get("cost_basis") == "per_unit" and not missing:
        amount = cost_amount(row)
        if amount is None:
            missing.append("computed_total")
        elif row.get("current_amount") is not None and abs(amount - row["current_amount"]) > .01:
            missing.append("reconcile_current_amount")
    return missing


def cost_amount(row, side="current"):
    """One selected cost basis, never a scalar total plus its unit components."""
    if row.get("cost_basis") != "per_unit":
        return row.get(f"{side}_amount")
    values = [row.get(f"{side}_units_per_period"), row.get(f"{side}_unit_cost"), row.get(f"{side}_fixed_amount")]
    if any(value is None for value in values):
        return None
    total = values[0] * values[1] + values[2]
    return total if total <= 1e15 else None


def completion(raw):
    body = validate_body(raw)
    missing = [key for key in ("business_name", "period_start", "period_end", "currency") if not body[key]]
    if body["basis"] == "unknown":
        missing.append("basis")
    active = [row for row in body["expenses"] if not row["excluded"]]
    if not active and not body["no_operating_expenses_confirmed"]:
        missing.append("expenses_or_no_expenses_confirmation")
    missing.extend(f"category_confirmations.{category}" for category, decision in body["category_confirmations"].items()
                   if decision == "needs_help")
    active_categories = {row["category"] for row in active}
    missing.extend(f"category_confirmations.{category}.expense_detail"
                   for category, decision in body["category_confirmations"].items()
                   if decision == "confirmed" and category not in active_categories)
    for row in active:
        missing.extend(f"expenses.{row['id']}.{key}" for key in expense_missing(row))
    for row in body["adjustments"]:
        if not row["excluded"]:
            missing.extend(f"adjustments.{row['id']}.{key}" for key in action_missing(row))
    return {"complete": not missing, "missing": missing, "missing_count": len(missing), "active_expenses": len(active),
            "active_adjustments": sum(not row["excluded"] for row in body["adjustments"])}


def key_facts(raw):
    body = validate_body(raw)
    safe = {**body, "adjustments": [{key: value for key, value in row.items() if key not in REVIEW_FIELDS}
                                     for row in body["adjustments"]]}
    return {"source_form": "operating_plan", "document_type": "operating_expenses_and_earnings_plan",
            "business_name": body["business_name"], "period_start": body["period_start"], "period_end": body["period_end"],
            "currency": body["currency"], "basis": body["basis"], "operating_plan": safe,
            "financial_fact_policy": "Expense disclosure and operating proposals; not a P&L, current earnings or program qualification."}
