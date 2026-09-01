"""SMS through a physical Android handset, via QCRelay.

The relay (a small Node service at /home/ubuntu/QCRelay, deployed on this box)
forwards to android-sms-gateway running on a tablet, reached over Tailscale. The
text leaves a real SIM.

Deliberately NOT gated on `sms_production`. That flag means "AWS has granted
production access" and is additionally forced to false on every service start by
the A2P pause drop-in; hanging the tablet off it would make this provider
permanently unreachable for exactly the wrong reason. What matters here is
whether the relay is configured, so that is what `available()` checks.

Scope note: the relay is a dumb transport. It holds no consent state and applies
no business rules — those stay in `dealer_os.services.sms_consent`, which is the
single source of truth about who may be contacted.

Throughput: Android trips a confirmation dialog on the device itself at roughly
30 messages per 30 minutes per app, and it cannot be raised without root. The
relay paces sends to stay under that ceiling, so a burst here will queue rather
than fail — hence the generous read timeout.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

from .base import SmsResult

log = logging.getLogger(__name__)

name = "android"


def available() -> bool:
    s = get_settings()
    return bool(getattr(s, "relay_sms_url", "") and getattr(s, "relay_auth_token", ""))


def unavailable_reason() -> str:
    return "The SMS relay is not configured (RELAY_SMS_URL / RELAY_AUTH_TOKEN)."


def send(to_phone: str, body: str) -> SmsResult:
    s = get_settings()
    url = f"{s.relay_sms_url.rstrip('/')}/send-sms"

    # Read timeout covers the relay's own send pacing, not just the HTTP round
    # trip: a queued message waits its turn before the tablet is asked to send.
    timeout = httpx.Timeout(connect=5.0, read=45.0, write=5.0, pool=5.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                json={"to": to_phone, "message": body},
                headers={"Authorization": f"Bearer {s.relay_auth_token}"},
            )
    except httpx.HTTPError as exc:
        log.warning("sms(android): relay unreachable at %s: %s", url, exc)
        return SmsResult(
            False,
            name,
            detail=(
                "Text could not be sent: the SMS relay is unreachable. "
                "Check the relay container and that the tablet is awake and on the tailnet."
            ),
        )

    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code >= 400 or not payload.get("ok"):
        detail = payload.get("detail") or payload.get("error") or resp.text[:200]
        log.warning("sms(android): relay rejected to=%s %s", to_phone, detail)
        return SmsResult(False, name, detail=f"Text could not be sent: {detail}")

    return SmsResult(True, name, str(payload.get("messageId") or "sent"))
