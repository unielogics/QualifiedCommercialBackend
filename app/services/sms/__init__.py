"""One seam for outbound SMS, three transports behind it.

    aws      AWS End User Messaging. Dormant until the account leaves the SMS
             sandbox and the toll-free number finishes verification.
    twilio   Programmable Messaging. Paused until A2P 10DLC clears.
    android  A physical handset reached over Tailscale, via QCRelay. Works
             today, and is the testing path while the other two are blocked.

`SMS_PROVIDER` selects. It defaults to `aws`, so nothing changes for anything
that has not been explicitly switched over.

Adding the tablet did not replace the carrier paths and was not meant to: a
consumer handset is the wrong place to put borrower-facing volume, because
routing application-generated traffic over a P2P route is what carriers filter
for and ban SIMs over. AWS and Twilio remain the production transports.

This module does not decide who may be contacted. `dealer_os.services.
sms_consent.consent_for` is the gate, and it is keyed on the phone number rather
than the file, so a STOP anywhere applies everywhere.
"""

from __future__ import annotations

import logging

from app.config import get_settings

from . import android, aws, twilio
from .base import SmsResult

log = logging.getLogger(__name__)

__all__ = ["SmsResult", "send_sms", "sms_available", "unavailable_reason", "active_provider"]

_PROVIDERS = {
    aws.name: aws,
    twilio.name: twilio,
    android.name: android,
}

_DEFAULT = aws.name


def active_provider():
    """The module selected by `SMS_PROVIDER`, falling back to AWS.

    An unrecognised value falls back rather than raising: a typo in an env var
    should not take the API down at import time, and the warning plus the
    honest "unavailable" answer downstream make it visible.
    """
    configured = (getattr(get_settings(), "sms_provider", "") or _DEFAULT).strip().lower()
    provider = _PROVIDERS.get(configured)
    if provider is None:
        log.warning(
            "SMS_PROVIDER=%r is not one of %s — falling back to %s",
            configured,
            sorted(_PROVIDERS),
            _DEFAULT,
        )
        return _PROVIDERS[_DEFAULT]
    return provider


def sms_available() -> bool:
    """Whether a send can actually reach a stranger's phone right now."""
    return active_provider().available()


def unavailable_reason() -> str:
    """Why not, for operators. Empty string when SMS is in fact available."""
    provider = active_provider()
    return "" if provider.available() else provider.unavailable_reason()


def send_sms(to_phone: str, body: str) -> SmsResult:
    """Send one message. Never raises; failures come back as `ok=False`.

    Callers are expected to have checked consent already — see
    `deliver_link_checked`, which resolves the grant from the database rather
    than trusting a caller-supplied flag.
    """
    provider = active_provider()
    if not provider.available():
        return SmsResult(False, provider.name, detail=provider.unavailable_reason())
    return provider.send(to_phone, body)
