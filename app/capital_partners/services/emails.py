"""Every email the program sends, through the one outbox door.

Plain text, short, no offering terms in any message that could be forwarded:
a subscription email names the loan reference and the amount the member
chose, never the rate. Wiring instructions travel only in the documents
email to the subscribing member.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.messaging import outbox

log = logging.getLogger(__name__)

CONTEXT = "capital_partners"


def _portal() -> str:
    return get_settings().capital_app_url.rstrip("/")


async def _send(db: AsyncSession, *, to: str, subject: str, body: str, template_key: str, secrets: tuple = ()) -> bool:
    if not to:
        return False
    draft = outbox.Draft(to=to, subject=subject, body_text=body, secrets=secrets)
    try:
        outcome = await outbox.deliver_email(db, draft, context=CONTEXT, template_key=template_key)
        return bool(getattr(outcome, "ok", False))
    except Exception:
        log.exception("capital: email %s to %s failed", template_key, to)
        return False


async def notify_desk(db: AsyncSession, *, recipients: list[str], subject: str, body: str, template_key: str) -> int:
    sent = 0
    for to in recipients:
        if await _send(db, to=to, subject=subject, body=body, template_key=template_key):
            sent += 1
    return sent


def access_request_desk_body(req: Any) -> str:
    return (
        "A new capital partner introduction request.\n\n"
        f"Name: {req.full_name}\n"
        f"Email: {req.email}\n"
        f"Phone: {req.phone}\n"
        f"Investing as: {req.investing_as}\n"
        f"Entity: {req.entity_name or 'n/a'}\n"
        f"Capital band: {req.capital_band}\n"
        f"How they heard about us: {req.source}\n"
        f"Language: {req.locale}\n\n"
        f"{req.note or '(no note)'}\n\n"
        "Review it under Marketplace > Members > Access requests."
    )


async def member_invited(db: AsyncSession, *, to: str, name: str | None) -> bool:
    first = (name or "").split(" ")[0] or "there"
    body = (
        f"Hello {first},\n\n"
        "Thank you for the conversation. Your invitation to the Qualified Commercial capital partner portal is on its way "
        "in a separate email from our sign-in provider. Once your account is open, the next step is a short "
        "accreditation questionnaire. No offering is shown until we have reviewed it and the waiting period has passed.\n\n"
        f"Portal: {_portal()}/sign-in\n\n"
        "This message is not an offer to sell or a solicitation of an offer to buy any security.\n"
    )
    return await _send(db, to=to, subject="Your capital partner invitation", body=body, template_key="cp.member_invited")


async def questionnaire_received(db: AsyncSession, *, to: str, name: str | None) -> bool:
    first = (name or "").split(" ")[0] or "there"
    body = (
        f"Hello {first},\n\n"
        "We received your accreditation questionnaire. Our principal will review it and let you know the outcome. "
        "If we need anything else, we will ask.\n"
    )
    return await _send(db, to=to, subject="Questionnaire received", body=body, template_key="cp.questionnaire_received")


async def member_approved(db: AsyncSession, *, to: str, name: str | None, waiting_ends: str | None) -> bool:
    first = (name or "").split(" ")[0] or "there"
    when = f" Offerings become visible to you on {waiting_ends}." if waiting_ends else " Offerings are visible to you now."
    body = (
        f"Hello {first},\n\n"
        f"Your accreditation review is complete and your membership is approved.{when}\n\n"
        f"Portal: {_portal()}/sign-in\n\n"
        "Participations are restricted securities, are not FDIC insured, and may lose value. "
        "Read each private placement memorandum before subscribing.\n"
    )
    return await _send(db, to=to, subject="Your membership is approved", body=body, template_key="cp.member_approved")


async def member_needs_changes(db: AsyncSession, *, to: str, name: str | None, notes: str | None) -> bool:
    first = (name or "").split(" ")[0] or "there"
    body = (
        f"Hello {first},\n\n"
        "We reviewed your accreditation questionnaire and need one more pass from you.\n\n"
        f"{notes or 'Please sign in and review the questionnaire.'}\n\n"
        f"Portal: {_portal()}/questionnaire\n"
    )
    return await _send(db, to=to, subject="One more step on your questionnaire", body=body, template_key="cp.member_needs_changes")


async def subscription_received_desk(db: AsyncSession, *, recipients: list[str], ref: str, member_name: str, amount: str) -> int:
    body = (
        f"{member_name} requested a subscription of {amount} in {ref}.\n\n"
        "Send the subscription documents from Marketplace > Listings > Subscriptions."
    )
    return await notify_desk(
        db, recipients=recipients, subject=f"Subscription request: {ref}", body=body, template_key="cp.subscription_received"
    )


async def subscription_documents_sent(
    db: AsyncSession, *, to: str, name: str | None, ref: str, amount: str, wiring_instructions: str | None
) -> bool:
    first = (name or "").split(" ")[0] or "there"
    wiring = f"\n\nWiring instructions:\n{wiring_instructions}\n" if wiring_instructions else ""
    body = (
        f"Hello {first},\n\n"
        f"Your allocation of {amount} in {ref} is held while you sign and fund. The subscription documents follow "
        "from our document service; sign them, then fund by wire. Your allocation is confirmed once the countersigned "
        f"agreement and the funds are in.{wiring}\n\n"
        "Participations are restricted securities, are not FDIC insured, and may lose value.\n"
    )
    return await _send(
        db,
        to=to,
        subject=f"Subscription documents: {ref}",
        body=body,
        template_key="cp.subscription_documents_sent",
        secrets=(wiring_instructions,) if wiring_instructions else (),
    )


async def subscription_confirmed(db: AsyncSession, *, to: str, name: str | None, ref: str, amount: str) -> bool:
    first = (name or "").split(" ")[0] or "there"
    body = (
        f"Hello {first},\n\n"
        f"Your participation of {amount} in {ref} is funded and appears in your portfolio. "
        "The independent servicer's reporting will show there as payments come in.\n\n"
        f"Portal: {_portal()}/portfolio\n"
    )
    return await _send(db, to=to, subject=f"Participation confirmed: {ref}", body=body, template_key="cp.subscription_confirmed")


async def transfer_requested_desk(db: AsyncSession, *, recipients: list[str], transfer_ref: str, listing_ref: str) -> int:
    body = (
        f"A member requested transfer {transfer_ref} in {listing_ref}. "
        "It needs your consent under Marketplace > Transfers before anything moves."
    )
    return await notify_desk(
        db, recipients=recipients, subject=f"Transfer request: {transfer_ref}", body=body, template_key="cp.transfer_requested"
    )


async def transfer_decided(db: AsyncSession, *, to: str, name: str | None, transfer_ref: str, decision: str) -> bool:
    first = (name or "").split(" ")[0] or "there"
    body = f"Hello {first},\n\nTransfer {transfer_ref} was {decision}. Details are in the portal under Secondary.\n"
    return await _send(db, to=to, subject=f"Transfer {transfer_ref}: {decision}", body=body, template_key="cp.transfer_decided")
