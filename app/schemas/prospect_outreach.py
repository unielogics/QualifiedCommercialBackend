"""API contracts for Dealer Prospect email and collateral workflows."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, EmailStr, Field, TypeAdapter, field_validator, model_validator

from app.lead_types import LeadType

DraftPurpose = Literal[
    "information",
    "dealer_information",
    "missed_call",
    "callback_confirmation",
    "client_will_call_back",
    "booking",
    "general",
]

CCScope = Literal["this_email", "this_and_future"]
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def normalize_cc_emails(value: object) -> list[str] | None:
    """Accept chips or pasted comma/newline-separated CC addresses."""
    if value is None:
        return None
    raw_values: list[object]
    if isinstance(value, str):
        raw_values = re.split(r"[,;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raise ValueError("cc_emails must be a list or separated email addresses")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        candidate = str(raw or "").strip().lower()
        if not candidate:
            continue
        normalized = str(_EMAIL_ADAPTER.validate_python(candidate)).lower()
        if normalized not in seen:
            seen.add(normalized)
            cleaned.append(normalized)
    if len(cleaned) > 10:
        raise ValueError("No more than 10 CC recipients are allowed")
    return cleaned


class ProspectSenderPreviewRead(BaseModel):
    sender_display_name: str
    sender_title: str | None = None
    sender_phone: str | None = None
    sender_display_email: str
    sender_from_name: str
    envelope_from_email: str
    reply_contact_email: str
    alternate_contact_email: str | None = None


class ProspectOutreachPolicyPatch(BaseModel):
    drafting_guidance: str | None = Field(default=None, max_length=3000)
    additional_blocked_phrases: list[str] | None = Field(default=None, max_length=50)

    @field_validator("drafting_guidance")
    @classmethod
    def strip_guidance(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("additional_blocked_phrases")
    @classmethod
    def normalize_blocked_phrases(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            phrase = " ".join(str(raw or "").split())
            if not phrase:
                continue
            if len(phrase) > 160:
                raise ValueError("blocked phrases must be 160 characters or fewer")
            key = phrase.casefold()
            if key not in seen:
                seen.add(key)
                cleaned.append(phrase)
        return cleaned

    @model_validator(mode="after")
    def require_a_change(self) -> ProspectOutreachPolicyPatch:
        if self.drafting_guidance is None and self.additional_blocked_phrases is None:
            raise ValueError("Provide drafting guidance or blocked phrases to update")
        return self


class ProspectOutreachPolicyRead(BaseModel):
    drafting_guidance: str
    additional_blocked_phrases: list[str] = Field(default_factory=list)
    locked_rules: list[str] = Field(default_factory=list)
    review_seconds: int = Field(ge=1)
    test_recipient_email: str
    sender_display_name: str
    sender_title: str | None = None
    sender_phone: str | None = None
    sender_display_email: str
    sender_from_name: str
    envelope_from_email: str
    reply_contact_email: str
    alternate_contact_email: str | None = None
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None


class ProspectOutreachPurposeTemplate(BaseModel):
    subject: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=12_000)

    @field_validator("subject", "body")
    @classmethod
    def strip_template_copy(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("template copy cannot be blank")
        return clean


class ProspectOutreachProfilePatch(BaseModel):
    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    desk_name: str | None = Field(default=None, min_length=1, max_length=80)
    audience_label: str | None = Field(default=None, min_length=1, max_length=120)
    audience_plural: str | None = Field(default=None, min_length=1, max_length=120)
    website_url: str | None = Field(default=None, min_length=1, max_length=500)
    drafting_guidance: str | None = Field(default=None, max_length=3000)
    purpose_templates: dict[str, ProspectOutreachPurposeTemplate] | None = None

    @field_validator(
        "display_name", "desk_name", "audience_label", "audience_plural", mode="before"
    )
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        return " ".join(str(value).split()) if value is not None else value

    @field_validator("website_url")
    @classmethod
    def require_https_website(cls, value: str | None) -> str | None:
        clean = (value or "").strip()
        if value is None:
            return None
        if not re.fullmatch(r"https://[^\s]+", clean, re.IGNORECASE):
            raise ValueError("website_url must be an absolute HTTPS URL")
        return clean

    @field_validator("drafting_guidance")
    @classmethod
    def clean_profile_guidance(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("purpose_templates")
    @classmethod
    def validate_template_purposes(
        cls, value: dict[str, ProspectOutreachPurposeTemplate] | None
    ) -> dict[str, ProspectOutreachPurposeTemplate] | None:
        if value is None:
            return None
        allowed = {"information", "missed_call", "callback_confirmation", "client_will_call_back", "booking", "general"}
        invalid = sorted(set(value) - allowed)
        if invalid:
            raise ValueError(f"unsupported purpose templates: {', '.join(invalid)}")
        if "information" not in value:
            raise ValueError("purpose_templates must include information")
        return value

    @model_validator(mode="after")
    def require_profile_change(self) -> ProspectOutreachProfilePatch:
        if not (self.model_fields_set - {"expected_version"}):
            raise ValueError("Provide at least one outreach profile field to update")
        return self


class ProspectOutreachProfileRead(BaseModel):
    id: UUID | None = None
    lead_type: LeadType
    version: int = Field(ge=1)
    status: Literal["active", "retired"] = "active"
    display_name: str
    desk_name: str
    audience_label: str
    audience_plural: str
    website_url: str
    drafting_guidance: str = ""
    purpose_templates: dict[str, ProspectOutreachPurposeTemplate] = Field(default_factory=dict)
    created_by_user_id: UUID | None = None
    created_at: datetime | None = None


class ProspectOutreachProfileList(BaseModel):
    items: list[ProspectOutreachProfileRead]


class ProspectTestEmailRequest(BaseModel):
    idempotency_key: UUID
    lead_type: LeadType = "dealer"
    purpose: DraftPurpose = "information"
    sample_contact_name: str = Field(default="Alex Morgan", min_length=1, max_length=160)
    sample_dealer_name: str = Field(default="Example Motors", min_length=1, max_length=180)
    sample_business_name: str | None = Field(default=None, min_length=1, max_length=180)
    ai_instructions: str | None = Field(default=None, max_length=1500)
    verified_conversation_context: str | None = Field(default=None, max_length=500)
    # True means the complete current active Dealer Outreach library. False
    # means exactly ``collateral_asset_ids`` (an empty list means no PDFs).
    include_collateral: bool = True
    collateral_asset_ids: list[UUID] = Field(default_factory=list, max_length=100)
    collateral_bundle_id: UUID | None = None
    collateral_bundle_version: int | None = Field(default=None, ge=1)

    @field_validator("sample_contact_name", "sample_dealer_name", "sample_business_name")
    @classmethod
    def strip_sample_names(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = " ".join(value.split())
        if not clean:
            raise ValueError("sample names cannot be blank")
        return clean

    @field_validator("ai_instructions")
    @classmethod
    def strip_test_instructions(cls, value: str | None) -> str | None:
        clean = (value or "").strip()
        return clean or None

    @field_validator("verified_conversation_context")
    @classmethod
    def normalize_verified_context(cls, value: str | None) -> str | None:
        clean = " ".join((value or "").split())
        return clean or None

    @model_validator(mode="after")
    def validate_collateral_selection(self) -> ProspectTestEmailRequest:
        if len(set(self.collateral_asset_ids)) != len(self.collateral_asset_ids):
            raise ValueError("collateral_asset_ids cannot contain duplicates")
        if self.include_collateral and self.collateral_asset_ids:
            raise ValueError(
                "collateral_asset_ids must be empty when include_collateral is true"
            )
        if (self.collateral_bundle_id is None) != (self.collateral_bundle_version is None):
            raise ValueError(
                "collateral_bundle_id and collateral_bundle_version must be provided together"
            )
        return self


class ProspectTestEmailResponse(BaseModel):
    ok: bool
    delivery_state: Literal["sent", "failed", "uncertain"]
    to_email: str
    subject: str
    draft_source: Literal["ai", "fallback"]
    generation_reason: Literal[
        "ai_generated",
        "ai_disabled",
        "ai_access_blocked",
        "ai_provider_error",
        "ai_output_rejected",
        "ai_usage_record_failed",
        "approved_fallback",
        "fallback_reason_not_recorded",
    ]
    instruction_disposition: Literal[
        "none", "submitted_to_ai", "not_applied_fallback", "unknown"
    ]
    attachment_names: list[str] = Field(default_factory=list)
    collateral_bundle_id: UUID | None = None
    collateral_bundle_version: int | None = None
    sender_display_name: str | None = None
    sender_title: str | None = None
    sender_phone: str | None = None
    sender_display_email: str | None = None
    sender_from_name: str | None = None
    envelope_from_email: str | None = None
    reply_contact_email: str | None = None
    detail: str = ""


class ProspectEmailDraftCreate(BaseModel):
    idempotency_key: UUID = Field(default_factory=uuid4)
    compose_mode: Literal["ai", "manual"] = "ai"
    purpose: DraftPurpose = "information"
    subject: str | None = Field(default=None, min_length=1, max_length=240)
    body: str | None = Field(default=None, min_length=1, max_length=30_000)
    ai_instructions: str | None = Field(default=None, max_length=1500)
    verified_conversation_context: str | None = Field(default=None, max_length=500)
    # This value is routed directly to DealerProspectActivity.  It is never
    # stored on the email draft and never included in the model prompt.
    private_note: str | None = Field(default=None, max_length=4000)
    # None inherits this prospect's saved defaults. An explicit empty list
    # means this draft has no CC recipients.
    cc_emails: list[str] | None = None
    cc_scope: CCScope = "this_email"
    # True snapshots every current active Dealer Outreach PDF. False snapshots
    # exactly the selected ids; false + [] deliberately means no attachments.
    include_collateral: bool = True
    collateral_asset_ids: list[UUID] = Field(default_factory=list, max_length=100)
    collateral_bundle_id: UUID | None = None
    collateral_bundle_version: int | None = Field(default=None, ge=1)

    @field_validator("subject", "body", "ai_instructions", "private_note")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        clean = (value or "").strip()
        return clean or None

    @field_validator("verified_conversation_context")
    @classmethod
    def normalize_verified_context(cls, value: str | None) -> str | None:
        clean = " ".join((value or "").split())
        return clean or None

    @field_validator("cc_emails", mode="before")
    @classmethod
    def validate_cc_emails(cls, value: object) -> list[str] | None:
        return normalize_cc_emails(value)

    @model_validator(mode="after")
    def validate_collateral_selection(self) -> ProspectEmailDraftCreate:
        if len(set(self.collateral_asset_ids)) != len(self.collateral_asset_ids):
            raise ValueError("collateral_asset_ids cannot contain duplicates")
        if self.include_collateral and self.collateral_asset_ids:
            raise ValueError(
                "collateral_asset_ids must be empty when include_collateral is true"
            )
        if (self.collateral_bundle_id is None) != (self.collateral_bundle_version is None):
            raise ValueError(
                "collateral_bundle_id and collateral_bundle_version must be provided together"
            )
        if self.compose_mode == "manual":
            if not self.subject or not self.body:
                raise ValueError("subject and body are required for manual drafts")
            if self.ai_instructions or self.verified_conversation_context:
                raise ValueError(
                    "AI instructions and verified conversation context are available only for AI drafts"
                )
        elif self.subject is not None or self.body is not None:
            raise ValueError("subject and body are available only for manual drafts")
        return self


class ProspectEmailDraftPatch(BaseModel):
    expected_version: int = Field(ge=1)
    subject: str | None = Field(default=None, min_length=1, max_length=240)
    body: str | None = Field(default=None, min_length=1, max_length=30_000)
    cc_emails: list[str] | None = None
    cc_scope: CCScope = "this_email"

    @field_validator("subject", "body")
    @classmethod
    def strip_required_if_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if not clean:
            raise ValueError("value cannot be blank")
        return clean

    @field_validator("cc_emails", mode="before")
    @classmethod
    def validate_cc_emails(cls, value: object) -> list[str] | None:
        return normalize_cc_emails(value)


class ProspectDraftAction(BaseModel):
    # Every interactive transition is optimistic-concurrency protected. The
    # scheduler calls the service directly and remains the only versionless
    # path because it acts on the row it just locked.
    expected_version: int = Field(ge=1)


class ProspectDraftCancelAction(ProspectDraftAction):
    source: Literal["composer", "prospect_banner", "marketing_audit"] = "composer"


class ProspectEmailAttachmentRead(BaseModel):
    id: UUID
    name: str
    version: int
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str
    preview_url: str
    download_url: str


class ProspectEmailDraftRead(BaseModel):
    id: UUID
    prospect_id: UUID
    to_email: str
    cc_emails: list[str] = Field(default_factory=list)
    from_email: str
    reply_to: str
    sender_display_name: str | None = None
    sender_title: str | None = None
    sender_phone: str | None = None
    sender_display_email: str | None = None
    sender_from_name: str | None = None
    envelope_from_email: str | None = None
    reply_contact_email: str | None = None
    subject: str
    body: str
    editable_body: str
    locked_footer_text: str
    compose_mode: Literal["ai", "manual"] = "ai"
    lead_type: LeadType = "dealer"
    funding_intent: str | None = None
    outreach_profile_key: str = "dealer"
    outreach_profile_version: int = 1
    outreach_profile_hash: str
    outreach_profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    collateral_bundle_id: UUID | None = None
    collateral_bundle_version: int | None = None
    collateral_bundle_snapshot: dict[str, Any] = Field(default_factory=dict)
    status: Literal[
        "pending_review", "editing", "sending", "sent", "cancelled", "failed", "blocked"
    ]
    send_after: datetime | None = None
    review_stopped_at: datetime | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancelled_by_user_id: UUID | None = None
    cancellation_source: str | None = None
    countdown_seconds: int | None = None
    version: int
    draft_source: Literal["ai", "fallback"]
    generation_reason: Literal["ai_generated", "approved_fallback"]
    instruction_disposition: Literal[
        "none", "submitted_to_ai", "not_applied_fallback"
    ]
    model_id: str | None = None
    error: str | None = None
    failure_code: str | None = None
    attachment_names: list[str] = Field(default_factory=list)
    attachment_count: int = 0
    attachment_total_bytes: int = 0
    delivery_mode: Literal["attachments", "secure_link"] = "attachments"
    secure_bundle_link_required: bool = False
    secure_bundle_expires_at: datetime | None = None
    prospect_archived_at: datetime | None = None
    contact_id: UUID | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    dealer_name: str | None = None
    owner_user_id: UUID | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    triggering_agent_id: UUID | None = None
    triggering_agent_name: str | None = None
    triggering_agent_email: str | None = None
    message_send_id: UUID | None = None
    delivery_status: Literal[
        "provider_accepted",
        "delivered",
        "bounced",
        "complaint",
        "failed",
        "blocked",
        "cancelled",
        "unavailable",
    ] | None = None
    provider_status: str | None = None
    provider_detail: str | None = None
    provider: str | None = None
    provider_message_id: str | None = None
    delivered_at: datetime | None = None
    opened_at: datetime | None = None
    failed_at: datetime | None = None
    attachments: list[ProspectEmailAttachmentRead] = Field(default_factory=list)
    created_at: datetime


class ProspectEmailDraftList(BaseModel):
    items: list[ProspectEmailDraftRead]


class ProspectEmailOutboxList(ProspectEmailDraftList):
    total: int
    limit: int
    offset: int


class MarketingCollateralRead(BaseModel):
    id: UUID
    assignment: str
    lead_type: LeadType = "dealer"
    purposes: list[str] = Field(default_factory=lambda: ["information"])
    included_by_default: bool = True
    logical_key: str
    name: str
    version: int
    sort_order: int
    status: Literal["pending_approval", "active", "retired"]
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str
    validation_status: str
    validation_detail: str | None = None
    uploaded_by_user_id: UUID | None = None
    approved_by_user_id: UUID | None = None
    retired_by_user_id: UUID | None = None
    approved_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime
    preview_url: str | None = None
    download_url: str | None = None


class MarketingCollateralList(BaseModel):
    items: list[MarketingCollateralRead]


class MarketingCollateralBundleItemWrite(BaseModel):
    asset_id: UUID
    inclusion_mode: Literal["default", "optional"] = "default"


class MarketingCollateralBundleCreate(BaseModel):
    lead_type: LeadType
    purpose: DraftPurpose
    name: str = Field(min_length=1, max_length=180)
    items: list[MarketingCollateralBundleItemWrite] = Field(default_factory=list, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_bundle_name(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def unique_bundle_assets(self) -> MarketingCollateralBundleCreate:
        ids = [item.asset_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("bundle items cannot contain duplicate assets")
        return self


class MarketingCollateralBundlePatch(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=180)
    items: list[MarketingCollateralBundleItemWrite] | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_optional_bundle_name(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None

    @model_validator(mode="after")
    def validate_bundle_patch(self) -> MarketingCollateralBundlePatch:
        if self.name is None and self.items is None:
            raise ValueError("Provide a bundle name or ordered items to update")
        if self.items is not None:
            ids = [item.asset_id for item in self.items]
            if len(ids) != len(set(ids)):
                raise ValueError("bundle items cannot contain duplicate assets")
        return self


class MarketingCollateralBundleAction(BaseModel):
    expected_revision: int = Field(ge=1)


class MarketingCollateralBundleItemRead(BaseModel):
    id: UUID
    asset_id: UUID
    inclusion_mode: Literal["default", "optional"]
    sort_order: int
    asset_name: str
    asset_version: int
    file_name: str
    size_bytes: int
    sha256: str


class MarketingCollateralBundleRead(BaseModel):
    id: UUID
    lead_type: LeadType
    purpose: str
    name: str
    version: int
    revision: int
    status: Literal["draft", "published", "retired"]
    items: list[MarketingCollateralBundleItemRead] = Field(default_factory=list)
    created_by_user_id: UUID | None = None
    published_by_user_id: UUID | None = None
    retired_by_user_id: UUID | None = None
    published_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime


class MarketingCollateralBundleList(BaseModel):
    items: list[MarketingCollateralBundleRead]


class ProspectCollateralOptionRead(BaseModel):
    """Minimum safe metadata exposed to outreach-enabled pipeline actors."""

    id: UUID
    lead_type: LeadType = "dealer"
    purposes: list[str] = Field(default_factory=lambda: ["information"])
    included_by_default: bool = True
    inclusion_mode: Literal["default", "optional"] = "default"
    name: str
    file_name: str
    version: int
    sort_order: int
    size_bytes: int
    preview_url: str


class ProspectCollateralOptionList(BaseModel):
    bundle_id: UUID | None = None
    bundle_version: int = Field(default=0, ge=0)
    bundle_source: Literal["published", "legacy_dealer_library"] = "published"
    lead_type: LeadType = "dealer"
    purpose: str = "information"
    items: list[ProspectCollateralOptionRead]


class MarketingCollateralAction(BaseModel):
    sort_order: int | None = Field(default=None, ge=0, le=100_000)


class MarketingCollateralPatch(BaseModel):
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100_000)
    purposes: list[DraftPurpose] | None = Field(default=None, min_length=1, max_length=7)
    included_by_default: bool | None = None

    @field_validator("purposes")
    @classmethod
    def normalize_purpose_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = ["information" if item == "dealer_information" else item for item in value]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("purposes cannot contain duplicates")
        return cleaned


class MarketingCollateralReorder(BaseModel):
    expected_ids: list[UUID] = Field(max_length=500)
    ordered_ids: list[UUID] = Field(max_length=500)

    @model_validator(mode="after")
    def validate_complete_unique_order(self) -> MarketingCollateralReorder:
        if len(set(self.expected_ids)) != len(self.expected_ids):
            raise ValueError("expected_ids cannot contain duplicates")
        if len(set(self.ordered_ids)) != len(self.ordered_ids):
            raise ValueError("ordered_ids cannot contain duplicates")
        if set(self.expected_ids) != set(self.ordered_ids):
            raise ValueError("ordered_ids must contain exactly the expected_ids")
        return self


class MarketingCollateralEventRead(BaseModel):
    id: UUID
    asset_id: UUID
    actor_user_id: UUID | None = None
    event_type: str
    details: dict = Field(default_factory=dict)
    created_at: datetime


class MarketingCollateralHistory(BaseModel):
    items: list[MarketingCollateralEventRead]


class EmailSuppressionCreate(BaseModel):
    email: EmailStr
    reason: Literal["unsubscribe", "bounce", "complaint", "bad_address", "administrative"]
    details: dict = Field(default_factory=dict)


class EmailSuppressionRead(BaseModel):
    id: UUID
    email: str
    reason: str
    source: str
    active: bool
    created_at: datetime
    revoked_at: datetime | None = None


class ProspectReplyIngest(BaseModel):
    provider: str = Field(default="gmail", min_length=1, max_length=24)
    provider_message_id: str = Field(min_length=1, max_length=320)
    from_email: EmailStr
    to_addresses: list[EmailStr] = Field(min_length=1, max_length=20)
    subject: str | None = Field(default=None, max_length=998)
    body: str | None = Field(default=None, max_length=100_000)
    in_reply_to: str | None = Field(default=None, max_length=500)
    references: list[str] = Field(default_factory=list, max_length=30)
    received_at: datetime | None = None


class ProspectReplyIngestResult(BaseModel):
    matched: bool
    duplicate: bool = False
    prospect_id: UUID | None = None
    draft_id: UUID | None = None


class ProspectReplyRead(BaseModel):
    id: UUID
    draft_id: UUID | None = None
    provider: str
    provider_message_id: str
    from_email: str
    subject: str | None = None
    body: str | None = None
    received_at: datetime | None = None
    created_at: datetime


class ProspectReplyList(BaseModel):
    items: list[ProspectReplyRead]
