"""Settings router — singleton row, super-admin write.

Read access is open to any authenticated user (the UI displays the values).
Write access is restricted to SUPER_ADMIN.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings as get_app_config
from app.db import get_db
from app.deps import CurrentUser, require_role
from app.enums import Role
from app.models.activity import Activity
from app.models.app_settings import AppSettings
from app.schemas.settings import (
    AppSettingsData,
    AppSettingsRead,
    AppSettingsUpdate,
    SignatureUploadInitResponse,
)

router = APIRouter(prefix="/settings", tags=["settings"])


# Deterministic S3 key — re-uploads overwrite the previous signature so
# we never accumulate orphaned files. Server-side encryption stays on
# (AES256) since this is the firm's signing image.
SIGNATURE_S3_KEY = "firm_settings/letterhead_signature.png"


async def _get_or_create(db: AsyncSession) -> AppSettings:
    row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    if row is None:
        row = AppSettings(id=uuid.uuid4(), singleton=True, data=AppSettingsData().model_dump(mode="json"))
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


def _coerce(data: dict[str, Any]) -> AppSettingsData:
    """Tolerate older partial blobs by letting Pydantic fill defaults."""
    return AppSettingsData.model_validate(data or {})


@router.get("", response_model=AppSettingsRead)
async def get_settings(_: CurrentUser, db: AsyncSession = Depends(get_db)) -> AppSettingsRead:
    row = await _get_or_create(db)
    return AppSettingsRead(data=_coerce(row.data))


@router.patch(
    "",
    response_model=AppSettingsRead,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def update_settings(
    payload: AppSettingsUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AppSettingsRead:
    row = await _get_or_create(db)

    # Section-level merge — replace whole sections that the caller sent,
    # leave the rest untouched. Keeps the wire format simple.
    current = _coerce(row.data).model_dump(mode="json")
    patch = payload.model_dump(mode="json", exclude_none=True)
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No settings sections supplied.")
    current.update(patch)

    row.data = current

    db.add(
        Activity(
            loan_id=None,
            actor_id=user.id,
            actor_label=user.role.value if hasattr(user.role, "value") else str(user.role),
            kind="settings.updated",
            summary=f"Updated settings sections: {', '.join(patch.keys())}",
            payload=patch,
        )
    )

    await db.flush()
    await db.refresh(row)
    return AppSettingsRead(data=_coerce(row.data))


# ── Signature image upload ──────────────────────────────────────────────
#
# Two-step flow (mirrors /documents/upload-init):
#   1. POST /settings/letterhead/signature/upload-init → presigned PUT URL
#   2. Browser PUTs the PNG bytes directly to S3
#   3. Frontend PATCHes /settings with letterhead.signature_s3_key
#
# Gate: SUPER_ADMIN. Anyone on the team listing settings can SEE the key
# (so they can know a signature is configured) but only the super-admin
# can issue a fresh upload URL or change the saved key.


class _SignatureUploadInitRequest(BaseModel):
    content_type: Literal["image/png", "image/jpeg"] = "image/png"


@router.post(
    "/letterhead/signature/upload-init",
    response_model=SignatureUploadInitResponse,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def signature_upload_init(
    payload: _SignatureUploadInitRequest,
    user: CurrentUser,
) -> SignatureUploadInitResponse:
    """Mint a presigned PUT URL for the firm's signature image. Caller
    uploads the bytes directly, then PATCHes /settings to save the key
    onto letterhead.signature_s3_key."""
    cfg = get_app_config()
    if not cfg.s3_bucket:
        # Local dev without S3 — return a null url; UI can warn the user.
        return SignatureUploadInitResponse(s3_key=SIGNATURE_S3_KEY, upload_url=None)

    # boto3 walks the credential chain (env → ~/.aws → EC2 instance
    # role) so we don't gate on cfg.aws_access_key_id like the legacy
    # documents router did. On prod this picks up the instance role.
    import boto3

    s3 = boto3.client("s3", region_name=cfg.aws_region)
    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": cfg.s3_bucket,
                "Key": SIGNATURE_S3_KEY,
                "ContentType": payload.content_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=300,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Could not mint signature upload URL: {exc}",
        ) from exc

    return SignatureUploadInitResponse(s3_key=SIGNATURE_S3_KEY, upload_url=upload_url)
