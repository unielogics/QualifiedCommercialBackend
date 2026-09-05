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
    underwriting_status: UnderwritingLifecycleStatus = "submitted"
    underwriting_approved_amount: float | None = None
    underwriting_term_sheet_amount: float | None = None
    underwriting_current_dscr: float | None = None
    underwriting_target_dscr: float | None = None
    underwriting_approved_dscr: float | None = None
    underwriting_close_outcome: str | None = None
    underwriting_notes: str | None = None
    underwriting_updated_by_user_id: UUID | None = None
    underwriting_updated_at: datetime | None = None
    owner_storage: Literal["application", "dealer"]


class ApplicationUnderwritingRead(BaseModel):
    profile_id: UUID
    source_kind: ApplicationSourceKind | None = None
    source_id: UUID | None = None
    loan_id: UUID | None = None
    underwriting_status: UnderwritingLifecycleStatus = "submitted"
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
    manual_statement_pending_count: int = 0
    assets_enabled: bool = False
    statements_enabled: bool = False
    selected_products: list[str] = Field(default_factory=list)
    available_products: list[str] = Field(default_factory=list)
    consent_product_scope: list[str] = Field(default_factory=list)
    connections_requiring_client_authorization: int = 0
    plaid_policy_updated_at: datetime | None = None
    plaid_policy_updated_by_user_id: UUID | None = None
    asset_reports: list[PlaidAssetReportRead] = Field(default_factory=list)


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
    action_kind: str
    channel: str
    recipient_masked: str | None = None
    status: str
    detail: str | None = None
    provider_accepted: bool = False
    created_at: datetime


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


class ApplicationRoomState(BaseModel):
    profile_id: UUID
    business_name: str
    room_url: str
    capabilities: list[str] = Field(default_factory=list)
    banking: ApplicationBankState
    signable: list[ApplicationRoomSignable] = Field(default_factory=list)


class PublicBankVerificationRead(BaseModel):
    business_name: str
    disclosure_version: str
    disclosure_text: str
    consent_granted: bool = False
    items: list[ApplicationBankConnectionRead] = Field(default_factory=list)
    manual_statement_months: list[str] = Field(default_factory=list)
    manual_statement_file_count: int = 0
    manual_statement_pending_count: int = 0
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
    storage: Literal["application", "dealer"]
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


class FinancialStatementSlotRead(BaseModel):
    """How the personal-financials request on this file has been met.

    `filled` means someone typed it into our form and we hold the rows;
    `uploaded` means a document satisfies the slot and we do not. The
    distinction is the point: only the first can be reopened or corrected.
    """

    requested: bool = False
    satisfied: bool = False
    source: Literal["filled", "uploaded", "none"] = "none"
    statement_id: UUID | None = None
