from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.enums import DealHealth, LoanPurpose, LoanStage, LoanType, PropertyType
from app.schemas.common import ORMModel


# ── Living Loan Profile (output of "The Associate" summarizer) ───────────

MarketWarning = Literal["Rate Pressure", "Rate Stability", "Rate Easing"]


class MarketContextBlock(BaseModel):
    narrative: str
    warning: MarketWarning | None = None


class NextActionsBlock(BaseModel):
    ai: list[str] = Field(default_factory=list)
    broker: list[str] = Field(default_factory=list)


class LivingLoanProfile(BaseModel):
    """Structured 4-section output from the summarizer.

    Stored as JSONB on `loans.living_profile` and returned in LoanRead so
    the frontend can render each section in its own card with the right
    treatment (warning pill on market_context, list bullets for bottlenecks
    and next_actions, etc.)."""
    current_status: str
    market_context: MarketContextBlock
    bottlenecks: list[str] = Field(default_factory=list)
    next_actions: NextActionsBlock = Field(default_factory=NextActionsBlock)
    deal_health: DealHealth = DealHealth.ON_TRACK


class LoanBase(BaseModel):
    address: str
    city: str | None = None
    property_type: PropertyType = PropertyType.SFR
    type: LoanType
    purpose: LoanPurpose | None = None
    amount: float
    ltv: float | None = None
    ltc: float | None = None
    arv: float | None = None
    base_rate: float | None = None
    discount_points: float = 0
    final_rate: float | None = None
    origination_pct: float = 0.015
    term_months: int | None = None
    monthly_rent: float | None = None
    annual_taxes: float = 0
    annual_insurance: float = 0
    monthly_hoa: float = 0
    close_date: date | None = None


class LoanCreate(LoanBase):
    client_id: UUID
    broker_id: UUID | None = None
    deal_id: str | None = None  # auto-generated if absent


class LoanUpdate(BaseModel):
    address: str | None = None
    city: str | None = None
    property_type: PropertyType | None = None
    purpose: LoanPurpose | None = None
    stage: LoanStage | None = None
    amount: float | None = None
    ltv: float | None = None
    ltc: float | None = None
    arv: float | None = None
    base_rate: float | None = None
    discount_points: float | None = None
    origination_pct: float | None = None
    term_months: int | None = None
    monthly_rent: float | None = None
    annual_taxes: float | None = None
    annual_insurance: float | None = None
    monthly_hoa: float | None = None
    close_date: date | None = None


class LoanRead(ORMModel):
    id: UUID
    deal_id: str
    client_id: UUID
    broker_id: UUID | None
    address: str
    city: str | None
    property_type: PropertyType
    type: LoanType
    purpose: LoanPurpose | None
    stage: LoanStage
    amount: float
    ltv: float | None
    ltc: float | None
    arv: float | None
    base_rate: float | None
    discount_points: float
    final_rate: float | None
    origination_pct: float
    term_months: int | None
    monthly_rent: float | None
    annual_taxes: float
    annual_insurance: float
    monthly_hoa: float
    dscr: float | None
    risk_score: int | None
    close_date: date | None
    # Living Loan File
    status_summary: str | None = None
    deal_health: DealHealth = DealHealth.ON_TRACK
    living_profile: LivingLoanProfile | None = None


class StageTransition(BaseModel):
    new_stage: LoanStage
    note: str = ""


class RecalcRequest(BaseModel):
    discount_points: float = Field(default=0, ge=0)
    loan_amount: float | None = None
    base_rate: float | None = None
    # Advanced-mode overrides — when present, they replace the loan record's
    # values for this single recalc. None = inherit from the loan.
    annual_taxes: float | None = None
    annual_insurance: float | None = None
    monthly_hoa: float | None = None
    ltv: float | None = None  # 0..1; recomputes amount = ltv * appraised value when given


class FreeCalcRequest(BaseModel):
    """Loan-less what-if calculator. Used by the standalone /simulator page
    so users can run pricing math without first creating a loan record."""
    type: LoanType
    property_type: PropertyType = PropertyType.SFR
    loan_amount: float = Field(gt=0)
    base_rate: float = Field(default=0.075, gt=0, lt=1)
    discount_points: float = Field(default=0, ge=0)
    term_months: int | None = None
    monthly_rent: float | None = None
    annual_taxes: float = 0
    annual_insurance: float = 0
    monthly_hoa: float = 0


class RecalcResponse(BaseModel):
    final_rate: float
    monthly_pi: float
    dscr: float | None
    cash_to_close_pricing: float
    hud_total: float
    warnings: list[dict]
