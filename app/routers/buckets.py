from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, with_loader_criteria
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.bucket import (
    Bucket,
    BucketActivityLog,
    BucketAIActionItem,
    BucketAIMessage,
    BucketAIReview,
    BucketDocumentTemplate,
    BucketFile,
    BucketFileAnnotation,
    BucketNote,
    BucketPublicShare,
    BucketRequestedDocument,
    BucketShare,
    BucketUploadLink,
    BucketVendorAccess,
)
from app.models.user import User
from app.schemas.bucket import (
    BucketActivityPage,
    BucketActivityRead,
    BucketAIActionItemCreate,
    BucketAIActionItemPatch,
    BucketAIActionItemRead,
    BucketAIChatMessageCreate,
    BucketAIChatResponse,
    BucketAIContextPatch,
    BucketAIMessageRead,
    BucketAIReviewCreate,
    BucketAIReviewRead,
    BucketAISummaryRead,
    BucketCreate,
    BucketDetail,
    BucketFileRead,
    BucketFileAnnotationCreate,
    BucketFileAnnotationRead,
    BucketFileReviewRead,
    BucketFileUploadInit,
    BucketFileUploadInitResponse,
    BucketFileUrl,
    BucketNoteCreate,
    BucketNoteRead,
    BucketPublicShareAccessRead,
    BucketPublicShareCreate,
    BucketPublicShareFileRead,
    BucketPublicShareRead,
    BucketRead,
    BucketRequestAccessRead,
    BucketRequestAccessRequest,
    BucketRequestBucketRead,
    BucketRequestInfoRead,
    BucketRequestUploadedFileRead,
    BucketRequestedDocumentCreate,
    BucketRequestedDocumentRead,
    BucketShareAccessRead,
    BucketShareAccessRequest,
    BucketShareCreate,
    BucketShareEmailRequest,
    BucketShareEmailResponse,
    BucketShareFileRead,
    BucketShareInfoRead,
    BucketSharePasscodeResetRead,
    BucketSharePatch,
    BucketShareRead,
    BucketSharedNoteCreate,
    BucketSharedDownloadCreate,
    BucketTemplateRead,
    BucketUploadComplete,
    BucketUploadLinkCreate,
    BucketUploadLinkPasscodeResetRead,
    BucketUploadLinkRead,
    BucketVendorAccessCreate,
    BucketVendorAccessPatch,
    BucketVendorAccessRead,
    BucketVendorAccessReadResponse,
    BucketVendorBucketRead,
    BucketVendorRead,
)
from app.services import clerk as clerk_service
from app.services.bucket_ai import (
    create_chat_reply,
    latest_review,
    log_bucket_ai_activity,
    share_visible_summary,
    upload_link_visible_summary,
    vendor_visible_summary,
    visible_action_items,
)

router = APIRouter(prefix="/buckets", tags=["buckets"])
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public_url(path: str) -> str:
    settings = get_settings()
    base = (getattr(settings, "frontend_app_url", "") or "").rstrip("/")
    if settings.app_env.lower() == "production" and (
        not base or "localhost" in base or "127.0.0.1" in base or base.startswith("http://")
    ):
        base = "https://app.qualifiedcommercial.com"
    if not base:
        base = "https://app.qualifiedcommercial.com"
    return f"{base}{path}"


def _s3_client():
    cfg = get_settings()
    kwargs = {
        "region_name": cfg.aws_region,
        "config": Config(signature_version="s3v4"),
    }
    if cfg.aws_access_key_id and cfg.aws_secret_access_key:
        kwargs["aws_access_key_id"] = cfg.aws_access_key_id
        kwargs["aws_secret_access_key"] = cfg.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def _bucket_storage_config() -> tuple[str, str, str]:
    cfg = get_settings()
    if not cfg.s3_bucket:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "S3 bucket is not configured")
    if not cfg.buckets_kms_key_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Buckets KMS key is not configured")
    prefix = cfg.buckets_s3_prefix.strip("/")
    return cfg.s3_bucket, prefix, cfg.buckets_kms_key_id


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return cleaned[:180] or "upload.bin"


def _hash_passcode(passcode: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 240_000
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


# Brute-force guard for share/upload passcodes. Generated passcodes are only
# 6 digits (900k space) and there is no edge rate limiting, so without this a
# leaked token URL could be cracked by enumerating passcodes. Single-instance
# in-memory counter (same assumption as the public.py throttle), keyed on the
# per-share/link passcode hash so the lock follows the target, not the caller IP.
_PASSCODE_ATTEMPTS: dict[str, tuple[int, float]] = {}
_PASSCODE_MAX_ATTEMPTS = 10
_PASSCODE_LOCKOUT_SECONDS = 900.0


def _passcode_locked(key: str) -> bool:
    entry = _PASSCODE_ATTEMPTS.get(key)
    if not entry:
        return False
    attempts, locked_until = entry
    if attempts < _PASSCODE_MAX_ATTEMPTS:
        return False
    if time.monotonic() >= locked_until:
        _PASSCODE_ATTEMPTS.pop(key, None)  # window elapsed — allow a fresh burst
        return False
    return True


def _verify_passcode(passcode: str, passcode_hash: str | None) -> bool:
    if not passcode_hash:
        return False
    # Deny (and skip the expensive PBKDF2) while the target is locked out.
    if _passcode_locked(passcode_hash):
        return False
    try:
        scheme, raw_iterations, salt, expected = passcode_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    if hmac.compare_digest(digest, expected):
        _PASSCODE_ATTEMPTS.pop(passcode_hash, None)  # success resets the counter
        return True
    attempts = _PASSCODE_ATTEMPTS.get(passcode_hash, (0, 0.0))[0] + 1
    _PASSCODE_ATTEMPTS[passcode_hash] = (attempts, time.monotonic() + _PASSCODE_LOCKOUT_SECONDS)
    return False


def _generate_passcode() -> str:
    return f"QC-{secrets.randbelow(900000) + 100000}"


def _require_upload_passcode(link: BucketUploadLink) -> None:
    if not link.passcode_hash:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This upload invite must be regenerated with an access code")


def _is_active(status_value: str, expires_at: datetime | None) -> bool:
    if status_value != "active":
        return False
    if expires_at and expires_at <= _now():
        return False
    return True


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()[:80] or None
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()[:80] or None
    return request.client.host[:80] if request.client else None


def _user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    value = request.headers.get("user-agent")
    return value[:500] if value else None


async def _log(
    db: AsyncSession,
    bucket_id: UUID,
    action: str,
    *,
    request: Request | None = None,
    user: User | None = None,
    actor_user_id: UUID | None = None,
    actor_name: str | None = None,
    actor_email: str | None = None,
    actor_role: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: str | None = None,
) -> None:
    if user is not None:
        actor_user_id = user.id
        actor_name = actor_name if actor_name is not None else user.name
        actor_email = actor_email if actor_email is not None else user.email
        actor_role = actor_role if actor_role is not None else user.role
    db.add(
        BucketActivityLog(
            id=uuid4(),
            bucket_id=bucket_id,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
            actor_email=actor_email,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            created_at=_now(),
        )
    )


async def _load_bucket_or_404(db: AsyncSession, bucket_id: UUID) -> Bucket:
    bucket = (
        await db.execute(
            select(Bucket)
            .where(Bucket.id == bucket_id, Bucket.archived_at.is_(None))
            .options(
                selectinload(Bucket.requested_documents),
                selectinload(Bucket.files),
                selectinload(Bucket.upload_links),
                selectinload(Bucket.shares).selectinload(BucketShare.files),
                selectinload(Bucket.vendor_access).selectinload(BucketVendorAccess.vendor),
                selectinload(Bucket.vendor_access).selectinload(BucketVendorAccess.files),
                selectinload(Bucket.public_shares).selectinload(BucketPublicShare.files),
                selectinload(Bucket.notes),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if bucket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket not found")
    return bucket


async def _load_bucket_detail_or_404(db: AsyncSession, bucket_id: UUID) -> Bucket:
    bucket = await _load_bucket_or_404(db, bucket_id)
    activity = (
        await db.execute(
            select(BucketActivityLog)
            .where(BucketActivityLog.bucket_id == bucket_id)
            .order_by(BucketActivityLog.created_at.desc())
            .limit(12)
        )
    ).scalars().all()
    set_committed_value(bucket, "activity", activity)
    return bucket


async def _load_upload_link_or_404(db: AsyncSession, token: str) -> BucketUploadLink:
    link = (
        await db.execute(
            select(BucketUploadLink)
            .where(BucketUploadLink.token == token)
            .options(
                selectinload(BucketUploadLink.bucket).selectinload(Bucket.requested_documents),
                selectinload(BucketUploadLink.bucket).selectinload(Bucket.files),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if link is None or not _is_active(link.status, link.expires_at):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload link not found or inactive")
    if link.completed_at and not link.allow_multiple_sessions:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This upload link has already been completed")
    return link


async def _load_share_or_404(db: AsyncSession, token: str) -> BucketShare:
    share = (
        await db.execute(
            select(BucketShare)
            .where(BucketShare.token == token)
            .options(
                selectinload(BucketShare.files),
                selectinload(BucketShare.bucket).selectinload(Bucket.notes),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if share is None or not _is_active(share.status, share.expires_at):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share link not found or inactive")
    return share


async def _load_vendor_access_or_404(db: AsyncSession, bucket_id: UUID, user: User) -> BucketVendorAccess:
    access = (
        await db.execute(
            select(BucketVendorAccess)
            .where(
                BucketVendorAccess.bucket_id == bucket_id,
                BucketVendorAccess.vendor_user_id == user.id,
            )
            .options(
                selectinload(BucketVendorAccess.vendor),
                selectinload(BucketVendorAccess.files),
                selectinload(BucketVendorAccess.bucket).selectinload(Bucket.notes),
                selectinload(BucketVendorAccess.bucket).selectinload(Bucket.files),
                selectinload(BucketVendorAccess.bucket).selectinload(Bucket.requested_documents),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if access is None or access.bucket.archived_at is not None or not _is_active(access.status, access.expires_at):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor bucket access not found or inactive")
    return access


async def _load_public_share_or_404(db: AsyncSession, token: str) -> BucketPublicShare:
    share = (
        await db.execute(
            select(BucketPublicShare)
            .where(BucketPublicShare.token == token)
            .options(
                selectinload(BucketPublicShare.files),
                selectinload(BucketPublicShare.bucket),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if share is None or not _is_active(share.status, share.expires_at):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share link not found or inactive")
    return share


# Content types the browser may safely render inline. Anything else (notably
# text/html, image/svg+xml, xml, javascript) can execute script in the S3
# origin when previewed inline, so it is neutralized to a download.
_INLINE_SAFE_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "text/plain",
        "text/csv",
    }
)


def _sanitize_upload_content_type(content_type: str | None) -> str:
    """Coerce an attacker-controlled upload content-type to a safe stored type.
    Executable/markup types (html, svg, xml, js) become application/octet-stream
    so the object can never be served as active content — the root fix for
    stored-XSS via uploaded files."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return "application/octet-stream"
    if any(token in ct for token in ("html", "svg", "xml", "javascript", "script", "xhtml")):
        return "application/octet-stream"
    return ct


def _upload_url(s3_key: str, content_type: str) -> tuple[str, dict[str, str]]:
    bucket, _, kms_key_id = _bucket_storage_config()
    content_type = _sanitize_upload_content_type(content_type)
    headers = {
        "Content-Type": content_type,
        "x-amz-server-side-encryption": "aws:kms",
        "x-amz-server-side-encryption-aws-kms-key-id": kms_key_id,
    }
    url = _s3_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": s3_key,
            "ContentType": content_type,
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": kms_key_id,
        },
        ExpiresIn=900,
    )
    return url, headers


def _download_url(s3_key: str, *, disposition: str = "inline", ttl: int = 900, content_type: str | None = None) -> str:
    bucket, _, _ = _bucket_storage_config()
    params: dict[str, str] = {"Bucket": bucket, "Key": s3_key}
    # Defense-in-depth for historical objects: only let known-safe types render
    # inline; force anything else to download so it cannot execute in the browser.
    if disposition == "inline" and content_type is not None and _sanitize_upload_content_type(content_type) not in _INLINE_SAFE_CONTENT_TYPES:
        disposition = "attachment"
        params["ResponseContentType"] = "application/octet-stream"
    params["ResponseContentDisposition"] = disposition
    return _s3_client().generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)


def _delete_s3_object(s3_key: str) -> str:
    bucket, _, _ = _bucket_storage_config()
    try:
        _s3_client().delete_object(Bucket=bucket, Key=s3_key)
    except Exception:  # noqa: BLE001
        logger.exception("bucket file S3 delete failed key=%s", s3_key)
        return "delete_failed"
    return "deleted"


async def _recalculate_requested_document_status(db: AsyncSession, requested_document_id: UUID | None) -> None:
    if requested_document_id is None:
        return
    req = await db.get(BucketRequestedDocument, requested_document_id)
    if req is None:
        return
    active_uploaded = (
        await db.execute(
            select(func.count())
            .select_from(BucketFile)
            .where(
                BucketFile.requested_document_id == requested_document_id,
                BucketFile.status == "uploaded",
                BucketFile.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    req.status = "uploaded" if active_uploaded else "requested"


async def _file_annotations(db: AsyncSession, bucket_id: UUID, file_id: UUID) -> list[BucketFileAnnotation]:
    return (
        await db.execute(
            select(BucketFileAnnotation)
            .where(BucketFileAnnotation.bucket_id == bucket_id, BucketFileAnnotation.file_id == file_id)
            .order_by(BucketFileAnnotation.created_at.asc())
        )
    ).scalars().all()


def _file_belongs_to_share(share: BucketShare, file_id: UUID) -> BucketFile | None:
    for file in share.files:
        if file.id == file_id and file.status == "uploaded" and file.deleted_at is None:
            return file
    return None


def _file_belongs_to_public_share(share: BucketPublicShare, file_id: UUID) -> BucketFile | None:
    for file in share.files:
        if file.id == file_id and file.status == "uploaded" and file.deleted_at is None:
            return file
    return None


def _vendor_access_files(access: BucketVendorAccess) -> list[BucketFile]:
    source = access.bucket.files if access.file_scope == "all_active" else access.files
    return [file for file in source if file.status == "uploaded" and file.deleted_at is None]


def _file_belongs_to_vendor_access(access: BucketVendorAccess, file_id: UUID) -> BucketFile | None:
    for file in _vendor_access_files(access):
        if file.id == file_id:
            return file
    return None


def _review_response(file: BucketFile, annotations: list[BucketFileAnnotation], *, preview: bool = True) -> BucketFileReviewRead:
    return BucketFileReviewRead(
        file=BucketFileRead.model_validate(file),
        preview_url=_download_url(file.s3_key, disposition="inline", content_type=file.content_type) if preview else None,
        annotations=[BucketFileAnnotationRead.model_validate(annotation) for annotation in annotations],
    )


def _share_read(share: BucketShare, *, passcode: str | None = None) -> BucketShareRead:
    data = BucketShareRead.model_validate(share)
    data.files = [file for file in data.files if file.status == "uploaded" and file.deleted_at is None]
    data.share_url = _public_url(f"/buckets/share/{share.token}")
    data.passcode = passcode
    return data


def _public_share_read(share: BucketPublicShare) -> BucketPublicShareRead:
    data = BucketPublicShareRead.model_validate(share)
    data.files = [file for file in data.files if file.status == "uploaded" and file.deleted_at is None]
    data.share_url = _public_url(f"/buckets/public-share/{share.token}")
    return data


async def _load_share_for_admin_read(db: AsyncSession, share_id: UUID) -> BucketShare:
    share = (
        await db.execute(
            select(BucketShare)
            .where(BucketShare.id == share_id)
            .options(selectinload(BucketShare.files))
        )
    ).scalar_one()
    return share


async def _load_public_share_for_admin_read(db: AsyncSession, share_id: UUID) -> BucketPublicShare:
    share = (
        await db.execute(
            select(BucketPublicShare)
            .where(BucketPublicShare.id == share_id)
            .options(selectinload(BucketPublicShare.files))
        )
    ).scalar_one()
    return share


def _upload_link_read(link: BucketUploadLink, *, passcode: str | None = None) -> BucketUploadLinkRead:
    data = BucketUploadLinkRead.model_validate(link)
    data.upload_url = _public_url(f"/buckets/request/{link.token}")
    data.passcode = passcode
    return data


def _vendor_access_read(access: BucketVendorAccess) -> BucketVendorAccessRead:
    data = BucketVendorAccessRead.model_validate(access)
    data.vendor_name = access.vendor.name if access.vendor else None
    data.vendor_email = access.vendor.email if access.vendor else None
    data.files = [BucketFileRead.model_validate(file) for file in _vendor_access_files(access)]
    return data


def _bucket_detail_read(bucket: Bucket) -> BucketDetail:
    data = BucketDetail.model_validate(bucket)
    data.files = [file for file in data.files if file.deleted_at is None]
    data.upload_links = [
        _upload_link_read(link) for link in bucket.upload_links
        if link.status == "active" and (link.expires_at is None or link.expires_at > _now())
    ]
    data.shares = [_share_read(share) for share in bucket.shares]
    data.vendor_access = [_vendor_access_read(access) for access in bucket.vendor_access]
    data.public_shares = [_public_share_read(share) for share in bucket.public_shares]
    return data


async def _uploaded_bucket_files(db: AsyncSession, bucket_id: UUID, file_ids: list[UUID]) -> list[BucketFile]:
    unique_ids = list(dict.fromkeys(file_ids))
    if not unique_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one uploaded file must be selected")
    files = (
        await db.execute(
            select(BucketFile).where(
                BucketFile.bucket_id == bucket_id,
                BucketFile.id.in_(unique_ids),
                BucketFile.status == "uploaded",
                BucketFile.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    if len(files) != len(unique_ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected files must be uploaded files in this bucket")
    return files


async def _vendor_user_from_payload(
    db: AsyncSession,
    *,
    vendor_user_id: UUID | None,
    vendor_name: str | None,
    vendor_email: str | None,
    send_invite: bool = True,
) -> User:
    if vendor_user_id is not None:
        user = await db.get(User, vendor_user_id)
        if user is None or user.deleted_at is not None or user.role != Role.VENDOR:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected user must be an active vendor")
        if send_invite:
            await clerk_service.invite_user(
                email=user.email,
                name=user.name,
                role=Role.VENDOR,
                redirect_url=_public_url("/vendor/buckets"),
            )
        return user
    if not vendor_email or not vendor_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vendor name and email are required")
    normalized = vendor_email.lower().strip()
    user = (await db.execute(select(User).where(User.email == normalized))).scalar_one_or_none()
    if user is not None and user.deleted_at is None and user.role != Role.VENDOR:
        raise HTTPException(status.HTTP_409_CONFLICT, "That email belongs to a non-vendor user")
    created_or_reactivated = False
    if user is None:
        user = User(email=normalized, name=vendor_name.strip(), role=Role.VENDOR, clerk_id=None)
        db.add(user)
        created_or_reactivated = True
    else:
        user.name = vendor_name.strip() or user.name
        user.role = Role.VENDOR
        if user.deleted_at is not None:
            user.deleted_at = None
            user.clerk_id = None
            created_or_reactivated = True
    await db.flush()
    if send_invite:
        await clerk_service.invite_user(
            email=normalized,
            name=user.name,
            role=Role.VENDOR,
            redirect_url=_public_url("/vendor/buckets"),
        )
    elif created_or_reactivated:
        await db.flush()
    return user


def _vendor_file_ids(access: BucketVendorAccess) -> list[UUID]:
    return [file.id for file in _vendor_access_files(access)]


async def _apply_vendor_access_file_scope(
    db: AsyncSession,
    access: BucketVendorAccess,
    bucket_id: UUID,
    *,
    file_scope: str,
    file_ids: list[UUID] | None,
) -> None:
    if file_scope not in {"all_active", "selected"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Vendor file scope must be all_active or selected")
    access.file_scope = file_scope
    if file_scope == "all_active":
        access.files = []
        return
    files = await _uploaded_bucket_files(db, bucket_id, file_ids or [])
    access.files = files


@router.get("/templates", response_model=list[BucketTemplateRead])
async def list_templates(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[BucketDocumentTemplate]:
    return (
        await db.execute(
            select(BucketDocumentTemplate)
            .where(BucketDocumentTemplate.is_active.is_(True))
            .order_by(BucketDocumentTemplate.category, BucketDocumentTemplate.name)
        )
    ).scalars().all()


@router.get("", response_model=list[BucketRead])
async def list_buckets(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[Bucket]:
    buckets = (
        await db.execute(
            select(Bucket)
            .where(Bucket.archived_at.is_(None))
            .options(
                selectinload(Bucket.files),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
            .order_by(Bucket.updated_at.desc())
        )
    ).scalars().all()
    for bucket in buckets:
        bucket.file_count = len(bucket.files)
        bucket.uploaded_file_count = len([file for file in bucket.files if file.status == "uploaded"])
    return buckets


@router.post("", response_model=BucketRead)
async def create_bucket(
    payload: BucketCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> Bucket:
    bucket = Bucket(**payload.model_dump(), created_by_id=user.id)
    db.add(bucket)
    await db.flush()
    await _log(db, bucket.id, "bucket_created", request=request, user=user, detail=bucket.name)
    await db.commit()
    await db.refresh(bucket)
    return bucket


@router.get("/admin/vendors", response_model=list[BucketVendorRead])
async def list_bucket_vendors(
    q: str | None = None,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    filters = [User.role == Role.VENDOR, User.deleted_at.is_(None)]
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(or_(User.name.ilike(pattern), User.email.ilike(pattern)))
    return (
        await db.execute(
            select(User)
            .where(*filters)
            .order_by(User.name.asc(), User.email.asc())
            .limit(100)
        )
    ).scalars().all()


@router.post("/admin/vendors", response_model=BucketVendorRead, status_code=status.HTTP_201_CREATED)
async def invite_bucket_vendor(
    payload: BucketVendorAccessCreate,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> User:
    vendor = await _vendor_user_from_payload(
        db,
        vendor_user_id=payload.vendor_user_id,
        vendor_name=payload.vendor_name,
        vendor_email=str(payload.vendor_email) if payload.vendor_email else None,
        send_invite=True,
    )
    await db.commit()
    await db.refresh(vendor)
    return vendor


@router.get("/admin/{bucket_id}", response_model=BucketDetail)
async def get_bucket(
    bucket_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketDetail:
    bucket = await _load_bucket_detail_or_404(db, bucket_id)
    return _bucket_detail_read(bucket)


@router.get("/admin/{bucket_id}/activity", response_model=BucketActivityPage)
async def list_bucket_activity(
    bucket_id: UUID,
    limit: int = Query(default=12, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    action: str | None = None,
    actor_role: str | None = None,
    target_type: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketActivityPage:
    await _load_bucket_or_404(db, bucket_id)
    filters = [BucketActivityLog.bucket_id == bucket_id]
    if action:
        filters.append(BucketActivityLog.action == action)
    if actor_role:
        filters.append(BucketActivityLog.actor_role == actor_role)
    if target_type:
        filters.append(BucketActivityLog.target_type == target_type)
    if date_from:
        filters.append(BucketActivityLog.created_at >= date_from)
    if date_to:
        filters.append(BucketActivityLog.created_at <= date_to)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                BucketActivityLog.action.ilike(pattern),
                BucketActivityLog.actor_name.ilike(pattern),
                BucketActivityLog.actor_email.ilike(pattern),
                BucketActivityLog.detail.ilike(pattern),
                BucketActivityLog.target_type.ilike(pattern),
            )
        )

    total = (await db.execute(select(func.count()).select_from(BucketActivityLog).where(*filters))).scalar_one()
    items = (
        await db.execute(
            select(BucketActivityLog)
            .where(*filters)
            .order_by(BucketActivityLog.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return BucketActivityPage(
        items=[BucketActivityRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/admin/{bucket_id}/ai-reviews", response_model=list[BucketAIReviewRead])
async def list_ai_reviews(
    bucket_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[BucketAIReview]:
    await _load_bucket_or_404(db, bucket_id)
    return (
        await db.execute(
            select(BucketAIReview)
            .where(BucketAIReview.bucket_id == bucket_id)
            .order_by(BucketAIReview.created_at.desc())
            .limit(20)
        )
    ).scalars().all()


@router.post("/admin/{bucket_id}/ai-reviews", response_model=BucketAIReviewRead)
async def queue_ai_review(
    bucket_id: UUID,
    payload: BucketAIReviewCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketAIReview:
    bucket = await _load_bucket_or_404(db, bucket_id)
    context_patch = payload.context.model_dump(exclude_none=True) if payload.context else {}
    if context_patch:
        bucket.ai_context = {**(bucket.ai_context or {}), **context_patch}
    active_files = [file for file in bucket.files if file.status == "uploaded" and file.deleted_at is None]
    review = BucketAIReview(
        bucket_id=bucket_id,
        requested_by_user_id=user.id,
        status="queued",
        context_snapshot=bucket.ai_context or {},
        file_ids=[str(file.id) for file in active_files],
        provider="bedrock",
    )
    db.add(review)
    await db.flush()
    await _log(db, bucket_id, "ai_review_queued", request=request, user=user, target_type="ai_review", target_id=str(review.id), detail=f"{len(active_files)} files")
    await db.commit()
    await db.refresh(review)
    return review


@router.get("/admin/{bucket_id}/ai-reviews/{review_id}", response_model=BucketAIReviewRead)
async def get_ai_review(
    bucket_id: UUID,
    review_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketAIReview:
    review = await db.get(BucketAIReview, review_id)
    if review is None or review.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI review not found")
    return review


@router.get("/admin/{bucket_id}/ai-chat", response_model=list[BucketAIMessageRead])
async def list_admin_ai_chat(
    bucket_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[BucketAIMessage]:
    await _load_bucket_or_404(db, bucket_id)
    return (
        await db.execute(
            select(BucketAIMessage)
            .where(BucketAIMessage.bucket_id == bucket_id, BucketAIMessage.audience == "admin")
            .order_by(BucketAIMessage.created_at.asc())
            .limit(100)
        )
    ).scalars().all()


@router.post("/admin/{bucket_id}/ai-chat", response_model=BucketAIChatResponse)
async def admin_ai_chat(
    bucket_id: UUID,
    payload: BucketAIChatMessageCreate,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketAIChatResponse:
    bucket = await _load_bucket_or_404(db, bucket_id)
    messages, proposals, _ = await create_chat_reply(
        db,
        bucket=bucket,
        audience="admin",
        message=payload.message,
        actor_name=user.name or user.email,
        user=user,
    )
    await db.commit()
    return BucketAIChatResponse(
        messages=[BucketAIMessageRead.model_validate(message) for message in messages],
        proposed_action_items=[BucketAIActionItemRead.model_validate(item) for item in proposals],
    )


@router.post("/admin/{bucket_id}/ai-context/apply", response_model=BucketDetail)
async def apply_ai_context(
    bucket_id: UUID,
    payload: BucketAIContextPatch,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketDetail:
    bucket = await _load_bucket_or_404(db, bucket_id)
    patch = payload.model_dump(exclude_none=True)
    bucket.ai_context = {**(bucket.ai_context or {}), **patch}
    await _log(db, bucket_id, "ai_context_updated", request=request, user=user, target_type="bucket", target_id=str(bucket_id), detail=", ".join(patch.keys()))
    await db.commit()
    await db.refresh(bucket)
    return _bucket_detail_read(bucket)


@router.get("/admin/{bucket_id}/ai-action-items", response_model=list[BucketAIActionItemRead])
async def list_ai_action_items(
    bucket_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[BucketAIActionItem]:
    await _load_bucket_or_404(db, bucket_id)
    return await visible_action_items(db, bucket_id)


@router.post("/admin/{bucket_id}/ai-action-items", response_model=BucketAIActionItemRead)
async def create_ai_action_item(
    bucket_id: UUID,
    payload: BucketAIActionItemCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketAIActionItem:
    await _load_bucket_or_404(db, bucket_id)
    if payload.route == "share":
        if payload.share_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select a share recipient for this task")
        share = await db.get(BucketShare, payload.share_id)
        if share is None or share.bucket_id != bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected share does not belong to this bucket")
        payload.upload_link_id = None
        payload.vendor_access_id = None
    if payload.route == "vendor":
        if payload.vendor_access_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select a vendor for this task")
        access = await db.get(BucketVendorAccess, payload.vendor_access_id)
        if access is None or access.bucket_id != bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected vendor access does not belong to this bucket")
        payload.upload_link_id = None
        payload.share_id = None
    if payload.route == "uploader":
        if payload.upload_link_id is not None:
            link = await db.get(BucketUploadLink, payload.upload_link_id)
            if link is None or link.bucket_id != bucket_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected uploader does not belong to this bucket")
        payload.share_id = None
        payload.vendor_access_id = None
    if payload.route == "admin":
        payload.upload_link_id = None
        payload.share_id = None
        payload.vendor_access_id = None

    approved_at = _now() if payload.status in ("approved", "completed") else None
    item = BucketAIActionItem(
        bucket_id=bucket_id,
        status=payload.status,
        route=payload.route,
        upload_link_id=payload.upload_link_id,
        share_id=payload.share_id,
        vendor_access_id=payload.vendor_access_id,
        file_id=payload.file_id,
        requested_document_id=payload.requested_document_id,
        title=payload.title.strip(),
        instructions=payload.instructions.strip(),
        rationale=payload.rationale,
        created_by="admin",
        created_by_user_id=user.id,
        approved_by_user_id=user.id if approved_at else None,
        approved_at=approved_at,
        completed_at=_now() if payload.status == "completed" else None,
    )
    db.add(item)
    await _log(
        db,
        bucket_id,
        "ai_action_created",
        request=request,
        user=user,
        target_type="ai_action_item",
        target_id=str(item.id),
        detail=f"{item.route}: {item.title}",
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.patch("/admin/{bucket_id}/ai-action-items/{item_id}", response_model=BucketAIActionItemRead)
async def patch_ai_action_item(
    bucket_id: UUID,
    item_id: UUID,
    payload: BucketAIActionItemPatch,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketAIActionItem:
    item = await db.get(BucketAIActionItem, item_id)
    if item is None or item.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI action item not found")
    changes: list[str] = []
    for field in payload.model_fields_set:
        value = getattr(payload, field)
        setattr(item, field, value)
        changes.append(field)
    if item.route == "share":
        if item.share_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select a share recipient for this task")
        share = await db.get(BucketShare, item.share_id)
        if share is None or share.bucket_id != bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected share does not belong to this bucket")
        item.upload_link_id = None
        item.vendor_access_id = None
    elif item.route == "vendor":
        if item.vendor_access_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select a vendor for this task")
        access = await db.get(BucketVendorAccess, item.vendor_access_id)
        if access is None or access.bucket_id != bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected vendor access does not belong to this bucket")
        item.upload_link_id = None
        item.share_id = None
    elif item.route == "uploader":
        if item.upload_link_id is not None:
            link = await db.get(BucketUploadLink, item.upload_link_id)
            if link is None or link.bucket_id != bucket_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected uploader does not belong to this bucket")
        item.share_id = None
        item.vendor_access_id = None
    else:
        item.upload_link_id = None
        item.share_id = None
        item.vendor_access_id = None
    if "status" in payload.model_fields_set:
        if item.status == "approved" and item.approved_at is None:
            item.approved_at = _now()
            item.approved_by_user_id = user.id
        if item.status == "completed" and item.completed_at is None:
            item.completed_at = _now()
    await _log(db, bucket_id, f"ai_action_{item.status}", request=request, user=user, target_type="ai_action_item", target_id=str(item.id), detail=", ".join(changes) or item.title)
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/admin/{bucket_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bucket(
    bucket_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    bucket = await _load_bucket_or_404(db, bucket_id)
    bucket.archived_at = _now()
    bucket.status = "archived"
    await _log(db, bucket_id, "bucket_deleted", request=request, user=user, detail=bucket.name)
    await db.commit()


@router.post("/admin/{bucket_id}/requested-documents", response_model=BucketRequestedDocumentRead)
async def add_requested_document(
    bucket_id: UUID,
    payload: BucketRequestedDocumentCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketRequestedDocument:
    await _load_bucket_or_404(db, bucket_id)
    template_id = None
    if payload.save_to_library:
        existing = (
            await db.execute(select(BucketDocumentTemplate).where(BucketDocumentTemplate.name == payload.name))
        ).scalar_one_or_none()
        if existing is None:
            existing = BucketDocumentTemplate(
                name=payload.name,
                category=payload.category,
                description=payload.description,
                required=payload.required,
                allow_multiple_files=payload.allow_multiple_files,
            )
            db.add(existing)
            await db.flush()
        else:
            same_category = (existing.category or "") == (payload.category or "")
            if same_category:
                existing.description = payload.description if payload.description is not None else existing.description
                existing.required = payload.required
                existing.allow_multiple_files = payload.allow_multiple_files
        template_id = existing.id
    doc = BucketRequestedDocument(
        bucket_id=bucket_id,
        template_id=template_id,
        name=payload.name,
        category=payload.category,
        description=payload.description,
        required=payload.required,
        allow_multiple_files=payload.allow_multiple_files,
        is_custom=payload.is_custom,
        requires_signature=payload.requires_signature,
        signature_kind=payload.signature_kind,
        template_file_id=payload.template_file_id,
        signature_document_text=payload.signature_document_text,
    )
    db.add(doc)
    await db.flush()
    await _log(db, bucket_id, "requested_document_added", request=request, user=user, target_type="requested_document", target_id=str(doc.id), detail=doc.name)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post("/admin/{bucket_id}/upload-links", response_model=BucketUploadLinkRead)
async def create_upload_link(
    bucket_id: UUID,
    payload: BucketUploadLinkCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketUploadLinkRead:
    await _load_bucket_or_404(db, bucket_id)
    passcode = payload.passcode or _generate_passcode()
    link = BucketUploadLink(
        bucket_id=bucket_id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.recipient_name,
        recipient_email=str(payload.recipient_email) if payload.recipient_email else None,
        expires_at=payload.expires_at,
        allow_notes=payload.allow_notes,
        allow_multiple_sessions=payload.allow_multiple_sessions,
        can_use_ai_chat=payload.can_use_ai_chat,
        can_view_ai_tasks=payload.can_view_ai_tasks,
        passcode_hash=_hash_passcode(passcode),
    )
    db.add(link)
    await db.flush()
    await _log(db, bucket_id, "upload_link_created", request=request, user=user, target_type="upload_link", target_id=str(link.id), detail=link.recipient_name)
    await db.commit()
    await db.refresh(link)
    return _upload_link_read(link, passcode=passcode)


@router.get("/admin/{bucket_id}/vendor-access", response_model=list[BucketVendorAccessRead])
async def list_bucket_vendor_access(
    bucket_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[BucketVendorAccessRead]:
    await _load_bucket_or_404(db, bucket_id)
    rows = (
        await db.execute(
            select(BucketVendorAccess)
            .where(BucketVendorAccess.bucket_id == bucket_id)
            .options(
                selectinload(BucketVendorAccess.vendor),
                selectinload(BucketVendorAccess.files),
                selectinload(BucketVendorAccess.bucket).selectinload(Bucket.files),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
            .order_by(BucketVendorAccess.created_at.desc())
        )
    ).scalars().all()
    return [_vendor_access_read(access) for access in rows]


@router.post("/admin/{bucket_id}/vendor-access", response_model=BucketVendorAccessRead)
async def create_bucket_vendor_access(
    bucket_id: UUID,
    payload: BucketVendorAccessCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketVendorAccessRead:
    await _load_bucket_or_404(db, bucket_id)
    vendor = await _vendor_user_from_payload(
        db,
        vendor_user_id=payload.vendor_user_id,
        vendor_name=payload.vendor_name,
        vendor_email=str(payload.vendor_email) if payload.vendor_email else None,
        send_invite=True,
    )
    access = (
        await db.execute(
            select(BucketVendorAccess)
            .where(BucketVendorAccess.bucket_id == bucket_id, BucketVendorAccess.vendor_user_id == vendor.id)
            .options(selectinload(BucketVendorAccess.files))
        )
    ).scalar_one_or_none()
    action = "vendor_access_created"
    if access is None:
        access = BucketVendorAccess(bucket_id=bucket_id, vendor_user_id=vendor.id)
        db.add(access)
    else:
        action = "vendor_access_reactivated" if access.status == "revoked" else "vendor_access_updated"
    access.status = "active"
    access.expires_at = payload.expires_at
    access.can_preview = payload.can_preview
    access.can_download = payload.can_download
    access.can_add_notes = payload.can_add_notes
    access.can_see_internal_notes = payload.can_see_internal_notes
    access.can_use_ai_chat = payload.can_use_ai_chat
    access.can_view_ai_summary = payload.can_view_ai_summary
    access.can_view_ai_tasks = payload.can_view_ai_tasks
    access.can_propose_tasks = payload.can_propose_tasks
    await _apply_vendor_access_file_scope(
        db,
        access,
        bucket_id,
        file_scope=payload.file_scope,
        file_ids=payload.file_ids,
    )
    await db.flush()
    await _log(
        db,
        bucket_id,
        action,
        request=request,
        user=user,
        target_type="vendor_access",
        target_id=str(access.id),
        detail=f"{vendor.name} | {access.file_scope}",
    )
    await db.commit()
    access = (
        await db.execute(
            select(BucketVendorAccess)
            .where(BucketVendorAccess.id == access.id)
            .options(
                selectinload(BucketVendorAccess.vendor),
                selectinload(BucketVendorAccess.files),
                selectinload(BucketVendorAccess.bucket).selectinload(Bucket.files),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one()
    return _vendor_access_read(access)


@router.patch("/admin/{bucket_id}/vendor-access/{access_id}", response_model=BucketVendorAccessRead)
async def patch_bucket_vendor_access(
    bucket_id: UUID,
    access_id: UUID,
    payload: BucketVendorAccessPatch,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketVendorAccessRead:
    access = (
        await db.execute(
            select(BucketVendorAccess)
            .where(BucketVendorAccess.id == access_id)
            .options(
                selectinload(BucketVendorAccess.vendor),
                selectinload(BucketVendorAccess.files),
                selectinload(BucketVendorAccess.bucket).selectinload(Bucket.files),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if access is None or access.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor access not found")
    changes: list[str] = []
    if "status" in payload.model_fields_set and payload.status is not None:
        access.status = payload.status
        changes.append(f"status={payload.status}")
    for field in (
        "expires_at",
        "can_preview",
        "can_download",
        "can_add_notes",
        "can_see_internal_notes",
        "can_use_ai_chat",
        "can_view_ai_summary",
        "can_view_ai_tasks",
        "can_propose_tasks",
    ):
        if field in payload.model_fields_set:
            setattr(access, field, getattr(payload, field))
            changes.append(field)
    if "file_scope" in payload.model_fields_set or "file_ids" in payload.model_fields_set:
        await _apply_vendor_access_file_scope(
            db,
            access,
            bucket_id,
            file_scope=payload.file_scope or access.file_scope,
            file_ids=payload.file_ids if payload.file_ids is not None else _vendor_file_ids(access),
        )
        changes.append(f"files={len(_vendor_file_ids(access))}")
    action = "vendor_access_reactivated" if "status=active" in changes else "vendor_access_revoked" if "status=revoked" in changes else "vendor_access_updated"
    await _log(
        db,
        bucket_id,
        action,
        request=request,
        user=user,
        target_type="vendor_access",
        target_id=str(access.id),
        detail=", ".join(changes) or None,
    )
    await db.commit()
    access = (
        await db.execute(
            select(BucketVendorAccess)
            .where(BucketVendorAccess.id == access.id)
            .options(
                selectinload(BucketVendorAccess.vendor),
                selectinload(BucketVendorAccess.files),
                selectinload(BucketVendorAccess.bucket).selectinload(Bucket.files),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one()
    return _vendor_access_read(access)


@router.delete("/admin/{bucket_id}/vendor-access/{access_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_bucket_vendor_access(
    bucket_id: UUID,
    access_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    access = await db.get(BucketVendorAccess, access_id)
    if access is None or access.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor access not found")
    access.status = "revoked"
    await _log(
        db,
        bucket_id,
        "vendor_access_revoked",
        request=request,
        user=user,
        target_type="vendor_access",
        target_id=str(access.id),
    )
    await db.commit()


@router.post("/admin/{bucket_id}/shares", response_model=BucketShareRead)
async def create_share(
    bucket_id: UUID,
    payload: BucketShareCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketShareRead:
    await _load_bucket_or_404(db, bucket_id)
    files = await _uploaded_bucket_files(db, bucket_id, payload.file_ids)
    passcode = payload.passcode or _generate_passcode()
    share = BucketShare(
        bucket_id=bucket_id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.recipient_name,
        recipient_email=str(payload.recipient_email) if payload.recipient_email else None,
        passcode_hash=_hash_passcode(passcode),
        can_preview=payload.can_preview,
        can_download=payload.can_download,
        can_add_notes=payload.can_add_notes,
        can_upload=payload.can_upload,
        can_see_internal_notes=payload.can_see_internal_notes,
        can_use_ai_chat=payload.can_use_ai_chat,
        can_view_ai_summary=payload.can_view_ai_summary,
        can_view_ai_tasks=payload.can_view_ai_tasks,
        can_propose_tasks=payload.can_propose_tasks,
        expires_at=payload.expires_at,
    )
    share.files = files
    db.add(share)
    await db.flush()
    await _log(db, bucket_id, "share_created", request=request, user=user, target_type="share", target_id=str(share.id), detail=share.recipient_name)
    await db.commit()
    share = await _load_share_for_admin_read(db, share.id)
    return _share_read(share, passcode=passcode)


@router.post("/admin/{bucket_id}/public-shares", response_model=BucketPublicShareRead)
async def create_public_share(
    bucket_id: UUID,
    payload: BucketPublicShareCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketPublicShareRead:
    await _load_bucket_or_404(db, bucket_id)
    files = await _uploaded_bucket_files(db, bucket_id, payload.file_ids)
    share = BucketPublicShare(
        bucket_id=bucket_id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.recipient_name,
        can_preview=payload.can_preview,
        can_download=payload.can_download,
        expires_at=payload.expires_at,
    )
    share.files = files
    db.add(share)
    await db.flush()
    await _log(db, bucket_id, "public_share_created", request=request, user=user, target_type="public_share", target_id=str(share.id), detail=share.recipient_name)
    await db.commit()
    share = await _load_public_share_for_admin_read(db, share.id)
    return _public_share_read(share)


@router.post("/admin/{bucket_id}/shares/{share_id}/email", response_model=BucketShareEmailResponse)
async def email_share(
    bucket_id: UUID,
    share_id: UUID,
    payload: BucketShareEmailRequest,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketShareEmailResponse:
    """Email an existing share's access (link + code) to recipients FROM the acting
    admin's connected Gmail (firm SES fallback). The composer supplies the full
    subject/body — the body already contains the link + passcode the operator chose.
    CC is applied once. Logs a share.emailed activity (no passcode persisted)."""
    await _load_bucket_or_404(db, bucket_id)
    share = (
        await db.execute(
            select(BucketShare).where(BucketShare.id == share_id, BucketShare.bucket_id == bucket_id)
        )
    ).scalar_one_or_none()
    if share is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    # Don't re-advertise a revoked/expired share — the access layer would reject
    # the link+code anyway, confusing the recipient.
    if not _is_active(share.status, share.expires_at):
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot email a revoked or expired share")

    from app.services.email.user_mailer import send_as_user

    # De-dup case-insensitively; drop any CC already in To so no address gets the
    # passcode twice.
    to_emails = list(dict.fromkeys(str(e).lower().strip() for e in payload.to_emails if "@" in str(e)))
    to_set = set(to_emails)
    cc_emails = [c for c in dict.fromkeys(str(e).lower().strip() for e in payload.cc_emails if "@" in str(e)) if c not in to_set]
    subject = payload.subject.strip()
    body = payload.body  # composer body already carries link + passcode
    html_body = "<br>".join(html.escape(line) for line in body.splitlines())

    sent = 0
    last_detail = None
    cc_delivered = False  # attach CC to the first SUCCESSFUL send, not just idx 0
    for email in to_emails:
        result = await send_as_user(
            db,
            user.id,
            to_emails=[email],
            cc_emails=[] if cc_delivered else cc_emails,
            subject=subject,
            body_text=body,
            body_html=f"<p>{html_body}</p>",
        )
        last_detail = result.detail
        if result.ok:
            sent += 1
            if cc_emails and not cc_delivered:
                cc_delivered = True
    # Audit only — never store the passcode (the body carries it live to the
    # recipient; the log records recipients + outcome, not the code).
    await _log(
        db,
        bucket_id,
        "share_emailed",
        request=request,
        user=user,
        target_type="share",
        target_id=str(share.id),
        detail=f"Emailed share access to {len(to_emails)} recipient(s); {sent} sent.",
    )
    await db.commit()
    return BucketShareEmailResponse(ok=sent > 0, sent=sent, detail=last_detail)


@router.patch("/admin/{bucket_id}/shares/{share_id}", response_model=BucketShareRead)
async def patch_share(
    bucket_id: UUID,
    share_id: UUID,
    payload: BucketSharePatch,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketShareRead:
    share = (
        await db.execute(
            select(BucketShare)
            .where(BucketShare.id == share_id)
            .options(selectinload(BucketShare.files))
        )
    ).scalar_one_or_none()
    if share is None or share.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    changes = []
    for field in payload.model_fields_set:
        value = getattr(payload, field)
        if field == "file_ids":
            files = await _uploaded_bucket_files(db, bucket_id, value or [])
            share.files = files
            changes.append(f"files={len(files)}")
            continue
        if field == "status" and value not in ("active", "revoked"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Share status must be active or revoked")
        changes.append(f"{field}={value}")
        setattr(share, field, value)
    action = "share_status_changed" if "status" in payload.model_fields_set else "share_updated"
    await _log(db, bucket_id, action, request=request, user=user, target_type="share", target_id=str(share.id), detail=", ".join(changes) or None)
    await db.commit()
    share = await _load_share_for_admin_read(db, share.id)
    return _share_read(share)


@router.post("/admin/{bucket_id}/shares/{share_id}/regenerate-passcode", response_model=BucketSharePasscodeResetRead)
async def regenerate_share_passcode(
    bucket_id: UUID,
    share_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketSharePasscodeResetRead:
    share = (
        await db.execute(
            select(BucketShare)
            .where(BucketShare.id == share_id)
            .options(selectinload(BucketShare.files))
        )
    ).scalar_one_or_none()
    if share is None or share.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    passcode = _generate_passcode()
    share.passcode_hash = _hash_passcode(passcode)
    await _log(db, bucket_id, "share_passcode_regenerated", request=request, user=user, target_type="share", target_id=str(share.id), detail=share.recipient_name)
    await db.commit()
    share = await _load_share_for_admin_read(db, share.id)
    return BucketSharePasscodeResetRead(share=_share_read(share, passcode=passcode), passcode=passcode)


@router.post("/admin/{bucket_id}/upload-links/{link_id}/regenerate-passcode", response_model=BucketUploadLinkPasscodeResetRead)
async def regenerate_upload_link_passcode(
    bucket_id: UUID,
    link_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketUploadLinkPasscodeResetRead:
    link = await db.get(BucketUploadLink, link_id)
    if link is None or link.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload link not found")
    passcode = _generate_passcode()
    link.passcode_hash = _hash_passcode(passcode)
    await _log(
        db,
        bucket_id,
        "upload_link_passcode_regenerated",
        request=request,
        user=user,
        target_type="upload_link",
        target_id=str(link.id),
        detail=link.recipient_name,
    )
    await db.commit()
    await db.refresh(link)
    return BucketUploadLinkPasscodeResetRead(upload_link=_upload_link_read(link, passcode=passcode), passcode=passcode)


@router.post("/admin/{bucket_id}/notes", response_model=BucketNoteRead)
async def create_admin_note(
    bucket_id: UUID,
    payload: BucketNoteCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketNote:
    await _load_bucket_or_404(db, bucket_id)
    note = BucketNote(
        bucket_id=bucket_id,
        author_name=user.name,
        author_role=user.role,
        visibility=payload.visibility if payload.visibility in ("admin", "shared") else "admin",
        content=payload.content,
    )
    db.add(note)
    await db.flush()
    await _log(db, bucket_id, "note_created", request=request, user=user, target_type="note", target_id=str(note.id), detail=note.visibility)
    await db.commit()
    await db.refresh(note)
    return note


@router.post("/admin/{bucket_id}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def admin_upload_init(
    bucket_id: UUID,
    payload: BucketFileUploadInit,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    await _load_bucket_or_404(db, bucket_id)
    req = None
    if payload.requested_document_id:
        req = await db.get(BucketRequestedDocument, payload.requested_document_id)
        if req is None or req.bucket_id != bucket_id:
            await _log(db, bucket_id, "admin_file_upload_failed", request=request, user=user, target_type="requested_document", target_id=str(payload.requested_document_id), detail="requested document mismatch")
            await db.commit()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Requested document does not belong to this bucket")
    _, prefix, _ = _bucket_storage_config()
    safe = _safe_filename(payload.file_name)
    requested_doc_condition = (
        BucketFile.requested_document_id == payload.requested_document_id
        if payload.requested_document_id
        else BucketFile.requested_document_id.is_(None)
    )
    existing_file = (
        await db.execute(
            select(BucketFile)
            .where(
                BucketFile.bucket_id == bucket_id,
                BucketFile.upload_link_id.is_(None),
                BucketFile.file_name == payload.file_name,
                BucketFile.size_bytes == payload.size_bytes,
                requested_doc_condition,
                BucketFile.status == "uploading",
                BucketFile.deleted_at.is_(None),
            )
            .order_by(BucketFile.created_at.desc())
        )
    ).scalars().first()
    if existing_file:
        upload_url, headers = _upload_url(existing_file.s3_key, payload.content_type)
        return BucketFileUploadInitResponse(file_id=existing_file.id, upload_url=upload_url, s3_key=existing_file.s3_key, required_headers=headers)
    if req is not None and not req.allow_multiple_files:
        existing_for_doc = (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.bucket_id == bucket_id,
                    BucketFile.requested_document_id == req.id,
                    BucketFile.status.in_(("uploading", "uploaded")),
                    BucketFile.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existing_for_doc is not None:
            await _log(db, bucket_id, "admin_file_upload_failed", request=request, user=user, target_type="requested_document", target_id=str(req.id), detail="single file document already has a file")
            await db.commit()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This requested document only allows one file")
    file_id = uuid4()
    s3_key = f"{prefix}/admin-uploads/{bucket_id}/{file_id}-{safe}"
    file = BucketFile(
        id=file_id,
        bucket_id=bucket_id,
        requested_document_id=payload.requested_document_id,
        upload_link_id=None,
        file_name=payload.file_name,
        s3_key=s3_key,
        content_type=_sanitize_upload_content_type(payload.content_type),
        size_bytes=payload.size_bytes,
        uploaded_by_name=payload.uploader_name,
        uploaded_by_email=str(payload.uploader_email) if payload.uploader_email else None,
        status="uploading",
    )
    db.add(file)
    await _log(
        db,
        bucket_id,
        "admin_file_upload_started",
        request=request,
        user=user,
        target_type="file",
        target_id=str(file.id),
        detail=f"{payload.file_name} for {payload.uploader_name}",
    )
    await db.commit()
    upload_url, headers = _upload_url(s3_key, payload.content_type)
    return BucketFileUploadInitResponse(file_id=file.id, upload_url=upload_url, s3_key=s3_key, required_headers=headers)


@router.post("/admin/{bucket_id}/files/complete", response_model=BucketFileRead)
async def admin_upload_complete(
    bucket_id: UUID,
    payload: BucketUploadComplete,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    bucket = await _load_bucket_or_404(db, bucket_id)
    file = await db.get(BucketFile, payload.file_id)
    if file is None or file.bucket_id != bucket_id or file.upload_link_id is not None or file.deleted_at is not None:
        await _log(db, bucket_id, "admin_file_upload_failed", request=request, user=user, target_type="file", target_id=str(payload.file_id), detail="complete failed: file not found")
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if file.status == "uploaded":
        return file
    file.status = "uploaded"
    if file.requested_document_id:
        req = await db.get(BucketRequestedDocument, file.requested_document_id)
        if req:
            req.status = "uploaded"
    if payload.note:
        db.add(
            BucketNote(
                bucket_id=bucket_id,
                author_name=user.name or user.email or "Super Admin",
                author_role=user.role,
                visibility="admin",
                content=payload.note,
            )
        )
    await _log(
        db,
        bucket_id,
        "admin_file_uploaded",
        request=request,
        user=user,
        target_type="file",
        target_id=str(file.id),
        detail=f"{file.file_name} for {file.uploaded_by_name or 'client'}",
    )
    try:
        from app.services.notifications import notify_bucket_file_uploaded

        await notify_bucket_file_uploaded(db, bucket=bucket, file=file)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("bucket upload notification failed bucket=%s file=%s", bucket_id, file.id)
    try:
        from app.services.bucket_ai import enqueue_file_analysis

        await enqueue_file_analysis(db, file)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("enqueue file analysis failed bucket=%s file=%s", bucket_id, file.id)
    await db.commit()
    await db.refresh(file)
    return file


@router.delete("/admin/{bucket_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_file(
    bucket_id: UUID,
    file_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _load_bucket_or_404(db, bucket_id)
    file = (
        await db.execute(
            select(BucketFile)
            .where(
                BucketFile.id == file_id,
                BucketFile.bucket_id == bucket_id,
            )
            .options(selectinload(BucketFile.shares), selectinload(BucketFile.vendor_access))
        )
    ).scalar_one_or_none()
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if file.deleted_at is not None:
        return

    file.deleted_at = _now()
    file.deleted_by_user_id = user.id
    file.delete_storage_status = _delete_s3_object(file.s3_key)
    file.shares.clear()
    file.vendor_access.clear()
    await _recalculate_requested_document_status(db, file.requested_document_id)
    await _log(
        db,
        bucket_id,
        "file_deleted",
        request=request,
        user=user,
        target_type="file",
        target_id=str(file.id),
        detail=f"{file.file_name} | storage={file.delete_storage_status}",
    )
    await db.commit()


@router.get("/admin/{bucket_id}/files/{file_id}/url", response_model=BucketFileUrl)
async def admin_file_url(
    bucket_id: UUID,
    file_id: UUID,
    request: Request,
    download: bool = False,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileUrl:
    file = await db.get(BucketFile, file_id)
    if file is None or file.bucket_id != bucket_id or file.status != "uploaded" or file.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    disposition = "attachment" if download else "inline"
    await _log(db, bucket_id, "file_download_url_created" if download else "file_preview_url_created", request=request, user=user, target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return BucketFileUrl(url=_download_url(file.s3_key, disposition=disposition, content_type=file.content_type), expires_in=900)


@router.get("/admin/{bucket_id}/files/{file_id}/review", response_model=BucketFileReviewRead)
async def admin_file_review(
    bucket_id: UUID,
    file_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileReviewRead:
    file = await db.get(BucketFile, file_id)
    if file is None or file.bucket_id != bucket_id or file.status != "uploaded" or file.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    annotations = await _file_annotations(db, bucket_id, file_id)
    await _log(db, bucket_id, "file_review_opened", request=request, user=user, target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return _review_response(file, annotations)


@router.post("/admin/{bucket_id}/files/{file_id}/annotations", response_model=BucketFileAnnotationRead)
async def create_admin_file_annotation(
    bucket_id: UUID,
    file_id: UUID,
    payload: BucketFileAnnotationCreate,
    request: Request,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileAnnotation:
    file = await db.get(BucketFile, file_id)
    if file is None or file.bucket_id != bucket_id or file.status != "uploaded" or file.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    annotation = BucketFileAnnotation(
        bucket_id=bucket_id,
        file_id=file_id,
        page_number=payload.page_number,
        x=payload.x,
        y=payload.y,
        width=payload.width,
        height=payload.height,
        comment=payload.comment.strip(),
        author_name=user.name or user.email or "Super Admin",
        author_role=user.role,
    )
    db.add(annotation)
    await db.flush()
    await _log(db, bucket_id, "file_annotation_created", request=request, user=user, target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    await db.refresh(annotation)
    return annotation


@router.get("/vendor", response_model=list[BucketVendorBucketRead])
async def list_vendor_buckets(
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> list[BucketVendorBucketRead]:
    rows = (
        await db.execute(
            select(BucketVendorAccess)
            .where(BucketVendorAccess.vendor_user_id == user.id)
            .options(
                selectinload(BucketVendorAccess.vendor),
                selectinload(BucketVendorAccess.files),
                selectinload(BucketVendorAccess.bucket).selectinload(Bucket.files),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
            .order_by(BucketVendorAccess.created_at.desc())
        )
    ).scalars().all()
    output: list[BucketVendorBucketRead] = []
    for access in rows:
        if access.bucket.archived_at is not None or not _is_active(access.status, access.expires_at):
            continue
        files = _vendor_access_files(access)
        bucket = BucketVendorBucketRead.model_validate(access.bucket)
        bucket.file_count = len(files)
        bucket.uploaded_file_count = len(files)
        bucket.vendor_access = _vendor_access_read(access)
        output.append(bucket)
    return output


@router.get("/vendor/{bucket_id}", response_model=BucketVendorAccessReadResponse)
async def get_vendor_bucket(
    bucket_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> BucketVendorAccessReadResponse:
    access = await _load_vendor_access_or_404(db, bucket_id, user)
    access.last_accessed_at = _now()
    access.view_count += 1
    files: list[BucketShareFileRead] = []
    for file in _vendor_access_files(access):
        item = BucketShareFileRead.model_validate(file)
        if access.can_preview:
            item.preview_url = _download_url(file.s3_key, disposition="inline", content_type=file.content_type)
        if access.can_download:
            item.download_url = _download_url(file.s3_key, disposition="attachment")
        files.append(item)
    notes = [
        note for note in access.bucket.notes
        if note.visibility == "shared" or note.vendor_access_id == access.id or access.can_see_internal_notes
    ]
    review = await latest_review(db, access.bucket_id)
    tasks = await visible_action_items(db, access.bucket_id, route="vendor", vendor_access_id=access.id, approved_only=True) if access.can_view_ai_tasks else []
    await _log(
        db,
        access.bucket_id,
        "vendor_bucket_accessed",
        request=request,
        user=user,
        actor_role="vendor",
        target_type="vendor_access",
        target_id=str(access.id),
    )
    await db.commit()
    bucket = BucketRead.model_validate(access.bucket)
    bucket.file_count = len(files)
    bucket.uploaded_file_count = len(files)
    return BucketVendorAccessReadResponse(
        bucket=bucket,
        vendor_access=_vendor_access_read(access),
        files=files,
        notes=[BucketNoteRead.model_validate(note) for note in notes],
        ai_summary=vendor_visible_summary(review, access) if access.can_view_ai_summary else None,
        ai_tasks=[BucketAIActionItemRead.model_validate(item) for item in tasks],
    )


@router.get("/vendor/{bucket_id}/ai-summary", response_model=BucketAISummaryRead)
async def vendor_ai_summary(
    bucket_id: UUID,
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> BucketAISummaryRead:
    access = await _load_vendor_access_or_404(db, bucket_id, user)
    if not access.can_view_ai_summary:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI summary is disabled for this vendor access")
    return BucketAISummaryRead(summary=vendor_visible_summary(await latest_review(db, access.bucket_id), access))


@router.get("/vendor/{bucket_id}/ai-tasks", response_model=list[BucketAIActionItemRead])
async def vendor_ai_tasks(
    bucket_id: UUID,
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> list[BucketAIActionItem]:
    access = await _load_vendor_access_or_404(db, bucket_id, user)
    if not access.can_view_ai_tasks:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI tasks are disabled for this vendor access")
    return await visible_action_items(db, access.bucket_id, route="vendor", vendor_access_id=access.id, approved_only=True)


@router.post("/vendor/{bucket_id}/ai-chat", response_model=BucketAIChatResponse)
async def vendor_ai_chat(
    bucket_id: UUID,
    payload: BucketAIChatMessageCreate,
    request: Request,
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> BucketAIChatResponse:
    access = await _load_vendor_access_or_404(db, bucket_id, user)
    if not access.can_use_ai_chat:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI chat is disabled for this vendor access")
    messages, proposals, _ = await create_chat_reply(
        db,
        bucket=access.bucket,
        audience="vendor",
        message=payload.message,
        actor_name=user.name or user.email,
        user=user,
        vendor_access=access,
    )
    if not access.can_propose_tasks:
        for item in proposals:
            await db.delete(item)
        proposals = []
    await _log(
        db,
        access.bucket_id,
        "vendor_ai_chat",
        request=request,
        user=user,
        actor_role="vendor",
        target_type="vendor_access",
        target_id=str(access.id),
        detail=payload.message[:180],
    )
    if proposals:
        await _log(
            db,
            access.bucket_id,
            "vendor_task_proposed",
            request=request,
            user=user,
            actor_role="vendor",
            target_type="vendor_access",
            target_id=str(access.id),
            detail=f"{len(proposals)} proposed task{'s' if len(proposals) != 1 else ''}",
        )
    await db.commit()
    return BucketAIChatResponse(
        messages=[BucketAIMessageRead.model_validate(message) for message in messages],
        proposed_action_items=[BucketAIActionItemRead.model_validate(item) for item in proposals],
    )


@router.get("/vendor/{bucket_id}/files/{file_id}/review", response_model=BucketFileReviewRead)
async def vendor_file_review(
    bucket_id: UUID,
    file_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileReviewRead:
    access = await _load_vendor_access_or_404(db, bucket_id, user)
    if not access.can_preview:
        await _log(db, bucket_id, "vendor_file_review_denied", request=request, user=user, actor_role="vendor", target_type="file", target_id=str(file_id), detail="preview disabled")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Preview is disabled for this vendor access")
    file = _file_belongs_to_vendor_access(access, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    annotations = await _file_annotations(db, bucket_id, file_id)
    await _log(db, bucket_id, "vendor_file_previewed", request=request, user=user, actor_role="vendor", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return _review_response(file, annotations, preview=True)


@router.post("/vendor/{bucket_id}/files/{file_id}/download", response_model=BucketFileUrl)
async def vendor_file_download(
    bucket_id: UUID,
    file_id: UUID,
    request: Request,
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileUrl:
    access = await _load_vendor_access_or_404(db, bucket_id, user)
    file = _file_belongs_to_vendor_access(access, file_id)
    if file is None:
        await _log(db, bucket_id, "vendor_file_download_denied", request=request, user=user, actor_role="vendor", target_type="file", target_id=str(file_id), detail="file not assigned")
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if not access.can_download:
        await _log(db, bucket_id, "vendor_file_download_denied", request=request, user=user, actor_role="vendor", target_type="file", target_id=str(file.id), detail=file.file_name)
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Download is disabled for this vendor access")
    access.download_count += 1
    await _log(db, bucket_id, "vendor_file_download_requested", request=request, user=user, actor_role="vendor", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return BucketFileUrl(url=_download_url(file.s3_key, disposition="attachment"), expires_in=900)


@router.post("/vendor/{bucket_id}/files/{file_id}/annotations", response_model=BucketFileAnnotationRead)
async def create_vendor_file_annotation(
    bucket_id: UUID,
    file_id: UUID,
    payload: BucketFileAnnotationCreate,
    request: Request,
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileAnnotation:
    access = await _load_vendor_access_or_404(db, bucket_id, user)
    if not access.can_preview or not access.can_add_notes:
        await _log(db, bucket_id, "vendor_file_annotation_denied", request=request, user=user, actor_role="vendor", target_type="file", target_id=str(file_id), detail="preview or notes disabled")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Annotations are disabled for this vendor access")
    file = _file_belongs_to_vendor_access(access, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    annotation = BucketFileAnnotation(
        bucket_id=bucket_id,
        file_id=file_id,
        vendor_access_id=access.id,
        page_number=payload.page_number,
        x=payload.x,
        y=payload.y,
        width=payload.width,
        height=payload.height,
        comment=payload.comment.strip(),
        author_name=user.name or user.email,
        author_role="vendor",
    )
    db.add(annotation)
    await db.flush()
    await _log(db, bucket_id, "vendor_file_annotation_created", request=request, user=user, actor_role="vendor", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    await db.refresh(annotation)
    return annotation


@router.post("/vendor/{bucket_id}/notes", response_model=BucketNoteRead)
async def create_vendor_note(
    bucket_id: UUID,
    payload: BucketNoteCreate,
    request: Request,
    user: User = Depends(require_role(Role.VENDOR)),
    db: AsyncSession = Depends(get_db),
) -> BucketNote:
    access = await _load_vendor_access_or_404(db, bucket_id, user)
    if not access.can_add_notes:
        await _log(db, bucket_id, "vendor_note_denied", request=request, user=user, actor_role="vendor", target_type="vendor_access", target_id=str(access.id), detail="notes disabled")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Notes are disabled for this vendor access")
    note = BucketNote(
        bucket_id=bucket_id,
        vendor_access_id=access.id,
        author_name=user.name or user.email,
        author_role="vendor",
        visibility="shared",
        content=payload.content,
    )
    db.add(note)
    await db.flush()
    await _log(db, bucket_id, "vendor_note_created", request=request, user=user, actor_role="vendor", target_type="note", target_id=str(note.id))
    await db.commit()
    await db.refresh(note)
    return note


@router.get("/request/{token}", response_model=BucketRequestInfoRead)
async def request_link_info(token: str, db: AsyncSession = Depends(get_db)) -> BucketRequestInfoRead:
    link = await _load_upload_link_or_404(db, token)
    return BucketRequestInfoRead(
        bucket=BucketRequestBucketRead(name=link.bucket.name, client_name=link.bucket.client_name, purpose=link.bucket.purpose),
        recipient_name=link.recipient_name,
        recipient_email=link.recipient_email,
        requires_passcode=bool(link.passcode_hash),
        status=link.status,
    )


@router.post("/request/{token}/access", response_model=BucketRequestAccessRead)
async def request_link_access(
    token: str,
    payload: BucketRequestAccessRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketRequestAccessRead:
    link = await _load_upload_link_or_404(db, token)
    _require_upload_passcode(link)
    if not _verify_passcode(payload.passcode, link.passcode_hash):
        await _log(db, link.bucket_id, "upload_passcode_failed", request=request, actor_name=link.recipient_name, actor_role="uploader", target_type="upload_link", target_id=str(link.id))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid access code")
    await _log(db, link.bucket_id, "upload_link_accessed", request=request, actor_name=link.recipient_name, actor_email=link.recipient_email, actor_role="uploader", target_type="upload_link", target_id=str(link.id))
    files = sorted(
        (file for file in link.bucket.files if file.status == "uploaded" and file.deleted_at is None),
        key=lambda file: file.created_at,
        reverse=True,
    )
    review = await latest_review(db, link.bucket_id)
    await db.commit()
    return BucketRequestAccessRead(
        bucket=BucketRequestBucketRead(name=link.bucket.name, client_name=link.bucket.client_name, purpose=link.bucket.purpose),
        recipient_name=link.recipient_name,
        recipient_email=link.recipient_email,
        allow_notes=link.allow_notes,
        can_use_ai_chat=link.can_use_ai_chat,
        can_view_ai_tasks=link.can_view_ai_tasks,
        requested_documents=[BucketRequestedDocumentRead.model_validate(d) for d in link.bucket.requested_documents],
        files=[BucketRequestUploadedFileRead.model_validate(file) for file in files],
        ai_summary=upload_link_visible_summary(review, link.bucket),
    )


@router.post("/request/{token}/upload-init", response_model=BucketFileUploadInitResponse)
async def request_upload_init(
    token: str,
    payload: BucketFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    link = await _load_upload_link_or_404(db, token)
    _require_upload_passcode(link)
    if not _verify_passcode(payload.passcode or "", link.passcode_hash):
        await _log(db, link.bucket_id, "upload_passcode_failed", request=request, actor_name=payload.uploader_name or link.recipient_name, actor_email=str(payload.uploader_email) if payload.uploader_email else link.recipient_email, actor_role="uploader", target_type="upload_link", target_id=str(link.id), detail=payload.file_name)
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid access code")
    if payload.requested_document_id:
        req = await db.get(BucketRequestedDocument, payload.requested_document_id)
        if req is None or req.bucket_id != link.bucket_id:
            await _log(db, link.bucket_id, "file_upload_failed", request=request, actor_name=payload.uploader_name, actor_email=str(payload.uploader_email) if payload.uploader_email else None, actor_role="uploader", target_type="requested_document", target_id=str(payload.requested_document_id), detail="requested document mismatch")
            await db.commit()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Requested document does not belong to this bucket")
    else:
        req = None
    _, prefix, _ = _bucket_storage_config()
    safe = _safe_filename(payload.file_name)
    existing_conditions = [
        BucketFile.bucket_id == link.bucket_id,
        BucketFile.upload_link_id == link.id,
        BucketFile.file_name == payload.file_name,
        BucketFile.size_bytes == payload.size_bytes,
        BucketFile.status.in_(("uploading", "uploaded")),
        BucketFile.deleted_at.is_(None),
    ]
    if payload.requested_document_id:
        existing_conditions.append(BucketFile.requested_document_id == payload.requested_document_id)
    else:
        existing_conditions.append(BucketFile.requested_document_id.is_(None))
    existing_file = (
        await db.execute(
            select(BucketFile)
            .where(*existing_conditions)
            .order_by(BucketFile.created_at.desc())
        )
    ).scalars().first()
    if existing_file:
        upload_url, headers = _upload_url(existing_file.s3_key, payload.content_type)
        return BucketFileUploadInitResponse(file_id=existing_file.id, upload_url=upload_url, s3_key=existing_file.s3_key, required_headers=headers)
    if req is not None and not req.allow_multiple_files:
        existing_for_doc = (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.bucket_id == link.bucket_id,
                    BucketFile.requested_document_id == req.id,
                    BucketFile.status.in_(("uploading", "uploaded")),
                    BucketFile.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existing_for_doc is not None:
            await _log(db, link.bucket_id, "file_upload_failed", request=request, actor_name=payload.uploader_name, actor_email=str(payload.uploader_email) if payload.uploader_email else None, actor_role="uploader", target_type="requested_document", target_id=str(req.id), detail="single file document already has a file")
            await db.commit()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This requested document only allows one file")
    file_id = uuid4()
    s3_key = f"{prefix}/uploads/{link.bucket_id}/{file_id}-{safe}"
    file = BucketFile(
        id=file_id,
        bucket_id=link.bucket_id,
        requested_document_id=payload.requested_document_id,
        upload_link_id=link.id,
        file_name=payload.file_name,
        s3_key=s3_key,
        content_type=_sanitize_upload_content_type(payload.content_type),
        size_bytes=payload.size_bytes,
        uploaded_by_name=payload.uploader_name,
        uploaded_by_email=str(payload.uploader_email) if payload.uploader_email else None,
        status="uploading",
    )
    db.add(file)
    await _log(db, link.bucket_id, "file_upload_started", request=request, actor_name=payload.uploader_name, actor_email=str(payload.uploader_email) if payload.uploader_email else None, actor_role="uploader", target_type="file", target_id=str(file.id), detail=payload.file_name)
    await db.commit()
    upload_url, headers = _upload_url(s3_key, payload.content_type)
    return BucketFileUploadInitResponse(file_id=file.id, upload_url=upload_url, s3_key=s3_key, required_headers=headers)


@router.post("/request/{token}/complete", response_model=BucketFileRead)
async def request_upload_complete(
    token: str,
    payload: BucketUploadComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    link = await _load_upload_link_or_404(db, token)
    file = await db.get(BucketFile, payload.file_id)
    if file is None or file.bucket_id != link.bucket_id or file.upload_link_id != link.id or file.deleted_at is not None:
        await _log(db, link.bucket_id, "file_upload_failed", request=request, actor_name=link.recipient_name, actor_email=link.recipient_email, actor_role="uploader", target_type="file", target_id=str(payload.file_id), detail="complete failed: file not found")
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if file.status == "uploaded":
        return file
    file.status = "uploaded"
    link.completed_at = _now()
    if file.requested_document_id:
        req = await db.get(BucketRequestedDocument, file.requested_document_id)
        if req:
            req.status = "uploaded"
    if payload.note and link.allow_notes:
        db.add(
            BucketNote(
                bucket_id=link.bucket_id,
                author_name=file.uploaded_by_name or link.recipient_name,
                author_role="uploader",
                visibility="shared",
                content=payload.note,
            )
        )
    await _log(db, link.bucket_id, "file_uploaded", request=request, actor_name=file.uploaded_by_name or link.recipient_name, actor_email=file.uploaded_by_email or link.recipient_email, actor_role="uploader", target_type="file", target_id=str(file.id), detail=file.file_name)
    try:
        from app.services.notifications import notify_bucket_file_uploaded

        await notify_bucket_file_uploaded(db, bucket=link.bucket, file=file)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("bucket upload notification failed bucket=%s file=%s", link.bucket_id, file.id)
    await db.commit()
    await db.refresh(file)
    return file


async def _request_ai_tasks(db: AsyncSession, *, token: str, passcode: str, request: Request) -> list[BucketAIActionItem]:
    link = await _load_upload_link_or_404(db, token)
    if not link.can_view_ai_tasks:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI tasks are disabled for this upload link")
    if not _verify_passcode(passcode, link.passcode_hash):
        await _log(db, link.bucket_id, "upload_passcode_failed", request=request, actor_name=link.recipient_name, actor_email=link.recipient_email, actor_role="uploader", target_type="upload_link", target_id=str(link.id), detail="ai tasks")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid access code")
    return await visible_action_items(db, link.bucket_id, route="uploader", upload_link_id=link.id, approved_only=True)


@router.post("/request/{token}/ai-tasks", response_model=list[BucketAIActionItemRead])
async def request_ai_tasks(
    token: str,
    payload: BucketRequestAccessRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[BucketAIActionItem]:
    """Passcode travels in the POST body — query strings leak into access logs,
    Referer headers, and browser history."""
    return await _request_ai_tasks(db, token=token, passcode=payload.passcode, request=request)


@router.get("/request/{token}/ai-tasks", response_model=list[BucketAIActionItemRead], deprecated=True)
async def request_ai_tasks_get(
    token: str,
    request: Request,
    passcode: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> list[BucketAIActionItem]:
    """Deprecated: passcode-in-query variant kept for older mobile clients.
    Web uses the POST route above; remove once mobile is updated."""
    return await _request_ai_tasks(db, token=token, passcode=passcode, request=request)


@router.post("/request/{token}/ai-chat", response_model=BucketAIChatResponse)
async def request_ai_chat(
    token: str,
    payload: BucketAIChatMessageCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketAIChatResponse:
    link = await _load_upload_link_or_404(db, token)
    if not link.can_use_ai_chat:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI chat is disabled for this upload link")
    if not _verify_passcode(payload.passcode or "", link.passcode_hash):
        await _log(db, link.bucket_id, "upload_passcode_failed", request=request, actor_name=link.recipient_name, actor_email=link.recipient_email, actor_role="uploader", target_type="upload_link", target_id=str(link.id), detail="ai chat")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid access code")
    bucket = await _load_bucket_or_404(db, link.bucket_id)
    messages, proposals, _ = await create_chat_reply(
        db,
        bucket=bucket,
        audience="uploader",
        message=payload.message,
        actor_name=link.recipient_name,
        upload_link=link,
    )
    await db.commit()
    return BucketAIChatResponse(
        messages=[BucketAIMessageRead.model_validate(message) for message in messages],
        proposed_action_items=[BucketAIActionItemRead.model_validate(item) for item in proposals],
    )


@router.post("/share/{token}/access", response_model=BucketShareAccessRead)
async def share_access(
    token: str,
    payload: BucketShareAccessRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketShareAccessRead:
    share = await _load_share_or_404(db, token)
    if not _verify_passcode(payload.passcode, share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="share", target_id=str(share.id))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    share.last_accessed_at = _now()
    share.view_count += 1
    files = []
    for file in share.files:
        if file.status != "uploaded" or file.deleted_at is not None:
            continue
        item = BucketShareFileRead.model_validate(file)
        if share.can_preview:
            item.preview_url = _download_url(file.s3_key, disposition="inline", content_type=file.content_type)
        if share.can_download:
            item.download_url = _download_url(file.s3_key, disposition="attachment")
        files.append(item)
    # A "shared" note is visible to this recipient only if it was authored by
    # the operator (no share/vendor owner) or by this same share — not by other
    # share recipients or vendors on the bucket (which would leak their PII and
    # comments). can_see_internal_notes still widens to everything.
    notes = [
        n for n in share.bucket.notes
        if share.can_see_internal_notes
        or (n.visibility == "shared" and n.vendor_access_id is None and n.share_id in (None, share.id))
    ]
    review = await latest_review(db, share.bucket_id)
    tasks = await visible_action_items(db, share.bucket_id, route="share", share_id=share.id, approved_only=True) if share.can_view_ai_tasks else []
    await _log(db, share.bucket_id, "share_accessed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="share", target_id=str(share.id))
    await db.commit()
    share_out = _share_read(share)
    return BucketShareAccessRead(
        bucket=BucketRead.model_validate(share.bucket),
        share=share_out,
        files=files,
        notes=[BucketNoteRead.model_validate(n) for n in notes],
        ai_summary=share_visible_summary(review, share) if share.can_view_ai_summary else None,
        ai_tasks=[BucketAIActionItemRead.model_validate(item) for item in tasks],
    )


@router.get("/share/{token}", response_model=BucketShareInfoRead)
async def share_info(token: str, db: AsyncSession = Depends(get_db)) -> BucketShareInfoRead:
    share = await _load_share_or_404(db, token)
    return BucketShareInfoRead(
        bucket=BucketRead.model_validate(share.bucket),
        recipient_name=share.recipient_name,
        recipient_email=share.recipient_email,
        can_download=share.can_download,
        can_add_notes=share.can_add_notes,
        can_use_ai_chat=share.can_use_ai_chat,
        can_view_ai_summary=share.can_view_ai_summary,
        can_view_ai_tasks=share.can_view_ai_tasks,
        can_propose_tasks=share.can_propose_tasks,
    )


@router.get("/public-share/{token}", response_model=BucketPublicShareAccessRead)
async def public_share_access(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> BucketPublicShareAccessRead:
    share = await _load_public_share_or_404(db, token)
    share.last_accessed_at = _now()
    share.view_count += 1
    files = []
    for file in share.files:
        if file.status != "uploaded" or file.deleted_at is not None:
            continue
        item = BucketPublicShareFileRead.model_validate(file)
        if share.can_preview:
            item.preview_url = _download_url(file.s3_key, disposition="inline", content_type=file.content_type)
        if share.can_download:
            item.download_url = _download_url(file.s3_key, disposition="attachment")
        files.append(item)
    await _log(db, share.bucket_id, "public_share_accessed", request=request, actor_name=share.recipient_name, actor_role="public_share_recipient", target_type="public_share", target_id=str(share.id))
    await db.commit()
    return BucketPublicShareAccessRead(
        bucket=BucketRead.model_validate(share.bucket),
        share=_public_share_read(share),
        files=files,
    )


@router.post("/public-share/{token}/files/{file_id}/download", response_model=BucketFileUrl)
async def public_share_file_download(
    token: str,
    file_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUrl:
    share = await _load_public_share_or_404(db, token)
    file = _file_belongs_to_public_share(share, file_id)
    if file is None:
        await _log(db, share.bucket_id, "public_share_download_denied", request=request, actor_name=share.recipient_name, actor_role="public_share_recipient", target_type="file", target_id=str(file_id), detail="file not shared")
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if not share.can_download:
        await _log(db, share.bucket_id, "public_share_download_denied", request=request, actor_name=share.recipient_name, actor_role="public_share_recipient", target_type="file", target_id=str(file.id), detail=file.file_name)
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Download is disabled for this share")
    share.download_count += 1
    await _log(db, share.bucket_id, "public_share_download_requested", request=request, actor_name=share.recipient_name, actor_role="public_share_recipient", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return BucketFileUrl(url=_download_url(file.s3_key, disposition="attachment"), expires_in=900)


@router.get("/share/{token}/ai-summary", response_model=BucketAISummaryRead)
async def share_ai_summary(
    token: str,
    request: Request,
    passcode: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> BucketAISummaryRead:
    share = await _load_share_or_404(db, token)
    if not share.can_view_ai_summary:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI summary is disabled for this share")
    if not _verify_passcode(passcode, share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="share", target_id=str(share.id), detail="ai summary")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    return BucketAISummaryRead(summary=share_visible_summary(await latest_review(db, share.bucket_id), share))


@router.get("/share/{token}/ai-tasks", response_model=list[BucketAIActionItemRead])
async def share_ai_tasks(
    token: str,
    request: Request,
    passcode: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> list[BucketAIActionItem]:
    share = await _load_share_or_404(db, token)
    if not share.can_view_ai_tasks:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI tasks are disabled for this share")
    if not _verify_passcode(passcode, share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="share", target_id=str(share.id), detail="ai tasks")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    return await visible_action_items(db, share.bucket_id, route="share", share_id=share.id, approved_only=True)


@router.post("/share/{token}/ai-chat", response_model=BucketAIChatResponse)
async def share_ai_chat(
    token: str,
    payload: BucketAIChatMessageCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketAIChatResponse:
    share = await _load_share_or_404(db, token)
    if not share.can_use_ai_chat:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI chat is disabled for this share")
    if not _verify_passcode(payload.passcode or "", share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="share", target_id=str(share.id), detail="ai chat")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    messages, proposals, _ = await create_chat_reply(
        db,
        bucket=share.bucket,
        audience="shared_user",
        message=payload.message,
        actor_name=share.recipient_name,
        share=share,
    )
    if not share.can_propose_tasks:
        for item in proposals:
            await db.delete(item)
        proposals = []
    await db.commit()
    return BucketAIChatResponse(
        messages=[BucketAIMessageRead.model_validate(message) for message in messages],
        proposed_action_items=[BucketAIActionItemRead.model_validate(item) for item in proposals],
    )


@router.post("/share/{token}/files/{file_id}/review", response_model=BucketFileReviewRead)
async def shared_file_review(
    token: str,
    file_id: UUID,
    payload: BucketShareAccessRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileReviewRead:
    share = await _load_share_or_404(db, token)
    if not share.can_preview:
        await _log(db, share.bucket_id, "shared_file_review_denied", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file_id), detail="preview disabled")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Preview is disabled for this share")
    if not _verify_passcode(payload.passcode, share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file_id), detail="file review")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    file = _file_belongs_to_share(share, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    annotations = await _file_annotations(db, share.bucket_id, file_id)
    await _log(db, share.bucket_id, "shared_file_review_opened", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return _review_response(file, annotations, preview=share.can_preview)


@router.post("/share/{token}/files/{file_id}/download", response_model=BucketFileUrl)
async def shared_file_download(
    token: str,
    file_id: UUID,
    payload: BucketSharedDownloadCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUrl:
    share = await _load_share_or_404(db, token)
    if not _verify_passcode(payload.passcode, share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file_id), detail="download")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    file = _file_belongs_to_share(share, file_id)
    if file is None:
        await _log(db, share.bucket_id, "shared_file_download_denied", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file_id), detail="file not shared")
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if not share.can_download:
        await _log(db, share.bucket_id, "shared_file_download_denied", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file.id), detail=file.file_name)
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Download is disabled for this share")
    share.download_count += 1
    await _log(db, share.bucket_id, "shared_file_download_requested", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return BucketFileUrl(url=_download_url(file.s3_key, disposition="attachment"), expires_in=900)


@router.post("/share/{token}/files/{file_id}/annotations", response_model=BucketFileAnnotationRead)
async def create_shared_file_annotation(
    token: str,
    file_id: UUID,
    payload: BucketFileAnnotationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileAnnotation:
    share = await _load_share_or_404(db, token)
    if not share.can_preview:
        await _log(db, share.bucket_id, "shared_file_annotation_denied", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file_id), detail="preview disabled")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Preview is disabled for this share")
    if not share.can_add_notes:
        await _log(db, share.bucket_id, "shared_file_annotation_denied", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file_id), detail="notes disabled")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Notes are disabled for this share")
    if not _verify_passcode(payload.passcode or "", share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file_id), detail="annotation")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    file = _file_belongs_to_share(share, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    annotation = BucketFileAnnotation(
        bucket_id=share.bucket_id,
        file_id=file_id,
        share_id=share.id,
        page_number=payload.page_number,
        x=payload.x,
        y=payload.y,
        width=payload.width,
        height=payload.height,
        comment=payload.comment.strip(),
        author_name=share.recipient_name,
        author_role="shared_user",
    )
    db.add(annotation)
    await db.flush()
    await _log(db, share.bucket_id, "shared_file_annotation_created", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    await db.refresh(annotation)
    return annotation


@router.post("/share/{token}/notes", response_model=BucketNoteRead)
async def create_shared_note(
    token: str,
    payload: BucketSharedNoteCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketNote:
    share = await _load_share_or_404(db, token)
    if not share.can_add_notes:
        await _log(db, share.bucket_id, "shared_note_denied", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="note", target_id=str(share.id), detail="notes disabled")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Notes are disabled for this share")
    if not _verify_passcode(payload.passcode, share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="note", target_id=str(share.id), detail="shared note")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    note = BucketNote(
        bucket_id=share.bucket_id,
        share_id=share.id,
        author_name=share.recipient_name,
        author_role="shared_user",
        visibility="shared",
        content=payload.content,
    )
    db.add(note)
    await db.flush()
    await _log(db, share.bucket_id, "shared_note_created", request=request, actor_name=share.recipient_name, actor_email=share.recipient_email, actor_role="shared_user", target_type="note", target_id=str(note.id))
    await db.commit()
    await db.refresh(note)
    return note
