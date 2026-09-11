"""Readiness-aware missing-item email delivery for AI Intake profiles."""

from __future__ import annotations

import hashlib
import logging
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
from app.services.ai.bedrock_client import get_client, model_light
from app.services.ai.usage import tracked_messages_create
from app.services.application_programs import email_is_suppressed, get_program_readiness
from app.services.email.user_mailer import send_as_user
from app.services.financial_templates import template_for_requirement

log = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(UTC)


def _idempotency_key(*parts: object) -> str:
    material = ":".join(str(part) for part in parts)
    return f"missing:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _client_submission_needed(requirement: object) -> bool:
    status_value = str(getattr(requirement, "status", ""))
    if status_value in {"stale", "failed"}:
        return True
    return not bool(getattr(requirement, "coverage_complete", False))


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
    if not _client_submission_needed(requirement_read):
        raise HTTPException(status.HTTP_409_CONFLICT, "Evidence is waiting for staff verification")
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


async def _compose_combined_intro(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requirement_labels: list[str],
    user: User | None,
) -> tuple[str, bool]:
    fallback = (
        "Hello,\n\n"
        "To keep your application moving, please provide the outstanding items listed below. "
        "You can submit everything in one visit to your secure application room."
    )
    if not get_settings().ai_provider_enabled:
        return fallback, False
    try:
        result = await tracked_messages_create(
            db,
            feature="missing_item_email",
            client=get_client(),
            model=model_light(),
            user_id=user.id if user else None,
            client_id=profile.client_id,
            loan_id=profile.loan_id,
            metadata={"profile_id": str(profile.id), "requirement_count": len(requirement_labels)},
            max_tokens=220,
            system=(
                "Write the opening of a concise, professional commercial-finance document request email. "
                "Output plain text only, with a greeting and at most two short paragraphs. Explain that the "
                "items are consolidated into one request. Do not add a subject, document list, URL, deadline, "
                "recipient, PIN, promise, or facts not supplied."
            ),
            messages=[
                {
                    "role": "user",
                    "content": "Outstanding items:\n- " + "\n- ".join(requirement_labels),
                }
            ],
        )
        text = "".join(
            block.text
            for block in result.content
            if getattr(block, "type", None) == "text"
        ).strip()
        return (text or fallback), bool(text)
    except Exception as exc:  # noqa: BLE001 - email delivery keeps a deterministic fallback
        log.warning("missing_item_automation: combined email draft failed: %s", exc)
        return fallback, False


async def send_requirements_email(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requirement_keys: list[str],
    user: User | None,
    initiation_source: str,
    retry_failed: bool = False,
) -> ApplicationRoomDelivery:
    requested_keys = list(dict.fromkeys(requirement_keys))
    if not requested_keys:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Select at least one requirement")
    readiness = await get_program_readiness(db, profile)
    readable = {item.requirement_key: item for item in readiness.requirements}
    if any(key not in readable or not readable[key].client_visible for key in requested_keys):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client-visible requirement not found")
    if profile.client_id and await email_is_suppressed(db, profile.client_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Client opted out of automated email")
    blocking_keys = {
        key
        for program in readiness.programs
        for key in program.blocking_requirement_keys
    }
    active_keys = [
        key
        for key in requested_keys
        if key in blocking_keys and _client_submission_needed(readable[key])
    ]
    if not active_keys:
        raise HTTPException(status.HTTP_409_CONFLICT, "The selected requirements are already complete")
    rows = list(
        (
            await db.execute(
                select(ApplicationRequirementState).where(
                    ApplicationRequirementState.profile_id == profile.id,
                    ApplicationRequirementState.requirement_key.in_(active_keys),
                )
            )
        ).scalars().all()
    )
    rows_by_key = {row.requirement_key: row for row in rows}
    requirements = [rows_by_key[key] for key in active_keys if key in rows_by_key]
    if len(requirements) != len(active_keys):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement state not found")

    link = await _active_room_link(db, profile)
    recipient = await _recipient(db, profile, link)
    timestamp = now()
    automatic = initiation_source == "automatic_missing_item"
    if automatic:
        if not readiness.automation.enabled or not readiness.automation.eligible:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                readiness.automation.stop_reason or "Automation is not eligible",
            )
        if (
            profile.missing_item_email_last_sent_at
            and profile.missing_item_email_last_sent_at > timestamp - timedelta(hours=24)
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "The 24-hour email cadence has not elapsed")
        idempotency_key = _idempotency_key(profile.id, *sorted(active_keys), timestamp.date().isoformat())
    else:
        idempotency_key = _idempotency_key(
            profile.id,
            *sorted(active_keys),
            initiation_source,
            uuid.uuid4(),
        )

    if retry_failed:
        failed_rows = list(
            (
                await db.execute(
                    select(ApplicationRoomDelivery)
                    .where(
                        ApplicationRoomDelivery.profile_id == profile.id,
                        ApplicationRoomDelivery.action_kind == "missing_item_email",
                        ApplicationRoomDelivery.status == "failed",
                    )
                    .order_by(ApplicationRoomDelivery.created_at.desc())
                    .limit(20)
                )
            ).scalars().all()
        )
        failed = next(
            (
                row
                for row in failed_rows
                if set((row.provider_result or {}).get("requirement_keys") or []) == set(active_keys)
            ),
            None,
        )
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
    if len(requirements) == 1 and requirements[0].requested_document_id:
        room_url += f"&request={requirements[0].requested_document_id}"
    labels = [row.label for row in requirements]
    intro, ai_composed = await _compose_combined_intro(
        db,
        profile=profile,
        requirement_labels=labels,
        user=user,
    )
    attachments = []
    attachment_names: set[str] = set()
    for key in active_keys:
        template = template_for_requirement(key)
        if template and template.filename not in attachment_names:
            attachments.append((template.filename, template.content, template.content_type))
            attachment_names.add(template.filename)
    body = intro + "\n\nRequested items:\n" + "\n".join(f"- {label}" for label in labels)
    body += f"\n\nOpen your secure application room:\n{room_url}\n\n"
    if attachments:
        body += (
            "Approved blank templates for applicable items are attached. You may complete them or upload "
            "your own documents.\n\n"
        )
    body += "For security, your room PIN is not included in this email."
    result = await send_as_user(
        db,
        user.id if user else None,
        to_emails=[recipient],
        subject=(
            f"Action needed: {labels[0]}"
            if len(labels) == 1
            else f"Action needed: {len(labels)} items for your application"
        ),
        body_text=body,
        attachments=attachments or None,
    )
    attempt_number = profile.missing_item_email_attempts + 1 if automatic else 1
    delivery = ApplicationRoomDelivery(
        profile_id=profile.id,
        bucket_id=profile.primary_bucket_id,
        requested_document_id=requirements[0].requested_document_id,
        action_kind="missing_item_email",
        channel="email",
        recipient_email=recipient,
        status="sent" if result.ok else "failed",
        detail=result.detail,
        provider_result={
            "accepted": result.ok,
            "message_id": result.message_id,
            "requirement_keys": active_keys,
            "ai_composed": ai_composed,
            "attachment_names": sorted(attachment_names),
        },
        created_by_user_id=user.id if user else None,
        initiation_source=initiation_source,
        idempotency_key=idempotency_key,
        attempt_number=attempt_number,
    )
    db.add(delivery)
    for requirement in requirements:
        requirement.last_requested_at = timestamp
        requirement.first_requested_at = requirement.first_requested_at or timestamp
        if requirement.status == "missing":
            requirement.status = "requested"
    if automatic:
        profile.missing_item_email_last_sent_at = timestamp
        profile.missing_item_email_next_send_at = timestamp + timedelta(hours=24)
        profile.missing_item_email_attempts = attempt_number
        profile.missing_item_email_requirement_key = active_keys[0]
    await db.flush()
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "requirement.reminder_sent" if result.ok else "requirement.reminder_failed",
        f"Combined request for {len(requirements)} item(s): {result.detail}",
        target_type="application_requirement",
        metadata={
            "requirement_keys": active_keys,
            "channel": "email",
            "initiation_source": initiation_source,
            "delivery_id": str(delivery.id),
            "provider_accepted": result.ok,
            "ai_composed": ai_composed,
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
        blocking_keys = {
            key
            for program in readiness.programs
            for key in program.blocking_requirement_keys
        }
        consolidated_keys = [
            requirement.requirement_key
            for requirement in readiness.requirements
            if requirement.client_visible
            and requirement.requirement_key in blocking_keys
            and _client_submission_needed(requirement)
        ]
        try:
            delivery = await send_requirements_email(
                db,
                profile=profile,
                requirement_keys=consolidated_keys,
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
