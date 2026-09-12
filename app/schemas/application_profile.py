from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

ApplicationSourceKind = Literal["deal", "loan", "intake", "dealer"]
ApplicationVertical = Literal["real_estate", "main_street", "dealer", "mca"]
UnderwritingLifecycleStatus = Literal[
    "submitted",
    "collecting_docs",
    "in_underwriting",
    "term_sheet_provided",
    "approved",
    "closed_won",
    "closed_lost",
    "denied",
]
RequirementStateStatus = Literal[
    "missing",
    "requested",
    "received_unverified",
    "verified",
    "waived",
    "not_applicable",
    "stale",
    "failed",
]
ProgramRecommendationStatus = Literal[
    "recommended",
    "needs_information",
    "not_eligible",
    "criteria_unavailable",
]
EvidenceDecisionStatus = Literal[
    "processing",
    "accepted",
    "needs_more",
    "rejected",
    "failed",
]


class ApplicationProfileResolve(BaseModel):
    source_kind: ApplicationSourceKind
    source_id: UUID


class ApplicationProfileRead(BaseModel):
    id: UUID
    client_id: UUID | None = None
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    intake_id: UUID | None = None
    dealer_id: UUID | None = None
    primary_bucket_id: UUID | None = None
    plaid_assets_enabled: bool = True
    plaid_statements_enabled: bool = False
    plaid_policy_updated_at: datetime | None = None
    plaid_policy_updated_by_user_id: UUID | None = None
    vertical: str
    funding_category: str | None = None
    entity_type: str | None = None
    industry: str | None = None
    subindustry: str | None = None
    naics_code: str | None = None
    naics_label: str | None = None
    custom_industry: str | None = None
    industry_entry_id: UUID | None = None
    subindustry_entry_id: UUID | None = None
    activity_entry_id: UUID | None = None
    taxonomy_version: str = "2022"
    classification_provenance: dict | None = None
    classification_revision: int
    classification_state: dict | None = None
    classified_at: datetime | None = None
    backfill_needs_review: bool = False
    is_draft: bool = False
    draft_finalized_at: datetime | None = None
    extraction_reviewed_at: datetime | None = None
    bank_verification_override_at: datetime | None = None
    bank_verification_override_reason: str | None = None
    underwriting_status: UnderwritingLifecycleStatus = "collecting_docs"
    underwriting_approved_amount: float | None = None
    underwriting_term_sheet_amount: float | None = None
    underwriting_current_dscr: float | None = None
    underwriting_target_dscr: float | None = None
    underwriting_approved_dscr: float | None = None
    underwriting_close_outcome: str | None = None
    underwriting_notes: str | None = None
    underwriting_updated_by_user_id: UUID | None = None
    underwriting_updated_at: datetime | None = None
    program_selection_mode: Literal["auto", "manual"] = "auto"
    program_selection_locked_at: datetime | None = None
    program_selection_locked_by_user_id: UUID | None = None
    missing_item_email_enabled: bool = True
    missing_item_email_last_sent_at: datetime | None = None
    missing_item_email_next_send_at: datetime | None = None
    missing_item_email_attempts: int = 0
    missing_item_email_requirement_key: str | None = None
    owner_storage: Literal["application", "dealer"]


class ProgramFitCandidate(BaseModel):
    program_key: str
    program_name: str
    catalog_id: UUID
    public_slug: str
    playbook_id: UUID | None = None
    playbook_version: int | None = None
    eligible: bool
    recommendation_status: ProgramRecommendationStatus
    fit_score: float = 0
    confidence: float = 0
    priority: int = 0
    reasons: list[str] = Field(default_factory=list)


class ApplicationProgramSelectionRead(BaseModel):
    id: UUID
    program_key: str
    program_name: str
    playbook_id: UUID
    playbook_version: int
    source: Literal["ai_auto", "operator"]
    fit_score: float | None = None
    fit_confidence: float | None = None
    fit_reasons: list[str] = Field(default_factory=list)
    selected_at: datetime
    needs_scope_review: bool = False


class EvidencePolicySelectionRead(BaseModel):
    id: UUID
    policy_key: str
    policy_name: str
    playbook_id: UUID
    playbook_version: int
    selected_at: datetime


class ApplicationRequirementEvidenceRead(BaseModel):
    file_id: UUID
    file_name: str
    bucket_id: UUID
    created_at: datetime
    source: Literal["automatic", "filename_suggestion", "operator"]
    verified: bool = False
    verified_at: datetime | None = None
    ai_decision: EvidenceDecisionStatus = "processing"
    ai_reason_code: str | None = None
    ai_explanation: str | None = None
    ai_confidence: str | None = None
    decision_actor: Literal["ai", "staff", "system"] | None = None
    analysis_id: UUID | None = None
    coverage_contribution: dict = Field(default_factory=dict)


class ApplicationEvidenceOptionRead(BaseModel):
    file_id: UUID
    file_name: str
    bucket_id: UUID
    created_at: datetime


class ApplicationRequirementRead(BaseModel):
    requirement_key: str
    label: str
    category: str
    required_level: Literal["required", "recommended", "optional"]
    status: RequirementStateStatus
    requested_document_id: UUID | None = None
    evidence_file_id: UUID | None = None
    evidence_file_name: str | None = None
    evidence_files: list[ApplicationRequirementEvidenceRead] = Field(default_factory=list)
    evidence_count: int = 0
    verified_evidence_count: int = 0
    coverage: dict = Field(default_factory=dict)
    verified_coverage: dict = Field(default_factory=dict)
    coverage_complete: bool = False
    verified_coverage_complete: bool = False
    allow_multiple_files: bool = True
    verification_required: bool = False
    source_program_keys: list[str] = Field(default_factory=list)
    source_policy_keys: list[str] = Field(default_factory=list)
    program_overrides: dict[str, str] = Field(default_factory=dict)
    client_visible: bool = False
    can_waive: bool = False
    state_reason: str | None = None
    last_requested_at: datetime | None = None
    received_at: datetime | None = None
    verified_at: datetime | None = None
    provenance: dict = Field(default_factory=dict)


class ProgramReadinessItem(BaseModel):
    selection_id: UUID
    program_key: str
    program_name: str
    complete: bool = False
    completion_percent: int = 0
    required_count: int = 0
    satisfied_count: int = 0
    blocking_requirement_keys: list[str] = Field(default_factory=list)
    requirement_keys: list[str] = Field(default_factory=list)


class MissingItemAutomationRead(BaseModel):
    enabled: bool = False
    eligible: bool = False
    next_requirement_key: str | None = None
    next_send_at: datetime | None = None
    last_sent_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = 3
    stop_reason: str | None = None


class ApplicationEvidenceSummary(BaseModel):
    """Shared evidence truth consumed by both intake and banking workspaces."""

    bank_statement_months: list[str] = Field(default_factory=list)
    bank_statement_required_months: int = 6
    bank_statement_file_count: int = 0
    bank_statement_accepted_count: int = 0
    bank_statement_processing_count: int = 0
    bank_statement_needs_more_count: int = 0
    bank_statement_rejected_count: int = 0
    bank_statement_failed_count: int = 0
    bank_statement_coverage_complete: bool = False


class ApplicationProgramReadiness(BaseModel):
    profile_id: UUID
    lending_applicable: bool = True
    selection_mode: Literal["auto", "manual"] = "auto"
    selections: list[ApplicationProgramSelectionRead] = Field(default_factory=list)
    evidence_policies: list[EvidencePolicySelectionRead] = Field(default_factory=list)
    candidates: list[ProgramFitCandidate] = Field(default_factory=list)
    programs: list[ProgramReadinessItem] = Field(default_factory=list)
    requirements: list[ApplicationRequirementRead] = Field(default_factory=list)
    available_evidence_files: list[ApplicationEvidenceOptionRead] = Field(default_factory=list)
    evidence_summary: ApplicationEvidenceSummary = Field(default_factory=ApplicationEvidenceSummary)
    can_advance: bool = False
    automatic_stage_status: Literal[
        "not_ready", "ready", "advanced", "already_in_underwriting", "not_applicable"
    ] = "not_ready"
    automation: MissingItemAutomationRead


class ApplicationRequirementAIReviewResult(BaseModel):
    readiness: ApplicationProgramReadiness
    reviewed_file_count: int = 0
    verified_file_count: int = 0
    already_verified_count: int = 0
    retained_for_staff_count: int = 0
    analysis_required_count: int = 0


class ApplicationProgramsPatch(BaseModel):
    program_keys: list[str] = Field(default_factory=list, max_length=20)
    return_to_ai: bool = False
    confirmed: Literal[True]
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _validate_program_action(self) -> ApplicationProgramsPatch:
        if self.return_to_ai and self.program_keys:
            raise ValueError("Return to AI selection cannot include manual program keys")
        return self


class ApplicationRequirementPatch(BaseModel):
    action: Literal[
        "link_evidence",
        "unlink_evidence",
        "verify",
        "unverify",
        "waive",
        "not_applicable",
        "restore",
        "failed",
    ]
    evidence_file_id: UUID | None = None
    evidence_file_ids: list[UUID] = Field(default_factory=list, max_length=100)
    program_keys: list[str] = Field(default_factory=list)
    all_programs: bool = False
    reason: str | None = Field(default=None, max_length=2000)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def _validate_requirement_action(self) -> ApplicationRequirementPatch:
        selected_ids = set(self.evidence_file_ids)
        if self.evidence_file_id is not None:
            selected_ids.add(self.evidence_file_id)
        if self.action in {"link_evidence", "unlink_evidence"} and not selected_ids:
            raise ValueError("Select at least one evidence file")
        self.evidence_file_ids = list(selected_ids)
        if self.action in {"waive", "not_applicable"} and len((self.reason or "").strip()) < 8:
            raise ValueError("A reason of at least eight characters is required")
        if self.all_programs and self.program_keys:
            raise ValueError("Choose specific programs or all programs, not both")
        return self


class ApplicationRequirementReminder(BaseModel):
    channel: Literal["email"] = "email"
    retry_failed: bool = False


class ApplicationRequirementBatchReminder(BaseModel):
    requirement_keys: list[str] = Field(min_length=1, max_length=25)
    channel: Literal["email"] = "email"
    retry_failed: bool = False

    @field_validator("requirement_keys")
    @classmethod
    def _deduplicate_requirement_keys(cls, value: list[str]) -> list[str]:
        keys = list(dict.fromkeys(key.strip() for key in value if key.strip()))
        if not keys:
            raise ValueError("Select at least one requirement")
        return keys


class ApplicationRequirementAIReview(BaseModel):
    requirement_keys: list[str] = Field(default_factory=list, max_length=25)
    confirmed: Literal[True]

    @field_validator("requirement_keys")
    @classmethod
    def _deduplicate_review_keys(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(key.strip() for key in value if key.strip()))


class EvidenceDecisionOverride(BaseModel):
    decision: Literal["accepted", "rejected", "needs_more"]
    reason_code: Literal[
        "wrong_document",
        "wrong_entity",
        "wrong_period",
        "incomplete",
        "unreadable",
        "duplicate",
        "other",
        "ai_override",
    ]
    reason: str = Field(min_length=8, max_length=2000)
    confirmed: Literal[True]


class EvidenceReanalyzeResult(BaseModel):
    file_id: UUID
    queued: bool = True
    status: Literal["queued"] = "queued"


class MissingItemAutomationPatch(BaseModel):
    enabled: bool


class ApplicationUnderwritingRead(BaseModel):
    profile_id: UUID
    source_kind: ApplicationSourceKind | None = None
    source_id: UUID | None = None
    loan_id: UUID | None = None
    underwriting_status: UnderwritingLifecycleStatus = "collecting_docs"
    approved_amount: float | None = None
    term_sheet_amount: float | None = None
    current_dscr: float | None = None
    target_dscr: float | None = None
    approved_dscr: float | None = None
    close_outcome: str | None = None
    reviewer_notes: str | None = None
    updated_by_user_id: UUID | None = None
    updated_at: datetime | None = None


class ApplicationUnderwritingPatch(BaseModel):
    underwriting_status: UnderwritingLifecycleStatus | None = None
    approved_amount: float | None = Field(default=None, ge=0)
    term_sheet_amount: float | None = Field(default=None, ge=0)
    current_dscr: float | None = Field(default=None, ge=0)
    target_dscr: float | None = Field(default=None, ge=0)
    approved_dscr: float | None = Field(default=None, ge=0)
    close_outcome: str | None = Field(default=None, max_length=32)
    reviewer_notes: str | None = Field(default=None, max_length=5000)


class FileOwnerCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=48)
    ownership_pct: float | None = Field(default=None, ge=0, le=100)
    is_primary: bool = False
    is_guarantor: bool = True
    dob: date | None = None
    street: str | None = Field(default=None, max_length=240)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=8)
    zip: str | None = Field(default=None, max_length=12)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("first_name", "last_name")
    @classmethod
    def trim_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Required")
        return value


class FileOwnerPatch(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=80)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=48)
    ownership_pct: float | None = Field(default=None, ge=0, le=100)
    is_guarantor: bool | None = None
    dob: date | None = None
    street: str | None = Field(default=None, max_length=240)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=8)
    zip: str | None = Field(default=None, max_length=12)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("first_name", "last_name")
    @classmethod
    def trim_optional_required(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Required")
        value = value.strip()
        if not value:
            raise ValueError("Required")
        return value


class FileOwnerRead(BaseModel):
    id: UUID
    full_name: str
    first_name: str
    last_name: str
    email: str | None = None
    phone: str | None = None
    ownership_pct: float | None = None
    is_primary: bool = False
    is_guarantor: bool = True
    dob: date | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    invite_sent_at: datetime | None = None
    invite_opened_at: datetime | None = None
    has_invite: bool = False
    credit_score: int | None = None
    credit_tier: str | None = None
    credit_pulled_at: datetime | None = None
    credit_required: bool = False
    credit_complete: bool = False
    credit_contact_complete: bool = False
    backfill_needs_review: bool = False
    source: Literal["application", "dealer"] = "application"


class FileOwnerRequirementState(BaseModel):
    ownership_total: float = 0
    ownership_complete: bool = False
    owner_contact_complete: bool = False
    owner_count: int = 0
    required_credit_owner_count: int = 0
    completed_credit_owner_count: int = 0
    pending_credit_owner_ids: list[UUID] = Field(default_factory=list)
    missing_credit_contact_owner_ids: list[UUID] = Field(default_factory=list)
    bank_linked: bool = False
    bank_connection_count: int = 0
    bank_statement_months: int = 0
    credit_returned: bool = False
    owner_credit_complete: bool = False
    business_banking_complete: bool = False
    evidence_complete: bool = False
    ready_for_step_2: bool = False
    unlocked: bool = False
    ownership_blockers: list[str] = Field(default_factory=list)
    credit_blockers: list[str] = Field(default_factory=list)
    banking_blockers: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class FileCreditInviteRequest(BaseModel):
    channel: Literal["email", "sms", "none"] = "email"


class FileCreditInviteRead(BaseModel):
    owner_id: UUID
    owner_name: str
    token: str | None = None
    path: str | None = None
    delivered: bool = False
    channel: str = "none"
    detail: str = ""


class FileCreditInviteBatch(BaseModel):
    items: list[FileCreditInviteRead] = Field(default_factory=list)


class ApplicationBankConnectionRead(BaseModel):
    id: UUID
    institution_name: str | None = None
    accounts_label: str | None = None
    status: str
    environment: str = "sandbox"
    error: str | None = None
    update_mode_reason: str | None = None
    update_mode_account_selection: bool = False
    auto_refresh: bool = True
    is_primary_operating: bool = False
    last_pulled_at: datetime | None = None
    next_refresh_at: datetime | None = None
    statement_months: list[str] = Field(default_factory=list)
    source: Literal["application", "dealer"] = "application"
    products: list[str] = Field(default_factory=list)
    consented_products: list[str] = Field(default_factory=list)
    billed_products: list[str] = Field(default_factory=list)
    unavailable_products: list[str] = Field(default_factory=list)
    pending_products: list[str] = Field(default_factory=list)
    authorization_state: str = "checking"
    products_checked_at: datetime | None = None


class PlaidAssetReportRead(BaseModel):
    id: UUID
    status: str
    environment: str
    days_requested: int
    error: str | None = None
    ready_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApplicationBankState(BaseModel):
    enabled: bool = False
    environment: str = "disabled"
    consent_granted: bool = False
    disclosure_version: str
    disclosure_text: str
    items: list[ApplicationBankConnectionRead] = Field(default_factory=list)
    manual_override: bool = False
    manual_override_reason: str | None = None
    manual_statement_months: list[str] = Field(default_factory=list)
    manual_statement_file_count: int = 0
    manual_statement_accepted_count: int = 0
    manual_statement_pending_count: int = 0
    manual_statement_rejected_count: int = 0
    manual_statement_failed_count: int = 0
    evidence_summary: ApplicationEvidenceSummary = Field(default_factory=ApplicationEvidenceSummary)
    assets_enabled: bool = False
    statements_enabled: bool = False
    selected_products: list[str] = Field(default_factory=list)
    available_products: list[str] = Field(default_factory=list)
    consent_product_scope: list[str] = Field(default_factory=list)
    connections_requiring_client_authorization: int = 0
    plaid_policy_updated_at: datetime | None = None
    plaid_policy_updated_by_user_id: UUID | None = None
    asset_reports: list[PlaidAssetReportRead] = Field(default_factory=list)


class BusinessBankEvidence(BaseModel):
    source: Literal["none", "plaid", "uploaded_statements", "mixed"] = "none"
    connected_institutions: int = 0
    banking_access_complete: bool = False
    accepted_statement_months: list[str] = Field(default_factory=list)
    required_statement_months: int = 6
    statement_coverage_complete: bool = False
    processing_files: int = 0
    needs_attention_files: int = 0
    reconnect_required: bool = False


class ClientEvidenceRequirementRead(BaseModel):
    requirement_key: str
    label: str
    required_level: Literal["required", "recommended", "optional"]
    status: RequirementStateStatus
    complete: bool = False
    evidence_count: int = 0
    accepted_evidence_count: int = 0
    processing_evidence_count: int = 0
    coverage: dict = Field(default_factory=dict)


class ClientEvidenceBankingSummary(BaseModel):
    """Client-safe evidence state without program candidates or staff criteria."""

    requirements: list[ClientEvidenceRequirementRead] = Field(default_factory=list)
    required_count: int = 0
    completed_required_count: int = 0
    missing_required_count: int = 0
    processing_file_count: int = 0
    supporting_group_id: UUID | None = None
    supporting_group_name: str | None = None
    supporting_file_count: int = 0
    bank_evidence: BusinessBankEvidence = Field(default_factory=BusinessBankEvidence)


class ApplicationPlaidSettingsPatch(BaseModel):
    assets_enabled: bool
    statements_enabled: bool
    acknowledged: Literal[True]
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _one_product_required(self) -> ApplicationPlaidSettingsPatch:
        if not self.assets_enabled and not self.statements_enabled:
            raise ValueError("At least one Plaid product must remain enabled")
        return self


class ManualBankOverrideRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=1000)


class ApplicationBankConsentGrant(BaseModel):
    granted: bool = True
    method: Literal["electronic"] = "electronic"
    consenter_name: str = Field(min_length=2, max_length=160)


class ApplicationPlaidLinkTokenRead(BaseModel):
    link_token: str


class ApplicationPlaidUpdateLinkRequest(BaseModel):
    account_selection_enabled: bool = False


class PlaidAssetReportCreate(BaseModel):
    days_requested: int = Field(default=210, ge=0, le=731)


class ApplicationPlaidExchange(BaseModel):
    public_token: str = Field(min_length=1)
    institution_name: str | None = Field(default=None, max_length=160)
    is_primary_operating: bool | None = None


class ApplicationPlaidItemPatch(BaseModel):
    auto_refresh: bool | None = None
    is_primary_operating: bool | None = None


class ApplicationPlaidRefreshRead(BaseModel):
    pulled: int = 0
    skipped: int = 0
    failed: int = 0


class ClassificationPatch(BaseModel):
    vertical: ApplicationVertical
    funding_category: str | None = Field(default=None, max_length=64)
    entity_type: str | None = Field(default=None, max_length=32)
    industry: str | None = Field(default=None, max_length=80)
    subindustry: str | None = Field(default=None, max_length=120)
    naics_code: str | None = Field(default=None, max_length=8)
    naics_label: str | None = Field(default=None, max_length=180)
    custom_industry: str | None = Field(default=None, max_length=180)
    industry_entry_id: UUID | None = None
    subindustry_entry_id: UUID | None = None
    activity_entry_id: UUID | None = None

    @field_validator("naics_code")
    @classmethod
    def validate_naics(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if value and (not value.isdigit() or len(value) != 6):
            raise ValueError("NAICS/PBA activity codes must contain exactly six digits")
        return value or None


class TaxonomyPathEntry(BaseModel):
    id: UUID
    level: Literal[2, 3, 6]
    code: str | None = None
    label: str
    parent_id: UUID | None = None


class TaxonomyEntryRead(BaseModel):
    id: UUID
    level: Literal[2, 3, 6]
    code: str | None = None
    label: str
    parent_id: UUID | None = None
    source: str
    taxonomy_version: str
    status: str
    aliases: list[str] = Field(default_factory=list)
    originating_profile_id: UUID | None = None
    canonical_entry_id: UUID | None = None
    path: list[TaxonomyPathEntry] = Field(default_factory=list)


class TaxonomySearchRead(BaseModel):
    items: list[TaxonomyEntryRead] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


class TaxonomyContributionCreate(BaseModel):
    level: Literal[2, 3, 6]
    label: str = Field(min_length=2, max_length=180)
    code: str | None = Field(default=None, max_length=6)
    parent_id: UUID | None = None

    @field_validator("code")
    @classmethod
    def contribution_code(cls, value: str | None, info) -> str | None:
        value = (value or "").strip()
        if info.data.get("level") == 6 and (len(value) != 6 or not value.isdigit()):
            raise ValueError("A custom activity requires a six-digit code")
        return value or None


class TaxonomyReviewRequest(BaseModel):
    action: Literal["approve", "edit", "reject", "merge", "map"]
    canonical_entry_id: UUID | None = None
    label: str | None = Field(default=None, min_length=2, max_length=180)
    code: str | None = Field(default=None, max_length=8)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("code")
    @classmethod
    def normalize_review_code(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if value and not value.isdigit():
            raise ValueError("Classification codes must contain digits only")
        return value or None


class FundingCategoryRead(BaseModel):
    id: UUID
    vertical: str
    slug: str
    label: str
    status: str
    is_system: bool = False


class FundingCategoryCreate(BaseModel):
    vertical: ApplicationVertical
    label: str = Field(min_length=2, max_length=120)


class ExtractedFactRead(BaseModel):
    id: UUID
    field_key: str
    value: dict
    normalized_value: str | None = None
    confidence: float | None = None
    source_file_id: UUID | None = None
    status: str
    extraction_method: str
    created_at: datetime


class ExtractedFactReview(BaseModel):
    action: Literal["accept", "reject"]


class ApplicationDraftAnalysisStatus(BaseModel):
    profile_id: UUID
    uploaded_file_count: int = 0
    analyzed_file_count: int = 0
    processing_file_count: int = 0
    failed_file_count: int = 0
    suggested_fact_count: int = 0
    reviewed_fact_count: int = 0
    can_finalize: bool = False


class VerificationInvitationCreate(BaseModel):
    channel: Literal["email", "sms", "none"] = "email"
    recipient_email: EmailStr | None = None
    recipient_phone: str | None = Field(default=None, max_length=48)


class VerificationInvitationRead(BaseModel):
    id: UUID
    path: str
    token: str | None = None
    delivery_status: str
    expires_at: datetime


class RoomPinRotateRequest(BaseModel):
    secure_room_pin: str = Field(pattern=r"^\d{6}$")


class RoomDeliveryReceipt(BaseModel):
    id: UUID
    requested_document_id: UUID | None = None
    action_kind: str
    channel: str
    recipient_masked: str | None = None
    status: str
    detail: str | None = None
    provider_accepted: bool = False
    initiation_source: str | None = None
    attempt_number: int = 1
    scheduled_for: datetime | None = None
    created_at: datetime
    requirement_keys: list[str] = Field(default_factory=list)


class RoomRequestCreate(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    category: str | None = Field(default=None, max_length=100)
    instructions: str | None = Field(default=None, max_length=2000)
    allow_multiple_files: bool = False
    recipient_email: EmailStr | None = None
    recipient_phone: str | None = Field(default=None, max_length=48)
    email_room_link: bool = True
    sms_reminder: bool = False


class RoomReminderCreate(BaseModel):
    purpose: Literal["documents", "business_banking", "room"] = "room"
    recipient_email: EmailStr | None = None
    recipient_phone: str | None = Field(default=None, max_length=48)
    email_room_link: bool = True
    sms_reminder: bool = False


class RoomRequestResult(BaseModel):
    requested_document_id: UUID | None = None
    room_url: str
    overall_status: Literal["created", "success", "partial", "failed"]
    deliveries: list[RoomDeliveryReceipt] = Field(default_factory=list)


class ApplicationRoomAccess(BaseModel):
    passcode: str = Field(min_length=6, max_length=16)


class ApplicationRoomConsentGrant(ApplicationRoomAccess):
    granted: bool = True
    consenter_name: str = Field(min_length=2, max_length=160)


class ApplicationRoomPlaidExchange(ApplicationRoomAccess):
    public_token: str = Field(min_length=1)
    institution_name: str | None = Field(default=None, max_length=160)
    is_primary_operating: bool | None = None


class ApplicationRoomPlaidUpdate(ApplicationRoomAccess):
    account_selection_enabled: bool = False


class ApplicationRoomPrimaryBank(ApplicationRoomAccess):
    is_primary_operating: Literal[True] = True


class ApplicationRoomOwnerCreate(ApplicationRoomAccess):
    owner: FileOwnerCreate


class ApplicationRoomOwnerPatch(ApplicationRoomAccess):
    owner: FileOwnerPatch


class ApplicationRoomCreditInvite(ApplicationRoomAccess):
    # The owner has not granted SMS consent inside the shared business room;
    # keep this private authorization link on email until the owner consents.
    channel: Literal["email"] = "email"


class ApplicationRoomPrecallState(BaseModel):
    status: Literal["in_progress", "complete", "stopped", "disabled"] = "disabled"
    complete: bool = False
    done_count: int = 0
    missing: list[str] = Field(default_factory=list)


class ApplicationRoomSignable(BaseModel):
    id: UUID
    name: str
    kind: str | None = None
    signed: bool = False
    signable: bool = False
    document_text: str = ""


class ApplicationRoomSignRequest(ApplicationRoomAccess):
    requested_document_id: UUID
    typed_name: str = Field(min_length=1, max_length=160)
    esign_consent: bool
    signature_data_url: str = Field(min_length=1)
    applicant_legal_first_name: str | None = Field(default=None, max_length=120)
    applicant_legal_last_name: str | None = Field(default=None, max_length=120)
    applicant_dob: str | None = Field(default=None, max_length=32)
    applicant_street: str | None = Field(default=None, max_length=240)
    applicant_city: str | None = Field(default=None, max_length=120)
    applicant_state: str | None = Field(default=None, max_length=2)
    applicant_zip: str | None = Field(default=None, max_length=10)


class ApplicationRoomSignResult(BaseModel):
    signed: bool
    certificate_file_id: UUID | None = None
    message: str


class ApplicationRoomMerchantOfferSummary(BaseModel):
    """Enough for the room to show its "Your offer" tab; the offer itself is
    fetched from its own endpoint."""

    id: UUID
    status: str
    estimated_annual_savings: float | None = None
    client_response: str | None = None


class ApplicationRoomState(BaseModel):
    profile_id: UUID
    business_name: str
    room_url: str
    capabilities: list[str] = Field(default_factory=list)
    owners: list[FileOwnerRead] = Field(default_factory=list)
    verification: FileOwnerRequirementState
    precall: ApplicationRoomPrecallState | None = None
    banking: ApplicationBankState
    evidence_banking_summary: ClientEvidenceBankingSummary = Field(
        default_factory=ClientEvidenceBankingSummary
    )
    signable: list[ApplicationRoomSignable] = Field(default_factory=list)
    merchant_offer: ApplicationRoomMerchantOfferSummary | None = None


class ApplicationRoomMerchantOfferRespond(ApplicationRoomAccess):
    response: Literal["accepted", "declined"]
    responder_name: str = Field(min_length=1, max_length=160)
    reason: str | None = Field(default=None, max_length=2000)
    #: The version the client was looking at; a stale one is refused.
    terms_version: int = Field(ge=1)


class PublicBankVerificationRead(BaseModel):
    business_name: str
    disclosure_version: str
    disclosure_text: str
    consent_granted: bool = False
    items: list[ApplicationBankConnectionRead] = Field(default_factory=list)
    manual_statement_months: list[str] = Field(default_factory=list)
    manual_statement_file_count: int = 0
    manual_statement_accepted_count: int = 0
    manual_statement_pending_count: int = 0
    manual_statement_rejected_count: int = 0
    manual_statement_failed_count: int = 0
    evidence_summary: ApplicationEvidenceSummary = Field(default_factory=ApplicationEvidenceSummary)
    assets_enabled: bool = False
    statements_enabled: bool = False
    selected_products: list[str] = Field(default_factory=list)
    available_products: list[str] = Field(default_factory=list)
    consent_product_scope: list[str] = Field(default_factory=list)
    asset_reports: list[PlaidAssetReportRead] = Field(default_factory=list)
    statement_upload_enabled: bool = False
    expires_at: datetime


class SecureBankFileUploadInit(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=160)
    size_bytes: int = Field(gt=0, le=100 * 1024 * 1024)
    requested_document_id: UUID | None = None


class SecureBankFileUploadComplete(BaseModel):
    file_id: UUID
    note: str | None = Field(default=None, max_length=2000)


class IntelligenceMetric(BaseModel):
    key: str
    label: str
    applicable: bool = True
    value: float | str | None = None
    unit: str | None = None
    status: Literal["ready", "needs_evidence", "not_applicable"]
    confidence: float | None = None
    period: str | None = None
    source: str | None = None
    action: str | None = None


class ApplicationIntelligenceRead(BaseModel):
    profile_id: UUID
    metrics: list[IntelligenceMetric] = Field(default_factory=list)
    dscr_inputs: dict = Field(default_factory=dict)


class ClassificationPreview(BaseModel):
    profile_id: UUID
    current_revision: int
    before: dict
    after: dict
    effects: list[str]
    requires_confirmation: bool = True


class ClassificationConfirm(ClassificationPatch):
    expected_revision: int = Field(ge=1)


class EvidenceSourceRead(BaseModel):
    id: str
    kind: str
    relationship: str
    label: str
    bucket_id: UUID | None = None
    active_file_count: int = 0
    selected_file_count: int = 0
    accessible_file_count: int = 0


class EvidenceFileRead(BaseModel):
    id: UUID
    source_id: str
    bucket_id: UUID
    file_name: str
    content_type: str
    size_bytes: int
    selected: bool = True
    included_in_review: bool = True
    preview_url: str | None = None
    created_at: datetime


class ApplicationEvidenceRead(BaseModel):
    profile_id: UUID
    sources: list[EvidenceSourceRead] = Field(default_factory=list)
    files: list[EvidenceFileRead] = Field(default_factory=list)
    total_files: int = 0
    review_file_count: int = 0
    blockers: list[str] = Field(default_factory=list)


class UnifiedAuditEvent(BaseModel):
    id: str
    occurred_at: datetime
    action: str
    summary: str
    actor_name: str | None = None
    actor_role: str | None = None
    source: str
    metadata: dict = Field(default_factory=dict)


class PublicFileOwnerConsentRead(BaseModel):
    first_name: str
    last_name: str
    last_initial: str
    email: str
    phone: str
    business_name: str
    fields_needed: list[str] = Field(default_factory=list)
    completed: bool = False


class PublicFileOwnerConsentSubmit(BaseModel):
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
    ssn: str | None = None

    @field_validator("ssn")
    @classmethod
    def ssn_digits(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        digits = "".join(c for c in value if c.isdigit())
        if len(digits) != 9:
            raise ValueError("SSN must contain 9 digits")
        return digits


class PublicFileOwnerConsentResult(BaseModel):
    completed: bool
    credit_tier: str | None = None
    credit_score_band: str | None = None


class FinancialStatementOwnerLink(BaseModel):
    """Which applicant a statement speaks for, in whichever table they live in.

    Owners are split across `application_owners` and `dos_owners` depending on
    how the file was opened; `owner_storage` on the profile says which, and the
    same discriminator is echoed here so a caller never has to guess.
    """

    owner_id: UUID
    #: Echoed on reads so a caller never has to guess which table an owner sits
    #: in. Ignored on writes — the server decides it from the file, because a
    #: browser cannot know and a wrong value would point the link at the wrong
    #: table.
    storage: Literal["application", "dealer"] = "application"
    name: str | None = None


class FinancialStatementRead(BaseModel):
    id: UUID
    profile_id: UUID
    statement_date: date | None = None
    schema_version: str
    status: str
    body: dict
    total_assets: float = 0
    total_liabilities: float = 0
    net_worth: float = 0
    liquid_assets: float = 0
    submitted_at: datetime | None = None
    #: True when a staff member completed it for the borrower rather than the
    #: borrower filling it in themselves.
    filled_by_staff: bool = False
    bucket_file_id: UUID | None = None
    owners: list[FinancialStatementOwnerLink] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class FinancialStatementWrite(BaseModel):
    body: dict
    statement_date: str | None = None
    #: Owners this statement covers. A joint statement — one sheet for a married
    #: couple — is simply two entries.
    owners: list[FinancialStatementOwnerLink] = Field(default_factory=list)


class UploadedStatementFigures(BaseModel):
    """One personal financial statement, as the file analyzer read it.

    Kept as a list rather than folded into a single total because a PFS belongs
    to one person. Two documents on a file are two people, and adding their net
    worth together would state a household balance sheet nobody signed.
    """

    statement_date: str | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None
    net_worth: float | None = None
    #: Cash, deposits and marketable securities only. Carried through because it
    #: is the figure programme eligibility is screened on, not a nicety.
    liquid_assets: float | None = None


class FinancialFormStatus(BaseModel):
    """How one of the two financial forms stands on this file.

    `filled` means someone typed it into our form and we hold the figures;
    `uploaded` means a document satisfies the request and we do not. The
    distinction is the whole point of tracking it: only the first can be
    reopened, corrected, or handed back to the borrower to finish.
    """

    kind: Literal["pfs", "debt_schedule", "p_and_l", "balance_sheet"]
    label: str
    #: Whether the checklist actually asks for it on this file. A form nobody
    #: has requested is not outstanding — it is simply not part of this deal.
    requested: bool = False
    satisfied: bool = False
    source: Literal["filled", "uploaded", "none"] = "none"
    #: The statement to open, for a PFS that was filled in.
    statement_id: UUID | None = None
    #: Rows on the business debt schedule, and what they come to.
    row_count: int = 0
    total_monthly: float = 0
    total_balance: float = 0
    net_worth: float | None = None
    updated_at: datetime | None = None
    #: True when a staff member completed it rather than the borrower.
    filled_by_staff: bool = False
    #: Where these figures came from. "form" means someone typed them in and we
    #: hold the rows; "document" means the analyzer read them off an upload.
    #: None when we hold no figures at all — which is not the same as zero.
    figures_from: Literal["form", "document"] | None = None
    #: Every personal financial statement read off an upload, one per document.
    #: `net_worth` above mirrors the single entry when there is exactly one, so
    #: a caller that only wants the headline figure does not have to unpack this.
    statements: list[UploadedStatementFigures] = Field(default_factory=list)
    #: A document satisfies the slot but has not been read yet. The figures are
    #: on their way rather than missing, and the caller should come back.
    analysis_pending: bool = False
    #: The two business statements. "Jan–Jun 2026" / "as of 2026-06-30", the
    #: headline figures, and for a balance sheet whether it balances — from the
    #: form when filled, from the extractor when an upload was recognised.
    period_label: str | None = None
    net_income: float | None = None
    ebitda: float | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None
    total_equity: float | None = None
    balances: bool | None = None


class FinancialFormPacket(BaseModel):
    """One forwardable link that opens all four forms: four child links that
    share a `packet_id`. Listed so the desk can see what is out and close it."""

    packet_id: UUID
    created_at: datetime | None = None
    expires_at: datetime | None = None
    completed_kinds: list[str] = Field(default_factory=list)
    revoked: bool = False


class FinancialFormSave(BaseModel):
    """A form body from the desk. `submit` is the difference between keeping the
    figures and filing the sheet on the checklist."""

    body: dict
    submit: bool = False


class FinancialFormsRead(BaseModel):
    forms: list[FinancialFormStatus] = Field(default_factory=list)
    packets: list[FinancialFormPacket] = Field(default_factory=list)


class SupportingDocumentGroupRead(BaseModel):
    id: UUID
    bucket_id: UUID
    name: str
    description: str | None = None
    required: bool = False
    allow_multiple_files: bool = True
    status: str = "requested"
    file_count: int = 0


class EvidenceProcessingSummary(BaseModel):
    total_files: int = 0
    analyzing_files: int = 0
    accepted_files: int = 0
    needs_attention_files: int = 0
    failed_files: int = 0
    has_processing: bool = False


class ApplicationEvidenceWorkspace(BaseModel):
    profile_id: UUID
    primary_bucket_id: UUID | None = None
    primary_bucket_name: str | None = None
    program_readiness: ApplicationProgramReadiness
    verification: FileOwnerRequirementState
    banking: ApplicationBankState
    bank_evidence: BusinessBankEvidence
    forms: FinancialFormsRead
    evidence: ApplicationEvidenceRead
    supporting_group: SupportingDocumentGroupRead | None = None
    processing: EvidenceProcessingSummary
    can_manage_evidence: bool = False
    can_manage_plaid_settings: bool = False
    can_upload: bool = False


# ---------------------------------------------------------------------------
# The worksheet: the four forms as one grid
# ---------------------------------------------------------------------------

SheetKind = Literal["p_and_l", "balance_sheet", "debt_schedule", "pfs"]


class WorksheetCellEdit(BaseModel):
    """One cell. `key` is the save key the layout published for that cell —
    never the row and column it was drawn at, so a client rendering a stale
    layout cannot write a figure into the wrong line."""

    sheet: SheetKind
    key: str
    #: Raw text, exactly as typed. Parsing money and dates is the schema's job
    #: on save, not the browser's, so "1,250" and "(500)" mean the same here as
    #: they do on the stacked form.
    value: str | None = None


class WorksheetCellWrite(BaseModel):
    """A batch of cells and where the client believed the workbook stood.

    `base_rev` is per sheet. It is not a lock: a stale one still lands, because
    two people typing in different cells is the design, not a conflict. It is
    read only to notice a client so far behind that patching it would show
    figures nobody entered.
    """

    edits: list[WorksheetCellEdit] = Field(default_factory=list)
    base_rev: dict[str, int] = Field(default_factory=dict)


class WorksheetRowOp(BaseModel):
    """Add or remove a line on one of the two list-shaped sheets."""

    sheet: SheetKind
    op: Literal["insert", "delete"]
    #: The row to remove, or (on an insert) the row the new line follows.
    row_id: str | None = None
    after: str | None = None
    #: Which supporting schedule, on the personal financial statement. The debt
    #: schedule is one list and ignores it.
    block: str | None = None
    #: How many lines the grid is showing for that list, blanks included. The
    #: server pads a short list up to this before adding or removing, so the
    #: count always moves by one from the picture the person is looking at.
    #: Left out by an older client, which falls back to the read's own floor.
    visible: int | None = Field(default=None, ge=0, le=10_000)


class WorksheetLinkCreate(BaseModel):
    """What a share link opens, and what it lets the holder do.

    Both are stored on the link rather than derived from its token. The packet's
    `{base}.{kind}` derivation means any child token yields the base and the
    base yields every child, so "this link opens the P&L only" could not be
    true; an independent token plus a stored scope makes it true.
    """

    permission: Literal["edit", "view"] = "view"
    sheets: list[SheetKind] = Field(default_factory=list)
    ttl_days: int | None = None
    label: str | None = None
    invitee_email: str | None = None
