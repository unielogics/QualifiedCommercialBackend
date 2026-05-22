"""Gmail push (Pub/Sub) — real-time inbound email.

When `GMAIL_PUBSUB_TOPIC` is configured, the app registers a Gmail
`users.watch()` on the delegated mailbox's INBOX. Gmail then publishes
a notification to that Pub/Sub topic on every inbox change; a Pub/Sub
push subscription POSTs `/webhooks/gmail`, which triggers an immediate
inbound poll — turning the previous 60s-polling latency into ~1-2s.

`users.watch()` expires after ~7 days. `scheduler.job_gmail_watch_renew`
re-registers daily (and once at startup), well within that window.

The 60s `inbound_poller` stays scheduled as a fallback — Google itself
recommends a periodic backup sync because push delivery can drop
events. Push + poll together: fast and durable.

Everything here no-ops gracefully when push isn't configured (no topic,
USE_FAKE_INBOX, or Gmail unconfigured) — the poller alone keeps working.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)


def register_gmail_watch() -> dict[str, Any] | None:
    """Register / refresh the Gmail watch on the delegated mailbox's
    INBOX. Returns the watch response ({historyId, expiration}) or None
    when push isn't configured / the call fails. Safe to call repeatedly
    — Gmail treats a fresh watch() as a renewal."""
    settings = get_settings()
    if settings.use_fake_inbox:
        log.debug("gmail_push: USE_FAKE_INBOX=true; skipping watch")
        return None
    if not settings.gmail_pubsub_topic:
        log.info(
            "gmail_push: GMAIL_PUBSUB_TOPIC unset — real-time push disabled "
            "(the 60s inbound poller still runs)"
        )
        return None
    if not settings.gmail_service_account_path or not settings.gmail_delegated_user:
        log.debug("gmail_push: Gmail not configured; skipping watch")
        return None

    try:
        from app.services.email.gmail_client import gmail_config, get_gmail_service
    except ImportError as exc:  # noqa: BLE001
        log.warning("gmail_push: gmail client not importable: %s", exc)
        return None

    cfg = gmail_config()
    if cfg is None:
        log.warning("gmail_push: gmail_config() returned None")
        return None

    try:
        svc = get_gmail_service(cfg)
        resp = svc.users().watch(
            userId="me",
            body={
                "topicName": settings.gmail_pubsub_topic,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "INCLUDE",
            },
        ).execute()
        log.info(
            "gmail_push: watch registered — topic=%s historyId=%s expiration=%s",
            settings.gmail_pubsub_topic,
            resp.get("historyId"),
            resp.get("expiration"),
        )
        return resp
    except Exception as exc:  # noqa: BLE001
        log.warning("gmail_push: users.watch() failed: %s", exc)
        return None
