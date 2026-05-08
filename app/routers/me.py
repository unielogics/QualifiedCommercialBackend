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
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings as get_app_config
from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.broker import Broker
from app.schemas.broker_settings import AgentSettingsData, AgentSettingsRead

router = APIRouter(prefix="/me", tags=["me"])
log = logging.getLogger(__name__)


async def _broker_for_user(db: AsyncSession, user_id) -> Broker | None:
    return (
        await db.execute(select(Broker).where(Broker.user_id == user_id))
    ).scalar_one_or_none()


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
