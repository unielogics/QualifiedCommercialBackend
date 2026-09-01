"""Getting a secure link into the client's hands.

A rep standing in a business needs the owner to do something on their own
phone: connect the bank, upload statements, or authorise a credit pull. Each of
those is already a token-gated page; the only missing piece was delivery.

One seam for all of them, because the difference between "send the Plaid link"
and "send the credit-consent link" should be a URL and a sentence, not three
separate delivery implementations that drift apart.

Email is the guaranteed channel and always goes. It needs no carrier
registration, survives a mistyped mobile number, and leaves the client
something they can find again next week. SMS is a nudge toward it, never a
substitute: asking for a text sends both. That way a request is never lost
because texting is switched off or the number was wrong.

  email  works today, through the SES identity verified for this domain.
  sms    uses the explicitly selected provider — AWS End User Messaging,
         Twilio, or the Android handset relay. It remains inert until that
         provider is ready and degrades to a clear, honest failure rather than
         silently reporting success or switching to another provider.

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

__all__ = [
    "DeliveryResult",
    "deliver_link",
    "deliver_link_checked",
    "sms_available",
    "sms_provider_status",
    "sms_sender",
    "normalize_phone",
]

AUDIT_ORIGIN = "https://audit.qualifiedcommercial.com"


@dataclass
class DeliveryResult:
    """What actually happened, per channel.

    `ok` means the client was reached by SOMETHING. `email_ok` and `sms_ok`
    say by what, because "the text failed but the email went" and "nothing
    went" need different responses from the rep standing there.
    """

    ok: bool
    channel: str
    detail: str
    email_ok: bool = False
    sms_ok: bool = False
    provider: str | None = None
    provider_message_id: str | None = None
    sender: str | None = None
    delivery_status: str | None = None

    @property
    def message_id(self) -> str | None:
        """Compatibility alias used by reminder and delivery audit records."""
        return self.provider_message_id


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

    Requires complete configuration and production access for the explicitly
    selected provider. Provider failures never trigger cross-provider fallback.
    """
    from .sms_provider import sms_available as provider_available

    return provider_available()


def sms_provider_status() -> dict[str, object]:
    from .sms_provider import provider_readiness

    return provider_readiness()


def sms_sender() -> str | None:
    from .sms_provider import provider_sender

    return provider_sender()


def _send_sms(to_phone: str, body: str) -> DeliveryResult:
    from .sms_provider import send_sms

    result = send_sms(to_phone, body)
    return DeliveryResult(
        result.ok,
        "sms",
        result.detail,
        sms_ok=result.ok,
        provider=result.provider,
        provider_message_id=result.message_id,
        sender=result.sender,
        delivery_status=result.status,
    )


def _send_email(to_email: str, subject: str, body: str) -> DeliveryResult:
    result = ses_client.send_email(to_email=to_email, subject=subject, body_text=body)
    if result.ok:
        return DeliveryResult(
            True,
            "email",
            result.message_id or "sent",
            email_ok=True,
            provider="ses",
            provider_message_id=result.message_id,
            delivery_status="sent",
        )
    log.warning("consent delivery: email failed to=%s detail=%s", to_email, result.detail)
    return DeliveryResult(
        False,
        "email",
        f"Email could not be sent: {result.detail}",
        provider="ses",
        delivery_status="failed",
    )


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
    origin: str = AUDIT_ORIGIN,
) -> DeliveryResult:
    """Send one secure link.

    **Email always goes.** It is the channel that works for everyone, needs no
    carrier registration, survives a wrong mobile number, and leaves the client
    something they can find again a week later. A text is a nudge toward it,
    never a replacement for it, so `channel="sms"` means "email AND text", not
    "text instead of email". Only `channel="none"` sends nothing, for the case
    where the rep wants the link to read out loud.

    `purpose` is what the client is being asked to do, in their words:
    "connect your bank", "authorise a credit check", "sign the application".
    `path` is the token-bearing path; the origin is added here so no caller has
    to know it.

    `sms_consent_ok` has no default on purpose. Every caller has to say whether
    this number has agreed to be texted, and the only honest way to answer is a
    lookup against the consent table. Requiring it makes an omission a
    TypeError rather than a compliance hole. Use `deliver_link_checked` and it
    is answered for you.
    """
    if channel == "none":
        return DeliveryResult(True, "none", "Nothing sent. Read the link out or copy it.")

    url = f"{origin.rstrip('/')}{path}" if path.startswith("/") else path
    who = f"{rep_name} at Qualified Commercial" if rep_name else "Qualified Commercial"

    email_res: DeliveryResult | None = None
    to = (to_email or "").strip()
    if to and "@" in to:
        body = (
            f"Hello,\n\n"
            f"{who} has asked you to {purpose} for {business_name}.\n\n"
            f"Open this secure link:\n{url}\n\n"
            f"The link is personal to you. If you were not expecting this, you can\n"
            f"ignore it and nothing will happen.\n\n"
            f"Qualified Commercial\n"
        )
        email_res = _send_email(to, f"Action needed for {business_name}", body)

    sms_res: DeliveryResult | None = None
    if channel == "sms":
        phone = normalize_phone(to_phone)
        if not phone:
            sms_res = DeliveryResult(
                False, "sms", "That phone number does not look complete."
            )
        elif not sms_consent_ok:
            sms_res = DeliveryResult(
                False,
                "sms",
                "This number has not agreed to receive texts, so only the email went.",
            )
        else:
            # Short by necessity, and it names the business so it does not read
            # as a phishing text. The brand and STOP are in every message
            # because carriers require identification and an opt-out path in
            # the message itself, not only at sign-up.
            sms_res = _send_sms(
                phone,
                f"{who}: to {purpose} for {business_name}, open {url} Reply STOP to opt out.",
            )

    email_ok = bool(email_res and email_res.ok)
    sms_ok = bool(sms_res and sms_res.ok)

    if not email_res and not sms_res:
        return DeliveryResult(
            False, "none", "No email address or mobile number on file for this client."
        )

    if email_ok and sms_ok:
        detail = "Emailed and texted."
    elif email_ok and sms_res is not None:
        detail = f"Emailed. {sms_res.detail}"
    elif email_ok:
        detail = "Emailed."
    elif sms_ok:
        detail = f"Texted, but the email failed: {email_res.detail if email_res else 'no address on file'}"
    else:
        parts = [r.detail for r in (email_res, sms_res) if r is not None]
        detail = " ".join(parts) or "Could not reach this client."

    channel_label = (
        "email+sms" if email_ok and sms_ok else "email" if email_ok else "sms" if sms_ok else "none"
    )
    return DeliveryResult(
        email_ok or sms_ok,
        channel_label,
        detail,
        email_ok,
        sms_ok,
        provider=sms_res.provider if sms_res is not None else "ses" if email_res is not None else None,
        provider_message_id=(
            sms_res.provider_message_id
            if sms_res is not None and sms_res.provider_message_id
            else email_res.provider_message_id if email_res is not None else None
        ),
        sender=sms_res.sender if sms_res is not None else None,
        delivery_status=(
            sms_res.delivery_status
            if sms_res is not None
            else "sent" if email_ok else "failed"
        ),
    )


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
    from app.services.sms.optout import is_opted_out

    from . import sms_consent as sms_consent_svc

    ok = False
    if kwargs.get("channel") == "sms":
        phone = normalize_phone(kwargs.get("to_phone"))
        if phone:
            grant = await sms_consent_svc.consent_for(
                db, phone_e164=phone, kind="transactional"
            )
            # The grant lifecycle and the suppression list are separate records
            # and both can veto. A number can hold a live grant from one file
            # and still have said STOP through some other channel.
            ok = grant is not None and not await is_opted_out(db, phone)

    import asyncio

    result = await asyncio.to_thread(deliver_link, sms_consent_ok=ok, **kwargs)

    # Ledger row for the SMS leg. This path does not go through
    # send_sms_checked (the transport runs sync inside deliver_link), so the
    # write happens here. Body deliberately absent: consent links are tokened
    # pages and the module contract is that the link is never logged.
    if kwargs.get("channel") == "sms":
        phone = normalize_phone(kwargs.get("to_phone"))
        if phone:
            from app.services.sms import ledger

            from .sms_provider import selected_provider

            if result.sms_ok:
                status, detail = "sent", ""
            elif not ok:
                status, detail = "blocked", "No transactional consent, or number opted out."
            else:
                status, detail = "failed", result.detail
            await ledger.record(
                db, direction="outbound", phone_e164=phone, status=status,
                provider=getattr(result, "provider", None) or selected_provider(),
                provider_message_id=getattr(result, "provider_message_id", None) or "",
                detail=detail, context="consent_link",
            )

    return result
