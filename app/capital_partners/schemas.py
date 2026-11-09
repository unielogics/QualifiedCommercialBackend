"""Request and response shapes for /api/v1/capital/*.

Member-facing shapes carry nothing the member should not see (no other
member's name, no desk notes, no first-sale dates). Admin shapes carry it all.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.loan_repayment import LoanRepaymentConfig, validate_repayment_config
from app.schemas.phone import RequiredPhone

InvestingAsT = Literal["individual", "family_office", "entity"]
LocaleT = Literal["en", "es"]
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email(value: str) -> str:
    v = (value or "").strip().lower()
    if not _EMAIL_RE.match(v) or len(v) > 320:
        raise ValueError("Enter a valid email address.")
    return v


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


class AccessRequestIn(BaseModel):
    investing_as: InvestingAsT
    entity_name: str | None = Field(default=None, max_length=200)
    capital_band: str = Field(min_length=1, max_length=40)
    source: str = Field(min_length=1, max_length=120)
    full_name: str = Field(min_length=1, max_length=160)
    email: str
    phone: RequiredPhone
    note: str | None = Field(default=None, max_length=4000)
    consent: bool
    locale: LocaleT = "en"
    #: Honeypot. A real browser leaves it empty.
    website: str | None = None

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        return _email(v)


class AccessRequestResult(BaseModel):
    ok: bool
    id: UUID | None = None


# ---------------------------------------------------------------------------
# Member
# ---------------------------------------------------------------------------


class EntityIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["individual", "trust", "llc", "partnership", "family_office", "other"]
    state: str | None = Field(default=None, min_length=2, max_length=2)
    accreditation_basis: str | None = Field(default=None, max_length=64)
    is_default: bool = False


class EntityOut(_Out):
    id: UUID
    name: str
    kind: str
    state: str | None
    accreditation_basis: str | None
    status: str
    is_default: bool


class GateStep(BaseModel):
    key: str
    title: str
    state: Literal["done", "now", "later"]


class GateOut(BaseModel):
    status: str
    cleared: bool
    steps: list[GateStep]
    relationship_established_at: datetime | None
    waiting_period_days: int | None
    waiting_period_ends_at: datetime | None
    waiting_days_left: int | None
    waiting_pct: int | None
    suspended: bool


class MeOut(BaseModel):
    user_id: UUID
    email: str
    name: str
    role: str
    #: True for operators looking at the portal: read-only, nothing to subscribe.
    preview: bool
    member_id: UUID | None
    status: str
    status_label: str
    investing_as: str | None
    display_name: str | None
    state_of_residence: str | None
    gate: GateOut | None
    entities: list[EntityOut]
    has_questionnaire: bool
    questionnaire_outcome: str | None
    questionnaire_notes: str | None


class QuestionnaireIn(BaseModel):
    answers: dict[str, Any]
    basis: str | None = Field(default=None, max_length=64)
    investing_as: InvestingAsT | None = None
    state_of_residence: str | None = Field(default=None, min_length=2, max_length=2)
    entity_id: UUID | None = None
    entities: list[EntityIn] = Field(default_factory=list)


class QuestionnaireOut(_Out):
    id: UUID
    version: int
    answers: dict[str, Any]
    basis: str | None
    investing_as: str | None
    state_of_residence: str | None
    submitted_at: datetime
    outcome: str
    review_notes: str | None


class DocumentOut(_Out):
    id: UUID
    kind: str
    kind_label: str
    name: str
    content_type: str | None
    size_bytes: int | None
    version: int
    status: str


class DocumentUrlOut(BaseModel):
    url: str
    expires_in_seconds: int


class ListingCard(BaseModel):
    id: UUID
    ref: str
    name: str
    vertical: str
    vertical_label: str
    program_label: str
    city: str | None
    state: str | None
    place: str | None
    collateral_type: str | None
    lien_position: str
    note_rate_pct: Decimal | None
    term_months: int | None
    ltv_pct: Decimal | None
    ltv_kind: str
    minimum_investment: Decimal | None
    loan_amount: Decimal | None
    offered_amount: Decimal | None
    committed_amount: Decimal
    remaining_amount: Decimal
    pct_committed: int
    payment_frequency: str
    personal_guarantee: bool
    status: str
    outcome: str | None
    opens_at: datetime | None
    closes_at: datetime | None
    days_left: int | None
    funded_at: datetime | None
    repaid_at: datetime | None
    maturity_date: date | None
    published_at: datetime | None
    products: list[dict[str, Any]] = Field(default_factory=list)


class SubscriptionOut(_Out):
    id: UUID
    listing_id: UUID
    listing_ref: str | None = None
    listing_name: str | None = None
    entity_id: UUID | None
    entity_name: str | None = None
    amount: Decimal
    status: str
    requested_at: datetime
    documents_sent_at: datetime | None
    signed_at: datetime | None
    funded_at: datetime | None
    funded_amount: Decimal | None


class ListingDetail(ListingCard):
    summary: str | None
    guarantee_text: str | None
    borrower_track_record: str | None
    exit_plan: str | None
    valuation_amount: Decimal | None
    valuation_source: str | None
    title_status: str | None
    servicer_label: str | None
    documents: list[DocumentOut]
    my_subscriptions: list[SubscriptionOut]
    can_subscribe: bool
    #: The underwriting figures the file showed when the desk cut or last
    #: refreshed the listing, with the desk's plan: what changes, coverage
    #: before and after. None for a listing not cut from a file.
    financials: dict[str, Any] | None = None


class FacetCount(BaseModel):
    value: str
    label: str
    count: int


class MarketStats(BaseModel):
    open_count: int
    available_amount: Decimal
    weighted_note_rate_pct: Decimal | None
    average_ltv_pct: Decimal | None


class ListingsPage(BaseModel):
    items: list[ListingCard]
    total: int
    stats: MarketStats
    facets: dict[str, list[FacetCount]]
    vertical_counts: dict[str, int]
    as_of: datetime


class SubscriptionIn(BaseModel):
    amount: Decimal = Field(gt=0)
    entity_id: UUID | None = None
    ack_ppm: bool
    ack_accredited: bool


class ParticipationOut(BaseModel):
    id: UUID
    listing_id: UUID
    listing_ref: str
    listing_name: str
    vertical: str
    vertical_label: str
    note_rate_pct: Decimal | None
    entity_id: UUID | None
    entity_name: str | None
    invested_amount: Decimal
    principal_outstanding: Decimal
    interest_received_ytd: Decimal
    interest_received_total: Decimal
    next_payment_on: date | None
    servicer_status: str
    status_as_of: date | None
    funded_at: datetime | None
    closed_at: datetime | None
    maturity_date: date | None
    transferable: bool


class PortfolioStats(BaseModel):
    invested: Decimal
    outstanding: Decimal
    interest_ytd: Decimal
    active: int


class PortfolioOut(BaseModel):
    stats: PortfolioStats
    holdings: list[ParticipationOut]
    entities: list[EntityOut]
    as_of: date


class SoldRow(BaseModel):
    id: UUID
    ref: str
    name: str
    vertical: str
    vertical_label: str
    program_label: str
    place: str | None
    offered_amount: Decimal | None
    note_rate_pct: Decimal | None
    term_months: int | None
    funded_at: datetime | None
    repaid_at: datetime | None
    hold_months: int | None
    status: str
    outcome: str | None


class SoldStats(BaseModel):
    count: int
    capital_placed: Decimal
    repaid_count: int
    average_hold_months: int | None


class SoldOut(BaseModel):
    stats: SoldStats
    rows: list[SoldRow]


class TransferIn(BaseModel):
    participation_id: UUID
    principal_amount: Decimal = Field(gt=0)
    ask_pct: Decimal = Field(default=Decimal("100"), gt=0, le=Decimal("200"))


class TransferOut(BaseModel):
    id: UUID
    ref: str
    listing_id: UUID
    listing_ref: str
    listing_name: str
    vertical: str
    vertical_label: str
    principal_amount: Decimal
    note_rate_pct: Decimal | None
    remaining_term_months: int | None
    ask_pct: Decimal
    implied_yield_pct: Decimal | None
    listed_at: datetime
    status: str
    is_mine: bool
    can_request: bool


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class AccessRequestOut(_Out):
    id: UUID
    investing_as: str
    entity_name: str | None
    capital_band: str
    source: str
    full_name: str
    email: str
    phone: str
    note: str | None
    locale: str
    status: str
    emailed: bool
    invited_user_id: UUID | None
    handled_by_id: UUID | None
    handled_at: datetime | None
    handled_note: str | None
    created_at: datetime


class AccessRequestActionIn(BaseModel):
    note: str | None = Field(default=None, max_length=4000)


class InviteResult(BaseModel):
    ok: bool
    user_id: UUID
    member_id: UUID
    invitation_sent: bool
    message: str


class MemberAdminOut(BaseModel):
    id: UUID
    user_id: UUID
    email: str
    name: str
    status: str
    investing_as: str | None
    display_name: str | None
    phone: str | None
    state_of_residence: str | None
    capital_band: str | None
    source: str | None
    relationship_established_at: datetime | None
    waiting_period_days: int | None
    waiting_period_ends_at: datetime | None
    approved_at: datetime | None
    accreditation_basis: str | None
    review_notes: str | None
    suspended_at: datetime | None
    suspended_reason: str | None
    invited_at: datetime | None
    created_at: datetime
    entities: list[EntityOut]
    latest_questionnaire: QuestionnaireOut | None
    subscriptions_count: int
    participations_count: int
    gate: GateOut


class MemberReviewIn(BaseModel):
    decision: Literal["approve", "needs_changes", "suspend", "reinstate"]
    notes: str | None = Field(default=None, max_length=4000)
    relationship_established_at: datetime | None = None
    waiting_period_days: int | None = Field(default=None, ge=0, le=365)
    accreditation_basis: str | None = Field(default=None, max_length=64)


class SourceRow(BaseModel):
    kind: str
    id: str
    ref: str | None
    name: str
    vertical: str
    vertical_label: str | None
    amount: Decimal | None
    approved_amount: Decimal | None
    funded_amount: Decimal | None
    status: str | None
    loan_id: UUID | None
    profile_id: UUID | None
    updated_at: datetime | None
    already_listed_ref: str | None = None


class PrefillIn(BaseModel):
    loan_id: UUID | None = None
    profile_id: UUID | None = None
    #: An AI lead that was never opened has no application profile yet; the
    #: desk may still list it, and the prefill resolves the profile first.
    intake_id: UUID | None = None


class ListingRef(BaseModel):
    id: UUID
    ref: str
    status: str
    name: str


class SourceLookup(BaseModel):
    """What the file page needs before offering "Publish to marketplace"."""

    profile_id: UUID | None = None
    loan_id: UUID | None = None
    listing: ListingRef | None = None
    underwriting_status: str | None = None
    has_term_sheet: bool = False
    #: What the listing carries against the file now: published, up to date,
    #: and which sections changed since the last push. None without a listing.
    sync: dict[str, Any] | None = None


class ListingProductIn(BaseModel):
    """One product offered on a listing: a program with suggested amount, rate and term."""

    program_key: str | None = Field(default=None, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    amount: Decimal | None = Field(default=None, ge=0)
    rate_pct: Decimal | None = Field(default=None, ge=0, le=100)
    factor_rate: Decimal | None = Field(default=None, ge=1, le=3)
    term_months: int | None = Field(default=None, ge=1, le=600)
    #: Interest alone each month, principal at maturity.
    interest_only: bool = False
    #: amortizing | simple_interest | interest_only (defaults by term), and the
    #: months of the term that are interest-only before principal starts.
    structure: Literal["amortizing", "simple_interest", "interest_only"] | None = None
    interest_only_months: int | None = Field(default=None, ge=0, le=600)
    repayment_config: LoanRepaymentConfig | None = None
    payment_frequency: Literal["monthly", "weekly", "daily", "biweekly", "custom"] | None = "monthly"
    payments_per_year: float | None = Field(default=None, ge=1, le=366, allow_inf_nan=False)
    monthly_payment: Decimal | None = Field(default=None, ge=0)
    coverage_payment_monthly: Decimal | None = Field(default=None, ge=0)
    interest_only_payment_monthly: Decimal | None = Field(default=None, ge=0)
    repayment_payment_monthly: Decimal | None = Field(default=None, ge=0)
    maturity_balance: Decimal | None = Field(default=None, ge=0)
    payment_phases: list[dict[str, Any]] = Field(default_factory=list)
    dscr: Decimal | None = None
    notes: str | None = Field(default=None, max_length=600)
    primary: bool = False

    @model_validator(mode="after")
    def _repayment_schedule(self):
        if self.payment_frequency == "custom" and self.payments_per_year is None:
            raise ValueError("Custom repayment cadence requires payments per year")
        if self.repayment_config is None:
            return self
        if self.factor_rate is not None:
            raise ValueError("Advanced amortization and principal balloons require interest-rate pricing, not a factor rate")
        if not self.amount or self.rate_pct is None or not self.term_months:
            raise ValueError("Enter the amount, interest rate and maturity before configuring advanced repayment")
        from app.services.loan_schedule import build_schedule
        from app.services.production_term_structure import periods_per_year

        ppy = periods_per_year(self.payment_frequency or "monthly", self.payments_per_year)
        kind = "interest_only" if self.interest_only else self.structure
        self.repayment_config = validate_repayment_config(
            self.repayment_config, self.term_months, structure=kind,
            interest_only_months=self.interest_only_months, periods_per_year=ppy,
            amount=float(self.amount), rate_pct=float(self.rate_pct),
        )
        schedule = build_schedule(float(self.amount), float(self.rate_pct), self.term_months,
            structure=kind, interest_only_months=self.interest_only_months or 0,
            periods_per_year=ppy, repayment_config=self.repayment_config.model_dump(exclude_none=True))
        self.monthly_payment = Decimal(str(schedule["ordinary_payment_monthly"]))
        self.coverage_payment_monthly = Decimal(str(schedule["coverage_payment_monthly"]))
        self.interest_only_payment_monthly = Decimal(str(schedule["payment_io_monthly"])) if schedule["payment_io_monthly"] is not None else None
        self.repayment_payment_monthly = Decimal(str(schedule["payment_after_monthly"])) if schedule["payment_after_monthly"] is not None else None
        self.maturity_balance = Decimal(str(schedule["balloon"]))
        self.payment_phases = schedule["payment_phases"]
        return self


class OfferSuggestions(BaseModel):
    profile_id: UUID
    income_annual: float | None = None
    income_basis: str | None = None
    income_label: str | None = None
    existing_debt_service_monthly: float | None = None
    debt_service_source: str | None = None
    target_dscr: float
    requested_amount: float | None = None
    plan_active: bool = False
    payoff_total: float | None = None
    #: The loan the desk's plan carries (amount, rate, term, interest-only, payment).
    plan_loan: dict[str, Any] | None = None
    interest_only: bool = False
    products: list[dict[str, Any]] = Field(default_factory=list)


class ListingIn(BaseModel):
    """Create or patch. Every field optional on patch; create needs name + vertical."""

    source_kind: Literal["loan", "profile", "manual"] | None = None
    loan_id: UUID | None = None
    profile_id: UUID | None = None
    vertical: Literal["real_estate", "auto", "business"] | None = None
    program_label: str | None = Field(default=None, max_length=80)
    name: str | None = Field(default=None, max_length=200)
    summary: str | None = None
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, min_length=2, max_length=2)
    collateral_type: str | None = Field(default=None, max_length=80)
    lien_position: Literal["first", "second"] | None = None
    note_rate_pct: Decimal | None = Field(default=None, ge=0, le=100)
    term_months: int | None = Field(default=None, ge=1, le=600)
    ltv_pct: Decimal | None = Field(default=None, ge=0, le=200)
    ltv_kind: Literal["ltv", "ltc", "cltv"] | None = None
    loan_amount: Decimal | None = Field(default=None, ge=0)
    offered_amount: Decimal | None = Field(default=None, ge=0)
    minimum_investment: Decimal | None = Field(default=None, ge=0)
    payment_frequency: Literal["monthly", "quarterly", "at_maturity"] | None = None
    personal_guarantee: bool | None = None
    guarantee_text: str | None = Field(default=None, max_length=200)
    borrower_track_record: str | None = Field(default=None, max_length=200)
    exit_plan: str | None = None
    valuation_amount: Decimal | None = Field(default=None, ge=0)
    valuation_source: str | None = Field(default=None, max_length=120)
    title_status: str | None = Field(default=None, max_length=120)
    servicer_label: str | None = Field(default=None, max_length=120)
    maturity_date: date | None = None
    closes_at: datetime | None = None
    source_snapshot: dict[str, Any] | None = None
    products: list[ListingProductIn] | None = Field(default=None, max_length=8)


class ListingEventOut(_Out):
    id: UUID
    kind: str
    actor_user_id: UUID | None
    detail: dict[str, Any]
    occurred_at: datetime


class SubscriptionAdminOut(SubscriptionOut):
    member_id: UUID
    member_name: str | None = None
    member_email: str | None = None
    member_state: str | None = None
    ack_ppm: bool
    ack_accredited: bool
    acknowledged_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    declined_at: datetime | None
    admin_notes: str | None


class ParticipationAdminOut(ParticipationOut):
    member_id: UUID
    member_name: str | None = None
    member_email: str | None = None
    subscription_id: UUID


class ListingAdminOut(ListingDetail):
    source_kind: str
    loan_id: UUID | None
    profile_id: UUID | None
    first_sale_at: datetime | None
    published_by_id: UUID | None
    publish_reason: str | None
    withdrawn_at: datetime | None
    withdrawn_reason: str | None
    source_snapshot: dict[str, Any]
    sync: dict[str, Any] | None = None
    created_by_id: UUID | None
    created_at: datetime
    updated_at: datetime
    investor_count: int
    subscriptions: list[SubscriptionAdminOut]
    participations: list[ParticipationAdminOut]
    events: list[ListingEventOut]
    publish_blockers: list[str]


class PublishIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)
    confirmed: bool


class StatusIn(BaseModel):
    status: Literal["open", "closing", "fully_subscribed", "funded", "repaid", "workout"]
    outcome: Literal["performing", "repaid_in_full", "repaid_after_extension", "in_workout"] | None = None
    reason: str | None = Field(default=None, max_length=2000)
    effective_at: datetime | None = None


class WithdrawIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)
    confirmed: bool


class DocumentInitIn(BaseModel):
    kind: Literal["ppm", "subscription_agreement", "note", "valuation", "title", "servicing_summary", "other"]
    name: str = Field(min_length=1, max_length=200)
    content_type: str = Field(default="application/pdf", max_length=120)
    size_bytes: int | None = Field(default=None, ge=0, le=200 * 1024 * 1024)


class DocumentInitOut(BaseModel):
    document_id: UUID
    upload_url: str
    headers: dict[str, str]
    s3_key: str


class DocumentCompleteIn(BaseModel):
    document_id: UUID


class SubscriptionActionIn(BaseModel):
    notes: str | None = Field(default=None, max_length=4000)
    signed_at: datetime | None = None
    funded_at: datetime | None = None
    funded_amount: Decimal | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=2000)


class ParticipationPatchIn(BaseModel):
    principal_outstanding: Decimal | None = Field(default=None, ge=0)
    interest_received_ytd: Decimal | None = Field(default=None, ge=0)
    interest_received_total: Decimal | None = Field(default=None, ge=0)
    next_payment_on: date | None = None
    servicer_status: Literal["current", "late", "paid_in_full", "in_workout"] | None = None
    status_as_of: date | None = None


class ParticipationBulkItem(ParticipationPatchIn):
    id: UUID


class ParticipationBulkIn(BaseModel):
    items: list[ParticipationBulkItem] = Field(min_length=1, max_length=500)


class TransferAdminOut(TransferOut):
    participation_id: UUID
    seller_member_id: UUID
    seller_name: str | None = None
    buyer_member_id: UUID | None
    buyer_name: str | None = None
    requested_at: datetime | None
    consented_at: datetime | None
    completed_at: datetime | None
    declined_at: datetime | None
    withdrawn_at: datetime | None
    notes: str | None


class TransferDecisionIn(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


class FilingIn(BaseModel):
    kind: Literal["form_d", "form_d_amendment", "state_notice", "bad_actor_check"]
    listing_id: UUID | None = None
    state: str | None = Field(default=None, min_length=2, max_length=2)
    subject: str | None = Field(default=None, max_length=200)
    subject_role: str | None = Field(default=None, max_length=120)
    first_sale_at: datetime | None = None
    due_at: date | None = None
    filed_at: date | None = None
    checked_at: date | None = None
    status: Literal["due", "filed", "not_required", "cleared", "pending"] | None = None
    fee_amount: Decimal | None = Field(default=None, ge=0)
    reference: str | None = Field(default=None, max_length=120)
    sources: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=4000)


class FilingPatchIn(BaseModel):
    state: str | None = Field(default=None, min_length=2, max_length=2)
    subject: str | None = Field(default=None, max_length=200)
    subject_role: str | None = Field(default=None, max_length=120)
    due_at: date | None = None
    filed_at: date | None = None
    checked_at: date | None = None
    status: Literal["due", "filed", "not_required", "cleared", "pending"] | None = None
    fee_amount: Decimal | None = Field(default=None, ge=0)
    reference: str | None = Field(default=None, max_length=120)
    sources: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=4000)


class FilingOut(_Out):
    id: UUID
    kind: str
    listing_id: UUID | None
    listing_ref: str | None = None
    listing_name: str | None = None
    investor_count: int | None = None
    state: str | None
    subject: str | None
    subject_role: str | None
    first_sale_at: datetime | None
    due_at: date | None
    filed_at: date | None
    checked_at: date | None
    status: str
    fee_amount: Decimal | None
    reference: str | None
    sources: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class FilingsSummaryOut(BaseModel):
    form_d_due: int
    form_d_next_due: date | None
    state_notices_due: int
    state_notice_next_due: date | None
    covered_persons: int
    covered_persons_cleared: int
    amendment_next_due: date | None
    overdue: int
    controls: list[dict[str, Any]]


class IssuerSettingsIn(BaseModel):
    issuer_name: str | None = Field(default=None, max_length=200)
    cik: str | None = Field(default=None, max_length=20)
    edgar_status: str | None = Field(default=None, max_length=32)
    edgar_access_granted_at: date | None = None
    admin_count: int | None = Field(default=None, ge=0, le=100)
    default_waiting_period_days: int | None = Field(default=None, ge=0, le=365)
    servicer_label: str | None = Field(default=None, max_length=120)
    notify_emails: list[str] | None = None
    wiring_instructions: str | None = None

    @field_validator("notify_emails")
    @classmethod
    def _v_emails(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [_email(e) for e in v if e and e.strip()]


class IssuerSettingsOut(_Out):
    issuer_name: str | None
    cik: str | None
    edgar_status: str
    edgar_access_granted_at: date | None
    admin_count: int
    default_waiting_period_days: int
    servicer_label: str
    notify_emails: list[str]
    wiring_instructions: str | None
    updated_at: datetime
