from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

MetricStatus = Literal[
    "concerning", "acceptable", "healthy", "very_strong", "unavailable"
]
ReadinessBand = Literal[
    "ready_soon",
    "three_to_six_months",
    "six_to_twelve_months",
    "one_plus_year",
    "insufficient_evidence",
]
AddBackStatus = Literal[
    "candidate",
    "evidence_pending",
    "cpa_attested",
    "qc_verified",
    "lender_accepted",
    "lender_rejected",
    "expired",
]
ReadinessPhaseKey = Literal[
    "baseline_health_check",
    "financial_restructuring",
    "system_tracking",
    "pre_underwriting",
    "prime_capital",
]


class FinancialPeriodCreate(BaseModel):
    entity_name: str = Field(min_length=1, max_length=200)
    accounting_basis: Literal["cash", "accrual", "tax", "unknown"] = "unknown"
    currency: str = Field(default="USD", min_length=3, max_length=3)
    period_start: date
    period_end: date
    months_covered: int = Field(ge=1, le=60)
    source_kind: Literal["stated", "derived", "self_reported", "ai_extracted"]
    cogs_applicability: Literal["applicable", "not_applicable", "unknown"] = "unknown"
    revenue: Decimal | None = None
    cogs: Decimal | None = None
    gross_profit: Decimal | None = None
    operating_expenses: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None
    ebitda: Decimal | None = None
    adjusted_ebitda: Decimal | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    source_file_id: UUID | None = None
    source_analysis_id: UUID | None = None
    extractor_version: str | None = Field(default=None, max_length=80)
    content_hash: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("entity_name")
    @classmethod
    def _entity_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _validate_period(self) -> FinancialPeriodCreate:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")
        values = (
            self.revenue,
            self.cogs,
            self.gross_profit,
            self.operating_expenses,
            self.operating_income,
            self.net_income,
            self.ebitda,
            self.adjusted_ebitda,
        )
        if not any(value is not None for value in values):
            raise ValueError("At least one financial value is required")
        if self.cogs_applicability == "not_applicable" and self.cogs not in (None, Decimal("0")):
            raise ValueError("COGS must be blank or zero when explicitly not applicable")
        if self.source_kind == "ai_extracted" and self.source_file_id is None:
            raise ValueError("AI-extracted periods require a source_file_id")
        return self


class FinancialPeriodReview(BaseModel):
    status: Literal["confirmed", "rejected", "superseded"]
    note: str | None = Field(default=None, max_length=2000)


class FinancialPeriodRead(BaseModel):
    id: UUID
    profile_id: UUID
    entity_name: str
    accounting_basis: str
    currency: str
    period_start: date
    period_end: date
    months_covered: int
    source_kind: str
    review_status: str
    cogs_applicability: str
    revenue: float | None = None
    cogs: float | None = None
    gross_profit: float | None = None
    derived_gross_profit: float | None = None
    operating_expenses: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    ebitda: float | None = None
    adjusted_ebitda: float | None = None
    confidence: float | None = None
    source_file_id: UUID | None = None
    source_analysis_id: UUID | None = None
    extractor_version: str | None = None
    content_hash: str
    reconciliation_warnings: list[dict] = Field(default_factory=list)
    created_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by_user_id: UUID | None = None


class AddBackCreate(BaseModel):
    financial_period_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    requires_cpa_attestation: bool = False
    evidence_file_id: UUID | None = None
    evidence_note: str | None = Field(default=None, max_length=4000)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("title", "category")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return value.strip()


class AddBackTransition(BaseModel):
    expected_status: AddBackStatus
    status: AddBackStatus
    evidence_file_id: UUID | None = None
    evidence_note: str | None = Field(default=None, max_length=4000)
    lender_program_key: str | None = Field(default=None, max_length=120)
    expires_at: datetime | None = None


class AddBackRead(BaseModel):
    id: UUID
    profile_id: UUID
    financial_period_id: UUID | None = None
    title: str
    category: str
    description: str | None = None
    amount: float
    status: AddBackStatus
    requires_cpa_attestation: bool
    evidence_file_id: UUID | None = None
    evidence_note: str | None = None
    cpa_attested_at: datetime | None = None
    cpa_attested_by_user_id: UUID | None = None
    qc_verified_at: datetime | None = None
    qc_verified_by_user_id: UUID | None = None
    lender_program_key: str | None = None
    lender_decided_at: datetime | None = None
    lender_decided_by_user_id: UUID | None = None
    expires_at: datetime | None = None
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime


class ReadinessActionCreate(BaseModel):
    phase_key: ReadinessPhaseKey
    title: str = Field(min_length=1, max_length=240)
    detail: str | None = Field(default=None, max_length=4000)
    baseline: dict = Field(default_factory=dict)
    target: dict = Field(default_factory=dict)
    owner_user_id: UUID | None = None
    due_date: date | None = None
    dependencies: list[UUID] = Field(default_factory=list, max_length=50)
    required_evidence: list[str] = Field(default_factory=list, max_length=50)
    expected_impact: str | None = Field(default=None, max_length=2000)
    status: Literal["not_started", "in_progress", "blocked", "completed", "cancelled"] = (
        "not_started"
    )
    idempotency_key: str = Field(min_length=8, max_length=160)


class ReadinessActionPatch(BaseModel):
    expected_version: int = Field(ge=1)
    phase_key: ReadinessPhaseKey | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    detail: str | None = Field(default=None, max_length=4000)
    baseline: dict | None = None
    target: dict | None = None
    owner_user_id: UUID | None = None
    due_date: date | None = None
    dependencies: list[UUID] | None = Field(default=None, max_length=50)
    required_evidence: list[str] | None = Field(default=None, max_length=50)
    expected_impact: str | None = Field(default=None, max_length=2000)
    status: Literal["not_started", "in_progress", "blocked", "completed", "cancelled"] | None = None
    idempotency_key: str = Field(min_length=8, max_length=160)


class ReadinessActionRead(BaseModel):
    id: UUID
    action_key: UUID
    profile_id: UUID
    version: int
    phase_key: ReadinessPhaseKey
    title: str
    detail: str | None = None
    baseline: dict = Field(default_factory=dict)
    target: dict = Field(default_factory=dict)
    owner_user_id: UUID | None = None
    due_date: date | None = None
    dependencies: list[UUID] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    expected_impact: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class ReadinessMetric(BaseModel):
    key: str
    label: str
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    unit: str
    status: MetricStatus
    source_period_id: UUID | None = None
    confidence_pct: float | None = None
    source: dict = Field(default_factory=dict)


class ReadinessPillar(BaseModel):
    key: str
    label: str
    weight: float
    score: float | None = None
    coverage_pct: float
    status: MetricStatus


class ReadinessSignal(BaseModel):
    key: str
    label: str
    detail: str
    impact: Literal["low", "medium", "high", "critical"]
    metric_key: str | None = None


class ReadinessPhase(BaseModel):
    key: str
    label: str
    status: Literal["not_started", "in_progress", "ready", "completed"]
    description: str
    actions: list[dict] = Field(default_factory=list)


class CapitalReadinessRead(BaseModel):
    id: UUID
    profile_id: UUID
    snapshot_version: int
    policy_key: str
    policy_version: int
    formula_version: str = "score_v2"
    evidence_fingerprint: str
    as_of: datetime
    communication_locale: Literal["en", "es"] = "en"
    display_locale: Literal["en", "es"] = "en"
    review_status: Literal["provisional", "awaiting_review", "confirmed", "revised"]
    score: float | None = None
    band: ReadinessBand
    evidence_coverage_pct: float
    confidence_pct: float
    pillars: list[ReadinessPillar] = Field(default_factory=list)
    metrics: list[ReadinessMetric] = Field(default_factory=list)
    strengths: list[ReadinessSignal] = Field(default_factory=list)
    blockers: list[ReadinessSignal] = Field(default_factory=list)
    phases: list[ReadinessPhase] = Field(default_factory=list)
    program_opportunities: list[dict] = Field(default_factory=list)
    source_manifest: list[dict] = Field(default_factory=list)
    created_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by_user_id: UUID | None = None
    material_change: dict | None = None


class CapitalReadinessRecalculate(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    expected_snapshot_version: int | None = Field(default=None, ge=1)


class CapitalReadinessReviewRequest(BaseModel):
    expected_snapshot_version: int = Field(ge=1)
    status: Literal["confirmed", "revised"]
    note: str | None = Field(default=None, max_length=2000)


class CommunicationLocalePatch(BaseModel):
    communication_locale: Literal["en", "es"]
    source: Literal[
        "borrower_selection",
        "staff_selection",
        "contact_default",
        "inbound_link",
        "browser_preference",
        "system_default",
    ]
