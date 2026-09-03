"""Settings router — singleton row, super-admin write.

Read access is open to any authenticated user (the UI displays the values).
Write access is restricted to SUPER_ADMIN.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
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
from app.schemas.stored_signature import StoredSignatureRead
from app.services import stored_signatures as stored_sigs

router = APIRouter(prefix="/settings", tags=["settings"])


# Deterministic S3 key — re-uploads overwrite the previous signature so
# we never accumulate orphaned files. Server-side encryption stays on
# (AES256) since this is the firm's signing image.
SIGNATURE_S3_KEY = "firm_settings/letterhead_signature.png"
AI_MASTER_SWITCH_OWNER_EMAIL = "franco@qualifiedcommercial.com"


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
    if "ai_spend" in patch and "master_enabled" in patch["ai_spend"]:
        current_master = bool(_coerce(row.data).ai_spend.master_enabled)
        next_master = bool(patch["ai_spend"]["master_enabled"])
        if current_master != next_master and (user.email or "").lower() != AI_MASTER_SWITCH_OWNER_EMAIL:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only franco@qualifiedcommercial.com can change the AI master switch.",
            )
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


# ── Company signature on file ───────────────────────────────────────────
#
# Qualified Commercial's own signature on program agreements. The image is
# the letterhead signature already uploaded above (letterhead.signature_s3_key);
# adopting it records the signer name/title and the image hash as the one
# live "qc" row in stored_signatures (source="letterhead"), which the
# production package places on every agreement it sends. Adopting again
# retires the previous row; documents already sent are untouched.
#
# Gate: read is open to any signed-in team member (the settings page shows
# who signs for the firm); adopting is SUPER_ADMIN only.


class CompanySignatureAdoptBody(BaseModel):
    typed_name: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=120)


class CompanySignatureState(BaseModel):
    signature: StoredSignatureRead | None = None
    authorization_text: str
    authorization_version: str
    # Whether a letterhead signature image is saved — the precondition for adopting.
    letterhead_signature_present: bool = False


def _company_signature_state(sig, *, letterhead_present: bool) -> CompanySignatureState:
    return CompanySignatureState(
        signature=stored_sigs.read_model(sig, presign=True),
        authorization_text=stored_sigs.COMPANY_SIGNATURE_AUTHORIZATION_TEXT,
        authorization_version=stored_sigs.COMPANY_SIGNATURE_AUTHORIZATION_VERSION,
        letterhead_signature_present=letterhead_present,
    )


def _letterhead_signature_key(row: AppSettings) -> str | None:
    letterhead = (row.data or {}).get("letterhead") or {}
    key = letterhead.get("signature_s3_key") if isinstance(letterhead, dict) else None
    return key or None


@router.get("/company-signature", response_model=CompanySignatureState)
async def get_company_signature(_: CurrentUser, db: AsyncSession = Depends(get_db)) -> CompanySignatureState:
    row = await _get_or_create(db)
    sig = await stored_sigs.current(db, "qc", None)
    return _company_signature_state(sig, letterhead_present=bool(_letterhead_signature_key(row)))


@router.post(
    "/company-signature/adopt",
    response_model=CompanySignatureState,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def adopt_company_signature(
    payload: CompanySignatureAdoptBody,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CompanySignatureState:
    """Adopt the saved letterhead signature image as Qualified Commercial's
    signature on file, signed by the named officer. 422
    ``letterhead_signature_missing`` until an image is uploaded and saved."""
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super admin required")
    row = await _get_or_create(db)
    key = _letterhead_signature_key(row)
    sig = await stored_sigs.adopt_qc_signature(
        db, admin=user, signature_s3_key=key, signature_sha256=None,
        typed_name=payload.typed_name, title=payload.title, request=request,
    )
    db.add(
        Activity(
            loan_id=None,
            actor_id=user.id,
            actor_label=user.role.value if hasattr(user.role, "value") else str(user.role),
            kind="settings.company_signature_adopted",
            summary=f"Adopted the company signature on file: {sig.typed_name}" + (f", {sig.title}" if sig.title else ""),
            payload={"stored_signature_id": str(sig.id), "signature_s3_key": key, "signature_sha256": sig.signature_sha256},
        )
    )
    await db.flush()
    return _company_signature_state(sig, letterhead_present=True)
