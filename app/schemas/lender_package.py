from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


LenderRecipientStatus = Literal[
    "sent",
    "viewed",
    "downloaded",
    "terms_submitted",
    "no_quote",
    "expired",
    "revoked",
]
LenderTermSource = Literal["portal", "email", "phone", "manual"]
LenderTermStatus = Literal["pending", "received", "selected", "not_selected", "declined", "withdrawn"]


class LenderPackageCreate(BaseModel):
    lender_ids: list[UUID] = Field(min_length=1)
    document_ids: list[UUID] = Field(min_length=1)
    expires_in_days: Literal[1, 3, 7, 14] = 7
    subject: str | None = Field(default=None, max_length=512)
    message: str | None = None


class LenderPackageRevoke(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class LenderTermFields(BaseModel):
    requested_amount: float | None = None
    approved_amount: float | None = None
    base_rate: float | None = None
    final_rate: float | None = None
    discount_points: float | None = None
    origination_pct: float | None = None
    lender_fees: float | None = None
    term_months: int | None = None
    amortization_style: str | None = Field(default=None, max_length=24)
    interest_only: bool | None = None
    prepay_penalty: str | None = Field(default=None, max_length=32)
    ltv: float | None = None
    ltc: float | None = None
    dscr: float | None = None
    reserves_required: float | None = None
    estimated_close_days: int | None = None
    expires_at: datetime | None = None
    conditions: list[str] | None = None
    missing_items: list[str] | None = None
    construction_holdback_pct: float | None = None
    draw_count: int | None = None
    exit_strategy: str | None = Field(default=None, max_length=16)
    notes: str | None = None


class LenderTermManualCreate(LenderTermFields):
    lender_id: UUID
    package_recipient_id: UUID | None = None
    source: Literal["email", "phone", "manual"] = "manual"
    status: LenderTermStatus = "received"


class LenderTermUpdate(LenderTermFields):
    source: LenderTermSource | None = None
    status: LenderTermStatus | None = None


class LenderTermSelect(BaseModel):
    apply_to_loan: bool = False


class LenderTermRead(ORMModel):
    id: UUID
    loan_id: UUID
    lender_id: UUID
    lender_name: str | None = None
    package_id: UUID | None = None
    package_recipient_id: UUID | None = None
    source: LenderTermSource
    status: LenderTermStatus
    requested_amount: float | None = None
    approved_amount: float | None = None
    base_rate: float | None = None
    final_rate: float | None = None
    discount_points: float | None = None
    origination_pct: float | None = None
    lender_fees: float | None = None
    term_months: int | None = None
    amortization_style: str | None = None
    interest_only: bool | None = None
    prepay_penalty: str | None = None
    ltv: float | None = None
    ltc: float | None = None
    dscr: float | None = None
    reserves_required: float | None = None
    estimated_close_days: int | None = None
    expires_at: datetime | None = None
    conditions: list[str] | None = None
    missing_items: list[str] | None = None
    construction_holdback_pct: float | None = None
    draw_count: int | None = None
    exit_strategy: str | None = None
    notes: str | None = None
    selected_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LenderPackageDocumentRead(ORMModel):
    id: UUID
    document_id: UUID
    display_name: str
    category: str | None = None
    status: str | None = None
    received_on: date | None = None
    verified_at: datetime | None = None


class LenderPackageRecipientRead(ORMModel):
    id: UUID
    package_id: UUID
    lender_id: UUID
    lender_name: str | None = None
    email: str
    status: LenderRecipientStatus
    email_draft_id: UUID | None = None
    viewed_at: datetime | None = None
    downloaded_at: datetime | None = None
    terms_submitted_at: datetime | None = None
    no_quote_at: datetime | None = None
    last_event_at: datetime | None = None
    term: LenderTermRead | None = None
    created_at: datetime
    updated_at: datetime


class LenderPackageEventRead(ORMModel):
    id: UUID
    package_id: UUID
    recipient_id: UUID | None = None
    lender_id: UUID | None = None
    actor_user_id: UUID | None = None
    event: str
    detail: dict | None = None
    occurred_at: datetime


class LenderPackageRead(ORMModel):
    id: UUID
    loan_id: UUID
    deal_id: str | None = None
    address: str | None = None
    subject: str
    message: str | None = None
    status: str
    expires_at: datetime
    revoked_at: datetime | None = None
    documents: list[LenderPackageDocumentRead] = Field(default_factory=list)
    recipients: list[LenderPackageRecipientRead] = Field(default_factory=list)
    events: list[LenderPackageEventRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class LenderPortalPackageListItem(ORMModel):
    id: UUID
    loan_id: UUID
    deal_id: str
    address: str
    subject: str
    status: str
    recipient_status: LenderRecipientStatus
    expires_at: datetime
    viewed_at: datetime | None = None
    terms_submitted_at: datetime | None = None
    created_at: datetime


class LenderDownloadResponse(BaseModel):
    download_url: str
    expires_in_seconds: int
