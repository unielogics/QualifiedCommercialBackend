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

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models.billing import BillableExpense, ChargeAttempt, ClientPaymentMethod, PaymentAuthorization

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


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
    signed_payload = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
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
        expense.charged_at = datetime.now(timezone.utc)
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


@router.post("/sms/inbound")
async def sms_inbound(request: Request) -> Response:
    """An inbound SMS reply, forwarded by QCRelay.

    The only thing acted on here is an opt-out, and that is deliberate. STOP is
    the one inbound message with a legal consequence, and it has to be honoured
    whether or not the sender was ever a client, ever granted consent, or exists
    in the database at all — which is exactly why the suppression list needs no
    prior grant to write to.

    Every reply is recorded to the sms_messages ledger and matched to a
    client by number where possible, so inbound texts appear in the client's
    SMS history. Carrier state events (sms:sent / sms:delivered) advance the
    matching outbound row instead.

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
    from app.services.sms import ledger, optout

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
            await db.commit()
        log.info("sms webhook: %s for id=%s matched=%s", event, message_id, moved)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    raw_from = (body or {}).get("from") or ""
    message = (body or {}).get("message") or ""
    phone = normalize_phone(raw_from)

    if not phone:
        log.warning("sms webhook: inbound from an unusable number, ignored")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    is_stop = optout.is_opt_out_keyword(message)
    async with SessionLocal() as db:
        # Every reply becomes a ledger row, matched to a client when the number
        # is known — this is what puts inbound texts on the client's screen
        # instead of leaving them in a file on the relay.
        client = await ledger.client_for_phone(db, phone)
        await ledger.record(
            db,
            direction="inbound",
            phone_e164=phone,
            status="received",
            body=message,
            provider="android",
            detail="opt-out" if is_stop else "",
            context="reply",
            client_id=client.id if client is not None else None,
        )
        if is_stop:
            await optout.record_opt_out(
                db,
                phone_e164=phone,
                reason=message.strip()[:120] or "STOP",
                source="sms_reply",
                note="received via QCRelay",
            )
        await db.commit()
    if is_stop:
        log.info("sms webhook: opt-out honoured for %s", phone)
    else:
        log.info("sms webhook: reply recorded from %s client=%s", phone, bool(client))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
