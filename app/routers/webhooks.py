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
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response, status

from app.config import get_settings

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
