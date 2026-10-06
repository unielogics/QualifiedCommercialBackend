from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, model_validator

from app.schemas.common import ORMModel

AllocationMode = Literal["client_ach", "bank_direct", "split", "waived"]
OwnerType = Literal["business", "consumer"]
Cadence = Literal[
    "business_daily", "weekly", "biweekly", "semimonthly", "monthly", "custom"
]


class FeeAllocationInput(BaseModel):
    client_ach_cents: int = Field(default=0, ge=0)
    origination_client_ach_cents: int | None = Field(default=None, ge=0)
    consulting_client_ach_cents: int | None = Field(default=None, ge=0)
    bank_direct_cents: int = Field(default=0, ge=0)
    external_cents: int = Field(default=0, ge=0)
    deferred_cents: int = Field(default=0, ge=0)
    waived_cents: int = Field(default=0, ge=0)


class FeeObligationCreate(FeeAllocationInput):
    agreement_document_id: UUID | None = None
    agreement_attested_signed: bool = False
    # Deprecated compatibility inputs.  The service never trusts these values;
    # the immutable reference and digest are derived from the owned BucketFile.
    agreement_reference: str | None = Field(default=None, max_length=240)
    agreement_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    include_origination_fee: bool = Field(
        default=True, validation_alias=AliasChoices("include_origination_fee", "include_origination")
    )
    include_consulting_fee: bool = Field(
        default=False, validation_alias=AliasChoices("include_consulting_fee", "include_consulting")
    )
    consulting_milestone_confirmed: bool = False
    reason: str | None = Field(default=None, max_length=1000)


class FeeAllocationPatch(BaseModel):
    collection_mode: AllocationMode = "split"
    client_ach_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    bank_direct_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    external_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    deferred_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    waived_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    client_ach_cents: int | None = Field(default=None, ge=0)
    origination_client_ach_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    consulting_client_ach_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    origination_client_ach_cents: int | None = Field(default=None, ge=0)
    consulting_client_ach_cents: int | None = Field(default=None, ge=0)
    bank_direct_cents: int | None = Field(default=None, ge=0)
    external_cents: int | None = Field(default=None, ge=0)
    deferred_cents: int | None = Field(default=None, ge=0)
    waived_cents: int | None = Field(default=None, ge=0)
    expected_record_version: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("expected_record_version", "expected_version"),
    )
    reason: str = Field(default="Operator allocation update", min_length=1, max_length=1000)


class FundingConfirmationCreate(BaseModel):
    actual_funding_date: date = Field(validation_alias=AliasChoices("actual_funding_date", "funded_at"))
    actual_funded_amount: Decimal = Field(
        gt=0,
        max_digits=14,
        decimal_places=2,
        validation_alias=AliasChoices("actual_funded_amount", "funded_amount"),
    )
    funding_party_name: str = Field(
        min_length=1, max_length=180, validation_alias=AliasChoices("funding_party_name", "funding_party")
    )
    funding_reference: str | None = Field(
        default=None, max_length=160, validation_alias=AliasChoices("funding_reference", "transaction_reference")
    )
    note: str | None = Field(default=None, max_length=1000)
    evidence_document_id: UUID | None = None
    production_package_id: UUID | None = None
    source: Literal["manual", "production_attestation"] = "manual"


class PaymentFundingSourceCreate(BaseModel):
    owner_type: OwnerType
    plaid_item_id: str = Field(min_length=1, max_length=128)
    plaid_account_id: str = Field(min_length=1, max_length=128)
    access_token_ciphertext: str | None = None
    account_name: str | None = Field(default=None, max_length=180)
    account_mask: str | None = Field(default=None, max_length=8)
    account_subtype: str | None = Field(default=None, max_length=48)
    institution_name: str | None = Field(default=None, max_length=180)
    holder_name: str | None = Field(default=None, max_length=180)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class AchMandateCreate(BaseModel):
    funding_source_id: UUID
    fee_obligation_id: UUID | None = None
    private_plan_id: UUID | None = None
    authorized_amount_cents: int = Field(gt=0)
    authorization_text_version: str = Field(min_length=1, max_length=32)
    obligation_sha256: str = Field(min_length=64, max_length=64)
    typed_name: str = Field(min_length=1, max_length=180)
    payer_name: str = Field(min_length=1, max_length=180)
    payer_email: str | None = Field(default=None, max_length=320)
    signature_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    certificate_s3_key: str | None = Field(default=None, max_length=512)
    certificate_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> AchMandateCreate:
        if (self.fee_obligation_id is None) == (self.private_plan_id is None):
            raise ValueError("Exactly one of fee_obligation_id or private_plan_id is required")
        return self


class FeeReleaseRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    amount_cents: int | None = Field(default=None, gt=0)
    expected_version: int | None = Field(default=None, ge=1)


class BankDirectReceiptCreate(BaseModel):
    obligation_id: UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    amount_cents: int | None = Field(default=None, gt=0)
    received_on: date = Field(validation_alias=AliasChoices("received_on", "received_at"))
    reference: str = Field(min_length=1, max_length=180)
    note: str | None = Field(default=None, max_length=1000)
    evidence_document_id: UUID | None = None
    receipt_type: Literal["bank_direct", "external_manual"] = "bank_direct"

    @model_validator(mode="after")
    def normalize_amount(self) -> BankDirectReceiptCreate:
        if self.amount is None and self.amount_cents is None:
            raise ValueError("amount or amount_cents is required")
        if self.amount is not None and self.amount_cents is not None:
            if int((self.amount * Decimal("100")).quantize(Decimal("1"))) != self.amount_cents:
                raise ValueError("amount and amount_cents do not match")
        if self.amount is None:
            self.amount = Decimal(self.amount_cents or 0) / Decimal("100")
        return self


class RefundCreate(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    amount_cents: int | None = Field(default=None, gt=0)
    reason: str = Field(default="Operator-requested refund", min_length=1, max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def normalize_amount(self) -> RefundCreate:
        if self.amount is None and self.amount_cents is None:
            raise ValueError("amount or amount_cents is required")
        if self.amount is not None and self.amount_cents is not None:
            if int((self.amount * Decimal("100")).quantize(Decimal("1"))) != self.amount_cents:
                raise ValueError("amount and amount_cents do not match")
        if self.amount is None:
            self.amount = Decimal(self.amount_cents or 0) / Decimal("100")
        return self


class TransferRetryRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class ServicingAuthorityCreate(BaseModel):
    agreement_reference: str = Field(min_length=1, max_length=240)
    agreement_sha256: str = Field(min_length=64, max_length=64)
    creditor_name: str = Field(min_length=1, max_length=180)
    payee_name: str = Field(min_length=1, max_length=180)
    settlement_destination_ref: str = Field(min_length=1, max_length=240)
    effective_from: date
    effective_to: date | None = None


class PrivatePlanCreate(BaseModel):
    production_term_sheet_id: UUID | None = None
    creditor_name: str | None = Field(default=None, max_length=180)
    payee_name: str | None = Field(default=None, max_length=180)
    settlement_destination_ref: str | None = Field(default=None, max_length=240)
    cadence: Cadence
    total_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    total_amount_cents: int | None = Field(default=None, gt=0)
    installment_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    installment_amount_cents: int | None = Field(default=None, gt=0)
    installment_count: int = Field(gt=0, le=2000)
    first_due_date: date = Field(validation_alias=AliasChoices("first_due_date", "first_payment_date"))
    custom_due_dates: list[date] = Field(default_factory=list, max_length=2000)
    servicing_authority_id: UUID | None = None
    agreement_reference: str | None = Field(default=None, max_length=240)
    servicing_authority_reference: str | None = Field(default=None, max_length=240)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def custom_dates_match(self) -> PrivatePlanCreate:
        if self.total_amount is None and self.total_amount_cents is None:
            raise ValueError("total_amount or total_amount_cents is required")
        if self.total_amount is not None and self.total_amount_cents is not None:
            if int((self.total_amount * Decimal("100")).quantize(Decimal("1"))) != self.total_amount_cents:
                raise ValueError("total_amount and total_amount_cents do not match")
        if self.total_amount is None:
            self.total_amount = Decimal(self.total_amount_cents or 0) / Decimal("100")
        if self.installment_amount is not None and self.installment_amount_cents is not None:
            if int((self.installment_amount * Decimal("100")).quantize(Decimal("1"))) != self.installment_amount_cents:
                raise ValueError("installment_amount and installment_amount_cents do not match")
        if self.installment_amount is None and self.installment_amount_cents is not None:
            self.installment_amount = Decimal(self.installment_amount_cents) / Decimal("100")
        if self.cadence == "custom" and len(self.custom_due_dates) != self.installment_count:
            raise ValueError("Custom schedules require one date per installment")
        if self.cadence != "custom" and self.custom_due_dates:
            raise ValueError("custom_due_dates is only valid for custom schedules")
        return self


class PrivatePlanFromTermSheetCreate(BaseModel):
    production_term_sheet_id: UUID | None = None
    servicing_authority_id: UUID | None = None
    agreement_reference: str | None = Field(default=None, max_length=240)
    servicing_authority_reference: str | None = Field(default=None, max_length=240)
    reason: str | None = Field(default=None, max_length=1000)


class PrivateSchedulePreview(BaseModel):
    eligible: bool = False
    blockers: list[str] = Field(default_factory=list)
    production_term_sheet_id: UUID | None = None
    production_term_sheet_version: int | None = None
    funder_type: str | None = None
    funding_party_name: str | None = None
    creditor_name: str | None = None
    cadence: Cadence | None = None
    total_amount_cents: int | None = None
    installment_amount_cents: int | None = None
    installment_count: int | None = None
    first_due_date: date | None = None
    installments: list[dict[str, Any]] = Field(default_factory=list)


class PaymentReviewRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class PlanVersionAction(BaseModel):
    expected_record_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1000)


class FeeObligationRead(ORMModel):
    id: UUID
    application_profile_id: UUID
    version: int
    record_version: int
    status: str
    currency: str
    accepted_amount: Decimal | None
    funded_amount: Decimal | None
    origination_points: Decimal | None
    origination_fee_cents: int
    consulting_fee_cents: int
    gross_fee_cents: int
    client_ach_cents: int
    origination_client_ach_cents: int
    consulting_client_ach_cents: int
    bank_direct_cents: int
    external_cents: int
    deferred_cents: int
    waived_cents: int
    agreement_reference: str | None
    agreement_sha256: str | None
    consulting_milestone_confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FundingConfirmationRead(ORMModel):
    id: UUID
    application_profile_id: UUID
    version: int
    actual_funding_date: date
    actual_funded_amount: Decimal
    funding_party_name: str
    funding_reference: str | None
    note: str | None
    source: str
    confirmed_at: datetime


class PaymentFundingSourceRead(ORMModel):
    id: UUID
    application_profile_id: UUID
    client_id: UUID | None
    status: str
    owner_type: str
    ach_class: str
    account_name: str | None
    account_mask: str | None
    account_subtype: str | None
    institution_name: str | None
    holder_name: str | None
    verified_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class AchMandateRead(ORMModel):
    id: UUID
    application_profile_id: UUID
    funding_source_id: UUID
    fee_obligation_id: UUID | None
    private_plan_id: UUID | None
    status: str
    version: int
    ach_class: str
    authorized_amount_cents: int
    authorization_text_version: str
    typed_name: str
    payer_name: str
    payer_email: str | None
    signed_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


class PaymentTransferRead(ORMModel):
    id: UUID
    application_profile_id: UUID
    fee_obligation_id: UUID | None
    installment_id: UUID | None
    funding_source_id: UUID
    mandate_id: UUID
    idempotency_key: str
    attempt_no: int
    status: str
    amount_cents: int
    ach_class: str
    plaid_transfer_id: str | None
    provider_status: str | None
    provider_failure_code: str | None
    provider_failure_message: str | None
    provider_failure_retryable: bool = False
    submitted_at: datetime | None
    funds_available_at: datetime | None
    returned_at: datetime | None
    created_at: datetime


class BankDirectReceiptRead(ORMModel):
    id: UUID
    obligation_id: UUID
    amount_cents: int
    receipt_type: str
    received_on: date
    reference: str
    note: str | None
    created_at: datetime


class PaymentInstallmentRead(ORMModel):
    id: UUID
    plan_id: UUID
    sequence: int
    due_date: date
    amount_cents: int
    status: str


class PrivatePlanRead(ORMModel):
    id: UUID
    application_profile_id: UUID
    production_term_sheet_id: UUID
    production_package_id: UUID
    production_package_revision_id: UUID | None
    agreement_sha256: str
    agreement_executed_at: datetime
    funding_confirmation_id: UUID | None
    servicing_authority_id: UUID | None
    version: int
    record_version: int
    status: str
    cadence: str
    timezone: str
    total_amount_cents: int
    installment_count: int
    first_due_date: date
    next_due_date: date | None
    schedule_sha256: str
    created_at: datetime


class DealEconomicsSnapshot(BaseModel):
    approved_amount: Decimal | None = None
    accepted_amount: Decimal | None = None
    funded_amount: Decimal | None = None
    origination_points: Decimal | None = None
    consulting_fee: Decimal | None = None
    origination_fee_cents: int = 0
    gross_fee_cents: int = 0
    estimated_close_date: date | None = None


class FeeAllocationResponse(BaseModel):
    id: UUID | None = None
    version: int = 0
    collection_mode: str = "split"
    gross_fee: float = 0
    client_ach_amount: float = 0
    origination_client_ach_amount: float = 0
    consulting_client_ach_amount: float = 0
    origination_client_ach_cents: int = 0
    consulting_client_ach_cents: int = 0
    bank_direct_amount: float = 0
    external_amount: float = 0
    deferred_amount: float = 0
    waived_amount: float = 0
    unallocated_amount: float = 0
    is_balanced: bool = False
    is_current: bool = True
    updated_at: datetime | None = None
    updated_by_name: str | None = None


class FeeObligationLineResponse(BaseModel):
    id: UUID
    component: str
    label: str
    amount: float
    collection_amount: float
    agreement_reference: str | None = None
    agreement_verified: bool = False
    earned: bool = False


class FeeObligationResponse(BaseModel):
    id: UUID
    version: int
    status: str
    amount: float
    authorized_amount: float = 0
    collected_amount: float = 0
    refunded_amount: float = 0
    outstanding_amount: float = 0
    lines: list[FeeObligationLineResponse] = Field(default_factory=list)
    agreement_ready: bool = False
    prepared_at: datetime | None = None
    authorization_sent_at: datetime | None = None
    released_at: datetime | None = None


class ActualFundingConfirmationResponse(BaseModel):
    id: UUID
    funded_at: date
    funded_amount: float
    funding_party: str
    transaction_reference: str | None = None
    source: str
    note: str | None = None
    confirmed_by_name: str | None = None
    confirmed_at: datetime


class PaymentFundingSourceResponse(BaseModel):
    id: UUID
    ownership_type: str
    institution_name: str | None = None
    account_name: str | None = None
    account_mask: str | None = None
    account_subtype: str | None = None
    status: str
    connected_at: datetime | None = None


class AchMandateResponse(BaseModel):
    id: UUID
    status: str
    authorized_amount: float
    sec_code: str
    payer_name: str
    signed_at: datetime | None = None
    revoked_at: datetime | None = None
    certificate_available: bool = False


class PaymentTransferResponse(BaseModel):
    id: UUID
    amount: float
    status: str
    provider_transfer_id: str | None = None
    submitted_at: datetime | None = None
    funds_available_at: datetime | None = None
    failed_at: datetime | None = None
    return_code: str | None = None
    return_reason: str | None = None
    retry_eligible: bool = False
    resume_eligible: bool = False
    retry_count: int = 0
    refundable_amount: float = 0


class PrivateFundingPlanResponse(BaseModel):
    id: UUID
    status: str
    creditor_name: str
    total_amount: float
    installment_amount: float
    cadence: str
    installment_count: int
    next_due_at: date | None = None
    servicing_authority_ready: bool = False
    agreement_ready: bool = False
    funding_confirmed: bool = False
    mandate_status: str | None = None
    record_version: int = 1
    production_term_sheet_id: UUID | None = None
    production_term_sheet_version: int | None = None
    production_package_id: UUID | None = None
    production_package_revision_id: UUID | None = None
    agreement_sha256: str | None = None
    agreement_executed_at: datetime | None = None
    schedule_sha256: str | None = None
    agreement_reference: str | None = None
    payee_name: str | None = None
    settlement_destination_ref: str | None = None
    funding_source_status: str | None = None
    servicing_authority_id: UUID | None = None
    servicing_authority_status: str | None = None
    activation_blockers: list[str] = Field(default_factory=list)
    installments: list[PaymentInstallmentRead] = Field(default_factory=list)


class ServicingAuthorityResponse(BaseModel):
    id: UUID
    status: str
    creditor_name: str
    payee_name: str
    settlement_destination_ref: str
    agreement_reference: str
    effective_from: date
    effective_to: date | None = None


class PaymentTimelineItem(BaseModel):
    id: UUID
    kind: str
    title: str
    detail: str | None = None
    amount: float | None = None
    tone: str = "default"
    actor_name: str | None = None
    occurred_at: datetime


class PaymentPermissions(BaseModel):
    can_view: bool = True
    can_edit_allocation: bool = False
    can_prepare_obligation: bool = False
    can_confirm_funding: bool = False
    can_send_authorization: bool = False
    can_release_ach: bool = False
    can_retry: bool = False
    can_refund: bool = False
    can_reconcile_bank_direct: bool = False
    can_manage_private_schedule: bool = False
    can_manage_servicing_authority: bool = False
    can_waive: bool = False
    can_request_review: bool = False


class AgreementDocumentCandidate(BaseModel):
    id: UUID
    name: str
    artifact_type: Literal["bucket_file"] = "bucket_file"
    sha256: str
    system_signed: bool = False
    signed_at: datetime | None = None
    signature_kind: str | None = None
    requires_staff_attestation: bool = True
    eligible_components: list[Literal["origination", "consulting"]] = Field(
        default_factory=lambda: ["origination", "consulting"]
    )


class PaymentReadiness(BaseModel):
    fee_obligation_current: bool = False
    agreement_signed: bool = False
    consulting_fee_earned: bool = False
    client_authorized: bool = False
    funding_confirmed: bool = False
    amount_covered: bool = False
    account_eligible: bool = False
    no_existing_claim: bool = False
    ready_for_release: bool = False
    blockers: list[str] = Field(default_factory=list)


class PaymentSummaryTotals(BaseModel):
    bank_direct_expected: float = 0
    bank_direct_received: float = 0
    external_received: float = 0
    client_ach_target: float = 0
    scheduled: float = 0
    processing: float = 0
    collected: float = 0
    refunded: float = 0
    waived: float = 0
    outstanding: float = 0


class PaymentSummary(BaseModel):
    profile_id: UUID
    client_id: UUID | None = None
    loan_id: UUID | None = None
    intake_id: UUID | None = None
    display_name: str | None = None
    approved_amount: float | None = None
    accepted_amount: float | None = None
    funded_amount: float | None = None
    origination_fee_points: float | None = None
    origination_fee: float = 0
    consulting_fee: float = 0
    gross_expected_fee: float = 0
    allocation: FeeAllocationResponse | None = None
    obligation: FeeObligationResponse | None = None
    funding_confirmation: ActualFundingConfirmationResponse | None = None
    funding_source: PaymentFundingSourceResponse | None = None
    mandate: AchMandateResponse | None = None
    transfer: PaymentTransferResponse | None = None
    private_funding_eligible: bool = False
    private_funding_reason: str | None = None
    private_plan: PrivateFundingPlanResponse | None = None
    servicing_authorities: list[ServicingAuthorityResponse] = Field(default_factory=list)
    agreement_documents: list[AgreementDocumentCandidate] = Field(default_factory=list)
    timeline: list[PaymentTimelineItem] = Field(default_factory=list)
    totals: PaymentSummaryTotals = Field(default_factory=PaymentSummaryTotals)
    readiness: PaymentReadiness = Field(default_factory=PaymentReadiness)
    permissions: PaymentPermissions = Field(default_factory=PaymentPermissions)
    server_now: datetime


class PaymentQueueItem(BaseModel):
    id: UUID
    profile_id: UUID
    client_id: UUID | None = None
    loan_id: UUID | None = None
    intake_id: UUID | None = None
    display_name: str
    reference: str | None = None
    owner_name: str | None = None
    owner_id: UUID | None = None
    kind: str
    status: str
    amount: float
    outstanding_amount: float
    next_action: str
    next_due_at: date | None = None
    updated_at: datetime


class PaymentQueueTotals(BaseModel):
    awaiting_authorization: int = 0
    ready_for_funding_confirmation: int = 0
    ready_for_release: int = 0
    processing: int = 0
    funds_available: int = 0
    externally_reconciled: int = 0
    action_required: int = 0
    refunded: int = 0
    bank_direct_outstanding: int = 0
    upcoming_private_installments: int = 0


class PaymentQueueResponse(BaseModel):
    items: list[PaymentQueueItem]
    total: int
    server_now: datetime
    totals: PaymentQueueTotals = Field(default_factory=PaymentQueueTotals)
