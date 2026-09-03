"""Production Package request/response shapes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.application_profile import UnifiedAuditEvent


class ProductionPackageResolve(BaseModel):
    profile_id: UUID


class ProductionPackagePatch(BaseModel):
    version: int
    changes: dict[str, Any] = Field(default_factory=dict)
    # Field keys the editor is confirming as correct (prefilled values).
    confirm: list[str] = Field(default_factory=list)


class ProductionPrefillRequest(BaseModel):
    force: bool = False
    fields: list[str] | None = None
    apply: bool = True


class ProductionComputeRequest(BaseModel):
    arrangement: dict[str, Any] = Field(default_factory=dict)


class ProductionComputeRead(BaseModel):
    computed: dict[str, Any]
    attention: list[dict[str, Any]]
    attention_presentation: list[dict[str, Any]]


class SponsorAgreementRead(BaseModel):
    id: UUID
    contract_number: str
    document_version: str
    signed_at: datetime | None
    signer_name: str | None = None
    signer_title: str | None = None
    # Presigned only for super admins; None otherwise.
    certificate_url: str | None = None
    admin_url: str | None = None


class SponsorOptionRead(BaseModel):
    company_id: UUID
    name: str
    entity_type: str | None = None
    state_of_formation: str | None = None
    principal_address: str | None = None
    notice_email: str | None = None
    notice_attention: str | None = None
    agreement: SponsorAgreementRead | None = None


class ProductionShareLinkCreate(BaseModel):
    rep_user_id: UUID
    label: str | None = Field(default=None, max_length=120)
    expires_in_days: int = Field(default=14, ge=1, le=30)
    outside_book: bool = False


class ProductionShareLinkRead(BaseModel):
    id: UUID
    rep_user_id: UUID
    rep_name: str | None = None
    label: str | None = None
    outside_book: bool = False
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    use_count: int = 0
    active: bool = True


class ProductionShareLinkCreated(BaseModel):
    link: ProductionShareLinkRead
    url: str
    expires_at: datetime


class ProductionSignatureRead(BaseModel):
    id: UUID
    party: Literal["dealer", "qc", "sponsor"]
    method: Literal["electronic", "manual"]
    status: Literal["pending", "signed", "voided"]
    expected_signer_name: str | None = None
    typed_name: str | None = None
    sent_at: datetime | None = None
    viewed_at: datetime | None = None
    signed_at: datetime | None = None
    signer_name: str | None = None
    signer_title: str | None = None
    signed_on: date | None = None
    recorded_at: datetime | None = None
    recorded_by_name: str | None = None
    scan_available: bool = False
    scan_url: str | None = None
    note: str | None = None
    voided_at: datetime | None = None
    void_reason: str | None = None


class ProductionRevisionRead(BaseModel):
    id: UUID
    revision_no: int
    stage: int
    status: str
    document_key: str
    document_title: str
    document_version: str
    content_sha256: str
    rendered_pdf_sha256: str | None = None
    current_pdf_sha256: str | None = None
    unsigned_url: str | None = None
    current_url: str | None = None
    executed_url: str | None = None
    sent_at: datetime | None = None
    completed_at: datetime | None = None
    voided_at: datetime | None = None
    void_reason: str | None = None
    sponsor_snapshot: dict[str, Any] | None = None
    signatures: list[ProductionSignatureRead] = Field(default_factory=list)


class ProductionPresentationRead(BaseModel):
    url: str | None = None
    sha256: str | None = None
    generated_at: datetime | None = None
    stale: bool = False
    available: bool = False


class ProductionCapabilities(BaseModel):
    can_edit: bool = False
    can_confirm: bool = False
    can_generate: bool = False
    can_send: bool = False
    can_reopen: bool = False
    can_void: bool = False
    can_record: bool = False
    can_execute: bool = False
    can_share: bool = False
    can_pick_sponsor: bool = False
    can_capture_consent: bool = False


class ProductionSmsConsentRead(BaseModel):
    phone: str | None = None
    status: Literal["granted", "missing", "opted_out", "no_phone"] = "no_phone"
    detail: str = ""


class ProductionPackageRead(BaseModel):
    id: UUID
    profile_id: UUID
    intake_id: UUID | None = None
    dealer_id: UUID | None = None
    stage: int
    status: str
    version: int
    business_name: str
    client_email: str | None = None
    client_phone: str | None = None
    arrangement: dict[str, Any]
    prefill_provenance: dict[str, Any]
    computed: dict[str, Any]
    attention: list[dict[str, Any]]
    attention_presentation: list[dict[str, Any]]
    sponsor: SponsorOptionRead | None = None
    presentation: ProductionPresentationRead
    active_revision: ProductionRevisionRead | None = None
    revisions: list[ProductionRevisionRead] = Field(default_factory=list)
    share_links: list[ProductionShareLinkRead] = Field(default_factory=list)
    delivery_history: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: ProductionCapabilities
    sms_consent: ProductionSmsConsentRead
    sent_at: datetime | None = None
    executed_at: datetime | None = None
    voided_at: datetime | None = None
    void_reason: str | None = None
    executed_url: str | None = None
    updated_at: datetime
    updated_by_name: str | None = None
    sponsor_signing_url: str
    mode: Literal["operator", "rep"] = "operator"


class ProductionSendRequest(BaseModel):
    channel: Literal["sms", "email"] = "sms"
    recipient_email: str | None = None
    recipient_phone: str | None = None


class ProductionSendResult(BaseModel):
    package: ProductionPackageRead
    delivered: bool
    emailed: bool
    texted: bool
    detail: str
    already_sent: bool = False


class ProductionReasonBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ProductionManualSignatureBody(BaseModel):
    party: Literal["qc", "sponsor"]
    signer_name: str = Field(min_length=2, max_length=160)
    signer_title: str = Field(min_length=1, max_length=120)
    signed_on: date
    attestation: bool
    note: str | None = Field(default=None, max_length=1000)
    override_reason: str | None = Field(default=None, max_length=300)
    # Optional scan: name + type start a presigned PUT; /complete records the hash.
    scan_file_name: str | None = Field(default=None, max_length=200)
    scan_content_type: str | None = Field(default=None, max_length=100)


class ProductionManualSignatureResult(BaseModel):
    signature: ProductionSignatureRead
    package: ProductionPackageRead
    scan_upload: dict[str, Any] | None = None


class ProductionScanCompleteBody(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)


class ProductionSmsConsentCapture(BaseModel):
    phone: str = Field(min_length=7, max_length=32)
    consenter_name: str = Field(min_length=2, max_length=160)
    method: Literal["rep_verbal", "web_form", "paper", "desk_capture"] = "rep_verbal"


class ProductionCapabilitiesRead(BaseModel):
    pdf: bool
    storage: bool


class ProductionHistoryRead(BaseModel):
    events: list[UnifiedAuditEvent]


# ---- client (intake room) ----

class ProductionSigningGateRead(BaseModel):
    package_id: UUID
    revision_id: UUID
    revision_no: int
    title: str
    document_version: str
    content_sha256: str
    pdf_sha256: str | None = None
    pdf_url: str | None = None
    signer_name: str
    signer_title: str | None = None
    business_name: str
    sent_at: datetime | None = None
    esign_consent_text: str
    esign_consent_version: str
    already_signed: bool = False


class ProductionClientSignBody(BaseModel):
    revision_id: UUID
    typed_name: str = Field(min_length=1, max_length=160)
    esign_consent: bool
    acknowledged: bool
    signature_data_url: str = Field(min_length=1)
    document_sha256: str = Field(min_length=64, max_length=64)


class ProductionClientSignResult(BaseModel):
    signed: bool
    signed_at: datetime | None
    pdf_sha256: str | None
    download_url: str | None
    execution_status: str
