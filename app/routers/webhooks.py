"""Inbound webhooks.

Two receivers, secured differently because their senders differ:

POST /webhooks/plaid
  Plaid item + statement events. Plaid signs every webhook with an ES256 JWT,
  so this one is verified cryptographically rather than by a URL secret — see
  dealer_os/services/plaid_client.verify_webhook. It fails closed.

POST /webhooks/gmail?token=<secret>
  A Google Cloud Pub/Sub push subscription delivers a notification
  here whenever the delegated mailbox's INBOX changes. We don't trust
  the body for routing — any authenticated push simply triggers an
  immediate inbound poll (`run_inbound_poll` already dedups via the
  Activity log and isolates per-message failures). The poll is the
  same code path the 60s scheduler uses, so push just removes the
  latency.

Unauthenticated by nature — Pub/Sub can't present a Clerk session —
so the endpoint is guarded by a shared secret in the URL
(`?token=`, matched against settings.gmail_push_token). It fails
closed: no configured token ⇒ all pushes rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.dealer_os.models import DealerRepContact, DealerRepInboxMessage, DealerRepInboxThread
from app.dealer_os.services import consent_delivery, rep_workflows
from app.dealer_os.services import sms_consent as sms_consent_svc
from app.models.billing import (
    BillableExpense,
    ChargeAttempt,
    ClientPaymentMethod,
    PaymentAuthorization,
)
from app.models.booking_notification import BookingNotification, BookingNotificationReminder

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _first_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _sms_payload(raw: dict) -> dict:
    if isinstance(raw.get("Message"), str):
        try:
            nested = json.loads(raw["Message"])
            if isinstance(nested, dict):
                return nested
        except json.JSONDecodeError:
            return raw
    if isinstance(raw.get("message"), dict):
        return raw["message"]
    return raw


async def _triggered_poll() -> None:
    """Run an inbound poll triggered by a Gmail push. Failure-isolated —
    a bad poll must never surface as a webhook error (Pub/Sub would
    just retry and hammer us)."""
    from app.services.email.inbound_poller import run_inbound_poll

    try:
        await run_inbound_poll()
    except Exception:  # noqa: BLE001
        log.exception("gmail webhook: triggered inbound poll failed")


@router.post("/gmail")
async def gmail_push(
    request: Request,
    background: BackgroundTasks,
    token: str = "",
) -> Response:
    """Gmail Pub/Sub push receiver. Validates the shared-secret token,
    acks fast, and runs the inbound poll in the background."""
    settings = get_settings()
    expected = settings.gmail_push_token
    # Fail closed — an unsecured webhook would let anyone trigger polls.
    if not expected or token != expected:
        log.warning("gmail webhook: rejected push (bad/missing token)")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    # Best-effort decode purely for the diagnostic log line — the poll
    # runs regardless of whether the body parses.
    try:
        body = await request.json()
        data = (body or {}).get("message", {}).get("data")
        if data:
            decoded = json.loads(base64.b64decode(data).decode("utf-8"))
            log.info(
                "gmail webhook: push for email=%s historyId=%s",
                decoded.get("emailAddress"),
                decoded.get("historyId"),
            )
    except Exception:  # noqa: BLE001
        log.debug("gmail webhook: could not decode push body (non-fatal)")

    # Ack Pub/Sub immediately; the poll runs after the response is sent.
    background.add_task(_triggered_poll)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _store_inbound_sms(
    *,
    provider: str,
    provider_id: str | None,
    from_phone: str,
    to_phone: str | None,
    body: str,
) -> Response:
    async with SessionLocal() as db:
        if provider_id:
            provider_names = [provider, "pinpoint"] if provider == "aws" else [provider]
            duplicate = (
                await db.execute(
                    select(DealerRepInboxMessage.id).where(
                        DealerRepInboxMessage.provider.in_(provider_names),
                        DealerRepInboxMessage.provider_message_id == provider_id,
                    )
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                return Response(status_code=status.HTTP_204_NO_CONTENT)

        contacts = list(
            (
                await db.execute(
                    select(DealerRepContact)
                    .where(DealerRepContact.phone_e164 == from_phone)
                    .order_by(DealerRepContact.last_activity_at.desc().nullslast(), DealerRepContact.updated_at.desc())
                )
            ).scalars().all()
        )
        if rep_workflows.is_stop_message(body):
            await sms_consent_svc.revoke(db, phone_e164=from_phone, reason="STOP")
            now = datetime.now(UTC)
            for contact in contacts:
                contact.sms_opted_out_at = now
                contact.last_activity_at = now
            await db.commit()
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        contact = contacts[0] if contacts else None
        if contact is None or contact.owner_user_id is None:
            log.warning("%s sms webhook: no rep contact for sender=%s", provider, from_phone)
            return Response(status_code=status.HTTP_202_ACCEPTED)

        now = datetime.now(UTC)
        thread = (
            await db.execute(
                select(DealerRepInboxThread)
                .where(
                    DealerRepInboxThread.contact_id == contact.id,
                    DealerRepInboxThread.channel == "sms",
                    DealerRepInboxThread.status == "open",
                )
                .order_by(
                    DealerRepInboxThread.last_message_at.desc().nullslast(),
                    DealerRepInboxThread.updated_at.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if thread is None:
            thread = DealerRepInboxThread(
                owner_user_id=contact.owner_user_id,
                contact_id=contact.id,
                dealer_id=contact.dealer_id,
                subject=f"SMS with {contact.full_name}",
                channel="sms",
                source="inbound_sms",
                last_message_at=now,
                unread_count=0,
            )
            db.add(thread)
            await db.flush()
        db.add(
            DealerRepInboxMessage(
                thread_id=thread.id,
                owner_user_id=thread.owner_user_id,
                contact_id=contact.id,
                dealer_id=thread.dealer_id,
                direction="inbound",
                channel="sms",
                subject=thread.subject,
                body=body,
                provider=provider,
                provider_message_id=provider_id,
                delivery_status="received",
                sender=from_phone,
                recipient=to_phone,
            )
        )
        thread.last_message_at = now
        thread.unread_count = int(thread.unread_count or 0) + 1
        contact.last_activity_at = now
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/aws-sms")
async def aws_sms_inbound(request: Request, token: str = "") -> Response:
    settings = get_settings()
    if not settings.sms_webhook_token or token != settings.sms_webhook_token:
        log.warning("aws sms webhook: rejected push (bad/missing token)")
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    payload = _sms_payload(raw if isinstance(raw, dict) else {})
    provider_id = _first_str(payload.get("messageId"), payload.get("MessageId"), payload.get("message_id"), payload.get("smsMessageId"))
    from_phone = consent_delivery.normalize_phone(_first_str(payload.get("originationNumber"), payload.get("from"), payload.get("sourcePhoneNumber"), payload.get("sender")))
    to_phone = consent_delivery.normalize_phone(_first_str(payload.get("destinationNumber"), payload.get("to"), payload.get("destinationPhoneNumber")))
    body = _first_str(payload.get("messageBody"), payload.get("body"), payload.get("text"), payload.get("message"))
    if not from_phone or not body:
        log.warning("aws sms webhook: missing sender/body keys=%s", sorted(payload.keys()))
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return await _store_inbound_sms(provider="aws", provider_id=provider_id, from_phone=from_phone, to_phone=to_phone, body=body)


async def _twilio_form(request: Request) -> tuple[dict[str, str], bool]:
    from app.dealer_os.services.sms_provider import validate_twilio_signature

    form_data = await request.form()
    form = {str(key): str(value) for key, value in form_data.items()}
    settings = get_settings()
    if not settings.twilio_validate_signatures and settings.app_env != "production":
        return form, True
    canonical_url = f"{settings.public_api_url.rstrip('/')}{request.url.path}"
    signature = request.headers.get("X-Twilio-Signature", "")
    return form, validate_twilio_signature(
        url=canonical_url,
        form=form,
        signature=signature,
        auth_token=settings.twilio_auth_token,
    )


@router.post("/twilio/sms/inbound")
async def twilio_sms_inbound(request: Request) -> Response:
    form, valid = await _twilio_form(request)
    if not valid:
        log.warning("twilio sms webhook: rejected invalid signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    from_phone = consent_delivery.normalize_phone(form.get("From"))
    to_phone = consent_delivery.normalize_phone(form.get("To"))
    body = _first_str(form.get("Body"))
    if not from_phone or not body:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    return await _store_inbound_sms(
        provider="twilio",
        provider_id=_first_str(form.get("MessageSid"), form.get("SmsSid")),
        from_phone=from_phone,
        to_phone=to_phone,
        body=body,
    )


@router.post("/twilio/sms/status")
async def twilio_sms_status(request: Request) -> Response:
    form, valid = await _twilio_form(request)
    if not valid:
        log.warning("twilio status webhook: rejected invalid signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    message_sid = _first_str(form.get("MessageSid"), form.get("SmsSid"))
    message_status = _first_str(form.get("MessageStatus"), form.get("SmsStatus"))
    error_code = _first_str(form.get("ErrorCode"))
    error_message = _first_str(form.get("ErrorMessage"))
    if error_message:
        error_message = re.sub(r"\+\d{8,15}", "[phone number]", error_message)[:320]
    if not message_sid or not message_status:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    async with SessionLocal() as db:
        message = (
            await db.execute(
                select(DealerRepInboxMessage).where(
                    DealerRepInboxMessage.provider == "twilio",
                    DealerRepInboxMessage.provider_message_id == message_sid,
                )
            )
        ).scalar_one_or_none()
        if message is not None:
            message.delivery_status = message_status[:24]
            message.provider_error = (
                f"Twilio {error_code or message_status}: {error_message or 'delivery failed'}"[:500]
                if message_status in {"failed", "undelivered", "canceled"}
                else None
            )
        reminder = (
            await db.execute(
                select(BookingNotificationReminder).where(
                    BookingNotificationReminder.provider_message_id == message_sid,
                    BookingNotificationReminder.channel == "sms",
                )
            )
        ).scalar_one_or_none()
        if reminder is not None:
            terminal_status = (
                "sent"
                if message_status in {"sent", "delivered", "read"}
                else "failed" if message_status in {"failed", "undelivered", "canceled"} else None
            )
            if terminal_status is not None:
                reminder.status = terminal_status
                reminder.error = None if terminal_status == "sent" else f"twilio_{message_status}"
                notice = await db.get(BookingNotification, reminder.booking_notification_id)
                if notice is not None:
                    notice.sms_reminder_status = terminal_status
                    if terminal_status == "failed":
                        notice.last_error = f"Twilio reminder delivery {message_status}."
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _verify_stripe_signature(
    *,
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int = 300,
) -> bool:
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts.setdefault(key, []).append(value)
    timestamp_values = parts.get("t") or []
    signatures = parts.get("v1") or []
    if not timestamp_values or not signatures:
        return False
    try:
        timestamp = int(timestamp_values[0])
    except ValueError:
        return False
    if abs(time.time() - timestamp) > tolerance_seconds:
        return False
    signed_payload = f"{timestamp}.{payload.decode('utf-8')}".encode()
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


def _uuid_from_metadata(value: object) -> UUID | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


async def _sync_setup_intent(db: AsyncSession, setup_intent: dict) -> None:
    setup_intent_id = setup_intent.get("id")
    if not isinstance(setup_intent_id, str) or not setup_intent_id:
        return
    status_value = setup_intent.get("status")
    payment_method_id = setup_intent.get("payment_method")
    customer_id = setup_intent.get("customer")
    auths = (
        await db.execute(
            select(PaymentAuthorization).where(PaymentAuthorization.setup_intent_id == setup_intent_id)
        )
    ).scalars().all()
    for auth in auths:
        auth.setup_intent_status = str(status_value) if status_value else auth.setup_intent_status
        if isinstance(payment_method_id, str) and payment_method_id:
            auth.stripe_payment_method_id = payment_method_id
        if isinstance(customer_id, str) and customer_id:
            auth.stripe_customer_id = customer_id
        if status_value in {"requires_payment_method", "canceled"} and auth.status != "active":
            auth.failure_message = f"Stripe SetupIntent status: {status_value}"


async def _find_charge_attempt(db: AsyncSession, payment_intent: dict) -> ChargeAttempt | None:
    payment_intent_id = payment_intent.get("id")
    if isinstance(payment_intent_id, str) and payment_intent_id:
        attempt = (
            await db.execute(
                select(ChargeAttempt)
                .where(ChargeAttempt.stripe_payment_intent_id == payment_intent_id)
                .order_by(ChargeAttempt.created_at.desc())
            )
        ).scalars().first()
        if attempt is not None:
            return attempt
    metadata = payment_intent.get("metadata") if isinstance(payment_intent.get("metadata"), dict) else {}
    expense_id = _uuid_from_metadata(metadata.get("expense_id"))
    if expense_id is None:
        return None
    return (
        await db.execute(
            select(ChargeAttempt)
            .where(ChargeAttempt.expense_id == expense_id)
            .order_by(ChargeAttempt.created_at.desc())
        )
    ).scalars().first()


async def _sync_payment_intent(db: AsyncSession, payment_intent: dict) -> None:
    attempt = await _find_charge_attempt(db, payment_intent)
    if attempt is None:
        return
    status_value = str(payment_intent.get("status") or "unknown")
    attempt.status = status_value
    payment_intent_id = payment_intent.get("id")
    if isinstance(payment_intent_id, str) and payment_intent_id:
        attempt.stripe_payment_intent_id = payment_intent_id

    expense = await db.get(BillableExpense, attempt.expense_id)
    last_error = payment_intent.get("last_payment_error")
    failure_message = None
    failure_code = None
    if isinstance(last_error, dict):
        failure_message = last_error.get("message")
        failure_code = last_error.get("code") or last_error.get("decline_code")
    if failure_code:
        attempt.failure_code = str(failure_code)
    if failure_message:
        attempt.failure_message = str(failure_message)

    if expense is None:
        return
    if status_value == "succeeded":
        expense.status = "charged"
        expense.charged_at = datetime.now(UTC)
        expense.failure_message = None
    elif status_value in {"requires_payment_method", "canceled"}:
        expense.status = "failed"
        expense.failure_message = failure_message or f"Stripe PaymentIntent status: {status_value}"
    elif status_value == "requires_action":
        expense.status = "failed"
        expense.failure_message = "Stripe requires customer authentication before this off-session charge can complete."
        attempt.failure_message = expense.failure_message
        next_action = payment_intent.get("next_action")
        if isinstance(next_action, dict):
            attempt.metadata_json = {**(attempt.metadata_json or {}), "next_action": next_action}
    elif status_value in {"processing", "requires_capture"}:
        expense.status = "approved"
        expense.failure_message = None


async def _sync_payment_method_detached(db: AsyncSession, payment_method: dict) -> None:
    payment_method_id = payment_method.get("id")
    if not isinstance(payment_method_id, str) or not payment_method_id:
        return
    methods = (
        await db.execute(
            select(ClientPaymentMethod).where(ClientPaymentMethod.stripe_payment_method_id == payment_method_id)
        )
    ).scalars().all()
    for method in methods:
        method.status = "detached"
        method.is_default = False


async def _handle_stripe_event(db: AsyncSession, event: dict) -> None:
    event_type = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data.get("object"), dict) else {}
    if event_type in {
        "setup_intent.succeeded",
        "setup_intent.setup_failed",
        "setup_intent.canceled",
        "setup_intent.requires_action",
    }:
        await _sync_setup_intent(db, obj)
    elif event_type in {
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
        "payment_intent.canceled",
        "payment_intent.requires_action",
        "payment_intent.processing",
    }:
        await _sync_payment_intent(db, obj)
    elif event_type == "payment_method.detached":
        await _sync_payment_method_detached(db, obj)


@router.post("/stripe")
async def stripe_webhook(request: Request) -> dict[str, bool]:
    """Stripe webhook receiver.

    Stripe signs each request with the endpoint-specific webhook secret. We
    require that signature before mutating payment authorization or charge
    state; this endpoint is intentionally unauthenticated by Clerk.
    """
    settings = get_settings()
    webhook_secret = settings.stripe_webhook_secret
    if not webhook_secret:
        log.warning("stripe webhook: rejected event because STRIPE_WEBHOOK_SECRET is unset")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    payload = await request.body()
    signature_header = request.headers.get("stripe-signature", "")
    if not _verify_stripe_signature(
        payload=payload,
        signature_header=signature_header,
        secret=webhook_secret,
    ):
        log.warning("stripe webhook: rejected event with invalid signature")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        log.warning("stripe webhook: rejected malformed JSON")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    event_type = event.get("type")
    event_id = event.get("id")
    try:
        async with SessionLocal() as db:
            await _handle_stripe_event(db, event)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("stripe webhook: failed handling event_id=%s type=%s", event_id, event_type)
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    log.info("stripe webhook: handled event_id=%s type=%s", event_id, event_type)
    return {"received": True}


# ── Plaid ───────────────────────────────────────────────────────────────────


@router.post("/plaid")
async def plaid_webhook(request: Request) -> Response:
    """Plaid item and statement events.

    Verified by signature, not by a URL secret. The body carries an item_id and
    nothing else identifying, so an unverified endpoint would let anyone who
    learned an item_id mark a connection revoked or trigger syncs. Plaid signs
    with ES256 and includes a hash of the raw body, which is why the RAW bytes
    are read here and the parsed body is only trusted afterwards.

    Always 200 on a verified webhook, even for events we do not act on: a
    non-2xx earns a retry, and retrying an event we deliberately ignore is a
    storm with no upside. Genuine rejections (bad signature) return 403.
    """
    from app.dealer_os.services import plaid_client, plaid_webhook

    raw = await request.body()
    header = request.headers.get("Plaid-Verification", "")

    try:
        ok = await plaid_client.verify_webhook(raw, header)
    except plaid_client.PlaidUnavailable:
        # Keys unreachable — we cannot prove this is genuine, so we must not act
        # on it. 503 asks Plaid to retry rather than dropping the event.
        log.warning("plaid webhook: verification key unavailable")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    if not ok:
        log.warning("plaid webhook: rejected unverified delivery")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        log.warning("plaid webhook: verified but unparseable body")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    async with SessionLocal() as db:
        outcome = await plaid_webhook.handle(db, payload)
        await db.commit()

    log.info(
        "plaid webhook: %s/%s -> %s",
        payload.get("webhook_type"),
        payload.get("webhook_code"),
        outcome,
    )
    return Response(status_code=status.HTTP_200_OK)
