"""Emailing a client the link that opens one of our online forms.

The debt schedule, the personal financial statement and the two business
statements can each be filled in on a page that needs no sign-in. Until now the
desk could copy that link, or put the form on the file's checklist, and the AI's
reminders carried the secure room's address. Nobody could send the form's own
link in one click, which is what the desk reaches for when a file has no debt
schedule yet.

One call does the whole job and is safe to repeat:

* the form goes on the checklist if it is not there (the same idempotent write
  the "request" route makes);
* a fresh link is minted for the client (the token is stored hashed; this is
  the only moment it exists in readable form);
* one plain email goes to the verified client address, from the staff member's
  own mailbox when they have connected one and from the firm's transport when
  they have not, with the secure room's address as the way to upload a file
  instead;
* the delivery is recorded without the token or the room PIN, the requirement
  is marked as asked, and the file's timeline says who sent it.

A second click inside two minutes returns the first send instead of mailing the
client twice.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dealer_os.services import client_room
from app.models.application_profile import (
    ApplicationProfile,
    ApplicationRequirementState,
    ApplicationRoomDelivery,
)
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import application_profiles as profiles
from app.services import file_events, financial_statements, pfs_schema
from app.services.application_programs import email_is_suppressed
from app.services.email.user_mailer import send_as_user

ACTION_KIND = "form_link_email"
INITIATION_SOURCE = "staff_form_link"

#: Two clicks closer together than this are one request.
DUPLICATE_WINDOW = timedelta(minutes=2)

#: The requirement each form answers on the checklist, so the row can say it was asked.
REQUIREMENT_KEYS = {
    "debt_schedule": "business_debt_schedule",
    "pfs": "owner_personal_financial_statement",
    "p_and_l": "ytd_p_and_l_balance_sheet",
    "balance_sheet": "ytd_p_and_l_balance_sheet",
    "real_estate_schedule": "real_estate_schedule",
}


def _long_date(value: datetime | None) -> str | None:
    if value is None:
        return None
    return f"{value:%B} {value.day}, {value:%Y}"


def compose_form_link_email(
    *, label: str, url: str, expires_on: str | None, room_url: str | None, sender: str | None
) -> tuple[str, str]:
    """The subject and plain-text body. No PIN, no token other than the form's own."""
    lines = [
        f"To keep your application moving, we need your {label.lower()}.",
        "",
        "The quickest way is to fill it in online. You do not need to sign in:",
        url,
    ]
    if expires_on:
        lines += ["", f"This link is for you and works until {expires_on}."]
    if room_url:
        lines += [
            "",
            "Prefer to send a file you already have? Upload it in your secure application room instead:",
            room_url,
        ]
    lines += ["", "For security, your room PIN is not included in this email."]
    if sender:
        lines += ["", "Thank you,", sender]
    return f"Action needed: {label}", "\n".join(lines)


async def _recipient(db: AsyncSession, profile: ApplicationProfile, room_link: Any) -> str | None:
    """The verified address every other client request uses."""
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    return profiles.normalized_email(
        (intake.email if intake else None)
        or (client.email if client else None)
        or (room_link.recipient_email if room_link is not None else None)
    )


async def _placeholder_statement_id(db: AsyncSession, profile: ApplicationProfile, user: Any) -> uuid.UUID | None:
    """A personal statement link fills a statement, so there has to be one. The
    debt schedule writes straight to the file's rows and needs no placeholder."""
    statement = await financial_statements.latest_for_profile(db, profile.id)
    if statement is None:
        statement = await financial_statements.save_statement(
            db, profile, body=pfs_schema.empty_body(), actor_user_id=user.id
        )
    return statement.id


async def email_form_link(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    kind: str,
    label: str,
    slot: Any,
    user: Any,
) -> ApplicationRoomDelivery:
    """Mint the form's link and email it. Raises 409 when there is nobody to email."""
    room_link = (
        await client_room.active_link(db, profile.primary_bucket_id) if profile.primary_bucket_id else None
    )
    recipient = await _recipient(db, profile, room_link)
    if not recipient:
        raise HTTPException(status.HTTP_409_CONFLICT, "No verified client email is available")
    if profile.client_id and await email_is_suppressed(db, profile.client_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Client opted out of automated email")

    now = datetime.now(UTC)
    recent = (
        await db.execute(
            select(ApplicationRoomDelivery)
            .where(
                ApplicationRoomDelivery.profile_id == profile.id,
                ApplicationRoomDelivery.action_kind == ACTION_KIND,
                ApplicationRoomDelivery.requested_document_id == getattr(slot, "id", None),
                ApplicationRoomDelivery.status == "sent",
                ApplicationRoomDelivery.created_at >= now - DUPLICATE_WINDOW,
            )
            .order_by(ApplicationRoomDelivery.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if recent is not None:
        return recent

    statement_id = await _placeholder_statement_id(db, profile, user) if kind == "pfs" else None
    real_estate_workspace = None
    if kind == "real_estate_schedule":
        from app.services.real_estate_forms import ensure_workspace

        real_estate_workspace = await ensure_workspace(db, profile, actor=user)
    link, token = await financial_statements.mint_link(
        db,
        profile,
        kind=kind,
        statement_id=statement_id,
        label="Emailed to the client",
        invitee_email=recipient,
        created_by=user.id,
    )
    if real_estate_workspace:
        link.financial_family_id = uuid.UUID(real_estate_workspace["family_id"])
    base = get_settings().frontend_app_url.rstrip("/")
    room_url = None
    if room_link is not None:
        room_url = f"{base}/buckets/request/{room_link.token}?tab=todo"
        if getattr(slot, "id", None):
            room_url += f"&request={slot.id}"
    sender = (getattr(user, "name", None) or "").strip() or None
    subject, body = compose_form_link_email(
        label=label,
        url=f"{base}/forms/{kind}/{token}",
        expires_on=_long_date(getattr(link, "expires_at", None)),
        room_url=room_url,
        sender=sender,
    )
    result = await send_as_user(
        db, user.id, to_emails=[recipient], subject=subject, body_text=body
    )

    delivery = ApplicationRoomDelivery(
        profile_id=profile.id,
        bucket_id=profile.primary_bucket_id,
        requested_document_id=getattr(slot, "id", None),
        action_kind=ACTION_KIND,
        channel="email",
        recipient_email=recipient,
        status="sent" if result.ok else "failed",
        detail=result.detail,
        provider_result={"accepted": result.ok, "message_id": result.message_id},
        created_by_user_id=user.id,
        initiation_source=INITIATION_SOURCE,
        idempotency_key=f"formlink:{hashlib.sha256(f'{profile.id}:{kind}:{uuid.uuid4()}'.encode()).hexdigest()}",
        attempt_number=1,
    )
    db.add(delivery)

    state = (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == profile.id,
                ApplicationRequirementState.requirement_key == REQUIREMENT_KEYS.get(kind, ""),
            )
        )
    ).scalar_one_or_none()
    if state is not None and result.ok:
        state.last_requested_at = now
        state.first_requested_at = state.first_requested_at or now
        if state.status == "missing":
            state.status = "requested"
    await db.flush()

    await profiles.log_profile_action(
        db,
        profile,
        user,
        "financial_form.link_emailed" if result.ok else "financial_form.link_email_failed",
        f"{label}: emailed the client a link to fill it in online" if result.ok else f"{label}: {result.detail}",
        target_type="financial_form",
        target_id=link.id,
        metadata={"kind": kind, "delivery_id": str(delivery.id), "provider_accepted": result.ok},
    )
    if result.ok:
        await file_events.emit(
            db,
            profile=profile,
            kind="document.link_emailed",
            visibility=file_events.VISIBILITY_TEAM,
            title=f"Emailed the client a link to fill in the {label.lower()}",
            actor=user,
            target_type="financial_form",
            target_id=link.id,
            meta={"kind": kind},
        )
    return delivery
