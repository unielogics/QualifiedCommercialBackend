"""Inbound webhooks.

Currently a single receiver — the Gmail Pub/Sub push endpoint.

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
