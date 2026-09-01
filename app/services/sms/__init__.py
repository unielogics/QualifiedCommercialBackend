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
from .optout import is_opted_out, record_opt_out

log = logging.getLogger(__name__)

__all__ = [
    "SmsResult",
    "send_sms",
    "send_sms_checked",
    "sms_available",
    "unavailable_reason",
    "active_provider",
    "is_opted_out",
    "record_opt_out",
]

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


async def send_sms_checked(
    db,
    *,
    to_phone: str | None,
    body: str,
    require_consent_kind: str | None = None,
) -> SmsResult:
    """The entry point every call site should use. Guarded, and async.

    `send_sms` is the raw transport and checks nothing. This wraps it with the
    two questions that have to be answered from the database first:

      1. Is the number suppressed? A STOP anywhere makes it unreachable
         everywhere — see services/sms/optout.py for why the suppression list
         exists alongside the dealer consent grants.
      2. If a consent kind is named, is there a live grant of that kind?
         `consent_for` is keyed on the number rather than the file, so a caller
         cannot launder a missing grant by opening a new file.

    Callers that already resolved consent themselves — `deliver_link_checked`
    does — pass `require_consent_kind=None` and get only the suppression check.

    Transport is synchronous and runs in a worker thread, matching how
    `deliver_link` already dispatches.
    """
    import asyncio

    # normalize_phone lives with the delivery seam that first needed it; it is
    # the conservative US-centric parser this codebase already trusts, and
    # having two would be worse than importing across.
    from app.dealer_os.services.consent_delivery import normalize_phone

    phone = normalize_phone(to_phone)
    if not phone:
        return SmsResult(False, active_provider().name, detail="No usable phone number.")

    if await is_opted_out(db, phone):
        log.info("sms suppressed: %s has opted out", phone)
        return SmsResult(
            False,
            active_provider().name,
            detail="This number has opted out of text messages.",
        )

    if require_consent_kind:
        from app.dealer_os.services import sms_consent as sms_consent_svc

        grant = await sms_consent_svc.consent_for(
            db, phone_e164=phone, kind=require_consent_kind
        )
        if grant is None:
            return SmsResult(
                False,
                active_provider().name,
                detail=f"No {require_consent_kind} SMS consent on file for this number.",
            )

    return await asyncio.to_thread(send_sms, phone, body)
