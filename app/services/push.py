"""Firebase Cloud Messaging push dispatch.

Thin wrapper around FCM HTTPv1 via `firebase-admin`. Fires a
notification to every device token registered for a user; called
from `post_ai_message` and the chat send-message handler when a
system-initiated AI message lands.

Previously this routed through Expo Push Service (`exp.host`). We
switched to direct FCM so the mobile can use raw FCM device tokens
without an Expo project in between. The mobile flow is:

  1. mobile gets an FCM token via expo-notifications'
     `getDevicePushTokenAsync()` (requires `google-services.json`)
  2. mobile POSTs to /devices/push-tokens with platform="device"
  3. we send via `firebase_admin.messaging.send_each()` here

Best-effort by design — we never block the calling request, and we
never raise on transport failure. Per-token errors (`UNREGISTERED`,
`INVALID_ARGUMENT`) prune the row from `device_tokens` so we stop
wasting requests on dead handsets.

FCM HTTPv1 accepts only one message per request, but `send_each()`
fans out in parallel and returns a `BatchResponse` with per-token
status. Initialization is lazy + idempotent so we can call this
service from anywhere without worrying about double-init.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any
from uuid import UUID

import firebase_admin
from firebase_admin import credentials, messaging
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import SessionLocal
from app.models.device_token import DeviceToken

log = logging.getLogger(__name__)

# Lazy-init lock so concurrent first-callers don't race
# `firebase_admin.initialize_app`. The SDK throws ValueError if
# called twice; we guard with a single flag + lock.
_init_lock = threading.Lock()
_init_done = False
_init_failed = False


def _ensure_initialized() -> bool:
    """Initialize the Firebase Admin app once on first push attempt.

    Returns True if the SDK is ready, False if credentials are
    missing or init failed (caller should skip push silently).
    """
    global _init_done, _init_failed
    if _init_done:
        return True
    if _init_failed:
        return False
    with _init_lock:
        if _init_done:
            return True
        if _init_failed:
            return False
        path = get_settings().firebase_credentials_path.strip()
        if not path:
            log.debug("push: FIREBASE_CREDENTIALS_PATH unset — skipping init")
            _init_failed = True
            return False
        try:
            # If something else (e.g. firebase_admin auto-init) already
            # created the default app, get_app() succeeds and we're done.
            firebase_admin.get_app()
        except ValueError:
            try:
                cred = credentials.Certificate(path)
                firebase_admin.initialize_app(cred)
            except Exception:  # noqa: BLE001
                log.exception("push: firebase-admin init failed (path=%s)", path)
                _init_failed = True
                return False
        _init_done = True
        log.info("push: firebase-admin initialized")
        return True


# Per-token errors we treat as "dead" and prune from the DB. Anything
# else is logged and left alone for a future retry.
_DEAD_TOKEN_CODES = {
    "registration-token-not-registered",
    "invalid-registration-token",
    "invalid-argument",
    "sender-id-mismatch",
    "third-party-auth-error",
}


async def send_push_to_user(
    user_id: UUID,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Look up the user's device tokens and send via FCM. Opens its
    own SessionLocal so it's safe to call from a fire-and-forget
    `asyncio.create_task` after the calling handler has committed.

    Returns the number of tokens we successfully delivered to. Never
    raises — FCM transport failures are logged."""
    if not _ensure_initialized():
        return 0

    try:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(DeviceToken.token, DeviceToken.id).where(
                        DeviceToken.user_id == user_id
                    )
                )
            ).all()
    except Exception:  # noqa: BLE001
        log.exception("send_push_to_user: token lookup failed user=%s", user_id)
        return 0

    if not rows:
        return 0

    # FCM data values MUST be strings — coerce here so callers can
    # pass through dicts with mixed types without surprises.
    fcm_data: dict[str, str] = {}
    for k, v in (data or {}).items():
        if v is None:
            continue
        fcm_data[str(k)] = v if isinstance(v, str) else str(v)

    messages = [
        messaging.Message(
            token=tok,
            notification=messaging.Notification(
                title=title[:200],
                body=body[:500],
            ),
            data=fcm_data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="default",
                    sound="default",
                ),
            ),
        )
        for (tok, _id) in rows
    ]

    bad_tokens: list[UUID] = []
    sent = 0
    try:
        # firebase-admin's `send_each` is synchronous — wrap in a
        # threadpool so the FastAPI event loop isn't blocked on
        # network IO. (`send_each_async` exists in newer versions but
        # we keep this portable across firebase-admin versions.)
        batch = await asyncio.to_thread(messaging.send_each, messages)
    except Exception:  # noqa: BLE001
        log.exception("fcm send_each failed user=%s", user_id)
        return 0

    for resp, (_tok, tok_id) in zip(batch.responses, rows):
        if resp.success:
            sent += 1
            continue
        err = resp.exception
        code = getattr(err, "code", None) if err else None
        if code in _DEAD_TOKEN_CODES:
            bad_tokens.append(tok_id)
        else:
            log.warning(
                "fcm push error user=%s token_id=%s code=%s msg=%s",
                user_id, tok_id, code, str(err)[:300] if err else "",
            )

    if bad_tokens:
        try:
            async with SessionLocal() as db:
                await db.execute(
                    delete(DeviceToken).where(DeviceToken.id.in_(bad_tokens))
                )
                await db.commit()
            log.info("pruned %d dead FCM tokens for user=%s", len(bad_tokens), user_id)
        except Exception:  # noqa: BLE001
            log.exception("failed to prune dead tokens for user=%s", user_id)

    return sent


def fire_and_forget_push(
    user_id: UUID,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Schedule a push without awaiting — used inside chat handlers
    so a slow FCM response doesn't block the borrower's reply."""
    try:
        asyncio.create_task(
            send_push_to_user(user_id, title=title, body=body, data=data)
        )
    except RuntimeError:
        # No running loop (called from sync ctx) — silently skip.
        log.debug("fire_and_forget_push: no running loop, skipping user=%s", user_id)
