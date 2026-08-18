"""Pydantic schemas for the Dealer OS API."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# 0119: what the client is raising money FOR — drives program sizing and the
# reverse-engineered metric targets.
_FUNDING_PURPOSES = "^(working_capital|equipment|real_estate|refinance|floorplan|other)$"


class DealerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    industry: str = "auto_dealer"
    notes: str | None = None
    funding_goal: float | None = Field(default=None, gt=0, le=999_999_999_999.99)
    funding_purpose: str | None = Field(default=None, pattern=_FUNDING_PURPOSES)
    group_id: UUID | None = None  # 0120: client file this LLC belongs to


class DealerUpdate(BaseModel):
    name: str | None = None
    bucket_id: UUID | None = None  # manual bucket link/unlink (PATCH with null unlinks)
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    industry: str | None = None
    status: str | None = None
    notes: str | None = None
    # Team links (or unlinks) a dealer self-serve login to this business.
    dealer_user_id: UUID | None = None
    started_on: date | None = None
    entity_type: str | None = None
    naics_code: str | None = None
    naics_label: str | None = None
    funding_goal: float | None = Field(default=None, gt=0, le=999_999_999_999.99)
    funding_purpose: str | None = Field(default=None, pattern=_FUNDING_PURPOSES)
    group_id: UUID | None = None  # 0120: client file link (PATCH null detaches)


class DealerRead(ORM):
    id: UUID
    name: str
    dealer_user_id: UUID | None = None
    bucket_name: str | None = None
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    industry: str
    status: str
    notes: str | None = None
    bucket_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    started_on: date | None = None
    entity_type: str | None = None
    naics_code: str | None = None
    naics_label: str | None = None
    funding_goal: float | None = None
    funding_purpose: str | None = None
    group_id: UUID | None = None


class DealerListItem(ORM):
    id: UUID
    name: str
    city: str | None = None
    state: str | None = None
    status: str
    created_at: datetime
    # 0120: client-file grouping (group_name filled by the router's outerjoin)
    group_id: UUID | None = None
    group_name: str | None = None
    # rollups filled by the router (no snapshot may exist yet)
    score: float | None = None
    tier: str | None = None
    open_alerts: int = 0
    # Phase 3 Wave 2 attention rollups (batched in the router, never N+1)
    missing_statement: bool = False   # no period row at all for the last calendar month
    overdue_actions: int = 0          # open plan actions past their due_on
    fundable_paths: int = 0           # unresolved fundability_* alerts
    attention_score: int = 0          # deterministic weighted sort key (services.rollups)


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
    account_id: UUID | None = None


class CashEventPatch(BaseModel):
    category: str | None = None
    flags: dict | None = None


class CashEventSearchRow(CashEventRead):
    """CashEventRead + source-document provenance (0119) for the explorer."""

    document_id: UUID | None = None
    document_filename: str | None = None


class CashEventSearchRead(BaseModel):
    total: int = 0
    offset: int = 0
    limit: int = 75
    rows: list[CashEventSearchRow] = []


class PeriodRead(ORM):
    id: UUID
    period: date
    account_id: UUID | None = None
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
    client_response: str | None = None  # accepted|declined (0123)
    client_response_at: datetime | None = None
    comments_count: int = 0
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
    # 0119: additive program sizing (PROVISIONAL — pending lending-desk
    # sign-off). Amounts are null when the path can't be sized from the data
    # on file; sizing_basis says which model produced the numbers.
    funding_min: float | None = None
    funding_typical: float | None = None
    funding_max: float | None = None
    sizing_basis: str = "insufficient data"
    sizing_constraints: list[str] = []


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
    source_event_id: UUID | None = None
    document_id: UUID | None = None


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
    account_id: UUID | None = None
    # Doc hub (0114): ZIP expansion parent link + AI classification outputs.
    parent_document_id: UUID | None = None
    detected_kind: str | None = None
    doc_meta: dict | None = None
    created_at: datetime
    updated_at: datetime


class DocumentCoverageRead(BaseModel):
    """Intake completeness rollup for the Documents tab — what the team still
    needs to collect vs. what the extracted documents already cover."""

    statement_months: list[str] = []   # distinct "YYYY-MM" covered by statements/periods
    statement_target: int = 6
    tax_years: list[int] = []          # years with a dos_tax_filings row
    tax_target: int = 2
    has_pl: bool = False
    has_debt_schedule: bool = False
    open_doc_requests: int = 0
    # Freshness (deterministic date math vs. today — services.recurrence):
    current_through: str | None = None  # latest covered "YYYY-MM", null = no coverage
    expected_months: list[str] = []     # the 6 most recent COMPLETED months
    missing_months: list[str] = []      # expected minus covered, sorted
    is_current: bool = False            # the most recent completed month is covered
    days_since_latest: int | None = None  # days from END of latest covered month to today


class PipelineStatusRead(BaseModel):
    """Live ingestion state for the cockpit header.

    Covers the work the browser CANNOT see: background bucket auto-ingest, and
    documents another team member is putting through right now. A client's own
    in-flight uploads are not visible here — a team upload holds one
    transaction until extraction finishes, so its 'extracting' status is never
    externally readable — which is why the header merges this with its local
    upload queue rather than relying on it alone."""

    # Committed document states.
    extracted: int = 0
    failed: int = 0
    pending_review: int = 0     # dealer self-uploads awaiting team approval
    in_flight: int = 0          # rows sitting at uploaded/extracting
    # Linked-bucket files with no DealerDocument yet — queued work.
    bucket_pending: int = 0
    # True when anything is moving: the header shows its live state on this.
    active: bool = False
    # Most recent document completion, so the header can show "just now".
    last_completed_at: datetime | None = None
    last_completed_name: str | None = None
    # What the ingest produced, for the "mapped" half of the readout.
    months_covered: int = 0
    tax_years_covered: int = 0
    accounts: int = 0


class RecurringGroupRead(BaseModel):
    """One detected recurring payment/deposit group (deterministic engine)."""

    key: str
    sample_description: str
    cadence: str                 # weekly|biweekly|monthly|quarterly
    occurrences: int
    avg_amount: float
    amount_stable: bool
    first_seen: date
    last_seen: date
    next_expected_on: date
    overdue: bool                # next_expected_on < today
    monthly_equivalent: float    # avg_amount normalized to monthly by cadence
    direction: str               # inflow|outflow


class IrregularEventRead(BaseModel):
    """A large one-off outflow outside every recurring group."""

    event_id: UUID
    occurred_on: date
    description: str
    amount: float
    category: str


class RecurringRead(BaseModel):
    groups: list[RecurringGroupRead] = []
    irregular: list[IrregularEventRead] = []
    computed_at: date


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
    # 0119: the tax-return document this filing was read from (provenance).
    document_id: UUID | None = None
    document_filename: str | None = None


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


# --- Phase 3 Wave 1: accounts, audit, rules, lineage, add-back evidence ------

_ACCOUNT_ROLES = "^(primary_operating|secondary|payroll|savings|floorplan_reserve|other)$"


class AccountRead(ORM):
    id: UUID
    name: str
    institution: str | None = None
    mask: str | None = None
    role: str
    ai_proposed_role: str | None = None
    ai_rationale: str | None = None
    role_set_by: str
    status: str
    created_at: datetime
    updated_at: datetime


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    institution: str | None = Field(default=None, max_length=160)
    role: str | None = Field(default=None, pattern=_ACCOUNT_ROLES)
    status: str | None = Field(default=None, pattern="^(active|closed)$")


class RuleRead(ORM):
    id: UUID
    dealer_id: UUID | None = None  # None = global rule
    pattern: str
    category: str
    active: bool
    created_at: datetime


class RuleCreate(BaseModel):
    pattern: str = Field(min_length=2, max_length=160)
    category: str = Field(min_length=1, max_length=48)
    apply_retroactive: bool = False


class RuleCreateResult(BaseModel):
    rule: RuleRead
    retro_applied: int = 0


class AuditRead(ORM):
    id: UUID
    actor_user_id: UUID | None = None
    actor_name: str
    action: str
    entity_kind: str
    entity_id: UUID | None = None
    before: dict | None = None
    after: dict | None = None
    created_at: datetime


class LineageEdgeRead(BaseModel):
    metric_key: str
    ref_kind: str
    ref_id: UUID | None = None
    period: date | None = None
    # Resolved context for cash_event refs
    description: str | None = None
    amount: float | None = None


class LineageRead(BaseModel):
    snapshot_id: UUID | None = None
    as_of: date | None = None
    edges: list[LineageEdgeRead] = []


class EventFeedsRead(BaseModel):
    event_id: UUID
    snapshot_id: UUID | None = None
    metric_keys: list[str] = []      # metrics referencing the event directly
    via_addbacks: list[str] = []     # metrics fed via an add-back sourced from it


class AddbackPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(verified|candidate|review|excluded)$")
    document_id: UUID | None = None


# --- Phase 3 Wave 2: handoff, doc requests, review, progress -----------------


class HandoffRead(BaseModel):
    intake_id: UUID
    url: str


_DOC_KINDS_PATTERN = "^(statement|pl|tax|debt_schedule|other)$"


class DocRequestRead(ORM):
    id: UUID
    title: str
    kind: str
    account_id: UUID | None = None
    due_on: date | None = None
    status: str
    fulfilled_document_id: UUID | None = None
    note: str | None = None
    created_at: datetime
    updated_at: datetime


class DocRequestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="statement", pattern=_DOC_KINDS_PATTERN)
    account_id: UUID | None = None
    due_on: date | None = None
    note: str | None = None


class DocRequestPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(open|fulfilled|cancelled)$")
    note: str | None = None
    due_on: date | None = None
    fulfilled_document_id: UUID | None = None


class DocumentReject(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class DocumentUrlRead(BaseModel):
    """Short-lived presigned URL for previewing/downloading archived bytes."""

    url: str
    expires_in: int = 900
    filename: str
    content_type: str


class MetricDelta(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_value: float | None = Field(default=None, alias="from")
    to_value: float | None = Field(default=None, alias="to")
    delta: float | None = None


class ProgressRead(BaseModel):
    from_date: date
    to_date: date
    score_from: float | None = None
    score_to: float | None = None
    deltas: dict[str, MetricDelta] = {}
    improved: list[str] = []
    slipped: list[str] = []
    actions_completed: list[str] = []


class VendorRowRead(BaseModel):
    """One counterparty's activity rollup — the per-vendor report."""

    key: str
    sample_description: str
    direction: int                 # +1 money in, -1 money out
    category: str
    category_source: str           # "rule" (admin-set) | "heuristic"
    rationale: str
    count: int
    months: int
    first_seen: date
    last_seen: date
    total: float
    median_amount: float
    monthly_average: float
    cadence: str
    is_recurring: bool
    amount_stable: bool
    debt_like: bool


class VendorReportRead(BaseModel):
    vendors: list[VendorRowRead] = []
    categories: list[str] = []
    recurring_count: int = 0
    one_off_count: int = 0
    events_analyzed: int = 0


class VendorCategoryPatch(BaseModel):
    """Admin correction of a vendor's category — persisted as a rule so it
    survives re-classification and applies to future events."""

    vendor_key: str
    category: str


class VendorAccountRead(BaseModel):
    """Per-account attribution of one vendor's activity (count desc)."""

    account_id: UUID | None = None    # None = legacy/unattributed events
    account_name: str | None = None
    count: int = 0
    total: float = 0.0


class VendorDetailRead(BaseModel):
    """Vendor drill-down (0119): the rollup row, its account attribution and
    the underlying ledger lines with document provenance."""

    vendor: VendorRowRead | None = None
    accounts: list[VendorAccountRead] = []
    events: list[CashEventSearchRow] = []


class DebtRead(BaseModel):
    id: UUID
    lender: str
    category: str
    monthly_payment: float | None = None
    balance: float | None = None
    rate: float | None = None
    term_months: int | None = None
    maturity_on: date | None = None
    origin: str
    status: str
    vendor_key: str | None = None
    evidence: dict | None = None
    notes: str | None = None

    model_config = ConfigDict(from_attributes=True)


class DebtCreate(BaseModel):
    lender: str = Field(min_length=1, max_length=180)
    category: str = "loan"
    monthly_payment: float | None = None
    balance: float | None = None
    rate: float | None = None
    term_months: int | None = None
    maturity_on: date | None = None
    notes: str | None = None


class DebtPatch(BaseModel):
    lender: str | None = Field(default=None, min_length=1, max_length=180)
    category: str | None = None
    monthly_payment: float | None = None
    balance: float | None = None
    rate: float | None = None
    term_months: int | None = None
    maturity_on: date | None = None
    status: str | None = None
    notes: str | None = None


class DebtDraftResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped_admin: int = 0     # rows a human owns — never touched
    total_monthly: float = 0.0
    debts: list[DebtRead] = []


class OwnerRead(ORM):
    id: UUID
    first_name: str
    last_name: str
    email: str | None = None
    phone: str | None = None
    ownership_pct: float | None = None
    is_guarantor: bool = True
    dob: date | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    # 0125: is_primary marks the login's own person (self-pull allowed once);
    # has_invite reflects an outstanding consent link — the token hash itself
    # is NEVER exposed (model property `has_invite` feeds it via ORM mode).
    is_primary: bool = False
    invite_sent_at: datetime | None = None
    invite_opened_at: datetime | None = None
    has_invite: bool = False
    credit_score: int | None = None
    credit_tier: str | None = None
    credit_pulled_at: datetime | None = None
    credit_summary: dict | None = None
    notes: str | None = None


class OwnerCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: str | None = None
    phone: str | None = None
    ownership_pct: float | None = None
    is_guarantor: bool = True
    dob: date | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    # 0125: at most ONE primary owner per dealer (the login's own person) —
    # create_owner 422s when true and another primary already exists.
    is_primary: bool = False
    notes: str | None = None


class OwnerPatch(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=80)
    email: str | None = None
    phone: str | None = None
    ownership_pct: float | None = None
    is_guarantor: bool | None = None
    dob: date | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    notes: str | None = None


class SoftPullRequest(BaseModel):
    """FCRA consent is a hard precondition: the gateway is never called
    without an explicit, recorded permissible-purpose acknowledgement."""

    fcra_consent: bool = False
    ssn: str | None = None  # optional; improves hit rate, never persisted here


class SoftPullResult(BaseModel):
    ok: bool
    owner: OwnerRead | None = None
    detail: str | None = None


# --- Owner credit-consent invites (0125) --------------------------------------
# Additional (non-primary) owners consent to their own pull through a one-time
# secure link the advisor shares with them directly — consent must come from
# the person the pull is ABOUT, never from the client on their behalf.


class CreditInviteResult(BaseModel):
    """Returned ONCE at mint time — only the sha256 of the token is stored."""

    token: str
    path: str  # "/credit-consent#t={token}" (fragment: token never hits server logs)


class PublicConsentView(BaseModel):
    """What an unauthenticated consent page may know: enough for the owner to
    recognize themself and the business, and which fields we still need.
    Never scores, summaries, or other owners."""

    first_name: str
    last_initial: str
    dealer_name: str
    fields_needed: list[str]  # subset of dob/street/city/state/zip still missing
    completed: bool


class PublicConsentSubmit(BaseModel):
    """FCRA consent is a hard precondition; profile fields fill ONLY currently
    empty owner columns (never overwrite what the advisor already has)."""

    fcra_consent: bool = False
    dob: date | None = None
    street: str | None = Field(default=None, max_length=240)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=8)
    zip: str | None = Field(default=None, max_length=12)


class PublicConsentResult(BaseModel):
    """Public echo after a consent-link pull: tier + a 50-point band only —
    the exact score never renders on an unauthenticated page."""

    credit_tier: str | None = None
    credit_score_band: str | None = None  # e.g. "700–749"
    completed: bool = True


class TradelineRead(BaseModel):
    """One observed credit relationship (0119) — a repeating outbound
    obligation in a credit-shaped category, with dominant-account attribution."""

    vendor_key: str
    sample_description: str
    category: str
    monthly_payment: float
    months: int
    first_seen: date
    last_seen: date
    on_time_pct: float | None = None
    account_id: UUID | None = None
    account_name: str | None = None


class BusinessCreditRead(BaseModel):
    """Business credit built from observed payment behaviour.

    A bureau file is not required to say something true about how this business
    pays: the ledger already shows every recurring obligation and whether it
    was paid on time and in full."""

    tradelines: int = 0
    on_time_pct: float | None = None
    months_observed: int = 0
    total_monthly_obligations: float = 0.0
    nsf_6mo: int = 0
    oldest_tradeline_months: int | None = None
    grade: str | None = None          # A..D, deterministic from the fields above
    factors: list[str] = []
    # 0119: the individual tradelines behind the scalar summary. INVARIANT:
    # len(tradeline_rows) == tradelines (same select_tradelines predicate).
    tradeline_rows: list[TradelineRead] = []


# --- Funding goal + reverse engineering (0119) --------------------------------


class FundingRangeRead(BaseModel):
    min: float | None = None
    typical: float | None = None
    max: float | None = None


class RequirementRead(BaseModel):
    """One reverse-engineered requirement: what a metric must reach for the
    dealer's funding goal to be fundable on a path."""

    metric_key: str
    label: str
    required_value: float | None = None
    current_value: float | None = None
    gap: float | None = None
    met: bool = False


class PathFundingRead(BaseModel):
    path_key: str
    fundable_now: FundingRangeRead | None = None   # None when the path can't be sized yet
    goal_feasible: bool | None = None              # None when no goal is set (or nothing to invert)
    requirements: list[RequirementRead] = []


class FundingPlanRead(BaseModel):
    goal: float | None = None
    purpose: str | None = None
    paths: list[PathFundingRead] = []


# --- Desk admin (0120): program settings, groups, payment timing & shifts -----


class ProgramSettingRead(BaseModel):
    """One program row: the code defaults side by side with the desk override
    (override is None when the desk has not touched the program)."""

    path_key: str
    label: str
    model: str  # dscr|deposit|collateral
    sizing_default: dict | None = None
    sizing_override: dict | None = None
    requirements_default: list[dict] = []
    requirements_override: list[dict] | None = None
    approved_at: datetime | None = None
    updated_by_name: str | None = None


class ProgramSettingsRead(BaseModel):
    programs: list[ProgramSettingRead] = []


class ProgramSettingUpdate(BaseModel):
    """PUT body: only the fields present are replaced (each WHOLESALE); an
    explicit null clears that override back to the code default. Shape
    validation happens in services.paths (422 on violation)."""

    sizing: dict | None = None
    requirements: list[dict] | None = None


class GroupRead(BaseModel):
    id: UUID
    name: str
    member_count: int = 0


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class GroupPatch(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class TimingDayRead(BaseModel):
    day: int
    out_total: float = 0.0
    out_avg_month: float = 0.0
    in_total: float = 0.0
    in_avg_month: float = 0.0
    count: int = 0


class TimingBigDepositDayRead(BaseModel):
    day: int
    in_avg_month: float


class TimingBigDayRead(BaseModel):
    day: int
    out_avg_month: float
    top_vendors: list[str] = []


class TimingRecurringRead(BaseModel):
    """A recurring vendor and when in the month its money moves: outflows
    (direction 'out' — payment-shift candidates) and, 0121, recurring
    deposit inflows (direction 'in' — receivables-acceleration candidates)."""

    vendor_key: str
    label: str
    direction: str = "out"  # "out" | "in"
    category: str | None = None
    cadence: str | None = None
    typical_day: int
    day_spread: list[float] = []  # [p25, p75] day-of-month
    monthly_amount: float
    account_id: UUID | None = None


class TimingCutoffRead(BaseModel):
    """Estimated statement cutoff of one account: the median last-activity
    day of the calendar month across observed months. account_id None = the
    unattributed-events bucket (legacy rows carrying no account)."""

    account_id: UUID | None = None
    account_name: str | None = None
    cutoff_day: int
    basis: str  # "calendar month-end" | "mid-cycle ~day N"
    months_observed: int


class PaymentTimingRead(BaseModel):
    days: list[TimingDayRead] = []
    big_days: list[TimingBigDayRead] = []
    big_deposit_days: list[TimingBigDepositDayRead] = []
    deposits_monthly_total: float = 0.0
    recurring: list[TimingRecurringRead] = []
    cutoffs: list[TimingCutoffRead] = []
    window_months: int
    computed_at: datetime


class PaymentShiftRead(ORM):
    id: UUID
    vendor_key: str | None = None
    direction: str = "out"  # "out" pay later | "in" collect earlier (0121)
    label: str
    from_day: int
    to_day: int
    monthly_amount: float | None = None
    est_adb_impact: float | None = None
    plan_action_id: UUID | None = None  # the Plan action this proposal created
    rationale: str | None = None
    status: str
    created_at: datetime


class PaymentShiftCreate(BaseModel):
    vendor_key: str | None = Field(default=None, max_length=120)
    # 'in' = collect a recurring deposit earlier (real receivables change) —
    # deposits only ever move EARLIER, so 'in' requires to_day < from_day.
    direction: str = Field(default="out", pattern="^(out|in)$")
    label: str = Field(min_length=1, max_length=200)
    from_day: int = Field(ge=1, le=31)
    to_day: int = Field(ge=1, le=31)
    monthly_amount: float | None = Field(default=None, gt=0)
    rationale: str | None = None

    @model_validator(mode="after")
    def _in_moves_earlier(self) -> "PaymentShiftCreate":
        if self.direction == "in" and self.to_day >= self.from_day:
            raise ValueError(
                "direction 'in' moves a deposit EARLIER: to_day must be before from_day"
            )
        return self


class PaymentShiftPatch(BaseModel):
    from_day: int | None = Field(default=None, ge=1, le=31)
    to_day: int | None = Field(default=None, ge=1, le=31)
    rationale: str | None = None
    status: str | None = Field(default=None, pattern="^(draft|proposed|done|dismissed)$")


# --- Timing optimizer (0121) ---------------------------------------------------


class TimingOptimizeShiftRead(BaseModel):
    """One drafted move: pay an early-month vendor later under its terms, or
    collect a recurring deposit earlier (a real receivables change)."""

    vendor_key: str | None = None
    label: str
    direction: str  # "out" | "in"
    from_day: int
    to_day: int
    monthly_amount: float
    est_adb_impact: float
    rationale: str


class TimingOptimizeRead(BaseModel):
    """GET /dealers/{id}/timing/optimize — a read-only draft, never stored;
    each row is a candidate the team can stage as a dos_payment_shift."""

    shifts: list[TimingOptimizeShiftRead] = []
    total_est_adb: float = 0.0
    computed_at: datetime


# --- What-if simulator (0121) --------------------------------------------------

# A delta past a billion dollars is a typo, not a scenario — 422, not a
# silently absurd metric tree.
_SIM_DELTA_LIMIT = 1_000_000_000


class SimulateShift(BaseModel):
    """An ad-hoc (staged, not yet saved) payment-date shift for live
    preview — e.g. a chip dragged on the Timing calendar before the team
    commits it as a dos_payment_shift row."""

    # Same rule as PaymentShiftCreate: an 'in' shift accelerates a deposit,
    # so it must move EARLIER (to_day < from_day) — 422 otherwise.
    direction: str = Field(default="out", pattern="^(out|in)$")
    from_day: int = Field(ge=1, le=31)
    to_day: int = Field(ge=1, le=31)
    monthly_amount: float = Field(gt=0, le=1e9)

    @model_validator(mode="after")
    def _in_moves_earlier(self) -> "SimulateShift":
        if self.direction == "in" and self.to_day >= self.from_day:
            raise ValueError(
                "direction 'in' moves a deposit EARLIER: to_day must be before from_day"
            )
        return self


class SimulateRequest(BaseModel):
    """Levers of POST /dealers/{id}/simulate. Every field is optional; an
    empty body is a no-op scenario that reproduces the baseline exactly."""

    adb_delta: float = Field(default=0.0, ge=-_SIM_DELTA_LIMIT, le=_SIM_DELTA_LIMIT)
    debt_service_monthly_delta: float = Field(
        default=0.0, ge=-_SIM_DELTA_LIMIT, le=_SIM_DELTA_LIMIT
    )
    deposits_monthly_delta: float = Field(
        default=0.0, ge=-_SIM_DELTA_LIMIT, le=_SIM_DELTA_LIMIT
    )
    ebitda_annual_delta: float = Field(default=0.0, ge=-_SIM_DELTA_LIMIT, le=_SIM_DELTA_LIMIT)
    nsf_zero: bool = False
    # Staged shifts simulated alongside (independently of) the stored
    # proposed rows — capped to keep the preview bounded.
    shifts: list[SimulateShift] = Field(default_factory=list, max_length=20)
    verify_all_addbacks: bool = False
    apply_proposed_shifts: bool = False
    statement_months: int | None = Field(default=None, ge=0, le=36)


class SimulateApplied(BaseModel):
    """Resolved numbers the engine actually used: the two flag-driven pools
    reported on their own AND folded into their delta channel (adb_delta
    includes shifts_adb_added; ebitda_annual_delta includes
    addbacks_annual_added)."""

    adb_delta: float = 0.0
    debt_service_monthly_delta: float = 0.0
    deposits_monthly_delta: float = 0.0
    ebitda_annual_delta: float = 0.0
    nsf_zero: bool = False
    addbacks_annual_added: float = 0.0
    shifts_adb_added: float = 0.0
    statement_months: int = 0  # months the scenario requirements graded against


class SimulateMetrics(BaseModel):
    """The flat scalar block reported for baseline and scenario alike."""

    score: float | None = None
    dscr: float | None = None
    ebitda_bankable: float | None = None
    adb: float | None = None
    liquidity: float | None = None
    nsf_6mo: int = 0


class SimulatePathRead(BaseModel):
    path_key: str
    label: str
    readiness_before: float
    readiness_after: float
    funding_typical_before: float | None = None
    funding_typical_after: float | None = None
    goal_feasible_before: bool | None = None  # None: no goal, or nothing to invert
    goal_feasible_after: bool | None = None


class SimulateCurvePoint(BaseModel):
    day: int
    baseline: float
    scenario: float


class SimulateCurveRead(BaseModel):
    """Intra-month daily-balance profile, baseline vs scenario — each side
    anchored to its engine ADB so the picture and the number always agree."""

    days: list[SimulateCurvePoint] = []
    adb_baseline: float
    adb_scenario: float
    adb_target: float | None = None
    cutoff_days: list[int] = []


class SimulateRead(BaseModel):
    applied: SimulateApplied
    baseline: SimulateMetrics
    scenario: SimulateMetrics
    paths: list[SimulatePathRead] = []
    goal: float | None = None
    daily_curve: SimulateCurveRead | None = None


class PlanRespond(BaseModel):
    """Client's answer to a published plan action."""

    response: str = Field(pattern="^(accepted|declined)$")
    comment: str | None = Field(default=None, max_length=4000)


class PlanCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class PlanCommentRead(ORM):
    id: UUID
    action_id: UUID
    author_role: str
    author_name: str | None = None
    body: str
    created_at: datetime


class RecurrenceMark(BaseModel):
    """Admin recurrence override for a cash event (optionally its vendor)."""

    mark: str = Field(pattern="^(recurring|one_time|clear)$")
    apply_similar: bool = True
