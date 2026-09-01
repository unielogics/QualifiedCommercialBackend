"""Explicit transactional SMS provider adapter.

AWS End User Messaging, Twilio, and the Android relay remain independently
configurable. The selected provider never falls back silently: a provider
outage or incomplete configuration is reported against the provider the
operator chose.

The android transport is a physical handset reached over Tailscale via QCRelay
(/home/ubuntu/QCRelay on the API box). It exists because both carrier paths are
blocked by external verification (AWS sandbox, A2P 10DLC) and is scoped as the
testing path: Android caps outgoing SMS at roughly 30 per 30 minutes per app,
and P2P routes carrying application traffic are what carriers filter for.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


@dataclass(frozen=True)
class SmsSendResult:
    ok: bool
    provider: str
    detail: str
    message_id: str | None = None
    sender: str | None = None
    status: str = "failed"


def selected_provider(settings: Settings | None = None) -> str:
    value = str((settings or get_settings()).sms_provider or "aws").strip().lower()
    return value if value in {"aws", "twilio", "android"} else "invalid"


def _twilio_auth(settings: Settings) -> tuple[str, str] | None:
    if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
        return settings.twilio_api_key_sid, settings.twilio_api_key_secret
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return settings.twilio_account_sid, settings.twilio_auth_token
    return None


def provider_sender(settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    if selected_provider(settings) == "twilio":
        return settings.twilio_messaging_service_sid or settings.twilio_from_number or None
    return settings.sms_origination_number or None


def provider_readiness(settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    provider = selected_provider(settings)
    if provider == "twilio":
        configured = bool(
            settings.twilio_account_sid
            and _twilio_auth(settings)
            and (settings.twilio_messaging_service_sid or settings.twilio_from_number)
            and settings.twilio_auth_token
        )
        detail = (
            "Ready"
            if configured and settings.sms_production
            else "Configured but SMS_PRODUCTION is disabled"
            if configured
            else "Twilio account, API credentials, auth token, and sender are required"
        )
        return {
            "provider": provider,
            "configured": configured,
            "production": bool(settings.sms_production),
            "sender": provider_sender(settings),
            "detail": detail,
        }
    if provider == "aws":
        configured = bool(settings.sms_origination_number)
        detail = (
            "Ready"
            if configured and settings.sms_production
            else "Configured but SMS_PRODUCTION is disabled"
            if configured
            else "AWS origination identity is not configured"
        )
        return {
            "provider": provider,
            "configured": configured,
            "production": bool(settings.sms_production),
            "sender": provider_sender(settings),
            "detail": detail,
        }
    if provider == "android":
        configured = bool(settings.relay_sms_url and settings.relay_auth_token)
        return {
            "provider": provider,
            "configured": configured,
            # Honest: this reflects sms_production, which the android path does
            # not require — see sms_available below.
            "production": bool(settings.sms_production),
            "sender": None,
            "detail": (
                "Ready — physical handset via QCRelay (testing path)"
                if configured
                else "Relay URL and auth token are not configured"
            ),
        }
    return {
        "provider": provider,
        "configured": False,
        "production": False,
        "sender": None,
        "detail": "SMS_PROVIDER must be aws, twilio, or android",
    }


def sms_available(settings: Settings | None = None) -> bool:
    status = provider_readiness(settings)
    if status["provider"] == "android":
        # Deliberately not gated on sms_production: that flag means "AWS granted
        # production access" and is forced false on every service start by the
        # A2P pause drop-in, which would strand the handset for an unrelated
        # reason. What matters here is whether the relay is configured.
        return bool(status["configured"])
    return bool(status["configured"] and status["production"])


def _safe_failure(provider: str, exc: Exception) -> SmsSendResult:
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    category = f"http_{status_code}" if status_code else type(exc).__name__
    log.warning("%s SMS send failed category=%s", provider, category)
    return SmsSendResult(
        ok=False,
        provider=provider,
        detail=f"{provider.title()} SMS send failed ({category}).",
        sender=provider_sender(),
    )


def _safe_twilio_failure(exc: Exception) -> SmsSendResult:
    """Retain a carrier-safe Twilio code without leaking phone numbers."""

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    code: str | None = None
    message: str | None = None
    if response is not None:
        try:
            payload = response.json()
            code = str(payload.get("code")) if payload.get("code") is not None else None
            message = str(payload.get("message") or "").strip() or None
        except Exception:  # noqa: BLE001
            pass
    if message:
        message = re.sub(r"\+\d{8,15}", "[phone number]", message)[:320]
    category = code or (f"http_{status_code}" if status_code else type(exc).__name__)
    log.warning("twilio SMS send failed category=%s", category)
    detail = f"Twilio rejected the message ({category})"
    if message:
        detail = f"{detail}: {message}"
    return SmsSendResult(
        ok=False,
        provider="twilio",
        detail=detail[:500],
        sender=provider_sender(),
        status="failed",
    )


def _send_aws(to_phone: str, body: str, settings: Settings) -> SmsSendResult:
    try:
        import boto3

        client = boto3.client("pinpoint-sms-voice-v2", region_name=settings.aws_region or "us-east-1")
        response = client.send_text_message(
            DestinationPhoneNumber=to_phone,
            OriginationIdentity=settings.sms_origination_number,
            MessageBody=body,
            MessageType="TRANSACTIONAL",
        )
        message_id = response.get("MessageId")
        return SmsSendResult(
            ok=True,
            provider="aws",
            detail="Sent through AWS End User Messaging.",
            message_id=message_id,
            sender=settings.sms_origination_number,
            status="accepted",
        )
    except Exception as exc:  # noqa: BLE001
        return _safe_failure("aws", exc)


def _send_twilio(to_phone: str, body: str, settings: Settings) -> SmsSendResult:
    auth = _twilio_auth(settings)
    if auth is None:
        return SmsSendResult(False, "twilio", "Twilio API credentials are incomplete.")
    payload = {"To": to_phone, "Body": body}
    if settings.twilio_messaging_service_sid:
        payload["MessagingServiceSid"] = settings.twilio_messaging_service_sid
    else:
        payload["From"] = settings.twilio_from_number
    if settings.public_api_url:
        payload["StatusCallback"] = (
            f"{settings.public_api_url.rstrip('/')}/api/v1/webhooks/twilio/sms/status"
        )
    try:
        with httpx.Client(timeout=12.0) as client:
            response = client.post(
                f"{TWILIO_API_BASE}/Accounts/{settings.twilio_account_sid}/Messages.json",
                data=payload,
                auth=auth,
            )
        response.raise_for_status()
        data = response.json()
        return SmsSendResult(
            ok=True,
            provider="twilio",
            detail="Accepted by Twilio.",
            message_id=data.get("sid"),
            sender=settings.twilio_messaging_service_sid or data.get("from") or settings.twilio_from_number,
            status=str(data.get("status") or "accepted"),
        )
    except Exception as exc:  # noqa: BLE001
        return _safe_twilio_failure(exc)


def _send_android(to_phone: str, body: str, settings: Settings) -> SmsSendResult:
    url = f"{settings.relay_sms_url.rstrip('/')}/send-sms"
    try:
        # Read timeout covers the relay's own send pacing (it serializes to
        # stay under Android's per-app ceiling), not just the HTTP round trip.
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=45.0, write=5.0, pool=5.0)) as client:
            response = client.post(
                url,
                json={"to": to_phone, "message": body},
                headers={"Authorization": f"Bearer {settings.relay_auth_token}"},
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("android SMS send failed category=%s", type(exc).__name__)
        return SmsSendResult(
            ok=False,
            provider="android",
            detail=(
                "The SMS relay is unreachable. Check the relay container and "
                "that the tablet is awake and on the tailnet."
            ),
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not payload.get("ok"):
        detail = str(payload.get("detail") or payload.get("error") or f"http_{response.status_code}")
        log.warning("android SMS send rejected category=%s", detail[:80])
        return SmsSendResult(ok=False, provider="android", detail=detail[:500])
    return SmsSendResult(
        ok=True,
        provider="android",
        detail="Sent through the handset relay.",
        message_id=str(payload.get("messageId") or "") or None,
        status=str(payload.get("detail") or "accepted"),
    )


def send_sms(to_phone: str, body: str) -> SmsSendResult:
    settings = get_settings()
    readiness = provider_readiness(settings)
    provider = str(readiness["provider"])
    if not sms_available(settings):
        return SmsSendResult(
            ok=False,
            provider=provider,
            detail=str(readiness["detail"]),
            sender=provider_sender(settings),
        )
    if provider == "twilio":
        return _send_twilio(to_phone, body, settings)
    if provider == "aws":
        return _send_aws(to_phone, body, settings)
    if provider == "android":
        return _send_android(to_phone, body, settings)
    return SmsSendResult(False, provider, "SMS provider selection is invalid.")


def validate_twilio_signature(
    *,
    url: str,
    form: Mapping[str, str],
    signature: str,
    auth_token: str,
) -> bool:
    """Validate Twilio's HMAC-SHA1 webhook signature without SDK coupling."""
    if not signature or not auth_token:
        return False
    payload = url + "".join(f"{key}{form[key]}" for key in sorted(form))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()  # noqa: S324
    ).decode()
    return hmac.compare_digest(expected, signature)
