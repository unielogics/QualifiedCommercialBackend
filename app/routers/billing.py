from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.billing import (
    BillableExpense,
    ChargeAttempt,
    ClientPaymentMethod,
    PaymentAuthorization,
)
from app.models.client import Client
from app.schemas.billing import (
    BillableExpenseRead,
    ChargeAttemptRead,
    ClientPaymentMethodRead,
    ExpenseChargeResponse,
    ExpenseListResponse,
    PaymentAuthorizationCompleteRequest,
    PaymentAuthorizationCompleteResponse,
    PaymentAuthorizationDocumentRead,
    PaymentAuthorizationRead,
    PaymentAuthorizationStartResponse,
    PaymentAuthorizationStatusRead,
    SetupIntentRequest,
    SetupIntentResponse,
)
from app.services import payment_authorization as pa
from app.services import stripe_billing
from app.services.stripe_billing import StripeBillingError, StripeConfigError

router = APIRouter(prefix="/billing", tags=["billing"])


def _require_client(user) -> Client:
    if user.role != Role.CLIENT or not user.client:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client profile required")
    return user.client


def _require_operator(user) -> None:
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator access required")


def _document_read() -> PaymentAuthorizationDocumentRead:
    return PaymentAuthorizationDocumentRead(
        version=pa.PAYMENT_AUTH_DOCUMENT_VERSION,
        sha256=pa.payment_authorization_hash(),
        text=pa.payment_authorization_document(),
    )


async def _status_payload(db: AsyncSession, user) -> PaymentAuthorizationStatusRead:
    settings = get_settings()
    client = user.client if user.role == Role.CLIENT else None
    latest = None
    method = None
    certificate_url = None
    authorized = True
    if user.role == Role.CLIENT:
        authorized = await pa.client_has_completed_payment_authorization(db, user)
        if client is not None:
            latest = await pa.latest_authorization(db, user_id=user.id, client_id=client.id)
            method = await pa.active_payment_method(db, client_id=client.id)
            if latest:
                certificate_url = pa.presign_private_s3_object(latest.certificate_s3_key)
    return PaymentAuthorizationStatusRead(
        role=user.role.value,
        requires_authorization=user.role == Role.CLIENT,
        authorized=authorized,
        client_id=client.id if client else None,
        latest_authorization=PaymentAuthorizationRead.model_validate(latest) if latest else None,
        payment_method=ClientPaymentMethodRead.model_validate(method) if method else None,
        certificate_url=certificate_url,
        stripe_publishable_key=settings.stripe_publishable_key or None,
    )


@router.get("/payment-authorization/status", response_model=PaymentAuthorizationStatusRead)
async def payment_authorization_status(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentAuthorizationStatusRead:
    return await _status_payload(db, user)


@router.get("/payment-authorization/document", response_model=PaymentAuthorizationDocumentRead)
async def payment_authorization_document(user: CurrentUser) -> PaymentAuthorizationDocumentRead:
    _require_client(user)
    return _document_read()


@router.post("/payment-authorization/start", response_model=PaymentAuthorizationStartResponse)
async def start_payment_authorization(
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentAuthorizationStartResponse:
    client = _require_client(user)
    latest = await pa.latest_authorization(db, user_id=user.id, client_id=client.id)
    if latest is None or latest.status not in ("started", "active"):
        latest = PaymentAuthorization(
            user_id=user.id,
            client_id=client.id,
            status="started",
            document_version=pa.PAYMENT_AUTH_DOCUMENT_VERSION,
            document_hash=pa.payment_authorization_hash(),
            ip_address=pa.client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        )
        db.add(latest)
        await db.flush()
        await pa.log_esign_event(
            db,
            authorization=latest,
            user=user,
            client_id=client.id,
            event_type="payment_authorization_started",
            request=request,
        )
    return PaymentAuthorizationStartResponse(
        authorization=PaymentAuthorizationRead.model_validate(latest),
        document=_document_read(),
    )


@router.post("/setup-intents", response_model=SetupIntentResponse)
async def create_setup_intent(
    payload: SetupIntentRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SetupIntentResponse:
    client = _require_client(user)
    settings = get_settings()
    if not settings.stripe_publishable_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe publishable key is not configured")
    auth: PaymentAuthorization | None = None
    if payload.authorization_id:
        auth = await db.get(PaymentAuthorization, payload.authorization_id)
        if auth is None or auth.user_id != user.id or auth.client_id != client.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment authorization not found")
    if auth is None:
        auth = await pa.latest_authorization(db, user_id=user.id, client_id=client.id)
    if auth is None or auth.status not in ("started", "active"):
        auth = PaymentAuthorization(
            user_id=user.id,
            client_id=client.id,
            status="started",
            document_version=pa.PAYMENT_AUTH_DOCUMENT_VERSION,
            document_hash=pa.payment_authorization_hash(),
            ip_address=pa.client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        )
        db.add(auth)
        await db.flush()

    method = await pa.active_payment_method(db, client_id=client.id)
    customer_id = method.stripe_customer_id if method else auth.stripe_customer_id
    try:
        if not customer_id:
            customer = await stripe_billing.create_customer(
                email=payload.billing.email if payload.billing and payload.billing.email else user.email,
                name=payload.billing.name if payload.billing else user.name,
            )
            customer_id = customer["id"]
        setup_intent = await stripe_billing.create_setup_intent(
            customer_id=customer_id,
            metadata={
                "authorization_id": str(auth.id),
                "client_id": str(client.id),
                "user_id": str(user.id),
            },
        )
    except StripeConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe is not configured") from exc
    except StripeBillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    auth.stripe_customer_id = customer_id
    auth.setup_intent_id = setup_intent.get("id")
    auth.setup_intent_status = setup_intent.get("status")
    await db.flush()
    await pa.log_esign_event(
        db,
        authorization=auth,
        user=user,
        client_id=client.id,
        event_type="stripe_setup_intent_created",
        request=request,
        metadata={"setup_intent_id": setup_intent.get("id")},
    )
    return SetupIntentResponse(
        authorization_id=auth.id,
        setup_intent_id=setup_intent["id"],
        client_secret=setup_intent["client_secret"],
        stripe_customer_id=customer_id,
        stripe_publishable_key=settings.stripe_publishable_key,
    )


@router.post("/payment-authorization/complete", response_model=PaymentAuthorizationCompleteResponse)
async def complete_payment_authorization(
    payload: PaymentAuthorizationCompleteRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentAuthorizationCompleteResponse:
    client = _require_client(user)
    auth = await db.get(PaymentAuthorization, payload.authorization_id)
    if auth is None or auth.user_id != user.id or auth.client_id != client.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment authorization not found")
    if auth.document_hash != pa.payment_authorization_hash():
        raise HTTPException(status.HTTP_409_CONFLICT, "Authorization document changed; restart the flow")
    if not payload.esign_consent or not payload.payment_terms_consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Required authorizations must be accepted")

    try:
        setup_intent = await stripe_billing.retrieve_setup_intent(payload.setup_intent_id)
    except StripeConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe is not configured") from exc
    except StripeBillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    if setup_intent.get("status") != "succeeded":
        auth.setup_intent_status = setup_intent.get("status")
        auth.failure_message = "Stripe SetupIntent did not succeed"
        await db.flush()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "setup_intent_not_succeeded",
                "message": "Card setup is not complete. Finish card authentication and try again.",
                "status": setup_intent.get("status"),
            },
        )
    stripe_pm_id = setup_intent.get("payment_method")
    if not isinstance(stripe_pm_id, str) or not stripe_pm_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Stripe setup did not return a payment method")
    try:
        payment_method_payload = await stripe_billing.retrieve_payment_method(stripe_pm_id)
    except StripeBillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    signature_bytes, signature_hash, signature_content_type = pa.decode_signature_data_url(payload.signature_data_url)
    if not signature_bytes or not signature_hash:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Signature is required")

    billing = payload.billing
    card = payment_method_payload.get("card") or {}
    billing_details = payment_method_payload.get("billing_details") or {}
    pm_billing_address = billing_details.get("address") or {}
    stripe_customer_id = setup_intent.get("customer") or auth.stripe_customer_id
    await db.execute(
        update(ClientPaymentMethod)
        .where(ClientPaymentMethod.client_id == client.id)
        .values(is_default=False)
    )
    payment_method = ClientPaymentMethod(
        client_id=client.id,
        user_id=user.id,
        stripe_customer_id=str(stripe_customer_id),
        stripe_payment_method_id=stripe_pm_id,
        setup_intent_id=setup_intent.get("id"),
        status="active",
        brand=card.get("brand"),
        last4=card.get("last4"),
        exp_month=card.get("exp_month"),
        exp_year=card.get("exp_year"),
        billing_name=billing_details.get("name") or billing.name,
        billing_email=billing_details.get("email") or billing.email or user.email,
        billing_phone=billing_details.get("phone") or billing.phone,
        billing_line1=pm_billing_address.get("line1") or billing.line1,
        billing_line2=pm_billing_address.get("line2") or billing.line2,
        billing_city=pm_billing_address.get("city") or billing.city,
        billing_state=pm_billing_address.get("state") or billing.state,
        billing_postal_code=pm_billing_address.get("postal_code") or billing.postal_code,
        billing_country=(pm_billing_address.get("country") or billing.country).upper(),
        verification_status=card.get("checks", {}).get("cvc_check") if isinstance(card.get("checks"), dict) else None,
        is_default=True,
        metadata_json={
            "setup_intent_status": setup_intent.get("status"),
            "stripe_payment_method_type": payment_method_payload.get("type"),
        },
    )
    db.add(payment_method)
    await db.flush()

    sig_ext = "json" if signature_content_type == "application/json" else "png"
    sig_key = f"billing/payment-authorizations/{client.id}/{auth.id}/signature.{sig_ext}"
    try:
        pa.put_private_s3_object(key=sig_key, body=signature_bytes, content_type=signature_content_type)
    except Exception as exc:  # noqa: BLE001
        auth.failure_message = f"Signature storage failed: {exc}"
        await db.flush()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Unable to store signature") from exc

    now = datetime.now(timezone.utc)
    auth.status = "active"
    auth.payment_method_row_id = payment_method.id
    auth.typed_name = payload.typed_name.strip()
    auth.esign_consent = payload.esign_consent
    auth.payment_terms_consent = payload.payment_terms_consent
    auth.signature_s3_key = sig_key
    auth.signature_hash = signature_hash
    auth.stripe_customer_id = str(stripe_customer_id)
    auth.stripe_payment_method_id = stripe_pm_id
    auth.setup_intent_id = setup_intent.get("id")
    auth.setup_intent_status = setup_intent.get("status")
    auth.billing_name = payment_method.billing_name
    auth.billing_email = payment_method.billing_email
    auth.billing_phone = payment_method.billing_phone
    auth.billing_line1 = payment_method.billing_line1
    auth.billing_line2 = payment_method.billing_line2
    auth.billing_city = payment_method.billing_city
    auth.billing_state = payment_method.billing_state
    auth.billing_postal_code = payment_method.billing_postal_code
    auth.billing_country = payment_method.billing_country
    auth.ip_address = pa.client_ip(request)
    auth.user_agent = (request.headers.get("user-agent") or "")[:512] or None
    auth.device_metadata = payload.device_metadata
    auth.signed_at = now
    auth.completed_at = now

    try:
        pdf = pa.render_certificate_pdf(
            authorization=auth,
            user=user,
            client=client,
            payment_method=payment_method,
        )
        cert_key = f"billing/payment-authorizations/{client.id}/{auth.id}/certificate.pdf"
        pa.put_private_s3_object(key=cert_key, body=pdf, content_type="application/pdf")
        auth.certificate_s3_key = cert_key
        auth.certificate_hash = hashlib.sha256(pdf).hexdigest()
    except Exception as exc:  # noqa: BLE001
        auth.failure_message = f"Certificate generation failed: {exc}"
        await db.flush()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Unable to generate authorization certificate") from exc

    await db.flush()
    await pa.log_esign_event(
        db,
        authorization=auth,
        user=user,
        client_id=client.id,
        event_type="payment_authorization_completed",
        request=request,
        metadata={"payment_method_id": stripe_pm_id, "setup_intent_id": setup_intent.get("id")},
    )
    await db.refresh(auth)
    await db.refresh(payment_method)
    return PaymentAuthorizationCompleteResponse(
        authorization=PaymentAuthorizationRead.model_validate(auth),
        payment_method=payment_method,
        certificate_url=pa.presign_private_s3_object(auth.certificate_s3_key),
    )


@router.get("/expenses", response_model=ExpenseListResponse)
async def list_expenses(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    client_id: UUID | None = Query(default=None),
) -> ExpenseListResponse:
    if user.role == Role.CLIENT:
        client = _require_client(user)
        target_client_id = client.id
    else:
        _require_operator(user)
        if client_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "client_id is required")
        target_client_id = client_id
    items = (
        await db.execute(
            select(BillableExpense)
            .where(BillableExpense.client_id == target_client_id)
            .order_by(BillableExpense.created_at.desc())
        )
    ).scalars().all()
    return ExpenseListResponse(items=[BillableExpenseRead.model_validate(item) for item in items])


@router.post("/expenses/{expense_id}/approve", response_model=BillableExpenseRead)
async def approve_expense(
    expense_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BillableExpenseRead:
    _require_operator(user)
    expense = await db.get(BillableExpense, expense_id)
    if expense is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    if expense.status not in ("pending_approval", "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Expense is not pending approval")
    expense.status = "approved"
    expense.approved_by_user_id = user.id
    expense.approved_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(expense)
    return BillableExpenseRead.model_validate(expense)


@router.post("/expenses/{expense_id}/charge", response_model=ExpenseChargeResponse)
async def charge_expense(
    expense_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ExpenseChargeResponse:
    _require_operator(user)
    expense = await db.get(BillableExpense, expense_id)
    if expense is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    if expense.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, "Expense must be approved before charging")
    method = await pa.active_payment_method(db, client_id=expense.client_id)
    if method is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Client has no active payment method")
    attempt = ChargeAttempt(
        expense_id=expense.id,
        client_id=expense.client_id,
        payment_method_row_id=method.id,
        status="pending",
        amount_cents=expense.amount_cents,
        currency=expense.currency,
    )
    db.add(attempt)
    await db.flush()
    try:
        pi = await stripe_billing.create_payment_intent(
            amount_cents=expense.amount_cents,
            currency=expense.currency,
            customer_id=method.stripe_customer_id,
            payment_method_id=method.stripe_payment_method_id,
            description=f"QC - Qualified Commercial LLC: {expense.description}",
            metadata={"expense_id": str(expense.id), "client_id": str(expense.client_id)},
        )
    except StripeConfigError as exc:
        attempt.status = "failed"
        attempt.failure_message = "Stripe is not configured"
        expense.status = "failed"
        expense.failure_message = attempt.failure_message
        await db.flush()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe is not configured") from exc
    except StripeBillingError as exc:
        attempt.status = "failed"
        attempt.failure_message = str(exc)
        expense.status = "failed"
        expense.failure_message = str(exc)
        await db.flush()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    attempt.stripe_payment_intent_id = pi.get("id")
    attempt.status = pi.get("status") or "unknown"
    expense.stripe_payment_intent_id = pi.get("id")
    if pi.get("status") == "succeeded":
        expense.status = "charged"
        expense.charged_at = datetime.now(timezone.utc)
    else:
        expense.status = "failed"
        expense.failure_message = f"Stripe PaymentIntent status: {pi.get('status')}"
    await db.flush()
    await db.refresh(expense)
    await db.refresh(attempt)
    return ExpenseChargeResponse(
        expense=BillableExpenseRead.model_validate(expense),
        attempt=ChargeAttemptRead.model_validate(attempt),
    )
