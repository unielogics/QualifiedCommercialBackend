"""SMS consent the client gives for themselves while they start an application.

The desk used to record this on the client's behalf, from a panel under the
composer: a rep ticking boxes for a person who may or may not have seen the
wording. The strongest proof is the person ticking the box on their own device,
at the moment they hand over their number, so the start of every AI application
now carries the choices and this module is what turns them into the record.

What is stored is the same thing the desk's panel stores (one `dos_sms_consent`
row per message type, the wording taken from the server's own copy, the IP and
browser as proof), with `method="self_web"` and the client as the consenter.
Texts are optional by the disclosure's own words, so nothing here is ever
required to start; declining is recorded too, as part of the intake's
`legal_acceptance`, so the file can show the client was asked.

The recorders never raise into the request that called them. A file must open
even when the consent row cannot be written; the choice is still on the intake.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.services import consent_delivery
from app.dealer_os.services import sms_consent as sms_consent_svc

log = logging.getLogger(__name__)

KINDS = ("transactional", "marketing")


def client_ip(request: Any) -> str | None:
    headers = getattr(request, "headers", None) or {}
    forwarded = headers.get("x-forwarded-for", "") if hasattr(headers, "get") else ""
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    client = getattr(request, "client", None)
    return client.host[:64] if client is not None and client.host else None


def choices_of(payload: Any) -> dict[str, bool]:
    return {
        "transactional": bool(getattr(payload, "sms_transactional_consent", False)),
        "marketing": bool(getattr(payload, "sms_marketing_consent", False)),
    }


def require_application_choices(payload: Any) -> None:
    from fastapi import HTTPException

    if not consent_delivery.normalize_phone(getattr(payload, "business_phone", None)):
        raise HTTPException(422, "Enter a valid business phone number.")
    if getattr(payload, "employee_count", None) is None:
        raise HTTPException(422, "Enter the number of employees.")
    if not payload.application_email_consent or not payload.sms_transactional_consent:
        raise HTTPException(422, "Consent to application text messages and emails is required.")


def choices_record(payload: Any, *, email: str | None = None) -> dict[str, Any]:
    """What the client chose, for the intake's `legal_acceptance`: kept whether
    they agreed or not, with the wording's version, so "we asked" is provable."""
    chosen = choices_of(payload)
    record = {"sms_choices": {**chosen, "disclosure_version": sms_consent_svc.SMS_DISCLOSURE_VERSION}}
    if getattr(payload, "application_email_consent", False):
        record["application_email_consent"] = {
            "granted": True,
            "version": "application-email-v1",
            "email": str(email or getattr(payload, "email", None) or getattr(payload, "contact_email", "")),
        }
    return record


async def record_choices(
    db: AsyncSession,
    profile: Any,
    *,
    phone: str | None,
    consenter_name: str | None,
    payload: Any,
    request: Any,
    entry: str,
) -> list[str]:
    """Write the grants the client ticked. Flushes; the caller commits.

    Returns the kinds that were recorded: none when nothing was ticked, when
    the number cannot be made a confident E.164 (the gate would never match
    it), or when that number already holds an active grant of the kind.
    """
    from app.services import application_profiles as profiles
    from app.services import file_events

    chosen = choices_of(payload)
    number = consent_delivery.normalize_phone(phone)
    if not number or not any(chosen.values()):
        return []
    recorded: list[str] = []
    for kind in KINDS:
        if not chosen[kind]:
            continue
        if await sms_consent_svc.consent_for(db, phone_e164=number, kind=kind) is not None:
            continue
        await sms_consent_svc.record_consent(
            db,
            dealer_id=None,
            profile_id=profile.id,
            phone_e164=number,
            kind=kind,
            method="self_web",
            captured_by_user_id=None,
            captured_by_name="The client, on the application",
            consenter_name=(consenter_name or None),
            ip_address=client_ip(request),
            user_agent=(getattr(request, "headers", None) or {}).get("user-agent"),
        )
        recorded.append(kind)
    if recorded:
        words = " and ".join(
            "application messages" if kind == "transactional" else "marketing messages"
            for kind in recorded
        )
        await profiles.log_profile_action(
            db,
            profile,
            None,
            "sms.consent_captured",
            f"The client agreed to {words} while {entry}",
            metadata={"phone": number, "kinds": recorded, "method": "self_web", "entry": entry},
        )
        await file_events.emit(
            db,
            profile=profile,
            kind="sms.consent_given",
            visibility=file_events.VISIBILITY_DESK,
            title="The client agreed to text messages",
            body=f"They agreed to {words} while {entry}.",
            target_type="application_profile",
            target_id=profile.id,
            meta={"kinds": recorded, "method": "self_web"},
        )
    return recorded


async def record_for_start(
    db: AsyncSession, intake: Any, payload: Any, request: Any, *, entry: str
) -> list[str]:
    """After an application's first commit: give the file its profile and write
    whatever the client ticked. Never raises; a failure leaves the intake as it
    was, with the choice still recorded on it."""
    if not any(choices_of(payload).values()):
        return []
    try:
        from app.services import application_profiles as profiles

        profile = await profiles.provision_profile_for_intake(db, intake)
        recorded = await record_choices(
            db,
            profile,
            phone=getattr(intake, "phone", None),
            consenter_name=getattr(intake, "full_name", None),
            payload=payload,
            request=request,
            entry=entry,
        )
        await db.commit()
        return recorded
    except Exception:  # noqa: BLE001 - the application must open either way
        log.exception(
            "onboarding consent could not be recorded intake=%s", getattr(intake, "id", None)
        )
        await db.rollback()
        return []
