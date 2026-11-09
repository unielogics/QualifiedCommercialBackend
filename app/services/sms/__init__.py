"""The guarded seam for outbound SMS: opt-out gate, consent check, ledger.

Transport lives in `app.dealer_os.services.sms_provider` — one adapter module
for aws | twilio | android, selected by `SMS_PROVIDER`, no silent fallback.
This package deliberately owns none of that. What it owns is everything that
must happen around a send regardless of transport:

  optout    the number-keyed suppression list. A STOP anywhere makes a number
            unreachable everywhere — including numbers that never held a
            consent grant, which revocation alone cannot record.
  ledger    one dated row per message, both directions, refused sends
            included, in `sms_messages`.
  send_sms_checked
            the entry point every call site should use: normalize, check
            suppression, optionally require a named consent kind, send,
            record.

History note: this package briefly carried its own aws/twilio adapters; they
were removed in favour of the dealer_os adapter module when the two lines of
work merged, so each transport has exactly one implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from .optout import is_opted_out, record_opt_out

log = logging.getLogger(__name__)

__all__ = [
    "SmsResult",
    "send_sms",
    "send_sms_checked",
    "sms_available",
    "unavailable_reason",
    "is_opted_out",
    "record_opt_out",
]


@dataclass(frozen=True)
class SmsResult:
    """Transport outcome, as the gated call sites consume it."""

    ok: bool
    provider: str
    message_id: str = ""
    detail: str = ""


def _provider():
    # Lazy: dealer_os.services.sms_provider must stay importable without this
    # package and vice versa.
    from app.dealer_os.services import sms_provider

    return sms_provider


def sms_available() -> bool:
    """Whether a send can actually reach a stranger's phone right now."""
    return _provider().sms_available()


def unavailable_reason() -> str:
    """Why not, for operators. Empty string when SMS is available."""
    readiness = _provider().provider_readiness()
    return "" if _provider().sms_available() else str(readiness["detail"])


def send_sms(to_phone: str, body: str, *, media_urls: list[str] | None = None) -> SmsResult:
    """Raw transport send — no gates, no ledger. Prefer send_sms_checked."""
    result = (_provider().send_sms(to_phone, body, media_urls=media_urls)
              if media_urls else _provider().send_sms(to_phone, body))
    return SmsResult(
        ok=result.ok,
        provider=result.provider,
        message_id=result.message_id or "",
        detail=result.detail,
    )


async def send_sms_checked(
    db,
    *,
    to_phone: str | None,
    body: str,
    require_consent_kind: str | None = None,
    client_id=None,
    profile_id=None,
    intake_id=None,
    portal_message_id=None,
    context: str = "",
    ledger_body: str | None = None,
    images=None,
) -> SmsResult:
    """The entry point every call site should use. Guarded, and async.

    Two questions are answered from the database before any transport runs:

      1. Is the number suppressed? See optout.py for why the suppression list
         exists alongside the dealer consent grants.
      2. If a consent kind is named, is there a live grant of that kind?
         `consent_for` is keyed on the number rather than the file, so a
         caller cannot launder a missing grant by opening a new file.

    Every outcome — blocked, failed, or sent — lands in the ledger. Callers
    that already resolved consent (deliver_link_checked does) pass
    `require_consent_kind=None` and get only the suppression check.
    """
    import asyncio

    from app.dealer_os.services.consent_delivery import normalize_phone

    from . import ledger

    provider_name = _provider().selected_provider()
    recorded_body = ledger_body if ledger_body is not None else body
    if images and ledger_body is None and not recorded_body.strip():
        recorded_body = "Image attachment"

    async def _blocked(detail: str, phone_for_row: str) -> SmsResult:
        # A refused send is a ledger row, not an absence — "why didn't the
        # text go out" must be answerable from a record.
        await ledger.record(
            db, direction="outbound", phone_e164=phone_for_row, status="blocked",
            body=recorded_body, provider=provider_name, detail=detail,
            context=context, client_id=client_id,
            profile_id=profile_id, intake_id=intake_id,
            portal_message_id=portal_message_id,
        )
        return SmsResult(False, provider_name, detail=detail)

    phone = normalize_phone(to_phone)
    if not phone:
        return await _blocked("No usable phone number.", (to_phone or "")[:20])

    if await is_opted_out(db, phone):
        log.info("sms suppressed: %s has opted out", phone)
        return await _blocked("This number has opted out of text messages.", phone)

    if require_consent_kind:
        from app.dealer_os.services import sms_consent as sms_consent_svc

        grant = await sms_consent_svc.consent_for(
            db, phone_e164=phone, kind=require_consent_kind
        )
        if grant is None:
            return await _blocked(
                f"No {require_consent_kind} SMS consent on file for this number.", phone
            )

    shares = []
    media_urls = None
    if images:
        from app.services import communication_images, inline_images

        mode = _provider().image_delivery_mode()
        if mode == "mms":
            if sum(len(image.data) for image in images) > 5 * 1024 * 1024:
                return await _blocked("MMS images must total no more than 5 MB. Attach smaller images or send them by email.", phone)
            media_urls = [inline_images.view_url(image.row) for image in images]
            if any(url is None for url in media_urls):
                return await _blocked("Image delivery is unavailable because image storage is not configured.", phone)
        elif mode == "secure_link":
            if not communication_images.sms_share_available():
                return await _blocked("Image links require a public HTTPS API address. Send the images by email instead.", phone)
            try:
                shares = await communication_images.reserve_sms_shares(db, images)
            except Exception:
                log.exception("SMS image-link preparation failed before sending")
                return await _blocked("Image links could not be prepared. No text was sent; try again.", phone)
            links = "\n".join(communication_images.sms_share_url(row) for row in shares)
            body = f"{body.rstrip()}\n\nImages (links expire in 7 days):\n{links}".lstrip()
        else:
            return await _blocked("The selected SMS provider cannot deliver images.", phone)

    result = (await asyncio.to_thread(send_sms, phone, body, media_urls=media_urls)
              if media_urls else await asyncio.to_thread(send_sms, phone, body))
    async def record_result():
        return await ledger.record(
            db, direction="outbound", phone_e164=phone,
            status="sent" if result.ok else "failed",
            body=recorded_body, provider=result.provider,
            provider_message_id=result.message_id, detail=result.detail,
            context=context, client_id=client_id,
            profile_id=profile_id, intake_id=intake_id,
            portal_message_id=portal_message_id,
        )

    if images:
        from app.services import communication_images

        try:
            async with db.begin_nested():
                row = await record_result()
                if row is not None:
                    await communication_images.bind(db, images, subject_kind="sms_message", subject_id=str(row.id))
        except Exception:
            log.exception("SMS delivery completed but image history could not be stored")
            row = None
        if result.ok and shares:
            try:
                await communication_images.activate_sms_shares(shares, row.id if row is not None else None)
            except Exception:
                log.exception("Text was sent but its image links could not be activated")
                result = replace(result, detail="sent_but_image_links_unavailable")
        if result.ok and row is None and result.detail != "sent_but_image_links_unavailable":
            result = replace(result, detail="sent_not_recorded")
    else:
        row = await record_result()
    if not result.ok:
        try:
            from app.services.notifications import notify_sms_delivery_failure

            await notify_sms_delivery_failure(
                db,
                sms_message_id=row.id if row else None,
                phone_e164=phone,
                provider=result.provider,
                detail=result.detail,
                client_id=client_id,
                profile_id=profile_id,
                intake_id=intake_id,
            )
        except Exception:  # noqa: BLE001
            # Never replace the true SMS result or lose its ledger row because
            # a secondary operator notification could not be created.
            log.exception("failed to create operator notification for SMS failure")
    return result
