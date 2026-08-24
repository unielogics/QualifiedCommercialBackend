from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

ApplicationSourceKind = Literal["deal", "loan", "intake", "dealer"]
ApplicationVertical = Literal["real_estate", "main_street", "dealer", "mca"]


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
    vertical: str
    funding_category: str | None = None
    entity_type: str | None = None
    industry: str | None = None
    naics_code: str | None = None
    naics_label: str | None = None
    custom_industry: str | None = None
    classification_revision: int
    classification_state: dict | None = None
    classified_at: datetime | None = None
    backfill_needs_review: bool = False
    owner_storage: Literal["application", "dealer"]


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
    ready_for_step_2: bool = False
    unlocked: bool = False
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
    error: str | None = None
    auto_refresh: bool = True
    is_primary_operating: bool = False
    last_pulled_at: datetime | None = None
    next_refresh_at: datetime | None = None
    statement_months: list[str] = Field(default_factory=list)
    source: Literal["application", "dealer"] = "application"


class ApplicationBankState(BaseModel):
    enabled: bool = False
    environment: str = "disabled"
    consent_granted: bool = False
    disclosure_version: str
    disclosure_text: str
    items: list[ApplicationBankConnectionRead] = Field(default_factory=list)


class ApplicationBankConsentGrant(BaseModel):
    granted: bool = True
    method: Literal["electronic"] = "electronic"
    consenter_name: str = Field(min_length=2, max_length=160)


class ApplicationPlaidLinkTokenRead(BaseModel):
    link_token: str


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
    naics_code: str | None = Field(default=None, max_length=8)
    naics_label: str | None = Field(default=None, max_length=180)
    custom_industry: str | None = Field(default=None, max_length=180)


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
    last_initial: str
    business_name: str
    fields_needed: list[str] = Field(default_factory=list)
    completed: bool = False


class PublicFileOwnerConsentSubmit(BaseModel):
    fcra_consent: bool = False
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
