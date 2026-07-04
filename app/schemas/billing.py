from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class BillingAddress(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=48)
    line1: str = Field(min_length=1, max_length=240)
    line2: str | None = Field(default=None, max_length=240)
    city: str = Field(min_length=1, max_length=160)
    state: str = Field(min_length=1, max_length=80)
    postal_code: str = Field(min_length=1, max_length=32)
    country: str = Field(default="US", min_length=2, max_length=2)


class ClientPaymentMethodRead(ORMModel):
    id: UUID
    stripe_customer_id: str
    stripe_payment_method_id: str
    setup_intent_id: str | None
    status: str
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None
    billing_name: str | None
    billing_email: str | None
    billing_line1: str | None
    billing_line2: str | None
    billing_city: str | None
    billing_state: str | None
    billing_postal_code: str | None
    billing_country: str | None
    verification_status: str | None
    created_at: datetime


class PaymentAuthorizationRead(ORMModel):
    id: UUID
    status: str
    document_version: str
    document_hash: str
    typed_name: str | None
    stripe_customer_id: str | None
    stripe_payment_method_id: str | None
    setup_intent_id: str | None
    setup_intent_status: str | None
    certificate_s3_key: str | None
    signed_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class PaymentAuthorizationDocumentRead(BaseModel):
    version: str
    sha256: str
    text: str


class PaymentAuthorizationStatusRead(BaseModel):
    role: str
    requires_authorization: bool
    authorized: bool
    client_id: UUID | None
    latest_authorization: PaymentAuthorizationRead | None
    payment_method: ClientPaymentMethodRead | None
    certificate_url: str | None = None
    stripe_publishable_key: str | None = None


class PaymentAuthorizationStartResponse(BaseModel):
    authorization: PaymentAuthorizationRead
    document: PaymentAuthorizationDocumentRead


class SetupIntentRequest(BaseModel):
    authorization_id: UUID | None = None
    billing: BillingAddress | None = None


class SetupIntentResponse(BaseModel):
    authorization_id: UUID
    setup_intent_id: str
    client_secret: str
    stripe_customer_id: str
    stripe_publishable_key: str


class PaymentAuthorizationCompleteRequest(BaseModel):
    authorization_id: UUID
    setup_intent_id: str
    typed_name: str = Field(min_length=1, max_length=160)
    esign_consent: bool
    payment_terms_consent: bool
    signature_data_url: str = Field(min_length=24)
    billing: BillingAddress
    device_metadata: dict[str, Any] | None = None


class PaymentAuthorizationCompleteResponse(BaseModel):
    authorization: PaymentAuthorizationRead
    payment_method: ClientPaymentMethodRead
    certificate_url: str | None = None


class BillableExpenseRead(ORMModel):
    id: UUID
    client_id: UUID
    loan_id: UUID | None
    bucket_id: UUID | None
    status: str
    category: str
    description: str
    amount_cents: int
    currency: str
    vendor_name: str | None
    stripe_payment_intent_id: str | None
    approved_at: datetime | None
    charged_at: datetime | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class ExpenseListResponse(BaseModel):
    items: list[BillableExpenseRead]


class ChargeAttemptRead(ORMModel):
    id: UUID
    expense_id: UUID
    status: str
    amount_cents: int
    currency: str
    stripe_payment_intent_id: str | None
    failure_code: str | None
    failure_message: str | None
    requires_action_url: str | None
    created_at: datetime


class ExpenseChargeResponse(BaseModel):
    expense: BillableExpenseRead
    attempt: ChargeAttemptRead


class CreditPullAccessRead(BaseModel):
    role: str
    requires_payment_authorization: bool
    payment_authorized: bool
    can_run_credit: bool
    reason_code: str | None = None
    message: str | None = None
