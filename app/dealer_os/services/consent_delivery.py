"""Getting a secure link into the client's hands.

A rep standing in a business needs the owner to do something on their own
phone: connect the bank, upload statements, or authorise a credit pull. Each of
those is already a token-gated page; the only missing piece was delivery.

One seam for all of them, because the difference between "send the Plaid link"
and "send the credit-consent link" should be a URL and a sentence, not three
separate delivery implementations that drift apart.

Channels, in the order they became available:
  email  works today, through the SES identity verified for this domain.
  sms    built and inert until AWS End User Messaging is out of sandbox and the
         toll-free number finishes verification. It degrades to a clear, honest
         failure rather than silently reporting success, because a rep who
         thinks the text went out will stand there waiting for it.

The link itself is never logged. Tokens ride in the URL fragment, which browsers
do not send to servers, and the delivery record stores only which channel was
used and when.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.services.email import ses_client

log = logging.getLogger(__name__)

__all__ = ["DeliveryResult", "deliver_link", "sms_available", "normalize_phone"]

AUDIT_ORIGIN = "https://audit.qualifiedcommercial.com"


@dataclass
class DeliveryResult:
    ok: bool
    channel: str
    detail: str


def normalize_phone(raw: str | None) -> str | None:
    """US numbers to E.164. Returns None when it cannot be made confident.

    Deliberately conservative: texting the wrong person a consent link is worse
    than failing to text at all, so anything ambiguous is refused rather than
    guessed at.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if raw and raw.strip().startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def sms_available() -> bool:
    """Whether an SMS send can actually reach a stranger's phone.

    Requires an origination number AND production access. In the AWS sandbox
    only pre-verified destinations receive anything, which for this use case is
    the same as not working.
    """
    from app.config import get_settings

    s = get_settings()
    return bool(getattr(s, "sms_origination_number", "") and getattr(s, "sms_production", False))


def _send_sms(to_phone: str, body: str) -> DeliveryResult:
    if not sms_available():
        return DeliveryResult(
            False,
            "sms",
            "Texting is not switched on yet. Send it by email, or read the link to them.",
        )
    try:
        import boto3
        from app.config import get_settings

        s = get_settings()
        client = boto3.client("pinpoint-sms-voice-v2", region_name=s.ses_region or "us-east-1")
        resp = client.send_text_message(
            DestinationPhoneNumber=to_phone,
            OriginationIdentity=s.sms_origination_number,
            MessageBody=body,
            MessageType="TRANSACTIONAL",
        )
        return DeliveryResult(True, "sms", resp.get("MessageId", "sent"))
    except Exception as exc:  # noqa: BLE001
        log.warning("consent delivery: sms failed to=%s: %s", to_phone, exc)
        return DeliveryResult(False, "sms", f"Text could not be sent: {exc}")


def _send_email(to_email: str, subject: str, body: str) -> DeliveryResult:
    result = ses_client.send_email(to_email=to_email, subject=subject, body_text=body)
    if result.ok:
        return DeliveryResult(True, "email", result.message_id or "sent")
    log.warning("consent delivery: email failed to=%s detail=%s", to_email, result.detail)
    return DeliveryResult(False, "email", f"Email could not be sent: {result.detail}")


def deliver_link(
    *,
    channel: str,
    to_email: str | None,
    to_phone: str | None,
    business_name: str,
    purpose: str,
    path: str,
    sms_consent_ok: bool,
    rep_name: str | None = None,
) -> DeliveryResult:
    """Send one secure link.

    `purpose` is what the client is being asked to do, in their words:
    "connect your bank", "authorise a credit check", "upload your statements".
    `path` is the token-bearing path; the origin is added here so no caller has
    to know it.

    `sms_consent_ok` has no default on purpose. Every caller has to say whether
    this number has agreed to be texted, and the only honest way to answer is a
    lookup against the consent table. A default of False would be safe but
    would let a caller forget the question entirely and quietly lose the
    ability to text; a default of True would be a compliance hole. Requiring it
    makes the omission a TypeError at import-gate time instead. Use
    `deliver_link_checked` and it is answered for you.
    """
    url = f"{AUDIT_ORIGIN}{path}"
    who = f"{rep_name} at Qualified Commercial" if rep_name else "Qualified Commercial"

    if channel == "sms":
        phone = normalize_phone(to_phone)
        if not phone:
            return DeliveryResult(
                False, "sms", "That phone number does not look complete. Check it and try again."
            )
        if not sms_consent_ok:
            return DeliveryResult(
                False,
                "sms",
                "This number has not agreed to receive texts. Ask the owner to opt in on "
                "the file, or send it by email instead.",
            )
        # Short by necessity, and it names the business so it does not read as
        # a phishing text. The brand and STOP are in every message because
        # carriers require identification and an opt-out path in the message
        # itself, not only at sign-up.
        body = (
            f"{who}: to {purpose} for {business_name}, open {url} "
            f"Reply STOP to opt out."
        )
        return _send_sms(phone, body)

    if channel == "email":
        to = (to_email or "").strip()
        if not to or "@" not in to:
            return DeliveryResult(
                False, "email", "No email address on file for this client."
            )
        body = (
            f"Hello,\n\n"
            f"{who} has asked you to {purpose} for {business_name}.\n\n"
            f"Open this secure link:\n{url}\n\n"
            f"The link is single-use and personal to you. If you were not expecting\n"
            f"this, you can ignore it and nothing will happen.\n\n"
            f"Qualified Commercial\n"
        )
        return _send_email(to, f"Action needed for {business_name}", body)

    return DeliveryResult(False, channel, f"Unknown delivery channel: {channel!r}")


async def deliver_link_checked(db, **kwargs) -> DeliveryResult:
    """`deliver_link` with the consent question answered from the database.

    This is the entry point every route should use. It looks up the grant for
    the destination number itself, so a caller cannot pass an optimistic
    `sms_consent_ok=True` from a stale form field.

    Email needs no consent check: a business that gave a rep their address to
    receive their own funding file expects to hear from us there, and CAN-SPAM
    governs marketing email separately through unsubscribe rather than prior
    opt-in.
    """
    from . import sms_consent as sms_consent_svc

    ok = False
    if kwargs.get("channel") == "sms":
        phone = normalize_phone(kwargs.get("to_phone"))
        if phone:
            grant = await sms_consent_svc.consent_for(
                db, phone_e164=phone, kind="transactional"
            )
            ok = grant is not None

    import asyncio

    return await asyncio.to_thread(deliver_link, sms_consent_ok=ok, **kwargs)
