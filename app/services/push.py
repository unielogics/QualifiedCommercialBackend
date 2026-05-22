"""Push dispatch — Android via FCM, iOS via APNs.

Fires a notification to every device token registered for a user;
called from `post_ai_message` and the chat send-message handler when
a system-initiated AI message lands.

Two transports, picked per device row by `DeviceToken.platform`:

  * platform == "ios"  → Apple APNs directly (aioapns, token .p8 auth).
                          The iOS app (qcmobile-ios) registers a raw
                          APNs device token via expo-notifications'
                          `getDevicePushTokenAsync()`.
  * anything else       → Firebase Cloud Messaging via firebase-admin.
                          The Android app (qcmobile) registers a raw
                          FCM token. ("device"/"android"/"expo" all
                          fall here — only "ios" diverts to APNs.)

A single user may have BOTH (same login on Android + iPhone); each
token is dispatched on its own transport and the results aggregated.

Best-effort by design — never blocks the calling request, never
raises on transport failure. Per-token "dead" errors (FCM
UNREGISTERED / INVALID_ARGUMENT, APNs Unregistered / BadDeviceToken)
prune the row from `device_tokens`. Each transport is independently
gated on its credentials: missing FCM creds → Android no-ops;
missing APNs key → iOS no-ops; the other still works.
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

# ── FCM (Android) lazy init ────────────────────────────────────────
# Lock so concurrent first-callers don't race
# `firebase_admin.initialize_app` (the SDK throws ValueError if
# called twice).
_fcm_lock = threading.Lock()
_fcm_done = False
_fcm_failed = False


def _ensure_fcm() -> bool:
    """Initialize the Firebase Admin app once. Returns True if ready,
    False if creds missing / init failed (Android push then no-ops)."""
    global _fcm_done, _fcm_failed
    if _fcm_done:
        return True
    if _fcm_failed:
        return False
    with _fcm_lock:
        if _fcm_done:
            return True
        if _fcm_failed:
            return False
        path = get_settings().firebase_credentials_path.strip()
        if not path:
            log.debug("push: FIREBASE_CREDENTIALS_PATH unset — Android push disabled")
            _fcm_failed = True
            return False
        try:
            firebase_admin.get_app()
        except ValueError:
            try:
                cred = credentials.Certificate(path)
                firebase_admin.initialize_app(cred)
            except Exception:  # noqa: BLE001
                log.exception("push: firebase-admin init failed (path=%s)", path)
                _fcm_failed = True
                return False
        _fcm_done = True
        log.info("push: firebase-admin initialized")
        return True


# FCM per-token errors treated as "dead" → prune.
_FCM_DEAD_CODES = {
    "registration-token-not-registered",
    "invalid-registration-token",
    "invalid-argument",
    "sender-id-mismatch",
    "third-party-auth-error",
}


# ── APNs (iOS) lazy init ───────────────────────────────────────────
# aioapns' APNs() opens an HTTP/2 connection pool bound to the running
# event loop, so it must be constructed inside an async context (not
# at import). Build once, reuse. The asyncio.Lock guards the first
# concurrent callers.
_apns_lock = asyncio.Lock()
_apns_client: Any = None
_apns_failed = False

# APNs reasons treated as "dead" → prune. (HTTP 410 Unregistered, or
# 400 BadDeviceToken / DeviceTokenNotForTopic.)
_APNS_DEAD_REASONS = {
    "Unregistered",
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
}


async def _ensure_apns() -> Any:
    """Construct the aioapns client once. Returns the client, or None
    if the APNs key isn't configured / init failed (iOS push no-ops)."""
    global _apns_client, _apns_failed
    if _apns_client is not None:
        return _apns_client
    if _apns_failed:
        return None
    async with _apns_lock:
        if _apns_client is not None:
            return _apns_client
        if _apns_failed:
            return None
        s = get_settings()
        key_path = s.apns_key_path.strip()
        if not key_path or not s.apns_key_id.strip() or not s.apns_team_id.strip():
            log.debug("push: APNS_KEY_PATH/KEY_ID/TEAM_ID unset — iOS push disabled")
            _apns_failed = True
            return None
        try:
            from aioapns import APNs

            _apns_client = APNs(
                key=key_path,
                key_id=s.apns_key_id.strip(),
                team_id=s.apns_team_id.strip(),
                topic=s.apns_bundle_id.strip(),
                use_sandbox=s.apns_use_sandbox,
            )
        except Exception:  # noqa: BLE001
            log.exception("push: aioapns init failed (key_path=%s)", key_path)
            _apns_failed = True
            return None
        log.info(
            "push: aioapns initialized (sandbox=%s topic=%s)",
            s.apns_use_sandbox, s.apns_bundle_id,
        )
        return _apns_client


def _string_data(data: dict[str, Any] | None) -> dict[str, str]:
    """Coerce arbitrary data values to strings. FCM requires it; APNs
    custom keys are happier as strings too — keep both transports'
    payload shape identical so deep-link handlers behave the same."""
    out: dict[str, str] = {}
    for k, v in (data or {}).items():
        if v is None:
            continue
        out[str(k)] = v if isinstance(v, str) else str(v)
    return out


async def _send_fcm(
    user_id: UUID,
    rows: list[tuple[str, UUID]],
    *,
    title: str,
    body: str,
    data: dict[str, str],
) -> tuple[int, list[UUID]]:
    """Send the Android rows via firebase-admin. Returns
    (delivered_count, dead_token_ids)."""
    if not rows or not _ensure_fcm():
        return 0, []
    messages = [
        messaging.Message(
            token=tok,
            notification=messaging.Notification(title=title[:200], body=body[:500]),
            data=data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="default", sound="default"
                ),
            ),
        )
        for (tok, _id) in rows
    ]
    try:
        # send_each is sync — offload so the event loop isn't blocked.
        batch = await asyncio.to_thread(messaging.send_each, messages)
    except Exception:  # noqa: BLE001
        log.exception("fcm send_each failed user=%s", user_id)
        return 0, []
    sent = 0
    dead: list[UUID] = []
    for resp, (_tok, tok_id) in zip(batch.responses, rows):
        if resp.success:
            sent += 1
            continue
        err = resp.exception
        code = getattr(err, "code", None) if err else None
        if code in _FCM_DEAD_CODES:
            dead.append(tok_id)
        else:
            log.warning(
                "fcm push error user=%s token_id=%s code=%s msg=%s",
                user_id, tok_id, code, str(err)[:300] if err else "",
            )
    return sent, dead


async def _send_apns(
    user_id: UUID,
    rows: list[tuple[str, UUID]],
    *,
    title: str,
    body: str,
    data: dict[str, str],
) -> tuple[int, list[UUID]]:
    """Send the iOS rows via APNs. Returns (delivered_count,
    dead_token_ids). aioapns sends one request per token; we fan out
    with asyncio.gather and never let one bad token sink the rest."""
    client = await _ensure_apns()
    if not rows or client is None:
        return 0, []

    from aioapns import NotificationRequest, PushType

    payload = {
        "aps": {
            "alert": {"title": title[:200], "body": body[:500]},
            "sound": "default",
        },
        # Custom keys sit alongside `aps` at the top level — this is
        # where usePushTapHandler reads kind/thread_id/loan_id from.
        **data,
    }

    async def _one(tok: str, tok_id: UUID) -> tuple[bool, UUID | None]:
        try:
            req = NotificationRequest(
                device_token=tok,
                message=payload,
                push_type=PushType.ALERT,
            )
            resp = await client.send_notification(req)
        except Exception:  # noqa: BLE001
            log.exception("apns send failed user=%s token_id=%s", user_id, tok_id)
            return False, None
        if resp.is_successful:
            return True, None
        reason = resp.description or ""
        if reason in _APNS_DEAD_REASONS:
            return False, tok_id
        log.warning(
            "apns push error user=%s token_id=%s status=%s reason=%s",
            user_id, tok_id, resp.status, reason,
        )
        return False, None

    results = await asyncio.gather(
        *(_one(tok, tok_id) for (tok, tok_id) in rows),
        return_exceptions=False,
    )
    sent = sum(1 for ok, _ in results if ok)
    dead = [tid for _, tid in results if tid is not None]
    return sent, dead


async def send_push_to_user(
    user_id: UUID,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Look up the user's device tokens and dispatch each on the right
    transport (iOS→APNs, else→FCM). Opens its own SessionLocal so it's
    safe from a fire-and-forget task. Returns the number of tokens we
    delivered to. Never raises."""
    try:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(
                        DeviceToken.token,
                        DeviceToken.id,
                        DeviceToken.platform,
                    ).where(DeviceToken.user_id == user_id)
                )
            ).all()
    except Exception:  # noqa: BLE001
        log.exception("send_push_to_user: token lookup failed user=%s", user_id)
        return 0

    if not rows:
        return 0

    ios_rows = [(r.token, r.id) for r in rows if (r.platform or "").lower() == "ios"]
    fcm_rows = [(r.token, r.id) for r in rows if (r.platform or "").lower() != "ios"]

    payload_data = _string_data(data)

    fcm_sent, fcm_dead = await _send_fcm(
        user_id, fcm_rows, title=title, body=body, data=payload_data
    )
    apns_sent, apns_dead = await _send_apns(
        user_id, ios_rows, title=title, body=body, data=payload_data
    )

    bad_tokens = fcm_dead + apns_dead
    if bad_tokens:
        try:
            async with SessionLocal() as db:
                await db.execute(
                    delete(DeviceToken).where(DeviceToken.id.in_(bad_tokens))
                )
                await db.commit()
            log.info("pruned %d dead push tokens for user=%s", len(bad_tokens), user_id)
        except Exception:  # noqa: BLE001
            log.exception("failed to prune dead tokens for user=%s", user_id)

    return fcm_sent + apns_sent


def fire_and_forget_push(
    user_id: UUID,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Schedule a push without awaiting — used inside chat handlers so
    a slow FCM/APNs round-trip doesn't block the borrower's reply."""
    try:
        asyncio.create_task(
            send_push_to_user(user_id, title=title, body=body, data=data)
        )
    except RuntimeError:
        # No running loop (called from sync ctx) — silently skip.
        log.debug("fire_and_forget_push: no running loop, skipping user=%s", user_id)
