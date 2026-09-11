"""Readiness-aware missing-item email delivery for AI Intake profiles."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

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
from app.models.bucket import BucketUploadLink
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.services import application_profiles as profiles
from app.services.application_programs import email_is_suppressed, get_program_readiness
from app.services.email.user_mailer import send_as_user
from app.services.financial_templates import template_for_requirement


def now() -> datetime:
    return datetime.now(UTC)


def _idempotency_key(*parts: object) -> str:
    material = ":".join(str(part) for part in parts)
    return f"missing:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


async def _active_room_link(db: AsyncSession, profile: ApplicationProfile) -> BucketUploadLink:
    if profile.primary_bucket_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This file has no secure room")
    link = await client_room.active_link(db, profile.primary_bucket_id)
    if link is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This file has no active secure room")
    return link


async def _recipient(db: AsyncSession, profile: ApplicationProfile, link: BucketUploadLink) -> str:
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    email = profiles.normalized_email(
        (intake.email if intake else None)
        or (client.email if client else None)
        or link.recipient_email
    )
    if not email:
        raise HTTPException(status.HTTP_409_CONFLICT, "No verified client email is available")
    return email


async def send_requirement_email(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requirement_key: str,
    user: User | None,
    initiation_source: str,
    retry_failed: bool = False,
) -> ApplicationRoomDelivery:
    readiness = await get_program_readiness(db, profile)
    requirement_read = next(
        (item for item in readiness.requirements if item.requirement_key == requirement_key),
        None,
    )
    if requirement_read is None or not requirement_read.client_visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client-visible requirement not found")
    if profile.client_id and await email_is_suppressed(db, profile.client_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Client opted out of automated email")
    is_blocking = any(
        requirement_key in program.blocking_requirement_keys
        for program in readiness.programs
    )
    if not is_blocking:
        raise HTTPException(status.HTTP_409_CONFLICT, "This requirement is already complete")
    requirement = (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == profile.id,
                ApplicationRequirementState.requirement_key == requirement_key,
            )
        )
    ).scalar_one()
    link = await _active_room_link(db, profile)
    recipient = await _recipient(db, profile, link)
    timestamp = now()
    is_automatic = initiation_source == "automatic_missing_item"
    if is_automatic:
        if not readiness.automation.enabled or not readiness.automation.eligible:
            raise HTTPException(status.HTTP_409_CONFLICT, readiness.automation.stop_reason or "Automation is not eligible")
        if profile.missing_item_email_last_sent_at and profile.missing_item_email_last_sent_at > timestamp - timedelta(hours=24):
            raise HTTPException(status.HTTP_409_CONFLICT, "The 24-hour email cadence has not elapsed")
        idempotency_key = _idempotency_key(
            profile.id,
            requirement_key,
            timestamp.date().isoformat(),
        )
    else:
        idempotency_key = _idempotency_key(
            profile.id,
            requirement_key,
            initiation_source,
            uuid.uuid4(),
        )

    if retry_failed:
        failed = (
            await db.execute(
                select(ApplicationRoomDelivery)
                .where(
                    ApplicationRoomDelivery.profile_id == profile.id,
                    ApplicationRoomDelivery.requested_document_id == requirement.requested_document_id,
                    ApplicationRoomDelivery.action_kind == "missing_item_email",
                    ApplicationRoomDelivery.status == "failed",
                )
                .order_by(ApplicationRoomDelivery.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if failed:
            idempotency_key = _idempotency_key(
                "retry",
                failed.idempotency_key or failed.id,
                failed.attempt_number + 1,
            )

    existing = (
        await db.execute(
            select(ApplicationRoomDelivery).where(ApplicationRoomDelivery.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    room_url = f"{get_settings().frontend_app_url.rstrip('/')}/buckets/request/{link.token}?tab=todo"
    if requirement.requested_document_id:
        room_url += f"&request={requirement.requested_document_id}"
    template = template_for_requirement(requirement_key)
    attachments = [(template.filename, template.content, template.content_type)] if template else None
    body = (
        f"We still need {requirement.label} to continue reviewing your application.\n\n"
        f"Open your secure application room:\n{room_url}\n\n"
    )
    if template:
        body += "An approved blank template is attached. You may complete it or upload your own document.\n\n"
    body += "For security, your room PIN is not included in this email."
    result = await send_as_user(
        db,
        user.id if user else None,
        to_emails=[recipient],
        subject=f"Action needed: {requirement.label}",
        body_text=body,
        attachments=attachments,
    )
    attempt_number = profile.missing_item_email_attempts + 1 if is_automatic else 1
    delivery = ApplicationRoomDelivery(
        profile_id=profile.id,
        bucket_id=profile.primary_bucket_id,
        requested_document_id=requirement.requested_document_id,
        action_kind="missing_item_email",
        channel="email",
        recipient_email=recipient,
        status="sent" if result.ok else "failed",
        detail=result.detail,
        provider_result={"accepted": result.ok, "message_id": result.message_id},
        created_by_user_id=user.id if user else None,
        initiation_source=initiation_source,
        idempotency_key=idempotency_key,
        attempt_number=attempt_number,
    )
    db.add(delivery)
    requirement.last_requested_at = timestamp
    requirement.first_requested_at = requirement.first_requested_at or timestamp
    if requirement.status == "missing":
        requirement.status = "requested"
    if is_automatic:
        profile.missing_item_email_last_sent_at = timestamp
        profile.missing_item_email_next_send_at = timestamp + timedelta(hours=24)
        profile.missing_item_email_attempts = attempt_number
        profile.missing_item_email_requirement_key = requirement_key
    await db.flush()
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "requirement.reminder_sent" if result.ok else "requirement.reminder_failed",
        f"{requirement.label}: {result.detail}",
        target_type="requested_document",
        target_id=requirement.requested_document_id,
        metadata={
            "requirement_key": requirement_key,
            "channel": "email",
            "initiation_source": initiation_source,
            "delivery_id": str(delivery.id),
            "provider_accepted": result.ok,
        },
    )
    return delivery


async def run_missing_item_email_pass(db: AsyncSession, *, limit: int = 100) -> dict[str, int]:
    rows = list(
        (
            await db.execute(
                select(ApplicationProfile)
                .where(
                    ApplicationProfile.intake_id.is_not(None),
                    ApplicationProfile.missing_item_email_enabled.is_(True),
                    ApplicationProfile.underwriting_status.in_(
                        ["submitted", "collecting_docs", "in_underwriting", "term_sheet_provided", "approved"]
                    ),
                )
                .order_by(ApplicationProfile.missing_item_email_next_send_at.asc().nullsfirst())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).scalars().all()
    )
    sent = failed = skipped = 0
    for profile in rows:
        readiness = await get_program_readiness(db, profile)
        automation = readiness.automation
        if not automation.enabled or not automation.eligible or not automation.next_requirement_key:
            skipped += 1
            continue
        if automation.next_send_at and automation.next_send_at > now():
            skipped += 1
            continue
        try:
            delivery = await send_requirement_email(
                db,
                profile=profile,
                requirement_key=automation.next_requirement_key,
                user=None,
                initiation_source="automatic_missing_item",
            )
        except HTTPException:
            skipped += 1
            continue
        if delivery.status == "sent":
            sent += 1
        else:
            failed += 1
    return {"considered": len(rows), "sent": sent, "failed": failed, "skipped": skipped}
