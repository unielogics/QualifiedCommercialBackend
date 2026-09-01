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
from dataclasses import dataclass

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


def send_sms(to_phone: str, body: str) -> SmsResult:
    """Raw transport send — no gates, no ledger. Prefer send_sms_checked."""
    result = _provider().send_sms(to_phone, body)
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
    context: str = "",
    ledger_body: str | None = None,
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

    async def _blocked(detail: str, phone_for_row: str) -> SmsResult:
        # A refused send is a ledger row, not an absence — "why didn't the
        # text go out" must be answerable from a record.
        await ledger.record(
            db, direction="outbound", phone_e164=phone_for_row, status="blocked",
            body=recorded_body, provider=provider_name, detail=detail,
            context=context, client_id=client_id,
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

    result = await asyncio.to_thread(send_sms, phone, body)
    await ledger.record(
        db, direction="outbound", phone_e164=phone,
        status="sent" if result.ok else "failed",
        body=recorded_body, provider=result.provider,
        provider_message_id=result.message_id, detail=result.detail,
        context=context, client_id=client_id,
    )
    return result
