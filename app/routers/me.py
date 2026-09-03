# ruff: noqa: B007, B008, I001, UP017
"""Per-user personal settings endpoints.

Today this is just the broker-settings overlay (`brokers.settings_data`,
alembic 0023) — the agent's checklist additions/disables, AI cadence
overrides, and personal letterhead. Replaces the v1 localStorage
persistence on the desktop /agent-settings page.

Scoped to `Role.BROKER` — super-admins use the firm-wide /settings
page, not this one.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings as get_app_config
from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.activity import Activity
from app.models.booking_settings import BookingSettings
from app.models.broker import Broker
from app.schemas.booking_settings import (
    BookingAssetUploadInitRequest,
    BookingAssetUploadInitResponse,
    UserBookingSettingsRead,
    UserBookingSettingsUpdate,
)
from app.schemas.broker_settings import AgentSettingsData, AgentSettingsRead
from app.schemas.stored_signature import StoredSignatureAdoptBody, StoredSignatureState
from app.services import stored_signatures as stored_sigs

router = APIRouter(prefix="/me", tags=["me"])
log = logging.getLogger(__name__)


async def _broker_for_user(db: AsyncSession, user_id) -> Broker | None:
    return (
        await db.execute(select(Broker).where(Broker.user_id == user_id))
    ).scalar_one_or_none()


def _normalize_booking_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:64].strip("-")


async def _unique_booking_slug(db: AsyncSession, seed: str, user_id) -> str:
    base = _normalize_booking_slug(seed) or "booking"
    candidate = base
    suffix = 2
    while True:
        existing = (
            await db.execute(
                select(BookingSettings.id).where(
                    BookingSettings.slug == candidate,
                    BookingSettings.user_id != user_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return candidate
        trim = 64 - len(f"-{suffix}")
        candidate = f"{base[:trim].rstrip('-')}-{suffix}"
        suffix += 1


async def _get_or_create_booking_settings(db: AsyncSession, user: CurrentUser) -> BookingSettings:
    row = (
        await db.execute(select(BookingSettings).where(BookingSettings.user_id == user.id))
    ).scalar_one_or_none()
    if row is not None:
        return row
    slug = await _unique_booking_slug(db, user.name or user.email or "booking", user.id)
    row = BookingSettings(
        id=uuid.uuid4(),
        user_id=user.id,
        enabled=False,
        slug=slug,
        title=None,
        intro=None,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


def _booking_asset_s3_key(user_id, asset: Literal["logo", "profile-photo"]) -> str:
    filename = "logo.png" if asset == "logo" else "profile-photo.png"
    return f"booking/{user_id}/{filename}"


def _presigned_get_url(s3_key: str | None) -> str | None:
    if not s3_key:
        return None
    cfg = get_app_config()
    if not cfg.s3_bucket:
        return None
    import boto3

    s3 = boto3.client("s3", region_name=cfg.aws_region)
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": cfg.s3_bucket, "Key": s3_key},
            ExpiresIn=3600,
        )
    except Exception:
        log.exception("booking-settings: failed to sign image key=%s", s3_key)
        return None


def _booking_settings_read(row: BookingSettings) -> UserBookingSettingsRead:
    weekly_schedule = row.weekly_schedule or [
        {
            "weekday": weekday,
            "intervals": (
                [{"start_time": row.start_time, "end_time": row.end_time}]
                if weekday in (row.available_days or [1, 2, 3, 4, 5])
                else []
            ),
        }
        for weekday in range(7)
    ]
    return UserBookingSettingsRead(
        id=str(row.id),
        user_id=str(row.user_id),
        enabled=row.enabled,
        slug=row.slug,
        title=row.title,
        intro=row.intro,
        primary_color=row.primary_color,
        background_color=row.background_color,
        duration_min=row.duration_min,
        buffer_before_min=row.buffer_before_min,
        buffer_after_min=row.buffer_after_min,
        confirmation_email_enabled=row.confirmation_email_enabled,
        confirmation_sms_enabled=row.confirmation_sms_enabled,
        reminder_email_enabled=row.reminder_email_enabled,
        reminder_email_minutes_before=row.reminder_email_minutes_before,
        reminder_email_minutes=row.reminder_email_minutes or [row.reminder_email_minutes_before],
        reminder_sms_enabled=row.reminder_sms_enabled,
        reminder_sms_minutes_before=row.reminder_sms_minutes_before,
        reminder_sms_minutes=row.reminder_sms_minutes or [row.reminder_sms_minutes_before],
        reminder_sms_messages=row.reminder_sms_messages or {},
        reminder_email_messages=row.reminder_email_messages or {},
        confirmation_messages=row.confirmation_messages or {},
        precall_enabled=row.precall_enabled,
        precall_messages=row.precall_messages or {},
        google_meet_enabled=row.google_meet_enabled,
        timezone=row.timezone,
        available_days=row.available_days or [1, 2, 3, 4, 5],
        weekly_schedule=weekly_schedule,
        advance_booking_window_enabled=row.advance_booking_window_enabled,
        minimum_notice_days=row.minimum_notice_days,
        maximum_advance_days=row.maximum_advance_days,
        blocked_intervals=row.blocked_intervals or [],
        booking_questions=row.booking_questions or {
            "business_name": True,
            "phone": True,
            "requested_amount": True,
            "bank_statement": False,
        },
        no_show_follow_up_enabled=row.no_show_follow_up_enabled,
        morning_digest_enabled=row.morning_digest_enabled,
        missing_outcome_reminder_hours=row.missing_outcome_reminder_hours,
        start_time=row.start_time,
        end_time=row.end_time,
        logo_s3_key=row.logo_s3_key,
        profile_photo_s3_key=row.profile_photo_s3_key,
        logo_url=_presigned_get_url(row.logo_s3_key),
        profile_photo_url=_presigned_get_url(row.profile_photo_s3_key),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _booking_public_url(row: BookingSettings) -> str | None:
    settings = get_app_config()
    base = (getattr(settings, "frontend_app_url", "") or "").rstrip("/")
    if not base or "localhost" in base or "127.0.0.1" in base or base.startswith("http://"):
        if settings.app_env.lower() == "production":
            base = "https://app.qualifiedcommercial.com"
    return f"{base}/book/{row.slug}" if (row.enabled and row.slug and base) else None


def _booking_invite_body(body: str, booking_url: str) -> str:
    cleaned = body.strip()
    if booking_url in cleaned:
        return cleaned
    return f"{cleaned}\n\nChoose a time that works for you:\n{booking_url}"


@router.get("/booking-settings", response_model=UserBookingSettingsRead)
async def get_booking_settings(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserBookingSettingsRead:
    row = await _get_or_create_booking_settings(db, user)
    await db.commit()
    await db.refresh(row)
    return _booking_settings_read(row)


class BookingLinkRead(BaseModel):
    enabled: bool
    slug: str | None
    url: str | None


@router.get("/booking-link", response_model=BookingLinkRead)
async def get_booking_link(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BookingLinkRead:
    """The current user's public booking link, for inserting into email composers.
    A slug always exists (auto-created); url is null when booking is disabled."""
    row = await _get_or_create_booking_settings(db, user)
    await db.commit()
    return BookingLinkRead(enabled=bool(row.enabled), slug=row.slug, url=_booking_public_url(row))


class BookingInviteShareRequest(BaseModel):
    to_emails: list[EmailStr] = Field(min_length=1, max_length=10)
    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=12_000)


class BookingInviteShareResponse(BaseModel):
    ok: bool
    detail: str | None = None
    message_id: str | None = None
    booking_url: str


@router.post("/booking-link/share", response_model=BookingInviteShareResponse)
async def share_booking_link(
    payload: BookingInviteShareRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BookingInviteShareResponse:
    """Email the current user's public booking page through the normal user-mail path."""
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client accounts cannot send booking invites")

    row = await _get_or_create_booking_settings(db, user)
    booking_url = _booking_public_url(row)
    if booking_url is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Enable your public booking page before sharing an invite",
        )

    recipients = list(dict.fromkeys(str(email).lower() for email in payload.to_emails))
    body = _booking_invite_body(payload.body, booking_url)

    from app.services.email.user_mailer import send_as_user

    result = await send_as_user(
        db,
        user.id,
        to_emails=recipients,
        subject=payload.subject.strip(),
        body_text=body,
    )
    db.add(
        Activity(
            actor_id=user.id,
            actor_label=user.email,
            kind="calendar.booking_invite_sent" if result.ok else "calendar.booking_invite_failed",
            summary=(
                f"Booking invite sent to {', '.join(recipients)}"
                if result.ok
                else f"Booking invite failed for {', '.join(recipients)}"
            ),
            payload={
                "booking_url": booking_url,
                "recipients": recipients,
                "provider_message_id": result.message_id,
                "provider_detail": result.detail,
            },
        )
    )
    await db.commit()

    if not result.ok:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            result.detail or "The booking invite could not be delivered",
        )
    return BookingInviteShareResponse(
        ok=True,
        detail=result.detail,
        message_id=result.message_id,
        booking_url=booking_url,
    )


@router.put("/booking-settings", response_model=UserBookingSettingsRead)
async def put_booking_settings(
    payload: UserBookingSettingsUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserBookingSettingsRead:
    row = await _get_or_create_booking_settings(db, user)
    if payload.enabled and not payload.slug:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "booking.slug is required when the booking page is enabled")
    if payload.slug:
        existing = (
            await db.execute(
                select(BookingSettings.id).where(
                    BookingSettings.slug == payload.slug,
                    BookingSettings.user_id != user.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"booking slug {payload.slug!r} is already used")

    row.enabled = payload.enabled
    row.slug = payload.slug
    row.title = payload.title
    row.intro = payload.intro
    row.primary_color = payload.primary_color
    row.background_color = payload.background_color
    row.duration_min = payload.duration_min
    row.buffer_before_min = payload.buffer_before_min
    row.buffer_after_min = payload.buffer_after_min
    row.confirmation_email_enabled = payload.confirmation_email_enabled
    row.confirmation_sms_enabled = payload.confirmation_sms_enabled
    row.reminder_email_enabled = payload.reminder_email_enabled
    row.reminder_email_minutes = payload.reminder_email_minutes
    row.reminder_email_minutes_before = payload.reminder_email_minutes[0] if payload.reminder_email_minutes else payload.reminder_email_minutes_before
    row.reminder_sms_enabled = payload.reminder_sms_enabled
    row.reminder_sms_minutes = payload.reminder_sms_minutes
    row.reminder_sms_messages = payload.reminder_sms_messages
    row.reminder_email_messages = payload.reminder_email_messages
    row.confirmation_messages = payload.confirmation_messages
    row.precall_enabled = payload.precall_enabled
    row.precall_messages = payload.precall_messages
    row.reminder_sms_minutes_before = payload.reminder_sms_minutes[0] if payload.reminder_sms_minutes else payload.reminder_sms_minutes_before
    row.google_meet_enabled = payload.google_meet_enabled
    row.timezone = payload.timezone
    row.available_days = payload.available_days
    row.weekly_schedule = [schedule.model_dump() for schedule in payload.weekly_schedule]
    row.advance_booking_window_enabled = payload.advance_booking_window_enabled
    row.minimum_notice_days = payload.minimum_notice_days
    row.maximum_advance_days = payload.maximum_advance_days
    row.blocked_intervals = [interval.model_dump() for interval in payload.blocked_intervals]
    row.booking_questions = dict(payload.booking_questions)
    row.no_show_follow_up_enabled = payload.no_show_follow_up_enabled
    row.morning_digest_enabled = payload.morning_digest_enabled
    row.missing_outcome_reminder_hours = payload.missing_outcome_reminder_hours
    row.start_time = payload.start_time
    row.end_time = payload.end_time
    row.logo_s3_key = payload.logo_s3_key
    row.profile_photo_s3_key = payload.profile_photo_s3_key

    await db.commit()
    await db.refresh(row)
    return _booking_settings_read(row)


async def _booking_asset_upload_init(
    *,
    payload: BookingAssetUploadInitRequest,
    user: CurrentUser,
    asset: Literal["logo", "profile-photo"],
) -> BookingAssetUploadInitResponse:
    s3_key = _booking_asset_s3_key(user.id, asset)
    cfg = get_app_config()
    if not cfg.s3_bucket:
        return BookingAssetUploadInitResponse(s3_key=s3_key, upload_url=None)

    import boto3

    s3 = boto3.client("s3", region_name=cfg.aws_region)
    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": cfg.s3_bucket,
                "Key": s3_key,
                "ContentType": payload.content_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=300,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Could not mint booking image upload URL: {exc}",
        ) from exc
    return BookingAssetUploadInitResponse(s3_key=s3_key, upload_url=upload_url)


@router.post("/booking-settings/logo/upload-init", response_model=BookingAssetUploadInitResponse)
async def booking_logo_upload_init(
    payload: BookingAssetUploadInitRequest,
    user: CurrentUser,
) -> BookingAssetUploadInitResponse:
    return await _booking_asset_upload_init(payload=payload, user=user, asset="logo")


@router.post("/booking-settings/profile-photo/upload-init", response_model=BookingAssetUploadInitResponse)
async def booking_profile_photo_upload_init(
    payload: BookingAssetUploadInitRequest,
    user: CurrentUser,
) -> BookingAssetUploadInitResponse:
    return await _booking_asset_upload_init(payload=payload, user=user, asset="profile-photo")


@router.get("/broker-settings", response_model=AgentSettingsRead)
async def get_broker_settings(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> AgentSettingsRead:
    """Returns the calling broker's overlay data, or empty defaults
    when the row is fresh / no overlay configured yet."""
    if user.role != Role.BROKER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Broker-settings is broker-only — super-admins use /settings.",
        )
    broker = await _broker_for_user(db, user.id)
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile for this user")
    raw = broker.settings_data
    data = AgentSettingsData() if not raw else AgentSettingsData.model_validate(raw)
    return AgentSettingsRead(data=data)


@router.put("/broker-settings", response_model=AgentSettingsRead)
async def put_broker_settings(
    payload: AgentSettingsData,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentSettingsRead:
    """Whole-document replacement — the desktop sends the full overlay
    every save. Validates that checklist keys are `buyer` | `seller`
    only (post-codex-PR shape). The Pydantic schema's
    `_migrate_v1_shapes` validator already strips legacy
    `loan_type:side` keys, so payloads from old clients are tolerated
    silently.
    """
    if user.role != Role.BROKER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Broker-settings is broker-only.",
        )
    broker = await _broker_for_user(db, user.id)
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile for this user")

    errors: list[str] = []
    for key, overlay in (payload.checklists or {}).items():
        if key not in ("buyer", "seller"):
            errors.append(
                f"checklist key {key!r} must be 'buyer' or 'seller'"
            )
            continue
        # Agent extras carry a `side` tag for downstream resolvers;
        # the wizard pre-fills it but defensively allow either the
        # current tab's side or 'both'.
        for it in overlay.extra_items:
            if it.side not in ("buyer", "seller", "both"):
                errors.append(
                    f"checklist[{key}].extra_items[{it.name!r}].side "
                    f"must be buyer | seller | both"
                )
    if payload.booking and payload.booking.enabled:
        if not payload.booking.slug:
            errors.append("booking.slug is required when the booking page is enabled")
        else:
            rows = (
                await db.execute(
                    select(Broker.id, Broker.settings_data).where(Broker.id != broker.id)
                )
            ).all()
            for other_id, raw_settings in rows:
                try:
                    other = AgentSettingsData.model_validate(raw_settings or {})
                except Exception:
                    continue
                if other.booking and other.booking.enabled and other.booking.slug == payload.booking.slug:
                    errors.append(
                        f"booking slug {payload.booking.slug!r} is already used by another agent"
                    )
                    break
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"errors": errors},
        )

    broker.settings_data = payload.model_dump(mode="json")
    await db.commit()
    await db.refresh(broker)
    return AgentSettingsRead(
        data=AgentSettingsData.model_validate(broker.settings_data or {})
    )


# ── Headshot upload (S3) ──────────────────────────────────────────────
#
# Two-step flow mirrors /settings/letterhead/signature/upload-init:
#   1. POST /me/broker-settings/headshot/upload-init  → presigned PUT URL
#   2. Browser PUTs PNG/JPEG bytes directly to S3
#   3. Frontend PUT /me/broker-settings with letterhead.headshot_s3_key
#
# Key is deterministic per broker — re-uploads overwrite. Prequal PDFs
# pull the key off broker.settings_data.letterhead.headshot_s3_key and
# composit the headshot beside the firm logo on co-branded letters.


def _headshot_s3_key_for(broker_id) -> str:
    """Deterministic key per broker so re-uploads overwrite cleanly."""
    return f"brokers/{broker_id}/headshot.png"


class _HeadshotUploadInitRequest(BaseModel):
    content_type: Literal["image/png", "image/jpeg"] = "image/png"


class HeadshotUploadInitResponse(BaseModel):
    """Same shape as the firm-signature upload-init response.

    s3_key      — caller PUTs bytes to upload_url then PUTs
                  /me/broker-settings with letterhead.headshot_s3_key=<this>.
    upload_url  — presigned PUT URL (5-min TTL). None when the backend
                  is running without S3 credentials (local dev).
    """
    s3_key: str
    upload_url: str | None


@router.post(
    "/broker-settings/headshot/upload-init",
    response_model=HeadshotUploadInitResponse,
)
async def headshot_upload_init(
    payload: _HeadshotUploadInitRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> HeadshotUploadInitResponse:
    """Mint a presigned PUT URL for the broker's headshot."""
    if user.role != Role.BROKER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Headshot upload is broker-only.",
        )
    broker = await _broker_for_user(db, user.id)
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile for this user")

    s3_key = _headshot_s3_key_for(broker.id)
    cfg = get_app_config()
    if not cfg.s3_bucket:
        return HeadshotUploadInitResponse(s3_key=s3_key, upload_url=None)

    import boto3

    s3 = boto3.client("s3", region_name=cfg.aws_region)
    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": cfg.s3_bucket,
                "Key": s3_key,
                "ContentType": payload.content_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=300,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Could not mint headshot upload URL: {exc}",
        ) from exc

    return HeadshotUploadInitResponse(s3_key=s3_key, upload_url=upload_url)


# ── Agent AI Playbook overlay (Phase 2) ────────────────────────────


# Closed enums mirrored from app/enums.py for the agent overlay. The
# super-admin path in lending_admin.py uses the same set — kept inline
# here too rather than imported across router files.
_RequirementCategoryLiteral = Literal[
    "borrower_info", "property_data", "financials", "credit", "agreements",
    "insurance", "title_and_escrow", "appraisal_and_inspection", "scheduling",
    "compliance", "communication", "ai_internal",
]
_TaskOwnerTypeLiteral = Literal["human", "ai", "shared", "funding_locked"]
_CompletionModeLiteral = Literal[
    "ai_can_complete", "requires_human_verify", "borrower_self_attest",
]
_LinkKindLiteral = Literal["docusign", "esign", "external_form", "reference"]


class PlaybookRequirementOut(BaseModel):
    id: UUID
    requirement_key: str
    label: str
    category: str
    required_level: str
    applies_when: dict | None
    blocks_stage: str | None
    visibility: list[str]
    can_agent_override: bool
    can_underwriter_waive: bool
    verification_required: bool
    expiration_days: int | None
    ai_request_message_template: str | None
    display_order: int
    # AI Deal Secretary fields (alembic 0038)
    default_owner_type: str
    default_channels: list[str]
    default_cadence_hours: int
    link_url: str | None
    link_label: str | None
    link_kind: str | None
    objective_text: str
    completion_criteria: str
    completion_mode: str
    wrong_upload_response_template: str | None
    # Timeline + grouping (alembic 0040)
    depends_on: list[str] = []
    parent_key: str | None = None


class AgentPlaybookOut(BaseModel):
    """Combined platform playbook + agent overlay for ONE
    playbook_type (buyer | seller | cadence). The UI renders the
    platform rows as a read-only top section, then the agent's
    overlay rows below as fully editable."""
    playbook_type: str
    platform_id: UUID | None
    platform_version: int | None
    agent_id: UUID | None
    agent_version: int | None
    rules: dict
    platform_requirements: list[PlaybookRequirementOut]
    agent_requirements: list[PlaybookRequirementOut]


@router.get("/ai-playbook/{playbook_type}", response_model=AgentPlaybookOut)
async def get_agent_playbook(
    playbook_type: Literal["buyer", "seller", "cadence"],
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentPlaybookOut:
    """Return the platform playbook + the agent's overlay for the given
    type. Creates an empty agent playbook on demand the first time the
    agent opens this surface."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")

    from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
    plat = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "platform",
            AIPlaybookTemplate.playbook_type == playbook_type,
            AIPlaybookTemplate.is_active.is_(True),
        )
    )).scalars().all()
    plat_pb = max(plat, key=lambda p: p.version) if plat else None
    agent_pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == playbook_type,
        )
    )).scalar_one_or_none()
    if agent_pb is None:
        agent_pb = AIPlaybookTemplate(
            owner_type="agent", owner_id=user.id,
            playbook_type=playbook_type,
            name=f"My {playbook_type} playbook",
            description="Agent overlay — overrides platform defaults where allowed.",
            rules={},
            version=1, status="published",
        )
        db.add(agent_pb)
        await db.flush()

    plat_reqs: list[AICollectionRequirement] = []
    if plat_pb is not None:
        plat_reqs = list((await db.execute(
            select(AICollectionRequirement)
            .where(AICollectionRequirement.playbook_id == plat_pb.id)
            .order_by(AICollectionRequirement.display_order)
        )).scalars().all())
    agent_reqs = list((await db.execute(
        select(AICollectionRequirement)
        .where(AICollectionRequirement.playbook_id == agent_pb.id)
        .order_by(AICollectionRequirement.display_order)
    )).scalars().all())

    def _ser(r: AICollectionRequirement) -> PlaybookRequirementOut:
        return PlaybookRequirementOut(
            id=r.id, requirement_key=r.requirement_key, label=r.label,
            category=r.category, required_level=r.required_level,
            applies_when=r.applies_when, blocks_stage=r.blocks_stage,
            visibility=list(r.visibility or []),
            can_agent_override=r.can_agent_override,
            can_underwriter_waive=r.can_underwriter_waive,
            verification_required=r.verification_required,
            expiration_days=r.expiration_days,
            ai_request_message_template=r.ai_request_message_template,
            display_order=r.display_order,
            # AI Deal Secretary fields (alembic 0038).
            default_owner_type=r.default_owner_type,
            default_channels=list(r.default_channels or []),
            default_cadence_hours=r.default_cadence_hours,
            link_url=r.link_url,
            link_label=r.link_label,
            link_kind=r.link_kind,
            objective_text=r.objective_text or "",
            completion_criteria=r.completion_criteria or "",
            completion_mode=r.completion_mode,
            wrong_upload_response_template=r.wrong_upload_response_template,
            # Timeline + grouping (alembic 0040).
            depends_on=list(r.depends_on or []),
            parent_key=r.parent_key,
        )

    return AgentPlaybookOut(
        playbook_type=playbook_type,
        platform_id=plat_pb.id if plat_pb else None,
        platform_version=plat_pb.version if plat_pb else None,
        agent_id=agent_pb.id, agent_version=agent_pb.version,
        rules=agent_pb.rules or {},
        platform_requirements=[_ser(r) for r in plat_reqs],
        agent_requirements=[_ser(r) for r in agent_reqs],
    )


class AgentRequirementUpsert(BaseModel):
    """Either creates a new agent-overlay requirement (no id) or
    edits an existing one (id supplied + caller must own it).

    AI Deal Secretary fields are optional on the wire — server applies
    sensible defaults (owner=human, channels=["portal"], 48h cadence,
    no link, empty objective/completion, ai_can_complete) when omitted."""
    id: UUID | None = None
    requirement_key: str
    label: str
    category: _RequirementCategoryLiteral
    required_level: Literal["required", "recommended", "optional"]
    applies_when: dict | None = None
    blocks_stage: str | None = None
    visibility: list[str] | None = None
    expiration_days: int | None = None
    ai_request_message_template: str | None = None
    display_order: int = 100
    # AI Deal Secretary fields (alembic 0038). All optional.
    default_owner_type: _TaskOwnerTypeLiteral = "human"
    default_channels: list[str] | None = None
    default_cadence_hours: int = 48
    link_url: str | None = None
    link_label: str | None = None
    link_kind: _LinkKindLiteral | None = None
    objective_text: str = ""
    completion_criteria: str = ""
    completion_mode: _CompletionModeLiteral = "ai_can_complete"
    wrong_upload_response_template: str | None = None
    # Timeline + grouping (alembic 0040). All optional.
    depends_on: list[str] | None = None
    parent_key: str | None = None


@router.post("/ai-playbook/{playbook_type}/requirements", response_model=PlaybookRequirementOut)
async def upsert_agent_requirement(
    playbook_type: Literal["buyer", "seller", "cadence"],
    payload: AgentRequirementUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaybookRequirementOut:
    """Add or edit a requirement on the agent's overlay playbook."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
    from app.services.ai.audit import record_event

    agent_pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == playbook_type,
        )
    )).scalar_one_or_none()
    if agent_pb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No agent overlay — GET first to provision")

    if payload.id is not None:
        req = await db.get(AICollectionRequirement, payload.id)
        if req is None or req.playbook_id != agent_pb.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found on your overlay")
        old = {
            "label": req.label,
            "required_level": req.required_level,
            "applies_when": req.applies_when,
        }
        req.requirement_key = payload.requirement_key
        req.label = payload.label
        req.category = payload.category
        req.required_level = payload.required_level
        req.applies_when = payload.applies_when
        req.blocks_stage = payload.blocks_stage
        if payload.visibility is not None:
            req.visibility = payload.visibility
        req.expiration_days = payload.expiration_days
        req.ai_request_message_template = payload.ai_request_message_template
        req.display_order = payload.display_order
        # AI Deal Secretary fields (alembic 0038).
        req.default_owner_type = payload.default_owner_type
        if payload.default_channels is not None:
            req.default_channels = payload.default_channels
        req.default_cadence_hours = payload.default_cadence_hours
        req.link_url = payload.link_url
        req.link_label = payload.link_label
        req.link_kind = payload.link_kind
        req.objective_text = payload.objective_text
        req.completion_criteria = payload.completion_criteria
        req.completion_mode = payload.completion_mode
        req.wrong_upload_response_template = payload.wrong_upload_response_template
        # Timeline + grouping (alembic 0040).
        if payload.depends_on is not None:
            req.depends_on = payload.depends_on
        req.parent_key = payload.parent_key
        await record_event(
            db, event_type="requirement_added", actor_type="user",
            actor_id=user.id, playbook_id=agent_pb.id,
            requirement_key=req.requirement_key,
            old_value=old,
            new_value={"label": req.label, "required_level": req.required_level},
        )
    else:
        req = AICollectionRequirement(
            playbook_id=agent_pb.id,
            requirement_key=payload.requirement_key,
            label=payload.label,
            category=payload.category,
            required_level=payload.required_level,
            applies_when=payload.applies_when,
            blocks_stage=payload.blocks_stage,
            visibility=payload.visibility or ["agent"],
            can_agent_override=True,
            can_underwriter_waive=True,
            verification_required=False,
            expiration_days=payload.expiration_days,
            ai_request_message_template=payload.ai_request_message_template,
            display_order=payload.display_order,
            # AI Deal Secretary fields (alembic 0038).
            default_owner_type=payload.default_owner_type,
            default_channels=payload.default_channels or ["portal"],
            default_cadence_hours=payload.default_cadence_hours,
            link_url=payload.link_url,
            link_label=payload.link_label,
            link_kind=payload.link_kind,
            objective_text=payload.objective_text,
            completion_criteria=payload.completion_criteria,
            completion_mode=payload.completion_mode,
            wrong_upload_response_template=payload.wrong_upload_response_template,
            # Timeline + grouping (alembic 0040).
            depends_on=payload.depends_on or [],
            parent_key=payload.parent_key,
        )
        db.add(req)
        await db.flush()
        await record_event(
            db, event_type="requirement_added", actor_type="user",
            actor_id=user.id, playbook_id=agent_pb.id,
            requirement_key=req.requirement_key,
            new_value={"label": req.label, "required_level": req.required_level},
        )
    await db.flush()
    return PlaybookRequirementOut(
        id=req.id, requirement_key=req.requirement_key, label=req.label,
        category=req.category, required_level=req.required_level,
        applies_when=req.applies_when, blocks_stage=req.blocks_stage,
        visibility=list(req.visibility or []),
        can_agent_override=req.can_agent_override,
        can_underwriter_waive=req.can_underwriter_waive,
        verification_required=req.verification_required,
        expiration_days=req.expiration_days,
        ai_request_message_template=req.ai_request_message_template,
        display_order=req.display_order,
        # AI Deal Secretary fields (alembic 0038).
        default_owner_type=req.default_owner_type,
        default_channels=list(req.default_channels or []),
        default_cadence_hours=req.default_cadence_hours,
        link_url=req.link_url,
        link_label=req.link_label,
        link_kind=req.link_kind,
        objective_text=req.objective_text or "",
        completion_criteria=req.completion_criteria or "",
        completion_mode=req.completion_mode,
        wrong_upload_response_template=req.wrong_upload_response_template,
        # Timeline + grouping (alembic 0040).
        depends_on=list(req.depends_on or []),
        parent_key=req.parent_key,
    )


@router.delete("/ai-playbook/{playbook_type}/requirements/{requirement_id}")
async def delete_agent_requirement(
    playbook_type: Literal["buyer", "seller", "cadence"],
    requirement_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
    from app.services.ai.audit import record_event

    req = await db.get(AICollectionRequirement, requirement_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    pb = await db.get(AIPlaybookTemplate, req.playbook_id)
    if pb is None or pb.owner_type != "agent" or pb.owner_id != user.id or pb.playbook_type != playbook_type:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not yours")
    key = req.requirement_key
    await db.delete(req)
    await record_event(
        db, event_type="requirement_removed", actor_type="user",
        actor_id=user.id, playbook_id=pb.id, requirement_key=key,
    )
    await db.flush()
    return {"ok": True}


class AgentPlaybookRulesPatch(BaseModel):
    """Free-form rules JSONB on the agent's overlay (e.g. before_handoff
    list, message style, cadence presets). The UI surfaces specific
    fields in this blob via the agent-settings tabs."""
    rules: dict


@router.patch("/ai-playbook/{playbook_type}/rules", response_model=AgentPlaybookOut)
async def patch_agent_playbook_rules(
    playbook_type: Literal["buyer", "seller", "cadence"],
    payload: AgentPlaybookRulesPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentPlaybookOut:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_playbook import AIPlaybookTemplate
    from app.services.ai.audit import record_event

    pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == playbook_type,
        )
    )).scalar_one_or_none()
    if pb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No agent overlay — GET first")
    old = pb.rules or {}
    pb.rules = payload.rules or {}
    await record_event(
        db, event_type="playbook_edited", actor_type="user", actor_id=user.id,
        playbook_id=pb.id, old_value=old, new_value=payload.rules,
    )
    await db.flush()
    return await get_agent_playbook(playbook_type, user, db)  # type: ignore[arg-type]


# ── Agent cadence rules CRUD (overlay) ─────────────────────────────


class AgentCadenceRuleOut(BaseModel):
    id: UUID
    trigger_event: str
    applies_to_requirement_key: str | None
    condition: dict | None
    wait_hours: int
    action_type: str
    approval_required: bool
    message_template: str | None
    visibility: str
    is_active: bool


class AgentCadenceRuleUpsert(BaseModel):
    id: UUID | None = None
    trigger_event: str
    applies_to_requirement_key: str | None = None
    condition: dict | None = None
    wait_hours: int = 0
    action_type: Literal["draft_message", "create_task", "escalate", "mark_stalled", "auto_send_reminder"]
    approval_required: bool = True
    message_template: str | None = None
    visibility: Literal["internal", "agent", "borrower", "broker"] = "agent"
    is_active: bool = True


@router.get("/ai-playbook/cadence/rules", response_model=list[AgentCadenceRuleOut])
async def list_agent_cadence_rules(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[AgentCadenceRuleOut]:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_cadence_rule import AICadenceRule
    from app.models.ai_playbook import AIPlaybookTemplate

    pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == "cadence",
        )
    )).scalar_one_or_none()
    if pb is None:
        return []
    rows = (await db.execute(
        select(AICadenceRule).where(AICadenceRule.playbook_id == pb.id).order_by(AICadenceRule.created_at)
    )).scalars().all()
    return [AgentCadenceRuleOut(
        id=r.id, trigger_event=r.trigger_event,
        applies_to_requirement_key=r.applies_to_requirement_key,
        condition=r.condition, wait_hours=r.wait_hours,
        action_type=r.action_type, approval_required=r.approval_required,
        message_template=r.message_template, visibility=r.visibility,
        is_active=r.is_active,
    ) for r in rows]


@router.post("/ai-playbook/cadence/rules", response_model=AgentCadenceRuleOut)
async def upsert_agent_cadence_rule(
    payload: AgentCadenceRuleUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentCadenceRuleOut:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_cadence_rule import AICadenceRule
    from app.models.ai_playbook import AIPlaybookTemplate

    pb = (await db.execute(
        select(AIPlaybookTemplate).where(
            AIPlaybookTemplate.owner_type == "agent",
            AIPlaybookTemplate.owner_id == user.id,
            AIPlaybookTemplate.playbook_type == "cadence",
        )
    )).scalar_one_or_none()
    if pb is None:
        pb = AIPlaybookTemplate(
            owner_type="agent", owner_id=user.id, playbook_type="cadence",
            name="My cadence playbook", rules={}, version=1, status="published",
        )
        db.add(pb)
        await db.flush()

    if payload.id is not None:
        row = await db.get(AICadenceRule, payload.id)
        if row is None or row.playbook_id != pb.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
        for f in (
            "trigger_event", "applies_to_requirement_key", "condition",
            "wait_hours", "action_type", "approval_required",
            "message_template", "visibility", "is_active",
        ):
            setattr(row, f, getattr(payload, f))
    else:
        row = AICadenceRule(
            playbook_id=pb.id,
            trigger_event=payload.trigger_event,
            applies_to_requirement_key=payload.applies_to_requirement_key,
            condition=payload.condition,
            wait_hours=payload.wait_hours,
            action_type=payload.action_type,
            approval_required=payload.approval_required,
            message_template=payload.message_template,
            visibility=payload.visibility,
            is_active=payload.is_active,
        )
        db.add(row)
    await db.flush()
    return AgentCadenceRuleOut(
        id=row.id, trigger_event=row.trigger_event,
        applies_to_requirement_key=row.applies_to_requirement_key,
        condition=row.condition, wait_hours=row.wait_hours,
        action_type=row.action_type, approval_required=row.approval_required,
        message_template=row.message_template, visibility=row.visibility,
        is_active=row.is_active,
    )


@router.delete("/ai-playbook/cadence/rules/{rule_id}")
async def delete_agent_cadence_rule(
    rule_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_cadence_rule import AICadenceRule
    from app.models.ai_playbook import AIPlaybookTemplate
    row = await db.get(AICadenceRule, rule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    pb = await db.get(AIPlaybookTemplate, row.playbook_id)
    if pb is None or pb.owner_type != "agent" or pb.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not yours")
    await db.delete(row)
    await db.flush()
    return {"ok": True}


# ── Agent AI Knowledge (PDF / FAQ upload) ──────────────────────────
#
# Backs the Knowledge & Voice section of /agent-settings/ai. Two-step
# upload mirrors documents.upload-init / upload-complete: backend mints
# a presigned PUT URL, browser PUTs the bytes, then notifies the
# backend which parses and flips status to 'ready'.
#
# Parse is synchronous in v1 — fine for small files (the page caps
# the user at PDFs and pasted FAQ). When upload volume grows, defer
# to the scheduler the same way the document scanner does.


class _KnowledgeUploadInitRequest(BaseModel):
    filename: str
    content_type: Literal["application/pdf", "text/plain", "text/markdown"] = "application/pdf"
    size_bytes: int = 0


class _KnowledgeDocumentOut(BaseModel):
    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    error: str | None
    created_at: datetime | None


class _KnowledgeUploadInitResponse(BaseModel):
    document: _KnowledgeDocumentOut
    upload_url: str | None  # None in local dev when S3 creds are absent
    s3_key: str


class _KnowledgeUploadCompleteRequest(BaseModel):
    document_id: UUID


def _knowledge_s3_key(user_id: UUID, doc_id: UUID, filename: str) -> str:
    """Namespaced per-agent so list/get can be scoped by prefix in S3
    audits, and a rogue read on another agent's bucket prefix surfaces
    as a 403 immediately."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)[:120]
    return f"agent-knowledge/{user_id}/{doc_id}/{safe}"


@router.post("/ai-knowledge/upload-init", response_model=_KnowledgeUploadInitResponse)
async def ai_knowledge_upload_init(
    payload: _KnowledgeUploadInitRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> _KnowledgeUploadInitResponse:
    """Mint a presigned PUT URL + create a row in status='uploading'."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")

    from app.models.ai_knowledge_document import AIKnowledgeDocument

    doc_id = uuid.uuid4()
    s3_key = _knowledge_s3_key(user.id, doc_id, payload.filename)
    doc = AIKnowledgeDocument(
        id=doc_id,
        agent_user_id=user.id,
        filename=payload.filename[:255] or "untitled",
        content_type=payload.content_type,
        size_bytes=max(0, int(payload.size_bytes or 0)),
        s3_key=s3_key,
        status="uploading",
    )
    db.add(doc)
    await db.flush()

    cfg = get_app_config()
    upload_url: str | None = None
    if cfg.s3_bucket and cfg.aws_access_key_id and cfg.aws_secret_access_key:
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=cfg.aws_access_key_id,
            aws_secret_access_key=cfg.aws_secret_access_key,
            region_name=cfg.aws_region,
        )
        try:
            upload_url = s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": cfg.s3_bucket,
                    "Key": s3_key,
                    "ContentType": payload.content_type,
                    "ServerSideEncryption": "AES256",
                },
                ExpiresIn=900,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"Could not mint knowledge upload URL: {exc}",
            ) from exc

    return _KnowledgeUploadInitResponse(
        document=_KnowledgeDocumentOut(
            id=doc.id, filename=doc.filename, content_type=doc.content_type,
            size_bytes=doc.size_bytes, status=doc.status, error=doc.error,
            created_at=doc.created_at,
        ),
        upload_url=upload_url,
        s3_key=s3_key,
    )


@router.post("/ai-knowledge/upload-complete", response_model=_KnowledgeDocumentOut)
async def ai_knowledge_upload_complete(
    payload: _KnowledgeUploadCompleteRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> _KnowledgeDocumentOut:
    """Called after the S3 PUT lands. Parses inline + flips status.
    Idempotent — re-calling on a ready row is a no-op; on a failed row
    it retries the parse."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")

    from app.models.ai_knowledge_document import AIKnowledgeDocument
    from app.services.ai.knowledge import parse_document_inline

    doc = await db.get(AIKnowledgeDocument, payload.document_id)
    if doc is None or doc.agent_user_id != user.id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    if doc.status != "ready":
        await parse_document_inline(db, doc)

    return _KnowledgeDocumentOut(
        id=doc.id, filename=doc.filename, content_type=doc.content_type,
        size_bytes=doc.size_bytes, status=doc.status, error=doc.error,
        created_at=doc.created_at,
    )


# ── Pasted knowledge — text in the textarea, no S3 upload ───────────


class _KnowledgePasteRequest(BaseModel):
    filename: str
    text: str


@router.post("/ai-knowledge/paste", response_model=_KnowledgeDocumentOut)
async def ai_knowledge_paste(
    payload: _KnowledgePasteRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> _KnowledgeDocumentOut:
    """Add a pasted-text knowledge note (no S3 object). Body of the note
    lives in `parsed_text` directly. Classified by Haiku the same way an
    uploaded PDF is, so the Knowledge UI shows a summary either way."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty pasted note."
        )

    from app.models.ai_knowledge_document import AIKnowledgeDocument
    from app.services.ai.ai_agent import classify_knowledge_document

    # Cap to the same window the parser uses so an enormous paste can't
    # blow out the AI context.
    MAX = 200_000
    if len(text) > MAX:
        text = text[:MAX]

    doc = AIKnowledgeDocument(
        id=uuid.uuid4(),
        agent_user_id=user.id,
        filename=(payload.filename or "Note").strip()[:255] or "Note",
        content_type="text/plain",
        size_bytes=len(text.encode("utf-8")),
        s3_key=None,
        parsed_text=text,
        status="ready",
    )
    db.add(doc)
    await db.flush()
    # Run the same classifier as the upload path so doc_type / summary /
    # key_facts are populated for the AI Agent builder's knowledge list.
    try:
        await classify_knowledge_document(db, doc)
    except Exception as exc:  # noqa: BLE001
        log.warning("paste classify failed for %s: %s", doc.id, exc)
    await db.commit()
    return _KnowledgeDocumentOut(
        id=doc.id,
        filename=doc.filename,
        content_type=doc.content_type,
        size_bytes=doc.size_bytes,
        status=doc.status,
        error=doc.error,
        created_at=doc.created_at,
    )


@router.get("/ai-knowledge", response_model=list[_KnowledgeDocumentOut])
async def list_ai_knowledge(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[_KnowledgeDocumentOut]:
    """List the agent's non-deleted knowledge documents, newest first."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    from app.models.ai_knowledge_document import AIKnowledgeDocument
    rows = (
        await db.execute(
            select(AIKnowledgeDocument)
            .where(
                AIKnowledgeDocument.agent_user_id == user.id,
                AIKnowledgeDocument.deleted_at.is_(None),
            )
            .order_by(AIKnowledgeDocument.created_at.desc())
        )
    ).scalars().all()
    return [
        _KnowledgeDocumentOut(
            id=r.id, filename=r.filename, content_type=r.content_type,
            size_bytes=r.size_bytes, status=r.status, error=r.error,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.delete("/ai-knowledge/{doc_id}")
async def delete_ai_knowledge(
    doc_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft-delete the row + hard-delete the S3 object."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")

    from app.models.ai_knowledge_document import AIKnowledgeDocument
    from app.services.ai.knowledge import _delete_s3_object

    doc = await db.get(AIKnowledgeDocument, doc_id)
    if doc is None or doc.agent_user_id != user.id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    doc.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    _delete_s3_object(doc.s3_key)
    return {"ok": True}


# ── Borrower file pipeline — GET /me/files ─────────────────────────────
#
# The single merged list behind the client-side /pipeline table. Unions
# the caller's Deals (agent-stage working files) + Loans (funding files)
# into one deduped row set, each carrying a unified status:
#
#   re_working  — an un-promoted Deal (the agent is still working it)
#   in_funding  — a Loan that hasn't funded yet
#   funded      — a funded Loan
#   lost        — a Deal marked lost
#
# A promoted Deal is represented by its Loan only (the Deal row is
# skipped) so a file never appears twice. Borrower-only — operators use
# the operator pipeline.


_LOAN_STAGE_LABELS: dict[str, str] = {
    "prequalified": "Pre-qualified",
    "collecting_docs": "Collecting documents",
    "lender_connected": "Lender connected",
    "processing": "In processing",
    "closing": "Closing",
    "funded": "Funded",
}
_DEAL_STATUS_LABELS: dict[str, str] = {
    "open": "New",
    "active": "Active",
    "paused": "Paused",
    "won": "Won",
    "lost": "Lost",
    "promoted": "In funding",
}


def _enum_str(v: object) -> str:
    return str(getattr(v, "value", v) or "")


def _ai_oneliner(living_profile: object, fallback: str | None) -> str | None:
    """Pull the AI's plain-English current-status line off a
    living_profile JSONB blob, falling back to status_summary/summary."""
    text: str | None = None
    if isinstance(living_profile, dict):
        cur = living_profile.get("current_status")
        if isinstance(cur, str) and cur.strip():
            text = cur.strip()
    if not text and isinstance(fallback, str) and fallback.strip():
        text = fallback.strip()
    if not text:
        return None
    return text if len(text) <= 200 else text[:197] + "…"


class MyFileRow(BaseModel):
    """One row in the borrower's file pipeline table."""

    id: str  # uuid of the backing record (deal or loan)
    kind: str  # "deal" | "loan"
    ref: str  # human label — loan deal_id ("L-1234") or the deal title
    status: str  # re_working | in_funding | funded | lost
    stage_detail: str  # human-readable stage
    address: str | None
    city: str | None
    loan_type: str | None
    amount: float | None
    ai_status: str | None  # living_profile.current_status one-liner
    updated_at: datetime
    # The modal loads loan-backed tabs when loan_uuid is set, else
    # deal-backed tabs. A promoted file carries both.
    deal_uuid: str | None
    loan_uuid: str | None


@router.get("/files", response_model=list[MyFileRow])
async def my_files(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[MyFileRow]:
    """Deduped union of the borrower's Deals + Loans for the client-side
    /pipeline table. Borrower-only."""
    if user.role != Role.CLIENT or user.client is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The file pipeline is a borrower view.",
        )

    from app.models.client_property import ClientProperty
    from app.models.deal import Deal
    from app.models.loan import Loan

    client_id = user.client.id

    loans = (
        await db.execute(
            select(Loan)
            .where(Loan.client_id == client_id)
            .order_by(Loan.updated_at.desc())
        )
    ).scalars().all()

    # Only un-promoted deals — a promoted deal is covered by its Loan row.
    deals = (
        await db.execute(
            select(Deal).where(
                Deal.client_id == client_id,
                Deal.promoted_loan_id.is_(None),
            )
        )
    ).scalars().all()

    prop_ids = [d.property_id for d in deals if d.property_id]
    props: dict = {}
    if prop_ids:
        props = {
            p.id: p
            for p in (
                await db.execute(
                    select(ClientProperty).where(ClientProperty.id.in_(prop_ids))
                )
            ).scalars().all()
        }

    rows: list[MyFileRow] = []

    for ln in loans:
        stage = _enum_str(ln.stage)
        funded = stage == "funded"
        rows.append(
            MyFileRow(
                id=str(ln.id),
                kind="loan",
                ref=ln.deal_id,
                status="funded" if funded else "in_funding",
                stage_detail=_LOAN_STAGE_LABELS.get(stage, stage.replace("_", " ").title() or "In funding"),
                address=ln.address,
                city=ln.city,
                loan_type=_enum_str(ln.type) or None,
                amount=float(ln.amount) if ln.amount is not None else None,
                ai_status=_ai_oneliner(ln.living_profile, ln.status_summary),
                updated_at=ln.updated_at,
                deal_uuid=str(ln.source_deal_id) if ln.source_deal_id else None,
                loan_uuid=str(ln.id),
            )
        )

    for d in deals:
        prop = props.get(d.property_id) if d.property_id else None
        status_val = "lost" if d.status == "lost" else "re_working"
        amount = None
        if prop is not None:
            amt = prop.target_price or prop.list_price or prop.sold_price
            amount = float(amt) if amt is not None else None
        rows.append(
            MyFileRow(
                id=str(d.id),
                kind="deal",
                ref=d.title or "Working file",
                status=status_val,
                stage_detail=_DEAL_STATUS_LABELS.get(d.status, d.status.title()),
                address=prop.address if prop else None,
                city=prop.city if prop else None,
                loan_type=d.deal_type or None,
                amount=amount,
                ai_status=_ai_oneliner(d.living_profile, d.summary),
                updated_at=d.updated_at,
                deal_uuid=str(d.id),
                loan_uuid=None,
            )
        )

    rows.sort(key=lambda r: r.updated_at, reverse=True)
    return rows


# ── Signature on file ───────────────────────────────────────────────────
#
# Any signed-in user may adopt one signature (E-SIGN adoption consent) that
# gets placed on program agreements on their behalf — relationship managers
# on the Production Package, dealer partners on their own paperwork. One live
# row per user; adopting again retires the previous one, revoking never
# touches documents already sent. Service: app.services.stored_signatures.

def _signature_state(sig) -> StoredSignatureState:
    return StoredSignatureState(
        signature=stored_sigs.read_model(sig, presign=True),
        consent_text=stored_sigs.STORED_SIGNATURE_CONSENT_TEXT,
        consent_version=stored_sigs.STORED_SIGNATURE_CONSENT_VERSION,
    )


@router.get("/signature", response_model=StoredSignatureState)
async def get_my_signature(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StoredSignatureState:
    """The caller's live signature on file (null when none) plus the consent
    text/version the pad shows before adopting one."""
    return _signature_state(await stored_sigs.current(db, "user", user.id))


@router.post("/signature", response_model=StoredSignatureState)
async def adopt_my_signature(
    payload: StoredSignatureAdoptBody,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StoredSignatureState:
    """Adopt a pad drawing as the caller's signature on file. Consent is
    required; a previous live signature is retired first."""
    sig = await stored_sigs.adopt_user_signature(
        db, user=user, signature_data_url=payload.signature_data_url, typed_name=payload.typed_name,
        title=payload.title, consent=payload.consent, request=request,
    )
    await db.commit()
    return _signature_state(sig)


@router.delete("/signature", response_model=StoredSignatureState)
async def revoke_my_signature(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StoredSignatureState:
    """Retire the caller's signature on file. Idempotent: returns the empty
    state when nothing was live."""
    await stored_sigs.revoke(db, subject_type="user", subject_id=user.id, user=user, reason="self")
    await db.commit()
    return _signature_state(None)
