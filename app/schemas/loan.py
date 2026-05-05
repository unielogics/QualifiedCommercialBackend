from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field

from app.enums import LoanPurpose, LoanStage, LoanType, PropertyType
from app.schemas.common import ORMModel


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


class StageTransition(BaseModel):
    new_stage: LoanStage
    note: str = ""


class RecalcRequest(BaseModel):
    discount_points: float = Field(default=0, ge=0)
    loan_amount: float | None = None
    base_rate: float | None = None


class RecalcResponse(BaseModel):
    final_rate: float
    monthly_pi: float
    dscr: float | None
    cash_to_close_pricing: float
    hud_total: float
    warnings: list[dict]
