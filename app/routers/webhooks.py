"""Inbound webhooks.

Several receivers, secured differently because their senders differ:

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

POST /webhooks/sms/inbound
  A reply someone sent back to one of our texts, forwarded by QCRelay from the
  handset. This is how STOP actually stops things: the relay is a transport and
  holds no consent state, so the opt-out has to land here to take effect. Shared
  secret in the X-Relay-Secret header, matched against settings.sms_webhook_token.
  Fails closed.

Unauthenticated by nature — Pub/Sub can't present a Clerk session —
so the endpoint is guarded by a shared secret in the URL
(`?token=`, matched against settings.gmail_push_token). It fails
closed: no configured token ⇒ all pushes rejected.
"""

from __future__ import annotations

import asyncio
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
from app.enums import Role
from app.models.billing import (
    BillableExpense,
    ChargeAttempt,
    ClientPaymentMethod,
    PaymentAuthorization,
)
from app.models.booking_notification import BookingNotification, BookingNotificationReminder
from app.models.sms_message import SmsMessage
from app.services.communication_events import publish_communication_event
from app.services.notifications import (
    client_agent_user_ids,
    notify_inbound_communication,
    users_with_roles,
)

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
    """Refresh lender and client inboxes from one coalesced Gmail push."""
    from app.services.email.inbound_poller import run_inbound_poll
    from app.services.email.user_inbox_sync import run_user_inbox_sync

    results = await asyncio.gather(
        run_inbound_poll(),
        run_user_inbox_sync(),
        return_exceptions=True,
    )
    for name, result in zip(("lender", "client"), results, strict=True):
        if isinstance(result, Exception):
            log.error("gmail webhook: %s inbox refresh failed", name, exc_info=result)


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


def _parse_occurred_at(value: object) -> datetime | None:
    """When the handset actually got the message.

    The inbox poller can surface a reply minutes after it landed, so dating a
    row by ingest time would misorder the conversation for whoever reads it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        log.warning("sms webhook: unparseable occurredAt %r", value[:40])
        return None


async def _store_inbound_attachments(
    db, *, ledger_row_id, attachments: list[dict]
) -> int:
    """Decode what the relay handed over and file it against the message.

    Deliberately forgiving: a malformed or oversized part is skipped rather
    than raised. The reply is the record that has to survive; its picture is a
    bonus, and losing the whole text because one attachment was odd would be
    the wrong trade.
    """
    if not attachments:
        return 0
    from app.services import inline_images

    stored = 0
    for part in attachments[:5]:  # a sane ceiling on one message
        if not isinstance(part, dict):
            continue
        raw = part.get("data") or ""
        if not isinstance(raw, str) or not raw:
            continue
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception:  # noqa: BLE001
            log.warning("sms webhook: undecodable attachment, skipped")
            continue
        row = await inline_images.store_bytes(
            db,
            subject_kind="sms_message",
            subject_id=str(ledger_row_id),
            filename=str(part.get("name") or "mms-image"),
            mime_type=str(part.get("content_type") or part.get("contentType") or ""),
            data=data,
        )
        if row is not None:
            stored += 1
    if stored:
        log.info("sms webhook: stored %d inbound image(s)", stored)
    return stored


async def _store_inbound_sms(
    *,
    provider: str,
    provider_id: str | None,
    from_phone: str,
    to_phone: str | None,
    body: str,
    occurred_at: datetime | None = None,
    attachments: list[dict] | None = None,
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
            if duplicate is None:
                duplicate = (
                    await db.execute(
                        select(SmsMessage.id).where(
                            SmsMessage.provider.in_(provider_names),
                            SmsMessage.provider_message_id == provider_id[:64],
                            SmsMessage.direction == "inbound",
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

        # Every inbound text becomes a ledger row FIRST, before any early
        # return below can skip it — the compliance record must not depend on
        # whether a rep contact or client match exists.
        from app.services.sms import ledger as sms_ledger
        from app.services.sms import optout as sms_optout

        is_stop = rep_workflows.is_stop_message(body)
        ledger_client = await sms_ledger.client_for_phone(db, from_phone)
        ledger_row = await sms_ledger.record(
            db,
            direction="inbound",
            phone_e164=from_phone,
            status="received",
            body=body,
            provider=provider,
            provider_message_id=provider_id or "",
            detail="opt-out" if is_stop else "",
            context="reply",
            client_id=ledger_client.id if ledger_client is not None else None,
            occurred_at=occurred_at,
        )

        # A picture the client texted us. MMS was arriving with the image
        # silently dropped — bank statements and IDs among them — because the
        # poller only ever read the text preview. Bound to the ledger row, so it
        # is visible wherever that message is.
        # Only when the ledger row actually landed — record() returns None on a
        # failed write, and an image with nothing to hang off is worse than no
        # image. The rest of this handler already treats that as possible.
        if ledger_row is not None:
            await _store_inbound_attachments(
                db, ledger_row_id=ledger_row.id, attachments=attachments or []
            )

        recipient_ids = {contact.owner_user_id for contact in contacts if contact.owner_user_id}
        if ledger_client is not None:
            recipient_ids.update(await client_agent_user_ids(db, ledger_client))
        if not recipient_ids:
            recipient_ids.update(
                user.id for user in await users_with_roles(db, Role.LOAN_EXEC, Role.SUPER_ADMIN)
            )
        sender_label = (
            contacts[0].full_name
            if contacts
            else ledger_client.name
            if ledger_client is not None
            else from_phone
        )
        sms_thread_id = f"sms:phone:{from_phone}"

        if is_stop:
            # record_opt_out writes the suppression row (needed even when the
            # number never held a grant — revoke alone matches nothing then)
            # AND revokes any dealer consent grants, inside a savepoint.
            await sms_optout.record_opt_out(
                db, phone_e164=from_phone, reason="STOP", source="sms_reply",
                note=f"received via {provider} webhook",
            )
            now = datetime.now(UTC)
            for contact in contacts:
                contact.sms_opted_out_at = now
                contact.last_activity_at = now
            await notify_inbound_communication(
                db,
                recipient_ids=recipient_ids,
                channel="sms",
                sender_label=sender_label,
                thread_id=sms_thread_id,
                message_id=str(ledger_row.id) if ledger_row is not None else provider_id,
                subject="Opt-out request received",
            )
            await db.commit()
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        contact = contacts[0] if contacts else None
        if contact is None or contact.owner_user_id is None:
            log.warning("%s sms webhook: no rep contact for sender=%s", provider, from_phone)
            # No rep thread to file it under, but the ledger row above stands.
            await notify_inbound_communication(
                db,
                recipient_ids=recipient_ids,
                channel="sms",
                sender_label=sender_label,
                thread_id=sms_thread_id,
                message_id=str(ledger_row.id) if ledger_row is not None else provider_id,
            )
            await db.commit()
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
        message_row = DealerRepInboxMessage(
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
        db.add(message_row)
        thread.last_message_at = now
        thread.unread_count = int(thread.unread_count or 0) + 1
        contact.last_activity_at = now
        await db.flush()
        if thread.owner_user_id is not None:
            await publish_communication_event(
                db,
                recipient_user_ids={thread.owner_user_id},
                event_type="message.created",
                dealer_id=thread.dealer_id,
                thread_id=thread.id,
                message_id=message_row.id,
                channel="sms",
                direction="inbound",
            )
        await notify_inbound_communication(
            db,
            recipient_ids=recipient_ids,
            channel="sms",
            sender_label=sender_label,
            thread_id=sms_thread_id,
            message_id=str(message_row.id),
        )
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


# Twilio's vocabulary mapped onto the ledger's. Anything unlisted (queued,
# sending, accepted) is a state the ledger already reflects, so it is ignored.
_LEDGER_STATUS = {
    "delivered": "delivered",
    "read": "delivered",
    "sent": "sent",
    "failed": "failed",
    "undelivered": "failed",
    "canceled": "failed",
}


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
    from app.services.sms import ledger as sms_ledger

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
            await db.flush()
            if message.owner_user_id is not None:
                await publish_communication_event(
                    db,
                    recipient_user_ids={message.owner_user_id},
                    event_type="message.delivery_updated",
                    dealer_id=message.dealer_id,
                    thread_id=message.thread_id,
                    message_id=message.id,
                    channel="sms",
                    direction="outbound",
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
                        notice.record_delivery_error(f"Twilio reminder delivery {message_status}.")
                    else:
                        notice.clear_delivery_error()
        # The ledger is the record of every text we sent, and until now nothing
        # advanced it: mark_delivery existed with no caller, so every outbound
        # row sat at "sent" forever and delivered_at was always NULL — while
        # this same webhook updated two other tables.
        await sms_ledger.mark_delivery(
            db,
            provider_message_id=message_sid,
            status=_LEDGER_STATUS.get(message_status, ""),
            detail=f"twilio {error_code or message_status}: {error_message}" if error_message else "",
        )
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
async def plaid_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
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

    if outcome == "asset report ready" and payload.get("asset_report_id"):
        from app.dealer_os.services.plaid_assets import ingest_asset_report_background

        background_tasks.add_task(
            ingest_asset_report_background, str(payload["asset_report_id"])
        )

    log.info(
        "plaid webhook: %s/%s -> %s",
        payload.get("webhook_type"),
        payload.get("webhook_code"),
        outcome,
    )
    return Response(status_code=status.HTTP_200_OK)


@router.post("/sms/inbound")
async def sms_inbound(request: Request) -> Response:
    """An inbound SMS reply, forwarded by QCRelay.

    Every reply uses the same ingestion path as AWS and Twilio: record the SMS
    ledger, update any rep thread, honour STOP, and create a durable notification
    for the responsible user. Carrier state events only advance outbound rows.

    Acks 204 on anything it understands. The relay should not retry a message we
    have already recorded, and a retry storm on a malformed payload would be
    worse than dropping one line we could not parse.
    """
    settings = get_settings()
    expected = settings.sms_webhook_token
    presented = request.headers.get("x-relay-secret", "")
    # Fail closed — this endpoint can suppress a phone number.
    if not expected or not hmac.compare_digest(presented, expected):
        log.warning("sms webhook: rejected inbound (bad/missing secret)")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        log.warning("sms webhook: unparseable body")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    from app.dealer_os.services.consent_delivery import normalize_phone
    from app.services.sms import ledger

    event = (body or {}).get("event") or "sms:received"

    # Carrier state events — the relay forwards the gateway's sms:sent /
    # sms:delivered webhooks so the ledger's outbound rows advance to what the
    # carrier actually confirmed, matched on the provider message id.
    if event in ("sms:sent", "sms:delivered"):
        message_id = (body or {}).get("messageId") or ""
        async with SessionLocal() as db:
            moved = await ledger.mark_delivery(
                db,
                provider_message_id=message_id,
                status="delivered" if event == "sms:delivered" else "sent",
            )
            inbox_message = (
                await db.execute(
                    select(DealerRepInboxMessage).where(
                        DealerRepInboxMessage.provider == "android",
                        DealerRepInboxMessage.provider_message_id == message_id,
                    )
                )
            ).scalar_one_or_none()
            if inbox_message is not None:
                inbox_message.delivery_status = "delivered" if event == "sms:delivered" else "sent"
                await db.flush()
                if inbox_message.owner_user_id is not None:
                    await publish_communication_event(
                        db,
                        recipient_user_ids={inbox_message.owner_user_id},
                        event_type="message.delivery_updated",
                        dealer_id=inbox_message.dealer_id,
                        thread_id=inbox_message.thread_id,
                        message_id=inbox_message.id,
                        channel="sms",
                        direction="outbound",
                    )
            await db.commit()
        log.info("sms webhook: %s for id=%s matched=%s", event, message_id, moved)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    raw_from = (body or {}).get("from") or ""
    message = (body or {}).get("message") or ""
    phone = normalize_phone(raw_from)

    if not phone:
        log.warning("sms webhook: inbound from an unusable number, ignored")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    occurred_at = _parse_occurred_at((body or {}).get("occurredAt"))
    return await _store_inbound_sms(
        provider="android",
        provider_id=_first_str((body or {}).get("messageId")),
        from_phone=phone,
        to_phone=normalize_phone((body or {}).get("to") or ""),
        body=message,
        occurred_at=occurred_at,
        attachments=(body or {}).get("attachments") or [],
    )
