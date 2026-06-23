from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.bucket import (
    Bucket,
    BucketActivityLog,
    BucketDocumentTemplate,
    BucketFile,
    BucketFileAnnotation,
    BucketNote,
    BucketRequestedDocument,
    BucketShare,
    BucketUploadLink,
)
from app.models.user import User
from app.schemas.bucket import (
    BucketActivityRead,
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
    BucketRead,
    BucketRequestAccessRead,
    BucketRequestAccessRequest,
    BucketRequestBucketRead,
    BucketRequestInfoRead,
    BucketRequestedDocumentCreate,
    BucketRequestedDocumentRead,
    BucketShareAccessRead,
    BucketShareAccessRequest,
    BucketShareCreate,
    BucketShareFileRead,
    BucketShareInfoRead,
    BucketSharePatch,
    BucketShareRead,
    BucketSharedNoteCreate,
    BucketTemplateRead,
    BucketUploadComplete,
    BucketUploadLinkCreate,
    BucketUploadLinkRead,
)

router = APIRouter(prefix="/buckets", tags=["buckets"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public_url(path: str) -> str:
    settings = get_settings()
    base = getattr(settings, "frontend_app_url", "https://app.qualifiedcommercial.com").rstrip("/")
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


def _verify_passcode(passcode: str, passcode_hash: str | None) -> bool:
    if not passcode_hash:
        return False
    try:
        scheme, raw_iterations, salt, expected = passcode_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return hmac.compare_digest(digest, expected)


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


async def _log(
    db: AsyncSession,
    bucket_id: UUID,
    action: str,
    *,
    actor_name: str | None = None,
    actor_role: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: str | None = None,
) -> None:
    db.add(
        BucketActivityLog(
            id=uuid4(),
            bucket_id=bucket_id,
            actor_name=actor_name,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
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
                selectinload(Bucket.shares).selectinload(BucketShare.files),
                selectinload(Bucket.notes),
                selectinload(Bucket.activity),
            )
        )
    ).scalar_one_or_none()
    if bucket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket not found")
    return bucket


async def _load_upload_link_or_404(db: AsyncSession, token: str) -> BucketUploadLink:
    link = (
        await db.execute(
            select(BucketUploadLink)
            .where(BucketUploadLink.token == token)
            .options(selectinload(BucketUploadLink.bucket).selectinload(Bucket.requested_documents))
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
            )
        )
    ).scalar_one_or_none()
    if share is None or not _is_active(share.status, share.expires_at):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share link not found or inactive")
    return share


def _upload_url(s3_key: str, content_type: str) -> tuple[str, dict[str, str]]:
    bucket, _, kms_key_id = _bucket_storage_config()
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


def _download_url(s3_key: str, *, disposition: str = "inline", ttl: int = 900) -> str:
    bucket, _, _ = _bucket_storage_config()
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key, "ResponseContentDisposition": disposition},
        ExpiresIn=ttl,
    )


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
        if file.id == file_id and file.status == "uploaded":
            return file
    return None


def _review_response(file: BucketFile, annotations: list[BucketFileAnnotation], *, preview: bool = True) -> BucketFileReviewRead:
    return BucketFileReviewRead(
        file=BucketFileRead.model_validate(file),
        preview_url=_download_url(file.s3_key, disposition="inline") if preview else None,
        annotations=[BucketFileAnnotationRead.model_validate(annotation) for annotation in annotations],
    )


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
            .options(selectinload(Bucket.files))
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
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> Bucket:
    bucket = Bucket(**payload.model_dump(), created_by_id=user.id)
    db.add(bucket)
    await db.flush()
    await _log(db, bucket.id, "bucket_created", actor_name=user.name, actor_role=user.role, detail=bucket.name)
    await db.commit()
    await db.refresh(bucket)
    return bucket


@router.get("/admin/{bucket_id}", response_model=BucketDetail)
async def get_bucket(
    bucket_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> Bucket:
    return await _load_bucket_or_404(db, bucket_id)


@router.delete("/admin/{bucket_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bucket(
    bucket_id: UUID,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    bucket = await _load_bucket_or_404(db, bucket_id)
    bucket.archived_at = _now()
    bucket.status = "archived"
    await _log(db, bucket_id, "bucket_deleted", actor_name=user.name, actor_role=user.role, detail=bucket.name)
    await db.commit()


@router.post("/admin/{bucket_id}/requested-documents", response_model=BucketRequestedDocumentRead)
async def add_requested_document(
    bucket_id: UUID,
    payload: BucketRequestedDocumentCreate,
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
            )
            db.add(existing)
            await db.flush()
        template_id = existing.id
    doc = BucketRequestedDocument(
        bucket_id=bucket_id,
        template_id=template_id,
        name=payload.name,
        category=payload.category,
        description=payload.description,
        required=payload.required,
        is_custom=payload.is_custom,
    )
    db.add(doc)
    await db.flush()
    await _log(db, bucket_id, "requested_document_added", actor_name=user.name, actor_role=user.role, target_type="requested_document", target_id=str(doc.id), detail=doc.name)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post("/admin/{bucket_id}/upload-links", response_model=BucketUploadLinkRead)
async def create_upload_link(
    bucket_id: UUID,
    payload: BucketUploadLinkCreate,
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
        passcode_hash=_hash_passcode(passcode),
    )
    db.add(link)
    await db.flush()
    await _log(db, bucket_id, "upload_link_created", actor_name=user.name, actor_role=user.role, target_type="upload_link", target_id=str(link.id), detail=link.recipient_name)
    await db.commit()
    await db.refresh(link)
    data = BucketUploadLinkRead.model_validate(link)
    data.upload_url = _public_url(f"/buckets/request/{link.token}")
    data.passcode = passcode
    return data


@router.post("/admin/{bucket_id}/shares", response_model=BucketShareRead)
async def create_share(
    bucket_id: UUID,
    payload: BucketShareCreate,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketShareRead:
    await _load_bucket_or_404(db, bucket_id)
    file_ids = list(dict.fromkeys(payload.file_ids))
    files = (
        await db.execute(
            select(BucketFile).where(
                BucketFile.bucket_id == bucket_id,
                BucketFile.id.in_(file_ids),
                BucketFile.status == "uploaded",
            )
        )
    ).scalars().all()
    if len(files) != len(file_ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected files must be uploaded files in this bucket")
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
        expires_at=payload.expires_at,
    )
    share.files = files
    db.add(share)
    await db.flush()
    await _log(db, bucket_id, "share_created", actor_name=user.name, actor_role=user.role, target_type="share", target_id=str(share.id), detail=share.recipient_name)
    await db.commit()
    await db.refresh(share)
    data = BucketShareRead.model_validate(share)
    data.share_url = _public_url(f"/buckets/share/{share.token}")
    data.passcode = passcode
    return data


@router.patch("/admin/{bucket_id}/shares/{share_id}", response_model=BucketShareRead)
async def patch_share(
    bucket_id: UUID,
    share_id: UUID,
    payload: BucketSharePatch,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketShare:
    share = await db.get(BucketShare, share_id)
    if share is None or share.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    for field in payload.model_fields_set:
        setattr(share, field, getattr(payload, field))
    await _log(db, bucket_id, "share_updated", actor_name=user.name, actor_role=user.role, target_type="share", target_id=str(share.id))
    await db.commit()
    await db.refresh(share)
    return share


@router.post("/admin/{bucket_id}/notes", response_model=BucketNoteRead)
async def create_admin_note(
    bucket_id: UUID,
    payload: BucketNoteCreate,
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
    await _log(db, bucket_id, "note_created", actor_name=user.name, actor_role=user.role, target_type="note", target_id=str(note.id), detail=note.visibility)
    await db.commit()
    await db.refresh(note)
    return note


@router.get("/admin/{bucket_id}/files/{file_id}/url", response_model=BucketFileUrl)
async def admin_file_url(
    bucket_id: UUID,
    file_id: UUID,
    download: bool = False,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileUrl:
    file = await db.get(BucketFile, file_id)
    if file is None or file.bucket_id != bucket_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    disposition = "attachment" if download else "inline"
    await _log(db, bucket_id, "file_download_url_created" if download else "file_preview_url_created", actor_name=user.name, actor_role=user.role, target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return BucketFileUrl(url=_download_url(file.s3_key, disposition=disposition), expires_in=900)


@router.get("/admin/{bucket_id}/files/{file_id}/review", response_model=BucketFileReviewRead)
async def admin_file_review(
    bucket_id: UUID,
    file_id: UUID,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileReviewRead:
    file = await db.get(BucketFile, file_id)
    if file is None or file.bucket_id != bucket_id or file.status != "uploaded":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    annotations = await _file_annotations(db, bucket_id, file_id)
    await _log(db, bucket_id, "file_review_opened", actor_name=user.name, actor_role=user.role, target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return _review_response(file, annotations)


@router.post("/admin/{bucket_id}/files/{file_id}/annotations", response_model=BucketFileAnnotationRead)
async def create_admin_file_annotation(
    bucket_id: UUID,
    file_id: UUID,
    payload: BucketFileAnnotationCreate,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> BucketFileAnnotation:
    file = await db.get(BucketFile, file_id)
    if file is None or file.bucket_id != bucket_id or file.status != "uploaded":
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
    await _log(db, bucket_id, "file_annotation_created", actor_name=user.name, actor_role=user.role, target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    await db.refresh(annotation)
    return annotation


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
    db: AsyncSession = Depends(get_db),
) -> BucketRequestAccessRead:
    link = await _load_upload_link_or_404(db, token)
    _require_upload_passcode(link)
    if not _verify_passcode(payload.passcode, link.passcode_hash):
        await _log(db, link.bucket_id, "upload_passcode_failed", actor_name=link.recipient_name, actor_role="uploader", target_type="upload_link", target_id=str(link.id))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid access code")
    await _log(db, link.bucket_id, "upload_link_accessed", actor_name=link.recipient_name, actor_role="uploader", target_type="upload_link", target_id=str(link.id))
    await db.commit()
    return BucketRequestAccessRead(
        bucket=BucketRequestBucketRead(name=link.bucket.name, client_name=link.bucket.client_name, purpose=link.bucket.purpose),
        recipient_name=link.recipient_name,
        recipient_email=link.recipient_email,
        allow_notes=link.allow_notes,
        requested_documents=[BucketRequestedDocumentRead.model_validate(d) for d in link.bucket.requested_documents],
    )


@router.post("/request/{token}/upload-init", response_model=BucketFileUploadInitResponse)
async def request_upload_init(
    token: str,
    payload: BucketFileUploadInit,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    link = await _load_upload_link_or_404(db, token)
    _require_upload_passcode(link)
    if not _verify_passcode(payload.passcode or "", link.passcode_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid access code")
    if payload.requested_document_id:
        req = await db.get(BucketRequestedDocument, payload.requested_document_id)
        if req is None or req.bucket_id != link.bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Requested document does not belong to this bucket")
    _, prefix, _ = _bucket_storage_config()
    safe = _safe_filename(payload.file_name)
    existing_conditions = [
        BucketFile.bucket_id == link.bucket_id,
        BucketFile.upload_link_id == link.id,
        BucketFile.file_name == payload.file_name,
        BucketFile.size_bytes == payload.size_bytes,
        BucketFile.status.in_(("uploading", "uploaded")),
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
    file_id = uuid4()
    s3_key = f"{prefix}/uploads/{link.bucket_id}/{file_id}-{safe}"
    file = BucketFile(
        id=file_id,
        bucket_id=link.bucket_id,
        requested_document_id=payload.requested_document_id,
        upload_link_id=link.id,
        file_name=payload.file_name,
        s3_key=s3_key,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        uploaded_by_name=payload.uploader_name,
        uploaded_by_email=str(payload.uploader_email) if payload.uploader_email else None,
        status="uploading",
    )
    db.add(file)
    await _log(db, link.bucket_id, "file_upload_started", actor_name=payload.uploader_name, actor_role="uploader", target_type="file", target_id=str(file.id), detail=payload.file_name)
    await db.commit()
    upload_url, headers = _upload_url(s3_key, payload.content_type)
    return BucketFileUploadInitResponse(file_id=file.id, upload_url=upload_url, s3_key=s3_key, required_headers=headers)


@router.post("/request/{token}/complete", response_model=BucketFileRead)
async def request_upload_complete(
    token: str,
    payload: BucketUploadComplete,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    link = await _load_upload_link_or_404(db, token)
    file = await db.get(BucketFile, payload.file_id)
    if file is None or file.bucket_id != link.bucket_id or file.upload_link_id != link.id:
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
    await _log(db, link.bucket_id, "file_uploaded", actor_name=file.uploaded_by_name or link.recipient_name, actor_role="uploader", target_type="file", target_id=str(file.id), detail=file.file_name)
    try:
        from app.services.notifications import notify_bucket_file_uploaded

        await notify_bucket_file_uploaded(db, bucket=link.bucket, file=file)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("bucket upload notification failed bucket=%s file=%s", link.bucket_id, file.id)
    await db.commit()
    await db.refresh(file)
    return file


@router.post("/share/{token}/access", response_model=BucketShareAccessRead)
async def share_access(
    token: str,
    payload: BucketShareAccessRequest,
    db: AsyncSession = Depends(get_db),
) -> BucketShareAccessRead:
    share = await _load_share_or_404(db, token)
    if not _verify_passcode(payload.passcode, share.passcode_hash):
        await _log(db, share.bucket_id, "share_passcode_failed", actor_name=share.recipient_name, actor_role="shared_user", target_type="share", target_id=str(share.id))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    share.last_accessed_at = _now()
    share.view_count += 1
    files = []
    for file in share.files:
        item = BucketShareFileRead.model_validate(file)
        if share.can_preview:
            item.preview_url = _download_url(file.s3_key, disposition="inline")
        if share.can_download:
            item.download_url = _download_url(file.s3_key, disposition="attachment")
        files.append(item)
    notes = [n for n in share.bucket.notes if n.visibility == "shared" or share.can_see_internal_notes]
    await _log(db, share.bucket_id, "share_accessed", actor_name=share.recipient_name, actor_role="shared_user", target_type="share", target_id=str(share.id))
    await db.commit()
    share_out = BucketShareRead.model_validate(share)
    share_out.share_url = _public_url(f"/buckets/share/{share.token}")
    return BucketShareAccessRead(
        bucket=BucketRead.model_validate(share.bucket),
        share=share_out,
        files=files,
        notes=[BucketNoteRead.model_validate(n) for n in notes],
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
    )


@router.post("/share/{token}/files/{file_id}/review", response_model=BucketFileReviewRead)
async def shared_file_review(
    token: str,
    file_id: UUID,
    payload: BucketShareAccessRequest,
    db: AsyncSession = Depends(get_db),
) -> BucketFileReviewRead:
    share = await _load_share_or_404(db, token)
    if not share.can_preview:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Preview is disabled for this share")
    if not _verify_passcode(payload.passcode, share.passcode_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid passcode")
    file = _file_belongs_to_share(share, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    annotations = await _file_annotations(db, share.bucket_id, file_id)
    await _log(db, share.bucket_id, "shared_file_review_opened", actor_name=share.recipient_name, actor_role="shared_user", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    return _review_response(file, annotations, preview=share.can_preview)


@router.post("/share/{token}/files/{file_id}/annotations", response_model=BucketFileAnnotationRead)
async def create_shared_file_annotation(
    token: str,
    file_id: UUID,
    payload: BucketFileAnnotationCreate,
    db: AsyncSession = Depends(get_db),
) -> BucketFileAnnotation:
    share = await _load_share_or_404(db, token)
    if not share.can_preview:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Preview is disabled for this share")
    if not share.can_add_notes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Notes are disabled for this share")
    if not _verify_passcode(payload.passcode or "", share.passcode_hash):
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
    await _log(db, share.bucket_id, "shared_file_annotation_created", actor_name=share.recipient_name, actor_role="shared_user", target_type="file", target_id=str(file.id), detail=file.file_name)
    await db.commit()
    await db.refresh(annotation)
    return annotation


@router.post("/share/{token}/notes", response_model=BucketNoteRead)
async def create_shared_note(
    token: str,
    payload: BucketSharedNoteCreate,
    db: AsyncSession = Depends(get_db),
) -> BucketNote:
    share = await _load_share_or_404(db, token)
    if not share.can_add_notes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Notes are disabled for this share")
    if not _verify_passcode(payload.passcode, share.passcode_hash):
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
    await _log(db, share.bucket_id, "shared_note_created", actor_name=share.recipient_name, actor_role="shared_user", target_type="note", target_id=str(note.id))
    await db.commit()
    await db.refresh(note)
    return note
