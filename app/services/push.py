"""Expo push dispatch.

Thin wrapper around Expo's HTTP push API
(https://docs.expo.dev/push-notifications/sending-notifications/).
Fires a notification to every device token registered for a user;
called from `post_ai_message` and the chat send-message handler when
a system-initiated AI message lands.

Best-effort by design — we never block the calling request on Expo
availability, and we never raise on transport failure (a missing
push doesn't justify rolling back a chat message). Expo errors are
logged; tokens that come back as `DeviceNotRegistered` get pruned
from the DB so we stop wasting requests on them.

Endpoint accepts up to 100 messages per call. We chunk in batches
of 100 — in practice a single user has 1-3 devices.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.device_token import DeviceToken

log = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_BATCH = 100
_TIMEOUT_S = 10.0


async def send_push_to_user(
    user_id: UUID,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Look up the user's device tokens and send via Expo. Opens its
    own SessionLocal so it's safe to call from a fire-and-forget
    `asyncio.create_task` after the calling handler has committed.

    Returns the number of tokens we attempted to send to (0 when the
    user has no registered devices). Never raises — Expo failures
    are logged."""
    try:
        async with SessionLocal() as db:
            tokens = (
                await db.execute(
                    select(DeviceToken.token, DeviceToken.id).where(
                        DeviceToken.user_id == user_id
                    )
                )
            ).all()
    except Exception:  # noqa: BLE001
        log.exception("send_push_to_user: token lookup failed user=%s", user_id)
        return 0

    if not tokens:
        return 0

    payload = [
        {
            "to": tok,
            "title": title[:200],
            "body": body[:500],
            "sound": "default",
            "data": data or {},
        }
        for (tok, _id) in tokens
    ]

    bad_tokens: list[UUID] = []
    sent = 0
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for i in range(0, len(payload), _BATCH):
                chunk = payload[i : i + _BATCH]
                chunk_meta = tokens[i : i + _BATCH]
                resp = await client.post(
                    EXPO_PUSH_URL,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json=chunk,
                )
                if resp.status_code != 200:
                    log.warning(
                        "expo push HTTP %s for user=%s body=%s",
                        resp.status_code, user_id, resp.text[:300],
                    )
                    continue
                body_json = resp.json() or {}
                tickets = body_json.get("data") or []
                for ticket, (_tok, tok_id) in zip(tickets, chunk_meta):
                    if ticket.get("status") == "ok":
                        sent += 1
                        continue
                    err = (ticket.get("details") or {}).get("error")
                    if err in ("DeviceNotRegistered", "InvalidCredentials"):
                        bad_tokens.append(tok_id)
                    else:
                        log.warning(
                            "expo push ticket error user=%s err=%s msg=%s",
                            user_id, err, ticket.get("message"),
                        )
    except Exception:  # noqa: BLE001
        log.exception("expo push request failed user=%s", user_id)

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

    return sent


def fire_and_forget_push(
    user_id: UUID,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Schedule a push without awaiting — used inside chat handlers
    so a slow Expo response doesn't block the borrower's reply."""
    try:
        asyncio.create_task(
            send_push_to_user(user_id, title=title, body=body, data=data)
        )
    except RuntimeError:
        # No running loop (called from sync ctx) — silently skip.
        log.debug("fire_and_forget_push: no running loop, skipping user=%s", user_id)
