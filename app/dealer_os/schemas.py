"""Pydantic schemas for the Dealer OS API."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DealerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    industry: str = "auto_dealer"
    notes: str | None = None


class DealerUpdate(BaseModel):
    name: str | None = None
    bucket_id: UUID | None = None  # manual bucket link/unlink (PATCH with null unlinks)
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    industry: str | None = None
    status: str | None = None
    notes: str | None = None
    # Team links (or unlinks) a dealer self-serve login to this business.
    dealer_user_id: UUID | None = None


class DealerRead(ORM):
    id: UUID
    name: str
    dealer_user_id: UUID | None = None
    bucket_name: str | None = None
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    industry: str
    status: str
    notes: str | None = None
    bucket_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class DealerListItem(ORM):
    id: UUID
    name: str
    city: str | None = None
    state: str | None = None
    status: str
    created_at: datetime
    # rollups filled by the router (no snapshot may exist yet)
    score: float | None = None
    tier: str | None = None
    open_alerts: int = 0


class TargetRead(ORM):
    id: UUID
    metric_key: str
    ai_proposed_value: float | None = None
    ai_rationale: str | None = None
    ai_proposed_at: datetime | None = None
    admin_value: float | None = None
    admin_set_at: datetime | None = None
    status: str
    effective_value: float | None = None


class TargetOverride(BaseModel):
    metric_key: str = Field(min_length=1, max_length=48)
    admin_value: float | None = None  # None clears the override back to the AI proposal


# --- Stream 2: ingestion & normalization -----------------------------------


class CashEventRow(BaseModel):
    occurred_on: date
    description: str = Field(min_length=1, max_length=320)
    amount: float
    invoice_date: date | None = None
    due_date: date | None = None


class CashImport(BaseModel):
    rows: list[CashEventRow] = Field(max_length=5000)


class CashImportResult(BaseModel):
    imported: int
    periods: int


class CashEventRead(ORM):
    id: UUID
    period: date
    occurred_on: date
    description: str
    amount: float
    category: str
    flags: dict | None = None
    categorized_by: str | None = None
    source: str


class CashEventPatch(BaseModel):
    category: str | None = None
    flags: dict | None = None


class PeriodRead(ORM):
    id: UUID
    period: date
    revenue: float | None = None
    net_income: float | None = None
    ebitda_reported: float | None = None
    ebitda_adjusted: float | None = None
    ebitda_bankable: float | None = None
    debt_service: float | None = None
    deposits: float | None = None
    withdrawals: float | None = None
    ending_balance: float | None = None
    low_balance: float | None = None
    avg_daily_balance: float | None = None
    nsf_count: int
    liquidity: dict | None = None
    source: str
    reconciled: bool


class PeriodUpsert(BaseModel):
    revenue: float | None = None
    net_income: float | None = None
    ebitda_reported: float | None = None
    ebitda_adjusted: float | None = None
    ebitda_bankable: float | None = None
    debt_service: float | None = None
    deposits: float | None = None
    withdrawals: float | None = None
    ending_balance: float | None = None
    low_balance: float | None = None
    avg_daily_balance: float | None = None
    nsf_count: int | None = None
    reconciled: bool | None = None


# --- Stream 3: engines, lineage & alerts -----------------------------------


class SnapshotRead(ORM):
    id: UUID
    as_of: date
    metrics: dict
    score: float | None = None
    tier: str | None = None


class AlertRead(ORM):
    id: UUID
    kind: str
    severity: str
    message: str
    ref_kind: str | None = None
    ref_id: UUID | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class HealthRead(BaseModel):
    snapshot: SnapshotRead | None = None
    targets: list[TargetRead] = []
    alerts: list[AlertRead] = []
    lineage_count: int = 0


# --- Stream 4: plan, forecast & funding paths --------------------------------


class PlanActionRead(ORM):
    id: UUID
    sort: int
    title: str
    detail: str | None = None
    category: str
    owner: str | None = None
    timeline: str | None = None
    due_on: date | None = None
    status: str
    expected_effect: str | None = None
    published: bool
    created_at: datetime
    updated_at: datetime


class PlanActionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str | None = None
    sort: int = 0
    category: str = Field(default="liquidity", max_length=24)
    owner: str | None = Field(default=None, max_length=80)
    timeline: str | None = Field(default=None, max_length=80)
    due_on: date | None = None
    status: str = Field(default="todo", pattern="^(todo|prog|done)$")
    expected_effect: str | None = Field(default=None, max_length=120)


class PlanActionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    detail: str | None = None
    sort: int | None = None
    category: str | None = Field(default=None, max_length=24)
    owner: str | None = Field(default=None, max_length=80)
    timeline: str | None = Field(default=None, max_length=80)
    due_on: date | None = None
    status: str | None = Field(default=None, pattern="^(todo|prog|done)$")
    expected_effect: str | None = Field(default=None, max_length=120)


class ForecastRead(BaseModel):
    months: list[str]
    baseline: dict[str, list[float | None]]
    adjusted: dict[str, list[float | None]]
    fundable_month: str | None = None
    uplift_pct: float | None = None
    assumptions: list[str] = []


class PathRequirement(BaseModel):
    label: str
    met: bool
    detail: str


class FundingPath(BaseModel):
    key: str
    label: str
    readiness_pct: float
    requirements: list[PathRequirement]


class LadderTier(BaseModel):
    name: str
    requirements: list[PathRequirement]
    met: bool
    status: str  # current|next|done|future


class LadderRead(BaseModel):
    current_tier: str
    tiers: list[LadderTier]


class PathsRead(BaseModel):
    paths: list[FundingPath]
    ladder: LadderRead


# --- Stream 5: messaging, sessions & lender package --------------------------


class MessageRead(ORM):
    id: UUID
    author_user_id: UUID | None = None
    author_name: str | None = None
    body: str
    internal: bool
    created_at: datetime


class MessageCreate(BaseModel):
    body: str = Field(min_length=1)
    internal: bool = False


class SessionRead(ORM):
    id: UUID
    title: str
    kind: str
    starts_at: datetime
    join_url: str | None = None
    notes: str | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="call", pattern="^(training|call|review)$")
    starts_at: datetime
    join_url: str | None = Field(default=None, max_length=500)
    notes: str | None = None


class GlobalAlertRead(AlertRead):
    dealer_id: UUID
    dealer_name: str


class AddbackRead(ORM):
    id: UUID
    title: str
    monthly_amount: float | None = None
    annual_amount: float | None = None
    status: str
    evidence: str | None = None


class LenderPackageRead(BaseModel):
    dealer: DealerRead
    snapshot: SnapshotRead | None = None
    targets: list[TargetRead] = []
    periods: list[PeriodRead] = []
    addbacks: list[AddbackRead] = []
    plan: list[PlanActionRead] = []
    forecast: ForecastRead | None = None
    paths: PathsRead | None = None


# --- Stream 7: document ingestion --------------------------------------------


class DocumentRead(ORM):
    id: UUID
    dealer_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    s3_key: str | None = None
    kind: str
    status: str
    error: str | None = None
    extracted: dict | None = None
    bucket_file_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


# --- Phase 2: bucket link, credit & IRS, AI analyst --------------------------


class BucketFileItem(BaseModel):
    """One file in the dealer's linked bucket, with ingest affordances."""

    id: UUID
    file_name: str
    content_type: str
    size_bytes: int
    created_at: datetime
    has_analysis: bool = False       # cached BucketFileAnalysis usable (no model call needed)
    already_ingested: bool = False   # a DealerDocument already references this bucket file


class CreditHistoryItem(BaseModel):
    """Free-form trade/merchant-processor line — extras are preserved."""

    model_config = ConfigDict(extra="allow")

    label: str = Field(min_length=1, max_length=200)
    months_on_time: int | None = None
    note: str | None = None


class CreditRead(BaseModel):
    business_history: list[dict] = []
    personal_score: int | None = None
    personal_tier: str | None = None
    updated_at: datetime | None = None


class CreditUpsert(BaseModel):
    business_history: list[CreditHistoryItem] | None = None
    personal_score: int | None = Field(default=None, ge=300, le=850)
    personal_tier: str | None = Field(default=None, pattern="^(tier1|tier2)$")


class TaxYearRead(BaseModel):
    year: int
    filed: bool = False
    revenue_reported: float | None = None
    deposits_observed: float | None = None   # sum of period deposits for the calendar year
    discrepancy_pct: float | None = None     # (observed - reported) / reported * 100
    filing_id: UUID | None = None            # None when the year has deposits but no filing row


class TaxFilingUpsert(BaseModel):
    filed: bool | None = None
    revenue_reported: float | None = None


class AISuggestedAction(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(default="liquidity", pattern="^(dscr|ebitda|liquidity|docs)$")
    owner: str | None = Field(default=None, max_length=80)
    timeline: str | None = Field(default=None, max_length=80)
    expected_effect: str | None = Field(default=None, max_length=120)
    rationale: str | None = None


class AIInsightsRead(BaseModel):
    narrative: str
    strengths: list[str] = []
    risks: list[str] = []
    suggested_actions: list[AISuggestedAction] = []


class AIInsightsAccept(BaseModel):
    actions: list[AISuggestedAction] = Field(min_length=1, max_length=20)


class DealerInvite(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str | None = None


class DealerInviteResult(BaseModel):
    status: str  # invited | linked
    email: str
    user_id: UUID
    clerk_sent: bool = False


class BucketSearchItem(ORM):
    id: UUID
    name: str
    client_name: str | None = None
    created_at: datetime
