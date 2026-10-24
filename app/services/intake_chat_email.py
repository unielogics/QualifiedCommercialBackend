"""AI-requested outstanding-items email from the client-facing room chat.

The client-facing model may ask for one email per turn by returning
``requested_email`` in its JSON answer.  It can only ask for the standing
outstanding-items reminder (``kind == "outstanding_items"``); the actual
send goes through :func:`missing_item_automation.send_requirements_email`
so every existing gate (client-visible requirement, opt-out, already
complete, active room link, verified recipient) still applies.  This
module adds the chat-specific guards and never lets a failure reach the
client: the answer the model wrote stays as it is and the outcome is
recorded under ``message.metadata_json["email"]``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile, ApplicationRoomDelivery
from app.models.bucket import BucketAIMessage, BucketUploadLink
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import missing_item_automation
from app.services.application_programs import get_program_readiness, profile_for_chat_scope

# Recorded on the ApplicationRoomDelivery row so the 24-hour guard, the
# activity log, and the delivery audit all say the same thing about who
# asked for the email: the chat AI, on the client's request, not a person.
INITIATION_SOURCE = "ai_chat"
EMAIL_KIND_OUTSTANDING_ITEMS = "outstanding_items"
# At most one AI-initiated email per profile inside this window, counted on
# the delivery rows themselves (sent or failed) so a retry loop cannot
# re-send on every turn.
AI_EMAIL_WINDOW = timedelta(hours=24)

CONFIRMATION_EN = "I've emailed this to {recipient}."
CONFIRMATION_ES = "Se lo envié por email a {recipient}."


def now() -> datetime:
    return datetime.now(UTC)


def mask_email(value: str | None) -> str:
    if not value or "@" not in value:
        return "***"
    local, domain = value.split("@", 1)
    return f"{local[:2]}***@{domain}"


def requested_email_from(message: BucketAIMessage) -> dict[str, Any] | None:
    """The model's email request, or None when it did not ask for the supported kind."""
    raw = (message.metadata_json or {}).get("raw")
    if not isinstance(raw, dict):
        return None
    requested = raw.get("requested_email")
    if not isinstance(requested, dict):
        return None
    if requested.get("kind") != EMAIL_KIND_OUTSTANDING_ITEMS:
        return None
    return requested


def outstanding_requirement_keys(readiness: Any) -> list[str]:
    """Client-visible required items the client still has to act on."""
    return [
        item.requirement_key
        for item in readiness.requirements
        if item.client_visible
        and item.required_level == "required"
        and missing_item_automation._client_submission_needed(item)
    ]


async def recent_ai_delivery(
    db: AsyncSession, profile_id: uuid.UUID, *, since: datetime
) -> ApplicationRoomDelivery | None:
    return (
        await db.execute(
            select(ApplicationRoomDelivery)
            .where(
                ApplicationRoomDelivery.profile_id == profile_id,
                ApplicationRoomDelivery.initiation_source == INITIATION_SOURCE,
                ApplicationRoomDelivery.created_at >= since,
            )
            .order_by(ApplicationRoomDelivery.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _record(message: BucketAIMessage, receipt: dict[str, Any]) -> dict[str, Any]:
    # Reassign rather than mutate: JSONB columns only see a new dict.
    message.metadata_json = {**(message.metadata_json or {}), "email": receipt}
    return receipt


async def _conversation_is_spanish(db: AsyncSession, profile: ApplicationProfile) -> bool:
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    return bool(intake and intake.preferred_language == "es")


async def fulfil_requested_email(
    db: AsyncSession,
    *,
    message: BucketAIMessage,
    upload_link: BucketUploadLink | None,
    intake_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """Send the outstanding-items email the model asked for, if it may.

    Returns None when nothing was requested (or the thread is not the client's),
    otherwise the receipt that was also stored under ``metadata_json["email"]``.
    Never raises for a refused or failed send.
    """
    if message.role != "assistant" or message.audience != "uploader":
        return None
    requested = requested_email_from(message)
    if requested is None:
        return None
    reason = str(requested.get("reason") or "")[:500] or None

    profile = await profile_for_chat_scope(
        db,
        bucket_id=message.bucket_id,
        intake_id=intake_id,
        upload_link_id=upload_link.id if upload_link else message.upload_link_id,
    )
    if profile is None:
        return _record(
            message,
            {"ok": False, "detail": "No application file is linked to this conversation", "reason": reason},
        )

    recent = await recent_ai_delivery(db, profile.id, since=now() - AI_EMAIL_WINDOW)
    if recent is not None:
        return _record(
            message,
            {
                "ok": False,
                "detail": "An AI-initiated email was already sent for this file in the last 24 hours",
                "skipped": "rate_limited",
                "delivery_id": str(recent.id),
                "reason": reason,
            },
        )

    readiness = await get_program_readiness(db, profile)
    keys = outstanding_requirement_keys(readiness)
    if not keys:
        return _record(
            message,
            {"ok": False, "detail": "Nothing is outstanding for the client to send", "reason": reason},
        )

    try:
        delivery = await missing_item_automation.send_requirements_email(
            db,
            profile=profile,
            requirement_keys=keys,
            user=None,
            initiation_source=INITIATION_SOURCE,
        )
    except HTTPException as exc:
        return _record(message, {"ok": False, "detail": str(exc.detail), "reason": reason})

    ok = delivery.status == "sent"
    masked = mask_email(delivery.recipient_email)
    receipt: dict[str, Any] = {
        "ok": ok,
        "detail": delivery.detail or ("Email accepted by the provider" if ok else "Email was not sent"),
        "delivery_id": str(delivery.id),
        "recipient_masked": masked,
        "requirement_keys": list(keys),
        "initiation_source": INITIATION_SOURCE,
        "reason": reason,
    }
    if ok:
        template = CONFIRMATION_ES if await _conversation_is_spanish(db, profile) else CONFIRMATION_EN
        line = template.format(recipient=masked)
        if line not in message.content:
            message.content = f"{message.content.rstrip()}\n\n{line}"
    return _record(message, receipt)
