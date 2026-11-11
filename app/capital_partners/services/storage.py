"""Private S3 storage for offering documents.

A read-only copy of app/dealer_os/services/storage.py (the isolation
contract forbids importing across products), with the key prefix changed.
Offering documents are confidential: every presigned GET is short-lived (60 s
by default) and the caller logs the access in cp_document_access first.
"""

from __future__ import annotations

import logging
import re
import uuid
from uuid import UUID

import boto3
from botocore.config import Config

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Short on purpose: the link is opened immediately by the member's browser.
DOCUMENT_URL_TTL_SECONDS = 60
UPLOAD_URL_TTL_SECONDS = 300


def _s3_client():
    cfg = get_settings()
    kwargs = {"region_name": cfg.aws_region, "config": Config(signature_version="s3v4")}
    if cfg.aws_access_key_id and cfg.aws_secret_access_key:
        kwargs["aws_access_key_id"] = cfg.aws_access_key_id
        kwargs["aws_secret_access_key"] = cfg.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def configured() -> bool:
    return bool(get_settings().s3_bucket)


def put_pdf(key: str, content: bytes) -> bool:
    """Store a new immutable offering PDF; callers always allocate a new key."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return False
    params = {"Bucket": cfg.s3_bucket, "Key": key, "Body": content, "ContentType": "application/pdf"}
    params.update({"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": cfg.buckets_kms_key_id}
        if cfg.buckets_kms_key_id else {"ServerSideEncryption": "AES256"})
    try:
        _s3_client().put_object(**params)
        return True
    except Exception:
        logger.exception("capital: generated PDF storage failed")
        return False


def safe_filename(name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name or "").strip(" .")
    return cleaned[:180] or "document.pdf"


def build_key(listing_id: UUID, filename: str) -> str:
    return f"capital/{listing_id}/{uuid.uuid4()}-{safe_filename(filename)}"


# Only these may render inline; anything else is forced to a download served
# as application/octet-stream so an object can never run as active content.
_INLINE_SAFE_CONTENT_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "text/plain", "text/csv"}
)


def _sanitize_content_type(content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return "application/octet-stream"
    if any(token in ct for token in ("html", "svg", "xml", "javascript", "script", "xhtml")):
        return "application/octet-stream"
    return ct


def presign_get(
    key: str,
    *,
    ttl: int = DOCUMENT_URL_TTL_SECONDS,
    disposition: str = "inline",
    content_type: str | None = None,
    download_filename: str | None = None,
    version_id: str | None = None,
) -> str | None:
    """Presigned GET, or None when S3 is unconfigured or presigning fails."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return None
    params: dict = {"Bucket": cfg.s3_bucket, "Key": key}
    if version_id:
        params["VersionId"] = version_id
    if disposition == "inline" and _sanitize_content_type(content_type) not in _INLINE_SAFE_CONTENT_TYPES:
        disposition = "attachment"
        params["ResponseContentType"] = "application/octet-stream"
    if download_filename:
        safe = safe_filename(download_filename).replace('"', "")
        disposition = f'{disposition}; filename="{safe}"'
    params["ResponseContentDisposition"] = disposition
    try:
        return _s3_client().generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)
    except Exception:
        logger.exception("capital: presign failed for %s", key)
        return None


def presign_put(key: str, *, content_type: str, ttl: int = UPLOAD_URL_TTL_SECONDS) -> dict[str, object] | None:
    """An encrypted direct-upload contract for a private object."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return None
    params: dict[str, object] = {"Bucket": cfg.s3_bucket, "Key": key, "ContentType": content_type}
    headers: dict[str, str] = {"Content-Type": content_type}
    if cfg.buckets_kms_key_id:
        params["ServerSideEncryption"] = "aws:kms"
        params["SSEKMSKeyId"] = cfg.buckets_kms_key_id
        headers["x-amz-server-side-encryption"] = "aws:kms"
        headers["x-amz-server-side-encryption-aws-kms-key-id"] = cfg.buckets_kms_key_id
    else:
        params["ServerSideEncryption"] = "AES256"
        headers["x-amz-server-side-encryption"] = "AES256"
    try:
        url = _s3_client().generate_presigned_url("put_object", Params=params, ExpiresIn=ttl)
        return {"upload_url": url, "headers": headers, "s3_key": key}
    except Exception:
        logger.exception("capital: upload presign failed for %s", key)
        return None


def copy_object(source_key: str, key: str, *, content_type: str = "application/pdf") -> bool:
    """Copy a private object inside the bucket (the file's own PDF onto a
    listing), keeping the bucket's encryption posture. False when storage is
    unconfigured or the copy fails."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return False
    params: dict[str, object] = {
        "Bucket": cfg.s3_bucket,
        "CopySource": {"Bucket": cfg.s3_bucket, "Key": source_key},
        "Key": key,
        "ContentType": content_type,
        "MetadataDirective": "REPLACE",
    }
    if cfg.buckets_kms_key_id:
        params["ServerSideEncryption"] = "aws:kms"
        params["SSEKMSKeyId"] = cfg.buckets_kms_key_id
    else:
        params["ServerSideEncryption"] = "AES256"
    try:
        _s3_client().copy_object(**params)
        return True
    except Exception:
        logger.exception("capital: copy failed %s -> %s", source_key, key)
        return False


def head_object(key: str) -> dict | None:
    """Size and content type of an uploaded object, or None if it is not there."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return None
    try:
        resp = _s3_client().head_object(Bucket=cfg.s3_bucket, Key=key)
        return {"size_bytes": int(resp.get("ContentLength") or 0), "content_type": resp.get("ContentType")}
    except Exception:
        logger.info("capital: head_object missing or failed for %s", key)
        return None
