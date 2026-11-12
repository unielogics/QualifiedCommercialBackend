"""Read-only money illustrations, separate from saved financial schemas."""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InvestorReturnsQuery(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    exit_month: Decimal = Field(default=Decimal(6), gt=0, le=601)
    investment_amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)


class InvestorReturnsSource(BaseModel):
    kind: Literal["staff_saved", "staff_preview", "listing_saved", "published_listing"]
    id: str
    budget_revision: int | None
    terms_version: int | None
    updated_at: datetime | None
    fingerprint: str


class InvestorReturnsRequest(BaseModel):
    exit_month: float
    investment_amount: float | None


class InvestorHoldingRow(BaseModel):
    month: int
    gross_receipts: float | None
    interest: float | None
    principal_returned: float | None
    fees: float | None
    reserve_release: float | None
    net_distribution: float | None
    net_cash_flow: float | None
    cumulative_interest: float | None
    cumulative_principal_returned: float | None
    cumulative_fees: float | None
    cumulative_net_profit: float | None
    cumulative_net_roi_pct: float | None
    cumulative_net_cash_flow: float | None
    outstanding_principal: float | None


class InvestorExitPoint(BaseModel):
    month: float
    payoff_month: float | None
    capped_at_maturity: bool
    status: Literal["calculated", "needs_review"]
    principal_returned: float | None
    gross_interest: float | None
    earned_interest: float | None
    minimum_interest_charge: float | None
    payoff_charge_income: float | None
    total_fees: float | None
    known_fee_subtotal: float | None
    initial_outlay: float | None
    net_income: float | None
    net_roi_pct: float | None
    net_cash_profit: float | None
    net_cash_received: float | None
    reserve_released: float | None
    reserve_held: float | None
    missing_assumptions: list[str]
    assumptions: list[str]


class InvestorProjectionPerformance(BaseModel):
    curve_work_estimate: int
    curve_work_limit: int
    curve_points: int


class InvestorReturnsRead(BaseModel):
    policy_version: str
    status: Literal["calculated", "needs_review", "unavailable"]
    currency: str | None
    source: InvestorReturnsSource
    request: InvestorReturnsRequest
    basis: Literal["offering_investor_capital", "pro_rata_illustration"]
    offering_investor_capital: float | None
    investment_amount: float | None
    initial_outlay: float | None
    maturity_month: float | None
    maximum_month: int | None
    holding: list[InvestorHoldingRow]
    selected_exit: InvestorExitPoint | None
    full_term: InvestorExitPoint | None
    payoff_curve_status: Literal["complete", "on_demand", "unavailable"]
    payoff_curve: list[InvestorExitPoint]
    performance: InvestorProjectionPerformance
    missing_inputs: list[str]
    assumptions: list[str]
