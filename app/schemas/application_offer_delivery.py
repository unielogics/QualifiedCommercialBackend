from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class MerchantOfferItemRef(BaseModel):
    kind: Literal["merchant_offer"]
    offer_id: UUID
    expected_version: int = Field(ge=1)


class ProductionTermSheetItemRef(BaseModel):
    kind: Literal["production_term_sheet"]
    term_sheet_id: UUID
    expected_version: int = Field(ge=1)


class ApplicationTermSheetItemRef(BaseModel):
    kind: Literal["application_term_sheet"]
    term_sheet_id: UUID
    expected_version: int = Field(ge=1)


OfferItemRef = Annotated[
    MerchantOfferItemRef | ProductionTermSheetItemRef | ApplicationTermSheetItemRef,
    Field(discriminator="kind"),
]


class EvidenceAttachmentRef(BaseModel):
    kind: Literal["evidence_file"]
    file_id: UUID


class OfferDraftRequest(BaseModel):
    items: list[OfferItemRef] = Field(min_length=1, max_length=3)
    guidance: str | None = Field(default=None, max_length=1000)


class OfferCanonicalSection(BaseModel):
    item_key: str
    title: str
    lines: list[str]


class OfferDraftItem(BaseModel):
    item_key: str
    kind: Literal["merchant_offer", "production_term_sheet", "application_term_sheet"]
    label: str
    file_name: str
    expected_version: int
    preview_url: str | None = None
    download_url: str | None = None


class OfferDraftResponse(BaseModel):
    subject: str
    personal_message: str
    canonical_sections: list[OfferCanonicalSection]
    deadline_notice: str
    disclaimer: str
    expires_in_hours: int = 48
    draft_source: Literal["ai", "fallback"]
    draft_fingerprint: str
    items: list[OfferDraftItem]


class OfferDeliveryCreate(BaseModel):
    idempotency_key: UUID
    to_contact_id: str = Field(min_length=1, max_length=80)
    cc_contact_ids: list[str] = Field(default_factory=list, max_length=8)
    subject: str = Field(min_length=1, max_length=200)
    personal_message: str = Field(min_length=1, max_length=10000)
    items: list[OfferItemRef] = Field(min_length=1, max_length=3)
    evidence_attachments: list[EvidenceAttachmentRef] = Field(default_factory=list, max_length=7)
    draft_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class OfferDeliveryItemRead(BaseModel):
    id: UUID
    item_key: str
    kind: Literal[
        "merchant_offer", "production_term_sheet", "application_term_sheet", "evidence_file"
    ]
    label: str
    title: str
    file_name: str
    content_type: str
    size_bytes: int
    status: str
    decision_status: str
    responded_at: datetime | None = None
    responded_name: str | None = None
    expires_at: datetime | None = None
    is_expired: bool = False
    response_label: str | None = None
    preview_url: str | None = None
    download_url: str | None = None


class OfferDeliveryRead(BaseModel):
    id: UUID
    subject: str
    body: str
    status: str
    sent_at: datetime | None = None
    expires_at: datetime | None = None
    is_expired: bool = False
    thread_id: UUID | None = None
    recipient_emails: list[str]
    items: list[OfferDeliveryItemRead]


class OfferDeliveriesRead(BaseModel):
    deliveries: list[OfferDeliveryRead]


class PublicOfferDeliveryAccess(BaseModel):
    passcode: str = Field(min_length=1, max_length=80)


class PublicOfferResponse(PublicOfferDeliveryAccess):
    response: Literal["accepted", "declined"]
    responder_name: str = Field(min_length=2, max_length=180)
    reason: str | None = Field(default=None, max_length=2000)
    acknowledged_non_binding: bool = False


class AuthenticatedOfferResponse(BaseModel):
    response: Literal["accepted", "declined"]
    responder_name: str = Field(min_length=2, max_length=180)
    reason: str | None = Field(default=None, max_length=2000)
    acknowledged_non_binding: bool = False


class ManualOfferResponse(BaseModel):
    response: Literal["accepted", "declined"]
    responder_name: str = Field(min_length=2, max_length=180)
    channel: Literal["email", "phone"]
    received_at: datetime
    attestation: str = Field(min_length=10, max_length=1000)
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must include a timezone")
        return value


class OfferResponseResult(BaseModel):
    delivery: OfferDeliveryRead
    item: OfferDeliveryItemRead


class OfferDeliveryReconciliationRequest(BaseModel):
    outcome: Literal["provider_accepted", "confirmed_not_sent"]
    provider: Literal["gmail", "ses"] | None = None
    provider_message_id: str | None = Field(default=None, min_length=1, max_length=320)
    accepted_at: datetime | None = None
    attestation: str = Field(min_length=20, max_length=2000)

    @field_validator("accepted_at")
    @classmethod
    def require_accepted_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("accepted_at must include a timezone")
        return value

    @model_validator(mode="after")
    def require_outcome_evidence(self):
        if self.outcome == "provider_accepted":
            if not self.provider or not self.provider_message_id or not self.accepted_at:
                raise ValueError(
                    "provider, provider_message_id, and accepted_at are required when the provider accepted the email"
                )
        elif self.provider or self.provider_message_id or self.accepted_at:
            raise ValueError(
                "provider evidence must be omitted when confirming that no provider send occurred"
            )
        return self


class OfferDeliveryReconciliationRead(BaseModel):
    delivery_id: UUID
    status: str
    provider_correlation_id: str
    provider_handoff_started_at: datetime | None = None
    reconciliation_available_at: datetime | None = None
    provider: str | None = None
    provider_message_id: str | None = None
    sent_at: datetime | None = None
    reconciled_at: datetime | None = None
    reconciled_by_user_id: UUID | None = None
    reconciliation_outcome: str | None = None


class OfferRecipientOverride(BaseModel):
    """Reserved typed shape for service callers; public send uses verified contacts."""

    to: EmailStr
    cc: list[EmailStr] = Field(default_factory=list)
