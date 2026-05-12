from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.enums import (
    AmortizationStyle,
    DealHealth,
    EntityType,
    ExitStrategy,
    ExperienceTier,
    LoanPurpose,
    LoanSide,
    LoanStage,
    LoanType,
    PrepayPenalty,
    PropertyType,
)
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
    # Buyer-side or seller-side transaction (alembic 0023). Drives
    # checklist filtering at kickoff. Defaults to buyer.
    side: LoanSide = LoanSide.BUYER
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
    side: LoanSide | None = None
    stage: LoanStage | None = None
    amount: float | None = None
    ltv: float | None = None
    ltc: float | None = None
    arv: float | None = None
    base_rate: float | None = None
    discount_points: float | None = None
    final_rate: float | None = None
    dscr: float | None = None
    origination_pct: float | None = None
    term_months: int | None = None
    monthly_rent: float | None = None
    annual_taxes: float | None = None
    annual_insurance: float | None = None
    monthly_hoa: float | None = None
    close_date: date | None = None
    # Underwriter fine-tuning fields (alembic 0044). All optional.
    amortization_style: AmortizationStyle | None = None
    prepay_penalty: PrepayPenalty | None = None
    vacancy_pct: float | None = None
    expense_ratio_pct: float | None = None
    reserves_required: float | None = None
    lender_fees: float | None = None
    fico_override: int | None = None
    entity_type: EntityType | None = None
    entity_name: str | None = None
    experience_tier: ExperienceTier | None = None
    construction_holdback_pct: float | None = None
    draw_count: int | None = None
    exit_strategy: ExitStrategy | None = None
    cash_to_borrower: float | None = None
    seasoning_months: int | None = None
    property_count: int | None = None
    # Property details — written by the AI property-intake tool +
    # editable from the desktop PropertyTab. unit_count is new in
    # alembic 0019; the others were on the ORM but not surfaced.
    sqft: int | None = None
    beds: int | None = None
    baths: float | None = None
    year_built: int | None = None
    unit_count: int | None = None
    # Listing-style fields (alembic 0043).
    description: str | None = None
    lot_size_sqft: int | None = None
    zoning: str | None = None
    parcel_id: str | None = None
    listing_status: str | None = None
    highlight_features: list[str] | None = None
    street_view_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class LoanRead(ORMModel):
    id: UUID
    deal_id: str
    client_id: UUID
    broker_id: UUID | None
    # Owner display name for the operator pipeline header — populated by
    # list endpoints via a join on brokers.display_name. Renders only
    # when the calling user is super_admin / loan_exec; agents see only
    # their own files so the name is implicit.
    broker_name: str | None = None
    client_name: str | None = None
    lender_id: UUID | None = None
    address: str
    city: str | None
    property_type: PropertyType
    type: LoanType
    purpose: LoanPurpose | None
    side: LoanSide = LoanSide.BUYER
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
    # Property details (writable via AI intake + desktop PropertyTab).
    sqft: int | None = None
    beds: int | None = None
    baths: float | None = None
    year_built: int | None = None
    unit_count: int | None = None
    # Listing-style fields (alembic 0043).
    description: str | None = None
    lot_size_sqft: int | None = None
    zoning: str | None = None
    parcel_id: str | None = None
    listing_status: str | None = None
    highlight_features: list[str] | None = None
    street_view_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # Underwriter fine-tuning fields (alembic 0044).
    amortization_style: AmortizationStyle | None = None
    prepay_penalty: PrepayPenalty | None = None
    vacancy_pct: float | None = None
    expense_ratio_pct: float | None = None
    reserves_required: float | None = None
    lender_fees: float | None = None
    fico_override: int | None = None
    entity_type: EntityType | None = None
    entity_name: str | None = None
    experience_tier: ExperienceTier | None = None
    construction_holdback_pct: float | None = None
    draw_count: int | None = None
    exit_strategy: ExitStrategy | None = None
    cash_to_borrower: float | None = None
    seasoning_months: int | None = None
    property_count: int | None = None
    # Living Loan File
    status_summary: str | None = None
    deal_health: DealHealth = DealHealth.ON_TRACK
    living_profile: LivingLoanProfile | None = None


class PropertyUpdate(BaseModel):
    """Broker-accessible patch for property/listing fields only. The
    full LoanUpdate endpoint stays internal-funding-only; this carves
    out the listing-style surface so agents can fill it from the
    PropertyTab without escalating their permissions."""
    address: str | None = None
    city: str | None = None
    state: str | None = None
    property_type: PropertyType | None = None
    sqft: int | None = None
    beds: int | None = None
    baths: float | None = None
    year_built: int | None = None
    unit_count: int | None = None
    annual_taxes: float | None = None
    annual_insurance: float | None = None
    monthly_hoa: float | None = None
    description: str | None = None
    lot_size_sqft: int | None = None
    zoning: str | None = None
    parcel_id: str | None = None
    listing_status: str | None = None
    highlight_features: list[str] | None = None
    street_view_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None


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
    term_months: int | None = None
    monthly_rent: float | None = None
    ltv: float | None = None  # 0..1; recomputes amount = ltv * appraised value when given
    # Sizing overrides — applied through services/math/sizing.compute_loan_amount.
    purpose: LoanPurpose | None = None
    arv: float | None = None
    brv: float | None = None             # F&F / Ground Up purchase price
    rehab_budget: float | None = None    # F&F / Ground Up rehab cost
    payoff: float | None = None          # DSCR refi existing-mortgage payoff
    ltv_tier_cap: float | None = None    # credit-tier-derived cap (0..1)
    # Underwriter fine-tuning overrides (alembic 0044). Drive monthly P&I,
    # DSCR, and cash-to-close math without persisting until Save Criteria.
    amortization_style: AmortizationStyle | None = None
    origination_pct: float | None = None
    vacancy_pct: float | None = None
    expense_ratio_pct: float | None = None
    reserves_required: float | None = None
    lender_fees: float | None = None
    construction_holdback_pct: float | None = None


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
    # Sizing inputs — when present, the server runs them through
    # compute_loan_amount and may clamp `loan_amount` down to the cap.
    purpose: LoanPurpose | None = None
    arv: float | None = None
    brv: float | None = None
    rehab_budget: float | None = None
    payoff: float | None = None
    ltv_tier_cap: float | None = None


class SizingBreakdown(BaseModel):
    """Mirror of services/math/sizing.SizingResult for wire transport."""
    loan_amount: float
    max_allowed: float
    binding_constraint: str
    clamped: bool
    ltv: float | None = None
    ltc: float | None = None
    arv_ltv: float | None = None
    effective_ltv_cap: float | None = None
    total_cost: float | None = None
    cash_to_borrower: float | None = None
    cash_to_close: float | None = None


class RecalcResponse(BaseModel):
    final_rate: float
    monthly_pi: float
    dscr: float | None
    cash_to_close_pricing: float
    hud_total: float
    warnings: list[dict]
    loan_amount: float | None = None
    sizing: SizingBreakdown | None = None
    # Underwriter calculator outputs — driven by alembic 0044 fields.
    # monthly_interest is the IO payment when amortization_style=IO.
    monthly_interest: float | None = None
    # Total interest over the full term (fully-amortizing) or 12 months
    # (IO ballpark, for in-page summary stats).
    total_interest: float | None = None
    # Total cash required at close: pricing + lender_fees + reserves -
    # the construction holdback (which the borrower doesn't wire day-1).
    total_cash_to_close: float | None = None
    # Effective monthly debt service used in DSCR (PITIA after vacancy /
    # expense ratio applied). Surfaced so the UI can show how the inputs
    # changed the ratio.
    effective_pitia: float | None = None
    # Effective rent (gross rent × (1 - vacancy_pct) × (1 - expense_ratio_pct)).
    effective_rent: float | None = None
