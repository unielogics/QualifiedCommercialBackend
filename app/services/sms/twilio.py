"""Twilio Programmable Messaging.

The credentials for this have been sitting in the environment for a while, but
no adapter was ever written — `SMS_PROVIDER` and every `TWILIO_*` var were
silently discarded because `Settings` sets `extra="ignore"`. This is that
adapter.

Uses the REST API through httpx rather than the `twilio` SDK: the whole surface
we need is one form POST, and the SDK is a dependency that would have to be
tracked and patched for no gain.

Still gated on `sms_production`. A2P 10DLC registration is what makes
application-generated traffic deliverable, and until it clears, sends either
fail at the carrier or get the number filtered. There is a systemd drop-in
(`20-pause-twilio-until-a2p-verified.conf`) that forces `SMS_PRODUCTION=false`
on every service start; removing it is the deliberate switch to live sending.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

from .base import SmsResult

log = logging.getLogger(__name__)

name = "twilio"

API_ROOT = "https://api.twilio.com/2010-04-01"


def _sender_configured(s) -> bool:
    return bool(
        getattr(s, "twilio_messaging_service_sid", "")
        or getattr(s, "twilio_from_number", "")
    )


def available() -> bool:
    s = get_settings()
    return bool(
        getattr(s, "twilio_account_sid", "")
        and getattr(s, "twilio_auth_token", "")
        and _sender_configured(s)
        and getattr(s, "sms_production", False)
    )


def unavailable_reason() -> str:
    s = get_settings()
    if not (getattr(s, "twilio_account_sid", "") and getattr(s, "twilio_auth_token", "")):
        return "Twilio credentials are not configured."
    if not _sender_configured(s):
        return "Twilio has no messaging service or from-number configured."
    return "Twilio sending is paused until A2P 10DLC verification completes."


def send(to_phone: str, body: str) -> SmsResult:
    s = get_settings()
    account_sid = s.twilio_account_sid
    url = f"{API_ROOT}/Accounts/{account_sid}/Messages.json"

    form: dict[str, str] = {"To": to_phone, "Body": body}
    if getattr(s, "twilio_messaging_service_sid", ""):
        form["MessagingServiceSid"] = s.twilio_messaging_service_sid
    else:
        form["From"] = s.twilio_from_number

    timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url, data=form, auth=(account_sid, s.twilio_auth_token)
            )
    except httpx.HTTPError as exc:
        log.warning("sms(twilio): transport error to=%s: %s", to_phone, exc)
        return SmsResult(False, name, detail=f"Text could not be sent: {exc}")

    if resp.status_code >= 400:
        # Twilio returns a stable numeric code that says far more than the HTTP
        # status — 21610 is "recipient has opted out", which must not be retried
        # or reported as a transient failure.
        detail = f"Twilio returned {resp.status_code}"
        try:
            payload = resp.json()
            detail = f"{detail}: {payload.get('message', '')} (code {payload.get('code')})"
        except ValueError:
            detail = f"{detail}: {resp.text[:200]}"
        log.warning("sms(twilio): rejected to=%s %s", to_phone, detail)
        return SmsResult(False, name, detail=f"Text could not be sent: {detail}")

    try:
        sid = resp.json().get("sid", "sent")
    except ValueError:
        sid = "sent"
    return SmsResult(True, name, sid)
