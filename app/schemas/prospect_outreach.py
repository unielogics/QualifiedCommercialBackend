"""API contracts for Dealer Prospect email and collateral workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

DraftPurpose = Literal[
    "dealer_information",
    "missed_call",
    "callback_confirmation",
    "booking",
    "general",
]


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


class ProspectTestEmailRequest(BaseModel):
    idempotency_key: UUID
    purpose: DraftPurpose = "dealer_information"
    sample_contact_name: str = Field(default="Alex Morgan", min_length=1, max_length=160)
    sample_dealer_name: str = Field(default="Example Motors", min_length=1, max_length=180)
    ai_instructions: str | None = Field(default=None, max_length=1500)
    verified_conversation_context: str | None = Field(default=None, max_length=500)
    # True means the complete current active Dealer Outreach library. False
    # means exactly ``collateral_asset_ids`` (an empty list means no PDFs).
    include_collateral: bool = True
    collateral_asset_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator("sample_contact_name", "sample_dealer_name")
    @classmethod
    def strip_sample_names(cls, value: str) -> str:
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
    purpose: DraftPurpose = "dealer_information"
    subject: str | None = Field(default=None, min_length=1, max_length=240)
    body: str | None = Field(default=None, min_length=1, max_length=30_000)
    ai_instructions: str | None = Field(default=None, max_length=1500)
    verified_conversation_context: str | None = Field(default=None, max_length=500)
    # This value is routed directly to DealerProspectActivity.  It is never
    # stored on the email draft and never included in the model prompt.
    private_note: str | None = Field(default=None, max_length=4000)
    # True snapshots every current active Dealer Outreach PDF. False snapshots
    # exactly the selected ids; false + [] deliberately means no attachments.
    include_collateral: bool = True
    collateral_asset_ids: list[UUID] = Field(default_factory=list, max_length=100)

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

    @model_validator(mode="after")
    def validate_collateral_selection(self) -> ProspectEmailDraftCreate:
        if len(set(self.collateral_asset_ids)) != len(self.collateral_asset_ids):
            raise ValueError("collateral_asset_ids cannot contain duplicates")
        if self.include_collateral and self.collateral_asset_ids:
            raise ValueError(
                "collateral_asset_ids must be empty when include_collateral is true"
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

    @field_validator("subject", "body")
    @classmethod
    def strip_required_if_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if not clean:
            raise ValueError("value cannot be blank")
        return clean


class ProspectDraftAction(BaseModel):
    # Every interactive transition is optimistic-concurrency protected. The
    # scheduler calls the service directly and remains the only versionless
    # path because it acts on the row it just locked.
    expected_version: int = Field(ge=1)


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
    status: Literal[
        "pending_review", "editing", "sending", "sent", "cancelled", "failed", "blocked"
    ]
    send_after: datetime | None = None
    review_stopped_at: datetime | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    cancelled_at: datetime | None = None
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


class ProspectCollateralOptionRead(BaseModel):
    """Minimum safe metadata exposed to outreach-enabled pipeline actors."""

    id: UUID
    name: str
    file_name: str
    version: int
    sort_order: int
    size_bytes: int
    preview_url: str


class ProspectCollateralOptionList(BaseModel):
    items: list[ProspectCollateralOptionRead]


class MarketingCollateralAction(BaseModel):
    sort_order: int | None = Field(default=None, ge=0, le=100_000)


class MarketingCollateralPatch(BaseModel):
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100_000)


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
