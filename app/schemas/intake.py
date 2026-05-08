"""SmartIntake (desktop) + AI Intake (mobile) — 4-step submission."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.enums import EntityType, ExperienceTier, LoanSide, LoanType, PropertyType


class BorrowerStep(BaseModel):
    name: str
    email: EmailStr
    phone: str
    entity_type: EntityType = EntityType.LLC
    entity_name: str | None = None
    experience: ExperienceTier = ExperienceTier.NONE


class AssetStep(BaseModel):
    address: str
    city: str | None = None
    # USPS 2-letter state code (alembic 0028). Captured separately
    # from city so address columns are queryable / sortable.
    state: str | None = None
    property_type: PropertyType
    sqft: int | None = None
    annual_taxes: float = 0
    annual_insurance: float = 0
    as_is_value: float | None = None


class NumbersStep(BaseModel):
    type: LoanType
    amount: float
    ltv: float
    ltc: float | None = None
    arv: float | None = None
    monthly_rent: float | None = None
    base_rate: float


class AIRulesStep(BaseModel):
    floor_rate: float
    max_buy_down_points: float = 3.0
    require_soft_pull: bool = True
    auto_send_terms: bool = True
    doc_auto_verify: bool = True
    escalation_delta_bps: int = 25
    notify_channel: str = "push"  # push | email | sms
    intro_message: str | None = None


class IntakeDocumentOverrides(BaseModel):
    """Optional pre-loan checklist edits captured in the
    SmartIntakeModal's Documents step (alembic 0023).

    `skip_names` — firm checklist item names the agent doesn't want
    materialized at kickoff (matches `DocChecklistItem.name`).
    `add_items` — custom one-off docs the agent wants the AI to
    collect on this loan in addition to the resolved checklist.
    """
    skip_names: list[str] = Field(default_factory=list)
    add_items: list["IntakeCustomDoc"] = Field(default_factory=list)


class IntakeCustomDoc(BaseModel):
    name: str
    due_date: date | None = None
    checklist_key: str | None = None


class SmartIntakePayload(BaseModel):
    borrower: BorrowerStep
    asset: AssetStep
    numbers: NumbersStep
    ai_rules: AIRulesStep
    # Buyer / seller (alembic 0023). Defaults to buyer for purchase
    # loans (the dominant case in current pipeline).
    side: LoanSide = LoanSide.BUYER
    # Pre-loan checklist edits applied at kickoff.
    document_overrides: IntakeDocumentOverrides | None = None


class SmartIntakeResponse(BaseModel):
    loan_id: UUID
    deal_id: str
