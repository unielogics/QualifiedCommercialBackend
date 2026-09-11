"""Guarded actions rendered beneath client-facing AI Intake messages."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.application_profile import (
    ApplicationProfile,
    ApplicationRequirementState,
    ApplicationRoomDelivery,
)
from app.models.bucket import BucketAIChatAction, BucketAIMessage, BucketUploadLink
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.bucket import IntakeChatActionRead, IntakeChatActionResult
from app.services import application_profiles as profiles
from app.services.application_programs import get_program_readiness, profile_for_chat_scope
from app.services.email.user_mailer import send_as_user
from app.services.financial_templates import FinancialTemplate, template_for_requirement

ACTIONABLE_REQUIREMENTS = {
    "owner_personal_financial_statement",
    "business_debt_schedule",
    "ytd_p_and_l_balance_sheet",
}
SATISFIED_STATES = {"verified", "waived", "not_applicable"}


def now() -> datetime:
    return datetime.now(UTC)


def _idempotency_key(*parts: object) -> str:
    material = ":".join(str(part) for part in parts)
    return f"chat:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _room_url(link: BucketUploadLink, requirement: ApplicationRequirementState) -> str:
    base = f"{get_settings().frontend_app_url.rstrip('/')}/buckets/request/{link.token}"
    query = f"tab=todo&requirement={requirement.requirement_key}"
    if requirement.requested_document_id:
        query += f"&request={requirement.requested_document_id}"
    return f"{base}?{query}"


async def _recipient(
    db: AsyncSession,
    profile: ApplicationProfile,
    link: BucketUploadLink | None,
) -> str | None:
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    return profiles.normalized_email(
        (intake.email if intake else None)
        or (client.email if client else None)
        or (link.recipient_email if link else None)
    )


async def author_actions_for_message(
    db: AsyncSession,
    *,
    message: BucketAIMessage,
    upload_link: BucketUploadLink | None,
    intake_id: uuid.UUID | None = None,
) -> list[BucketAIChatAction]:
    if message.role != "assistant" or message.audience not in {"uploader", "client"}:
        return []
    profile = await profile_for_chat_scope(
        db,
        bucket_id=message.bucket_id,
        intake_id=intake_id,
        upload_link_id=upload_link.id if upload_link else message.upload_link_id,
    )
    if profile is None:
        return []
    readiness = await get_program_readiness(db, profile)
    def remains_blocking(item: object) -> bool:
        state = getattr(item, "status", None)
        if state in SATISFIED_STATES:
            return False
        program_keys = list(getattr(item, "source_program_keys", []) or [])
        overrides = dict(getattr(item, "program_overrides", {}) or {})
        return not program_keys or not all(
            overrides.get(program_key) in SATISFIED_STATES
            for program_key in program_keys
        )

    requirement_read = next(
        (
            item
            for item in readiness.requirements
            if item.requirement_key in ACTIONABLE_REQUIREMENTS
            and item.client_visible
            and item.required_level == "required"
            and remains_blocking(item)
        ),
        None,
    )
    if requirement_read is None:
        return []
    requirement = (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == profile.id,
                ApplicationRequirementState.requirement_key == requirement_read.requirement_key,
            )
        )
    ).scalar_one()
    existing_available = (
        await db.execute(
            select(BucketAIChatAction.id).where(
                BucketAIChatAction.profile_id == profile.id,
                BucketAIChatAction.requirement_key == requirement.requirement_key,
                BucketAIChatAction.status == "available",
                BucketAIChatAction.expires_at > now(),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing_available is not None:
        return []
    template = template_for_requirement(requirement.requirement_key)
    recipient = await _recipient(db, profile, upload_link)
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    spanish = bool(intake and intake.preferred_language == "es")
    specs: list[tuple[str, str]] = [
        ("upload_own", "Subir mi documento" if spanish else "Upload my document")
    ]
    supports_online_form = bool(intake and intake.variant == "dealer_gatekeeper_v1")
    if supports_online_form and requirement.requirement_key in {
        "owner_personal_financial_statement",
        "business_debt_schedule",
    }:
        specs.append(("complete_now", "Completar ahora" if spanish else "Complete now"))
    if template:
        specs.extend(
            [
                ("download_template", "Descargar plantilla" if spanish else "Download template"),
                ("email_template", "Enviar por email" if spanish else "Email me"),
            ]
        )
    if spanish:
        question = (
            f"Para {requirement.label}, puede subir su propio documento"
            + (", completarlo ahora" if any(kind == "complete_now" for kind, _ in specs) else "")
            + (", descargar la plantilla o pedir que se la enviemos por email." if template else ".")
        )
    else:
        question = (
            f"For {requirement.label}, will you upload your own document"
            + (", complete it now" if any(kind == "complete_now" for kind, _ in specs) else "")
            + (", download the template, or have us email it to you?" if template else "?")
        )
    if question not in message.content:
        message.content = f"{message.content.rstrip()}\n\n{question}"
    rows: list[BucketAIChatAction] = []
    for action_type, label in specs:
        idempotency_key = _idempotency_key(
            message.id,
            requirement.requirement_key,
            action_type,
        )
        existing = (
            await db.execute(
                select(BucketAIChatAction).where(BucketAIChatAction.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing:
            rows.append(existing)
            continue
        row = BucketAIChatAction(
            bucket_id=message.bucket_id,
            profile_id=profile.id,
            source_message_id=message.id,
            upload_link_id=upload_link.id if upload_link else message.upload_link_id,
            requested_document_id=requirement.requested_document_id,
            requirement_key=requirement.requirement_key,
            action_type=action_type,
            template_kind=template.kind if template else None,
            label=label,
            recipient_email=recipient,
            idempotency_key=idempotency_key,
            expires_at=now() + timedelta(days=7),
        )
        db.add(row)
        await db.flush()
        rows.append(row)
    return rows


async def actions_for_messages(
    db: AsyncSession, messages: list[BucketAIMessage]
) -> list[IntakeChatActionRead]:
    ids = [message.id for message in messages]
    if not ids:
        return []
    rows = list(
        (
            await db.execute(
                select(BucketAIChatAction)
                .where(BucketAIChatAction.source_message_id.in_(ids))
                .order_by(BucketAIChatAction.created_at, BucketAIChatAction.action_type)
            )
        ).scalars().all()
    )
    return [IntakeChatActionRead.model_validate(row) for row in rows]


async def load_action_for_room(
    db: AsyncSession,
    *,
    action_id: uuid.UUID,
    link: BucketUploadLink,
) -> BucketAIChatAction:
    action = (
        await db.execute(
            select(BucketAIChatAction).where(
                BucketAIChatAction.id == action_id,
                BucketAIChatAction.bucket_id == link.bucket_id,
                BucketAIChatAction.upload_link_id == link.id,
            )
        )
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat action not found")
    if action.expires_at <= now() and action.status == "available":
        action.status = "expired"
        await db.flush()
    if action.status in {"expired", "disabled"}:
        raise HTTPException(status.HTTP_410_GONE, "This action is no longer available")
    return action


async def execute_room_action(
    db: AsyncSession,
    *,
    action: BucketAIChatAction,
    link: BucketUploadLink,
    download_url_override: str | None = None,
) -> IntakeChatActionResult:
    requirement = (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == action.profile_id,
                ApplicationRequirementState.requirement_key == action.requirement_key,
            )
        )
    ).scalar_one_or_none()
    if requirement is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The requested item is no longer active")
    profile = await db.get(ApplicationProfile, action.profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The application file is no longer active")
    readiness = await get_program_readiness(db, profile)
    requirement_read = next(
        (item for item in readiness.requirements if item.requirement_key == action.requirement_key),
        None,
    )
    overridden_for_all = bool(
        requirement_read
        and requirement_read.source_program_keys
        and all(
            requirement_read.program_overrides.get(program_key) in SATISFIED_STATES
            for program_key in requirement_read.source_program_keys
        )
    )
    if requirement.status in SATISFIED_STATES or overridden_for_all:
        action.status = "disabled"
        await db.flush()
        raise HTTPException(status.HTTP_409_CONFLICT, "This requested item is already complete")
    room_url = _room_url(link, requirement)
    if action.status == "executed" and action.result:
        return IntakeChatActionResult(
            action_id=action.id,
            status="executed",
            detail=str(action.result.get("detail") or "Action already completed"),
            download_url=action.result.get("download_url"),
            room_url=action.result.get("room_url"),
            delivery=action.result.get("delivery"),
        )

    if action.action_type == "upload_own":
        result = {"detail": "Upload area opened", "room_url": room_url.replace("tab=todo", "tab=documents")}
    elif action.action_type == "complete_now":
        form = "pfs" if action.requirement_key == "owner_personal_financial_statement" else "debt_schedule"
        result = {"detail": "Secure form opened", "room_url": f"{room_url}&form={form}"}
    elif action.action_type == "download_template":
        result = {
            "detail": "Template ready to download",
            "download_url": download_url_override
            or f"/api/v1/buckets/request/{link.token}/chat-actions/{action.id}/template",
        }
    elif action.action_type == "email_template":
        result = await _email_template(db, action=action, link=link, requirement=requirement, room_url=room_url)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported chat action")
    action.status = "executed" if result.get("ok", True) else "failed"
    action.executed_at = now()
    action.result = result
    await db.flush()
    return IntakeChatActionResult(
        action_id=action.id,
        status="executed" if action.status == "executed" else "failed",
        detail=str(result.get("detail") or "Action completed"),
        download_url=result.get("download_url"),
        room_url=result.get("room_url"),
        delivery=result.get("delivery"),
    )


async def _email_template(
    db: AsyncSession,
    *,
    action: BucketAIChatAction,
    link: BucketUploadLink,
    requirement: ApplicationRequirementState,
    room_url: str,
) -> dict:
    template = template_for_requirement(action.requirement_key)
    if template is None:
        return {"ok": False, "detail": "No approved template is available for this item"}
    if not action.recipient_email:
        return {"ok": False, "detail": "No verified client email is available"}
    delivery_key = f"{action.idempotency_key}:delivery"
    existing = (
        await db.execute(
            select(ApplicationRoomDelivery)
            .where(
                or_(
                    ApplicationRoomDelivery.idempotency_key == delivery_key,
                    ApplicationRoomDelivery.idempotency_key.startswith(
                        f"{delivery_key}:", autoescape=True
                    ),
                )
            )
            .order_by(ApplicationRoomDelivery.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing and existing.status == "sent":
        return {
            "ok": existing.status == "sent",
            "detail": existing.detail or "Email already processed",
            "delivery": {"id": str(existing.id), "status": existing.status},
        }
    attempt_number = (existing.attempt_number + 1) if existing else 1
    idempotency_key = (
        delivery_key
        if attempt_number == 1
        else f"{delivery_key}:retry:{attempt_number}"
    )
    email_result = await send_as_user(
        db,
        None,
        to_emails=[action.recipient_email],
        subject=f"Your {requirement.label} template",
        body_text=(
            f"Attached is the requested {requirement.label} template.\n\n"
            f"Use your secure application room to complete or upload it:\n{room_url}\n\n"
            "For security, the room PIN is not included in this email."
        ),
        attachments=[(template.filename, template.content, template.content_type)],
    )
    delivery = ApplicationRoomDelivery(
        profile_id=action.profile_id,
        bucket_id=action.bucket_id,
        requested_document_id=action.requested_document_id,
        action_kind="template_email",
        channel="email",
        recipient_email=action.recipient_email,
        status="sent" if email_result.ok else "failed",
        detail=email_result.detail,
        provider_result={"accepted": email_result.ok, "message_id": email_result.message_id},
        initiation_source="client_chat_action",
        idempotency_key=idempotency_key,
        attempt_number=attempt_number,
    )
    db.add(delivery)
    requirement.last_requested_at = now()
    await db.flush()
    return {
        "ok": email_result.ok,
        "detail": "Template email accepted by the provider" if email_result.ok else email_result.detail,
        "delivery": {
            "id": str(delivery.id),
            "status": delivery.status,
            "recipient_masked": _mask_email(action.recipient_email),
            "provider_accepted": email_result.ok,
        },
    }


def template_for_action(action: BucketAIChatAction) -> FinancialTemplate:
    template = template_for_requirement(action.requirement_key)
    if template is None or template.kind != action.template_kind:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    return template


def _mask_email(value: str) -> str:
    local, domain = value.split("@", 1)
    return f"{local[:2]}***@{domain}"
