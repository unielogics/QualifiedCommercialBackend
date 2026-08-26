"""Pydantic schemas for the Dealer OS API."""

from __future__ import annotations

from typing import Literal

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# 0119: what the client is raising money FOR — drives program sizing and the
# reverse-engineered metric targets.
_FUNDING_PURPOSES = "^(working_capital|equipment|real_estate|refinance|floorplan|other)$"


class UseOfProceedsRow(BaseModel):
    """One line of the breakdown. Label and amount, nothing else: the moment
    this grows a category enum it stops being what the owner said and starts
    being what our dropdown allowed."""

    label: str = Field(min_length=1, max_length=160)
    amount: float = Field(ge=0, le=999_999_999_999.99)


class DealerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    entity_type: str = Field(min_length=1, max_length=32)
    legal_name: str | None = None
    ein: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    industry: str = "auto_dealer"
    industry_label: str | None = None
    subindustry: str | None = None
    subindustry_label: str | None = None
    industry_entry_id: UUID | None = None
    subindustry_entry_id: UUID | None = None
    activity_entry_id: UUID | None = None
    naics_code: str | None = Field(default=None, min_length=6, max_length=6)
    naics_label: str | None = Field(default=None, min_length=1, max_length=180)
    notes: str | None = None
    funding_goal: float = Field(gt=0, le=999_999_999_999.99)
    funding_purpose: str = Field(pattern=_FUNDING_PURPOSES)
    use_of_proceeds_note: str = Field(min_length=1, max_length=4000)
    group_id: UUID | None = None  # 0120: client file this LLC belongs to
    # Consent captured in the same moment as the file. Optional, because a rep
    # may have only an email, or the owner may decline: a file must still open.
    sms_consent: "SmsConsentIn | None" = None


class SmsConsentIn(BaseModel):
    """What the form reports back about the boxes that were ticked.

    Note what is NOT here: the disclosure text. The server writes the wording
    from its own copy, so a client cannot submit a record claiming the person
    agreed to something other than what the server can show them.
    """

    phone: str = Field(min_length=7, max_length=32)
    transactional: bool = False
    marketing: bool = False
    accepted_legal: bool = False
    method: Literal["self_web", "in_person_device", "rep_attested"] = "in_person_device"
    consenter_name: str | None = Field(default=None, max_length=160)


# DealerCreate refers to SmsConsentIn before it exists. `from __future__ import
# annotations` turns that into a lazy string, which Pydantic only resolves when
# the model is first used — and then raises "not fully defined" at request
# time rather than at import. Resolve it here, where the failure is loud.
DealerCreate.model_rebuild()


class SmsConsentOut(BaseModel):
    id: UUID
    phone_e164: str
    consent_kind: str
    granted: bool
    method: str
    disclosure_version: str
    consenter_name: str | None = None
    captured_by_name: str | None = None
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SmsDisclosureOut(BaseModel):
    """The exact wording the form must render. Fetched, never hardcoded, so the
    text on screen and the text in the proof are the same string."""

    version: str
    brand: str
    transactional: str
    marketing: str
    legal: str
    terms_url: str
    privacy_url: str
    support_email: str


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
    industry_label: str | None = None
    subindustry: str | None = None
    subindustry_label: str | None = None
    industry_entry_id: UUID | None = None
    subindustry_entry_id: UUID | None = None
    activity_entry_id: UUID | None = None
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
    use_of_proceeds: list[UseOfProceedsRow] | None = None
    use_of_proceeds_note: str | None = Field(default=None, max_length=4000)


class DealerRead(ORM):
    id: UUID
    name: str
    email: str | None = None
    phone: str | None = None
    case_ref: str | None = None
    audit_client_since: datetime | None = None
    owner_user_id: UUID | None = None
    submitting_agent_name: str | None = None
    submitting_agent_email: str | None = None
    handoff_intake_id: UUID | None = None
    use_of_proceeds: list[UseOfProceedsRow] | None = None
    use_of_proceeds_note: str | None = None
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
    industry_label: str | None = None
    subindustry: str | None = None
    subindustry_label: str | None = None
    industry_entry_id: UUID | None = None
    subindustry_entry_id: UUID | None = None
    activity_entry_id: UUID | None = None
    status: str
    notes: str | None = None
    bucket_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    archived_by_user_id: UUID | None = None
    started_on: date | None = None
    entity_type: str | None = None
    naics_code: str | None = None
    naics_label: str | None = None
    funding_goal: float | None = None
    funded_amount: float | None = None
    client_requested_amount: float | None = None
    application_lifecycle: str = "active"
    funding_purpose: str | None = None
    group_id: UUID | None = None


class ConvertToAuditRequest(BaseModel):
    """Upgrade options. The invite defaults ON in the admin UI because an
    audit client without a login is a subscription nobody can use — but it
    stays a choice, for the desk that converts first and onboards by call."""

    send_login_invite: bool = False
    # Overrides the file's email when the login should go somewhere specific.
    login_email: str | None = Field(default=None, max_length=320)


class ConvertToAuditResult(BaseModel):
    dealer: DealerRead
    invite: DealerInviteResult | None = None
    invite_error: str | None = None


class DealerListItem(ORM):
    id: UUID
    name: str
    email: str | None = None
    phone: str | None = None
    case_ref: str | None = None
    audit_client_since: datetime | None = None
    city: str | None = None
    state: str | None = None
    address: str | None = None
    zip: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    archived_by_user_id: UUID | None = None
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
    # Verification, for the portfolio's Bank and Credit chips and its filter.
    # Batched in the list handler, never a per-row query.
    bank_linked: bool = False
    credit_returned: bool = False
    verified: bool = False
    funding_goal: float | None = None
    funding_purpose: str | None = None
    client_requested_amount: float | None = None
    application_lifecycle: str = "active"


class PortfolioOwnerSummary(BaseModel):
    id: UUID
    name: str
    email: str | None = None
    ownership_pct: float | None = None


class DealerPortfolioItem(DealerListItem):
    address: str | None = None
    owners: list[PortfolioOwnerSummary] = Field(default_factory=list)


class DealerPortfolioPage(BaseModel):
    items: list[DealerPortfolioItem] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class FieldDeskGlobalSearchItem(BaseModel):
    id: UUID
    kind: Literal["file", "contact", "email", "sms", "booking"]
    title: str
    subtitle: str | None = None
    context: str | None = None
    href: str
    dealer_id: UUID | None = None
    occurred_at: datetime | None = None


class FieldDeskGlobalSearchRead(BaseModel):
    query: str
    items: list[FieldDeskGlobalSearchItem] = Field(default_factory=list)


class IntegrationProviderStatus(BaseModel):
    configured: bool
    environment: str
    endpoint: str | None = None
    detail: str


class DealerIntegrationStatus(BaseModel):
    isoftpull: IntegrationProviderStatus
    plaid: IntegrationProviderStatus
    sms: IntegrationProviderStatus
    address: IntegrationProviderStatus


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
    channel: str = "client"
    edited_at: datetime | None = None
    created_at: datetime


class MessageCreate(BaseModel):
    body: str = Field(min_length=1)
    # Tri-state on purpose. None means "the caller did not say", which lets the
    # server pick a safe default per role: internal for a rep, client-visible
    # for the team's default. An explicit true/false always wins.
    internal: bool | None = None
    # 0132: which conversation this belongs to. Wins over `internal` when both
    # are given. Left unset, the server picks by role, which is what every
    # existing caller relies on.
    channel: Literal["desk", "client", "note"] | None = None


class MessageEdit(BaseModel):
    body: str = Field(min_length=1)


class AIThreadMessage(ORM):
    id: UUID
    role: str
    body: str
    created_at: datetime


class AIThreadAsk(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


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


class ApplicationProfileRead(ORM):
    id: UUID
    dealer_id: UUID
    dba_name: str | None = None
    website: str | None = None
    state_of_formation: str | None = None
    location_type: str | None = None
    mailing_address: str | None = None
    mailing_city: str | None = None
    mailing_state: str | None = None
    mailing_zip: str | None = None
    annual_sales: float | None = None
    annual_cash_flow_available_for_debt: float | None = None
    monthly_debt_payments: float | None = None
    signer_title: str | None = None
    human_review_status: Literal["pending", "fundable", "not_fundable"] = "pending"
    human_review_note: str | None = None
    human_reviewed_at: datetime | None = None
    human_reviewed_by_user_id: UUID | None = None
    landlord_mortgagee: str | None = None
    guarantor_home_address: str | None = None
    guarantor_dob: date | None = None
    selected_program: str | None = None
    term_requested_months: int | None = None
    collateral_description: str | None = None
    use_of_proceeds_text: str | None = None
    updated_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class ApplicationProfilePatch(BaseModel):
    dba_name: str | None = Field(default=None, max_length=180)
    website: str | None = Field(default=None, max_length=500)
    state_of_formation: str | None = Field(default=None, max_length=2)
    location_type: str | None = Field(default=None, max_length=32)
    mailing_address: str | None = Field(default=None, max_length=300)
    mailing_city: str | None = Field(default=None, max_length=120)
    mailing_state: str | None = Field(default=None, max_length=2)
    mailing_zip: str | None = Field(default=None, max_length=12)
    annual_sales: float | None = Field(default=None, ge=0)
    annual_cash_flow_available_for_debt: float | None = Field(default=None, ge=0)
    monthly_debt_payments: float | None = Field(default=None, ge=0)
    signer_title: str | None = Field(default=None, max_length=120)
    landlord_mortgagee: str | None = Field(default=None, max_length=200)
    guarantor_home_address: str | None = Field(default=None, max_length=300)
    guarantor_dob: date | None = None
    selected_program: str | None = Field(default=None, max_length=80)
    term_requested_months: int | None = Field(default=None, ge=1, le=360)
    collateral_description: str | None = Field(default=None, max_length=4000)
    use_of_proceeds_text: str | None = Field(default=None, max_length=4000)


class ApplicationHumanReviewPatch(BaseModel):
    status: Literal["pending", "fundable", "not_fundable"]
    note: str | None = Field(default=None, max_length=4000)


class ApplicationFinalizationPatch(BaseModel):
    status: Literal[
        "active", "decision_ready", "forms_out", "signed", "complete", "declined"
    ] | None = None
    funded_amount: float | None = Field(default=None, gt=0, le=999_999_999_999.99)

    @model_validator(mode="after")
    def _requires_a_change(self) -> "ApplicationFinalizationPatch":
        if self.status is None and self.funded_amount is None:
            raise ValueError("Provide a status or funded amount.")
        return self


class SubmissionReadinessItem(BaseModel):
    requirement: str
    status: Literal["complete", "missing", "supplemental", "not_applicable"]
    evidence: str
    route: str = "all"
    source: str | None = None


class SubmissionReadinessRead(BaseModel):
    ready: bool = False
    package_ready: bool = False
    route_key: str | None = None
    route_label: str | None = None
    human_review_status: Literal["pending", "fundable", "not_fundable"] = "pending"
    human_review_note: str | None = None
    human_reviewed_at: datetime | None = None
    human_reviewed_by_user_id: UUID | None = None
    rules_version: str | None = None
    items: list[SubmissionReadinessItem] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class BookingAvailabilitySlot(BaseModel):
    starts_at: datetime
    label: str
    date_label: str


class BookingAvailabilityRead(BaseModel):
    timezone: str
    duration_min: int
    buffer_before_min: int = 0
    buffer_after_min: int = 0
    host_name: str | None = None
    calendar_sync_status: Literal["connected", "disconnected", "unavailable"] = "disconnected"
    slots: list[BookingAvailabilitySlot]


class RepAppointmentRead(ORM):
    id: UUID
    dealer_id: UUID | None = None
    owner_user_id: UUID | None = None
    calendar_event_id: UUID | None = None
    contact_id: UUID | None = None
    kind: str
    title: str
    starts_at: datetime
    duration_min: int
    timezone: str
    invitee_name: str
    invitee_email: str | None = None
    invitee_phone: str | None = None
    company: str | None = None
    program_key: str | None = None
    program_name: str | None = None
    requested_amount: str | None = None
    full_address: str | None = None
    join_url: str | None = None
    notes: str | None = None
    status: str
    client_rsvp_status: Literal["needs_action", "accepted", "tentative", "declined", "unknown"] = "unknown"
    client_rsvp_at: datetime | None = None
    rsvp_checked_at: datetime | None = None
    booked_by_user_id: UUID | None = None
    outcome: Literal["not_converted", "did_not_show", "converted"] | None = None
    outcome_note: str | None = None
    outcome_at: datetime | None = None
    outcome_by_user_id: UUID | None = None
    archived_at: datetime | None = None
    archived_by_user_id: UUID | None = None
    cancellation_reason: str | None = None
    conversion_target: Literal["field_desk", "ai_intake"] | None = None
    converted_dealer_id: UUID | None = None
    converted_intake_id: UUID | None = None
    confirmation_email_status: str | None = None
    confirmation_sms_status: str | None = None
    email_reminder_status: str | None = None
    sms_reminder_status: str | None = None
    google_sync_status: str | None = None
    rep_notification_status: str | None = None
    rep_reminder_status: str | None = None
    delivery_error: str | None = None
    notification_results: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime


class RepAppointmentCreate(BaseModel):
    kind: Literal["callback", "program_intro", "underwriting_review"] = "callback"
    title: str | None = Field(default=None, max_length=200)
    starts_at: datetime
    duration_min: int | None = Field(default=None, ge=15, le=180)
    timezone: str | None = Field(default=None, max_length=80)
    invitee_name: str = Field(min_length=1, max_length=160)
    company: str | None = Field(default=None, max_length=180)
    invitee_email: str | None = Field(default=None, max_length=320)
    invitee_phone: str | None = Field(default=None, max_length=32)
    join_url: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=4000)
    program_key: str | None = Field(default=None, max_length=64)
    program_name: str | None = Field(default=None, max_length=180)
    requested_amount: str | None = Field(default=None, max_length=40)
    full_address: str | None = Field(default=None, max_length=500)
    transactional_sms_consent: bool = False

    @model_validator(mode="after")
    def _needs_a_recipient(self) -> "RepAppointmentCreate":
        if not (self.invitee_email or self.invitee_phone):
            raise ValueError("Provide an email or phone for the invitee.")
        return self


class RepAppointmentPatch(BaseModel):
    dealer_id: UUID | None = None
    kind: Literal["callback", "program_intro", "underwriting_review"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=80)
    duration_min: int | None = Field(default=None, ge=15, le=180)
    invitee_name: str | None = Field(default=None, min_length=1, max_length=160)
    invitee_email: str | None = Field(default=None, max_length=320)
    invitee_phone: str | None = Field(default=None, max_length=32)
    company: str | None = Field(default=None, max_length=180)
    program_key: str | None = Field(default=None, max_length=64)
    program_name: str | None = Field(default=None, max_length=180)
    requested_amount: str | None = Field(default=None, max_length=40)
    full_address: str | None = Field(default=None, max_length=500)
    status: Literal["pending", "confirmed", "cancelled", "done"] | None = None
    join_url: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=4000)
    reopen_outcome: bool = False


class RepAppointmentCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class RepAppointmentOutcomePatch(BaseModel):
    outcome: Literal["not_converted", "did_not_show", "converted"]
    note: str | None = Field(default=None, max_length=2000)
    conversion_target: Literal["field_desk", "ai_intake"] | None = None
    ai_variant: Literal["dealer", "real_estate"] | None = None
    notify_client: bool = False

    @model_validator(mode="after")
    def _conversion_requires_destination(self) -> "RepAppointmentOutcomePatch":
        if self.outcome == "converted" and self.conversion_target is None:
            raise ValueError("Choose a conversion destination.")
        if self.conversion_target == "ai_intake" and self.ai_variant is None:
            raise ValueError("Choose Dealer or Real Estate AI intake.")
        return self


class UnderwritingReviewPreferenceRead(ORM):
    id: UUID
    dealer_id: UUID
    rep_user_id: UUID | None = None
    timezone: str
    slots: list[dict]
    status: str
    submitted_at: datetime
    selected_slot_at: datetime | None = None
    selected_by_user_id: UUID | None = None
    appointment_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class UnderwritingReviewPreferenceCreate(BaseModel):
    timezone: str = Field(default="America/New_York", max_length=80)
    slots: list[datetime] = Field(min_length=3, max_length=3)


class UnderwritingReviewPreferenceBook(BaseModel):
    starts_at: datetime
    invitee_name: str = Field(min_length=1, max_length=160)
    invitee_email: EmailStr
    invitee_phone: str | None = Field(default=None, max_length=32)
    program_key: str | None = Field(default=None, max_length=64)
    program_name: str | None = Field(default=None, max_length=180)
    requested_amount: str | None = Field(default=None, max_length=40)
    full_address: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=4000)
    transactional_sms_consent: bool = False


class ContactShareRead(ORM):
    id: UUID
    owner_user_id: UUID | None = None
    contact_id: UUID | None = None
    dealer_id: UUID | None = None
    recipient_name: str
    recipient_email: str | None = None
    recipient_phone_e164: str | None = None
    channel: str
    card_token: str
    subject: str
    body: str
    email_status: str
    sms_status: str
    provider_refs: dict | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class ProgramPdfAttachmentRead(BaseModel):
    key: str
    title: str
    description: str
    filename: str


class ContactCardProgramPdfRead(ProgramPdfAttachmentRead):
    download_url: str


class ContactCardRead(BaseModel):
    recipient_name: str
    company: str | None = None
    rep_name: str
    rep_email: str | None = None
    rep_title: str | None = None
    rep_phone: str | None = None
    rep_bio: str | None = None
    rep_locale: Literal["en", "es"] = "en"
    headshot_url: str | None = None
    subject: str
    body: str
    message: str
    booking_url: str
    application_url: str
    vcard_url: str
    program_pdfs: list[ContactCardProgramPdfRead] = Field(default_factory=list)


class ContactShareCreate(BaseModel):
    dealer_id: UUID | None = None
    recipient_name: str = Field(min_length=1, max_length=160)
    company: str | None = Field(default=None, max_length=180)
    recipient_email: str | None = Field(default=None, max_length=320)
    recipient_phone: str | None = Field(default=None, max_length=32)
    channel: Literal["email", "sms", "email_sms"] = "email"
    marketing_sms_consent: bool = False
    transactional_sms_consent: bool = False
    consent_method: Literal["self_web", "in_person_device", "rep_attested"] = "rep_attested"
    program_pdf_keys: list[str] = Field(default_factory=list, max_length=5)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _needs_delivery(self) -> "ContactShareCreate":
        if not (self.recipient_email or self.recipient_phone):
            raise ValueError("Provide an email or phone number.")
        return self


class RepInboxThreadCreate(BaseModel):
    dealer_id: UUID | None = None
    recipient_name: str = Field(min_length=1, max_length=160)
    company: str | None = Field(default=None, max_length=180)
    recipient_email: str | None = Field(default=None, max_length=320)
    recipient_phone: str | None = Field(default=None, max_length=32)
    channels: list[Literal["email", "sms"]] = Field(min_length=1, max_length=2)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    marketing_sms_consent: bool = False
    transactional_sms_consent: bool = False
    consent_method: Literal["self_web", "in_person_device", "rep_attested"] = "rep_attested"

    @model_validator(mode="after")
    def _validate_channels(self) -> "RepInboxThreadCreate":
        channels = set(self.channels)
        if "email" in channels and not self.recipient_email:
            raise ValueError("Provide an email address or turn off email.")
        if "sms" in channels and not self.recipient_phone:
            raise ValueError("Provide a mobile number or turn off SMS.")
        return self


class RepInboxThreadRead(ORM):
    id: UUID
    owner_user_id: UUID | None = None
    contact_id: UUID | None = None
    dealer_id: UUID | None = None
    subject: str
    channel: str
    source: str
    last_message_at: datetime | None = None
    unread_count: int
    status: str
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    company: str | None = None
    created_at: datetime
    updated_at: datetime


class RepInboxMessageRead(ORM):
    id: UUID
    thread_id: UUID
    owner_user_id: UUID | None = None
    contact_id: UUID | None = None
    dealer_id: UUID | None = None
    direction: str
    channel: str
    subject: str | None = None
    body: str
    provider: str | None = None
    provider_message_id: str | None = None
    provider_error: str | None = None
    delivery_status: str
    sender: str | None = None
    recipient: str | None = None
    read_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RepInboxComposeResult(BaseModel):
    threads: list[RepInboxThreadRead]
    messages: list[RepInboxMessageRead]


class RepInboxMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    channel: Literal["email", "sms"] | None = None


class DeliveryRowRead(BaseModel):
    """One row of the step 2 delivery log. Status is derived from the trail,
    not stored: sent, opened and completed arrive as separate events."""

    kind: str
    request: str
    channel: str
    recipient: str
    status: str
    at: datetime
    detail: str = ""
    history: list[dict] = []


class VerificationRead(BaseModel):
    bank_linked: bool = False
    bank_source: Literal["plaid", "upload", "none"] = "none"
    statement_months: list[str] = []
    missing_statement_months: list[str] = []
    credit_returned: bool = False
    unlocked: bool = False
    returned: int = 0
    reason: str = ""
    stage: str = "intake"
    credit_enabled: bool = True
    ownership_total: float = 0.0
    ownership_complete: bool = False
    owner_contact_complete: bool = False
    missing_credit_contact_owner_ids: list[UUID] = Field(default_factory=list)
    required_credit_owner_count: int = 0
    completed_credit_owner_count: int = 0
    pending_credit_owner_ids: list[UUID] = Field(default_factory=list)
    pre_screen_complete: bool = False
    pre_screen_blockers: list[str] = Field(default_factory=list)
    preliminary_program_fit: dict | None = None


class ApplicationPreScreenPatch(BaseModel):
    refinance_debt: bool | None = None
    file_answers: dict | None = None
    owner_id: UUID | None = None
    owner_answers: dict | None = None


class ApplicationPreScreenRead(BaseModel):
    rules_version: str
    file_answers: dict = Field(default_factory=dict)
    owner_answers: dict = Field(default_factory=dict)
    required_owner_ids: list[UUID] = Field(default_factory=list)
    completed_owner_ids: list[UUID] = Field(default_factory=list)
    incomplete_owner_ids: list[UUID] = Field(default_factory=list)
    complete: bool = False
    blockers: list[str] = Field(default_factory=list)
    routing_result: dict | None = None
    self_report_routing_result: dict | None = None
    verified_routing_result: dict | None = None
    routing_history: list[dict] = Field(default_factory=list)
    completed_at: datetime | None = None


class DecisionRead(BaseModel):
    """One answer per file. Every surface renders this rather than deciding for
    itself, which is what stops a green banner sitting next to an amber note
    that contradicts it."""

    verdict: str
    headline: str
    blocking: list[dict] = []
    balance_passed: bool | None = None
    balance_reasons: list[str] = []
    capped_by_balance: bool = False
    best_path: dict | None = None
    goal_feasible: bool | None = None
    ready_for_forms: bool = False
    # The real catalogue, easiest-reachable first. Empty when the file has no
    # lending question to answer, which the catalogue decides rather than us.
    programs: list[dict] = []
    # The gate. Steps 3-5 of the application read `unlocked` from here rather
    # than deciding for themselves.
    verification: VerificationRead = Field(default_factory=VerificationRead)


class ContractTemplateRead(ORM):
    id: UUID
    key: str
    title: str
    page_count: int | None = None
    has_acroform: bool = False
    field_names: list | None = None
    field_map: dict | None = None
    revision: int = 1
    active: bool = True
    # Derived convenience: paper uploaded and at least one field mapped.
    s3_key: str | None = None
    render_kind: Literal["uploaded_pdf", "generated_html"] = "uploaded_pdf"


class ContractDocRead(ORM):
    id: UUID
    template_key: str
    template_revision: int | None = None
    status: str
    field_values: dict | None = None
    filled_sha256: str | None = None
    signed_at: datetime | None = None
    signer_name: str | None = None
    signer_title: str | None = None
    signature_sha256: str | None = None
    updated_at: datetime


class ContractGenerateResult(BaseModel):
    status: str
    placed: dict[str, str] = {}
    # Case data the fill wanted and could not find — the rep's checklist.
    missing_data: list[str] = []
    # Overlay anchors that failed to locate — a template problem, not a rep one.
    overlay_problems: list[str] = []
    sha256: str
    download_url: str | None = None


class ContractTemplateMapPatch(BaseModel):
    field_map: dict[str, str] = Field(default_factory=dict)


class UnreadSummary(BaseModel):
    """Unread counts for the nav badge. `per_file` is keyed by dealer id as a
    string, so the client can light up a row without a second request."""

    total: int = 0
    per_file: dict[str, int] = {}


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
    statement_target: int = 3
    tax_years: list[int] = []          # years with a dos_tax_filings row
    tax_target: int = 2
    has_pl: bool = False
    has_debt_schedule: bool = False
    open_doc_requests: int = 0
    # Freshness (deterministic date math vs. today — services.recurrence):
    current_through: str | None = None  # latest covered "YYYY-MM", null = no coverage
    expected_months: list[str] = []     # the 3 most recent COMPLETED months
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


# ConvertToAuditResult (defined far above) references DealerInviteResult
# lazily; resolve here, where the failure would be loud at import.
ConvertToAuditResult.model_rebuild()


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
    # Both null on GET when no funding file has been started yet.
    intake_id: UUID | None = None
    url: str | None = None


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
    # How to tell the client. "email" is the default because a request nobody
    # hears about does not get answered; "sms" adds a text on top of the email,
    # and "none" is for a request the desk will raise in person.
    notify: Literal["email", "sms", "none"] = "email"


class RoomPasscode(BaseModel):
    """The client's access code, re-sent on every call.

    The room is deliberately stateless: there is no session to steal and no
    cookie to fix, and each call is independently authorised. Matches how the
    existing bucket upload endpoints already work."""

    passcode: str = Field(min_length=1, max_length=64)


class RoomPlaidExchange(RoomPasscode):
    public_token: str = Field(min_length=1, max_length=512)
    institution_name: str | None = Field(default=None, max_length=160)
    is_primary_operating: bool | None = None


class RoomPlaidUpdateLink(RoomPasscode):
    account_selection_enabled: bool = False


class PublicPlaidResult(BaseModel):
    """What the business owner sees. Deliberately not the item row: an
    unauthenticated caller has no business seeing internal status, refresh
    scheduling or token metadata."""

    connected: bool
    institution_name: str | None = None
    message: str


class PublicPlaidItemRead(BaseModel):
    id: UUID
    institution_name: str | None = None
    accounts_label: str | None = None
    status: str
    environment: str = "sandbox"
    update_mode_reason: str | None = None
    update_mode_account_selection: bool = False
    is_primary_operating: bool = False
    last_pulled_at: datetime | None = None
    statement_months: list[str] = Field(default_factory=list)


class RoomSignableRead(BaseModel):
    """One checklist item the client can sign, with the full text they will
    sign. Shown, never summarised: a signature over hidden text is not one."""

    id: UUID
    name: str
    kind: str | None = None
    signed: bool = False
    signable: bool = False
    document_text: str = ""


class RoomFeaturesRead(BaseModel):
    business_name: str
    bank_connect_available: bool = False
    plaid_environment: str = ""
    # The room needs the authorization wording BEFORE the connect button does
    # anything, because the link-token endpoint refuses without a recorded
    # consent. Shipping the gate without this would break the client room.
    bank_consent_granted: bool = False
    bank_consent_disclosure: str = ""
    bank_connections: list[PublicPlaidItemRead] = Field(default_factory=list)
    plaid_assets_enabled: bool = False
    signable: list[RoomSignableRead] = []
    contracts: list["RoomContractRead"] = Field(default_factory=list)


class RoomContractRead(BaseModel):
    """One agreement in the client's room. Carries the FULL text for the
    Agreement toggle: what is shown is extracted from the exact PDF that gets
    signed, never a summary."""

    id: UUID
    key: str
    title: str
    status: str
    agreement_text: str = ""
    commission_note: str | None = None
    download_url: str | None = None
    pdf_sha256: str | None = None


class RoomContractSignRequest(RoomPasscode):
    typed_name: str = Field(min_length=2, max_length=160)
    esign_consent: bool
    # Absent for a typed-and-adopted signature; the stamp becomes the
    # conformed "/s/ Name" convention instead of an image.
    signature_data_url: str | None = Field(default=None, max_length=400_000)


class RoomSignRequest(RoomPasscode):
    requested_document_id: UUID
    typed_name: str = Field(min_length=1, max_length=160)
    esign_consent: bool
    signature_data_url: str = Field(min_length=1)


class RoomSignResult(BaseModel):
    signed: bool
    certificate_file_id: UUID | None = None
    message: str
    execution_status: Literal["executed", "delivery_warning"] = "executed"
    pdf_sha256: str | None = None
    download_url: str | None = None


class ClientRequestSend(BaseModel):
    """How to reach the client for a request. Email always goes unless the
    caller explicitly asks for nothing; "sms" means email AND text."""

    channel: Literal["email", "sms", "none"] = "email"


class SignatureRequestSend(ClientRequestSend):
    title: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    signature_kind: str | None = Field(default=None, max_length=48)


class ClientRequestResult(BaseModel):
    url: str
    # Plaintext only when this call opened the room. None means the client
    # already has their access code from an earlier link.
    passcode: str | None = None
    delivered: bool = False
    emailed: bool = False
    texted: bool = False
    detail: str | None = None


class BankUploadRequestResult(ClientRequestResult):
    bucket_id: UUID | None = None
    upload_link_id: UUID | None = None
    requested_document_id: UUID | None = None


class BankEvidenceRead(BaseModel):
    bank_linked: bool = False
    bank_source: Literal["plaid", "upload", "none"] = "none"
    statement_months: list[str] = Field(default_factory=list)
    missing_statement_months: list[str] = []
    statement_target: int = 3
    bucket_id: UUID | None = None
    upload_url: str | None = None
    passcode: str | None = None


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
    # Refinance workbench (0126): contract cadence + provenance.
    payment_amount: float | None = None
    payment_frequency: str | None = None
    factor_rate: float | None = None
    payoff_amount: float | None = None
    document_id: UUID | None = None
    count_in_dscr: bool = True

    model_config = ConfigDict(from_attributes=True)


_FREQ_PATTERN = r"^(daily|weekly|biweekly|monthly)$"


class DebtCreate(BaseModel):
    lender: str = Field(min_length=1, max_length=180)
    category: str = "loan"
    monthly_payment: float | None = None
    balance: float | None = None
    rate: float | None = None
    term_months: int | None = None
    maturity_on: date | None = None
    notes: str | None = None
    payment_amount: float | None = Field(default=None, ge=0)
    payment_frequency: str | None = Field(default=None, pattern=_FREQ_PATTERN)
    factor_rate: float | None = Field(default=None, gt=0, le=5)
    payoff_amount: float | None = Field(default=None, ge=0)


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
    payment_amount: float | None = Field(default=None, ge=0)
    payment_frequency: str | None = Field(default=None, pattern=_FREQ_PATTERN)
    factor_rate: float | None = Field(default=None, gt=0, le=5)
    payoff_amount: float | None = Field(default=None, ge=0)


class DebtDraftResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped_admin: int = 0     # rows a human owns — never touched
    total_monthly: float = 0.0
    debts: list[DebtRead] = []


# --- MCA-style statement-only readiness --------------------------------------


class McaCheckRead(BaseModel):
    key: str
    label: str
    value: float | None = None
    threshold: float
    unit: str
    passed: bool
    detail: str


class McaOfferRead(BaseModel):
    advance: float = 0.0
    advance_range: list[float] = []
    factor_rate: float = 1.3
    payback: float = 0.0
    term_business_days: int = 120
    daily_payment: float = 0.0
    pull_pct: float | None = None
    stretched: bool = False
    reduced: bool = False


class McaReadinessRead(BaseModel):
    amr: float = 0.0
    daily_revenue: float = 0.0
    months_used: int = 0
    existing_daily_pull: float = 0.0
    checks: list[McaCheckRead] = []
    offer: McaOfferRead | None = None
    verdict: str = "not_yet"
    failed_checks: list[str] = []


# --- DSCR composition (0129) — the clickable DSCR container ------------------


class DscrAddbackRead(ORM):
    id: UUID
    title: str
    status: str
    monthly_amount: float | None = None
    annual_amount: float | None = None
    annualized: float = 0.0            # annual ?? monthly x 12; 0 unless verified
    document_id: UUID | None = None
    source_event_id: UUID | None = None


class DscrComponentRead(BaseModel):
    debt_id: UUID
    lender: str
    category: str
    origin: str
    source: str                        # contract | drafted | manual
    stated_monthly: float | None = None
    observed_monthly: float | None = None
    effective_monthly: float = 0.0     # what actually enters the denominator
    count_in_dscr: bool = True
    vendor_key: str | None = None
    document_id: UUID | None = None


class DscrSuggestionRead(BaseModel):
    vendor_key: str
    label: str
    monthly_avg: float
    months: int
    count: int
    category: str


class DscrNumeratorRead(BaseModel):
    ebitda_source: str | None = None
    reported_ttm: float | None = None
    addbacks: list[DscrAddbackRead] = []
    adjusted: float | None = None
    bankable: float | None = None


class DscrResultsRead(BaseModel):
    dscr_current: float | None = None
    dscr_draft: float | None = None
    display: str | None = None
    at_goal: float | None = None
    cash_flow: float | None = None
    net_cash_flow_monthly: float | None = None
    monthly_debt_service: float | None = None
    ds_source: str | None = None
    funding_goal: float | None = None
    goal_monthly_payment: float | None = None


class DscrNetPoint(BaseModel):
    month: str                         # YYYY-MM
    net: float | None = None


class DscrImprovementRead(BaseModel):
    title: str
    detail: str
    impact: str | None = None    # e.g. "DSCR 0.07x -> 0.42x"


class DscrCompositionRead(BaseModel):
    improvements: list[DscrImprovementRead] = []
    numerator: DscrNumeratorRead
    components: list[DscrComponentRead] = []
    suggestions: list[DscrSuggestionRead] = []
    results: DscrResultsRead
    net_series: list[DscrNetPoint] = []


class DscrComponentAction(BaseModel):
    action: str = Field(pattern="^(toggle|add_vendor)$")
    debt_id: UUID | None = None
    count_in_dscr: bool | None = None
    vendor_key: str | None = Field(default=None, max_length=60)


# --- Plaid bank connections (0127, statements only) -------------------------


class PlaidItemRead(ORM):
    id: UUID
    institution_name: str | None = None
    accounts_label: str | None = None   # "Plaid Checking ··1111 · Savings ··4444"
    status: str
    environment: str = "sandbox"
    error: str | None = None
    update_mode_reason: str | None = None
    update_mode_account_selection: bool = False
    auto_refresh: bool = True
    last_pulled_at: datetime | None = None
    next_refresh_at: datetime | None = None
    created_at: datetime
    is_primary_operating: bool = False
    statement_months: list[str] = []


class PlaidItemPatch(BaseModel):
    auto_refresh: bool | None = None
    is_primary_operating: bool | None = None


class BankConsentState(BaseModel):
    """Whether this file may connect a bank, and the words it must show first."""

    granted: bool = False
    version: str | None = None
    at: datetime | None = None
    consenter_name: str | None = None
    # The exact wording to render. Server-owned: the client renders it and
    # sends back only the fact of agreement, never the text.
    disclosure_version: str = ""
    disclosure_text: str = ""


class BankConsentGrant(BaseModel):
    """What a client sends to agree. Note what is ABSENT: the disclosure text.

    The server stores the wording it served. Accepting text here would let a
    caller store a consent to wording of their own choosing, which is exactly
    the proof this record exists to provide.
    """

    consenter_name: str = Field(min_length=2, max_length=160)
    method: str = Field(default="self_web", max_length=24)


class RoomBankConsentGrant(BankConsentGrant):
    passcode: str = Field(min_length=1, max_length=64)


class PlaidStateRead(BaseModel):
    enabled: bool = False
    environment: str = "sandbox"
    items: list[PlaidItemRead] = []
    consent: BankConsentState = BankConsentState()
    assets_enabled: bool = False
    asset_reports: list["PlaidAssetReportRead"] = []


class PlaidLinkTokenRead(BaseModel):
    link_token: str


class PlaidUpdateLinkRequest(BaseModel):
    account_selection_enabled: bool = False


class PlaidAssetReportRead(ORM):
    id: UUID
    status: str
    environment: str
    days_requested: int
    error: str | None = None
    ready_at: datetime | None = None
    created_at: datetime


class PlaidAssetReportCreate(BaseModel):
    days_requested: int = Field(default=60, ge=0, le=731)


class PlaidExchange(BaseModel):
    public_token: str = Field(min_length=8, max_length=256)
    institution_name: str | None = Field(default=None, max_length=160)
    is_primary_operating: bool | None = None


class PlaidRefreshResult(BaseModel):
    queued: int = 0   # banks queued for a background pull


# --- Refinance workbench --------------------------------------------------


class RefiObservedRead(BaseModel):
    """Ledger-observed payment behavior for one debt (vendor-matched)."""
    matched: bool = False
    debit_count: int = 0
    months_observed: int = 0
    monthly_avg: float | None = None
    last_seen: date | None = None
    by_month: dict[str, float] = {}   # "YYYY-MM" -> observed outflow total


class RefiDebtRead(DebtRead):
    monthly_eq: float = 0.0            # engine-facing monthly cash-out
    financing_cost_monthly: float = 0.0
    payoff_est: float = 0.0
    refi_eligible: bool = True         # false for working lines (floorplan)
    observed: RefiObservedRead = RefiObservedRead()


class RefiProgramRead(BaseModel):
    path_key: str
    label: str
    annual_rate_pct: float
    term_months: int
    dscr_typical: float
    dscr_floor: float
    ceiling: float


class RefinanceRead(BaseModel):
    debts: list[RefiDebtRead] = []
    programs: list[RefiProgramRead] = []
    total_debt_service_monthly: float = 0.0
    dscr_current: float | None = None
    ebitda_bankable: float | None = None
    adb_current: float | None = None
    # Provenance — where each DSCR input actually comes from (periods |
    # observed_ledger | debt_schedule | tax_return | none) + the fully
    # ledger-derived cash-flow cross-check.
    dscr_source: str | None = None
    ebitda_source: str | None = None
    dscr_cash_flow: float | None = None
    net_cash_flow_monthly: float | None = None
    dscr_draft: float | None = None
    dscr_display: str | None = None


class RefinanceSimulateRequest(BaseModel):
    debt_ids: list[UUID] = Field(default_factory=list, max_length=40)
    amount: float | None = Field(default=None, ge=0, le=50_000_000)  # None -> payoff total
    annual_rate_pct: float = Field(default=9.5, ge=0.01, lt=50)
    term_months: int = Field(default=60, ge=1, le=360)
    path_key: str | None = None       # program whose floor/ceiling grade the scenario


class RefinanceScenarioRead(BaseModel):
    payoff_total: float
    freed_monthly: float               # removed debts' monthly cash-out
    new_payment_monthly: float
    retained_ds_monthly: float
    proforma_ds_monthly: float
    savings_monthly: float             # freed - new payment
    ebitda_addback_annual: float       # financing cost returned to EBITDA
    adb_lift_estimate: float           # uniform-debit estimate, flagged as such
    amount: float
    max_principal_at_floor: float
    headroom: float                    # max - amount
    dscr_floor: float
    dscr_typical: float
    verdict: str                       # feasible | conditional | not_yet | no_selection



class OwnerRead(ORM):
    id: UUID
    first_name: str
    last_name: str
    full_name: str
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
    credit_workflow_status: str | None = None
    credit_delivery_detail: str | None = None
    credit_provider_request_id: str | None = None
    credit_provider_error_category: str | None = None
    has_invite: bool = False
    credit_score: int | None = None
    credit_tier: str | None = None
    credit_pulled_at: datetime | None = None
    credit_summary: dict | None = None
    notes: str | None = None
    credit_required: bool = False
    credit_complete: bool = False
    credit_contact_complete: bool = False


class OwnerCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: str | None = None
    phone: str | None = None
    ownership_pct: float | None = Field(default=None, ge=0, le=100)
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
    ownership_pct: float | None = Field(default=None, ge=0, le=100)
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


class CreditInviteRequest(BaseModel):
    """How to get the consent link to the owner.

    `none` mints the link and hands it back for the rep to read out or paste
    themselves, which is what happens when they are standing next to the
    person and texting would be absurd."""

    channel: Literal["email", "sms", "none"] = "none"
    to_email: str | None = Field(default=None, max_length=320)
    to_phone: str | None = Field(default=None, max_length=40)


class CreditInviteResult(BaseModel):
    """Returned ONCE at mint time — only the sha256 of the token is stored."""

    token: str
    path: str  # "/credit-consent#t={token}" (fragment: token never hits server logs)
    delivered: bool = False
    channel: str = "none"
    detail: str = ""


class BulkCreditInviteRequest(BaseModel):
    channel: Literal["email", "sms", "none"] = "email"


class OwnerCreditInviteResult(BaseModel):
    owner_id: UUID
    owner_name: str
    token: str | None = None
    path: str | None = None
    delivered: bool = False
    channel: str = "none"
    detail: str = ""


class BulkCreditInviteResult(BaseModel):
    items: list[OwnerCreditInviteResult] = Field(default_factory=list)


class PublicConsentView(BaseModel):
    """What an unauthenticated consent page may know: enough for the owner to
    recognize themself and the business, and which fields we still need.
    Never scores, summaries, or other owners."""

    first_name: str
    last_name: str
    last_initial: str
    email: str
    phone: str
    dealer_name: str
    fields_needed: list[str]  # subset of dob/street/city/state/zip still missing
    completed: bool
    # Kept for older clients; owner consent links no longer depend on the
    # unrelated document-room passcode.
    requires_code: bool = False


class PublicConsentSubmit(BaseModel):
    """FCRA consent is a hard precondition. The owner confirms or corrects
    their identity/contact fields; bureau-only profile fields fill empty data."""
    # Backward-compatible no-op for older clients.
    access_code: str | None = Field(default=None, max_length=64)

    fcra_consent: bool = False
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    phone: str = Field(min_length=8, max_length=48)
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


class RefinanceSimulateRead(BaseModel):
    baseline: SimulateMetrics
    scenario: SimulateMetrics
    derived: RefinanceScenarioRead


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

    mark: str = Field(pattern="^(recurring|one_time|none|clear)$")
    apply_similar: bool = True


# --- Field-rep production (0130) ---------------------------------------------


class RepFileRow(ORM):
    """One file in a rep's book, as production reporting sees it."""

    dealer_id: UUID
    name: str
    city: str | None = None
    state: str | None = None
    industry: str | None = None
    status: str | None = None          # None = opened before the pipeline existed
    decision: str | None = None
    score: float | None = None
    documents: int = 0
    created_at: datetime
    last_activity: datetime | None = None


class RepCategoryMetric(BaseModel):
    industry: str
    opened: int = 0
    approved_or_fundable: int = 0


class RepLocationMetric(BaseModel):
    location: str
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    opened: int = 0
    approved_or_fundable: int = 0


class RepAmountMetric(BaseModel):
    average_requested: float | None = None
    average_approved: float | None = None
    approved_amount_source: str = "none"
    approved_amount_source_counts: dict[str, int] = {}


class RepProductionInsights(BaseModel):
    underwriting_ready: int = 0
    approved_or_fundable: int = 0
    underwriting_ready_ratio: float | None = None
    approved_or_fundable_ratio: float | None = None
    document_ratio: float | None = None
    contract_execution_ratio: float | None = None
    amount_metrics: RepAmountMetric = Field(default_factory=RepAmountMetric)
    top_new_app_industries: list[RepCategoryMetric] = []
    top_approved_industries: list[RepCategoryMetric] = []
    top_new_app_towns: list[RepLocationMetric] = []
    top_approved_towns: list[RepLocationMetric] = []
    top_new_app_zip_codes: list[RepLocationMetric] = []
    top_approved_zip_codes: list[RepLocationMetric] = []


class RepFunnel(BaseModel):
    """Where files stop.

    Deliberately six stages rather than a single conversion rate: the useful
    fact about a field team is WHERE the drop happens, and an aggregate rate
    hides it. The gap between authorizations_sent and bank_linked is the one
    that usually matters, because it is the only stage that depends on the
    applicant doing something on their own.
    """

    opened: int = 0
    authorizations_sent: int = 0
    bank_linked: int = 0
    credit_returned: int = 0
    verified: int = 0
    application_submitted: int = 0
    contract_executed: int = 0


class RepProduction(BaseModel):
    """What one rep has brought in over the window."""

    funnel: RepFunnel = Field(default_factory=RepFunnel)

    rep_user_id: UUID | None = None
    rep_name: str
    rep_email: str | None = None
    files_opened: int = 0
    active: int = 0
    complete: int = 0
    declined: int = 0
    stalled: int = 0
    # Files with documents in. The single most useful number for a desk: a file
    # opened and never fed is not production, and this is what separates them.
    with_documents: int = 0
    fundable: int = 0
    avg_score: float | None = None
    last_activity: datetime | None = None
    insights: RepProductionInsights = Field(default_factory=RepProductionInsights)
    files: list[RepFileRow] = []


class RepProductionRead(BaseModel):
    scope: Literal["own", "firm"] = "firm"
    since: datetime | None = None
    totals: RepProduction
    reps: list[RepProduction] = []
