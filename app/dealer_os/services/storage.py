"""Best-effort S3 archival for Dealer OS documents.

Reuses the SAME env settings the buckets router uses (s3_bucket / aws_region /
optional static creds / buckets_kms_key_id) without importing that router —
the client construction mirrors app/routers/buckets.py:_s3_client read-only.

Archival is best-effort by contract: if S3 is unconfigured or a call fails,
callers proceed with the in-memory bytes and record s3_key=NULL. Never raise
out of put_bytes.
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


def safe_filename(name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name or "").strip(" .")
    return cleaned[:180] or "upload.bin"


def build_key(dealer_id: UUID, filename: str) -> str:
    return f"dealer-os/{dealer_id}/{uuid.uuid4()}-{safe_filename(filename)}"


def put_bytes(key: str, raw: bytes, content_type: str) -> bool:
    """Archive bytes to S3. Returns True on success, False when S3 is
    unconfigured or the upload fails (callers then store s3_key=NULL)."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return False
    params: dict = {
        "Bucket": cfg.s3_bucket,
        "Key": key,
        "Body": raw,
        "ContentType": content_type or "application/octet-stream",
    }
    # Same SSE-KMS posture as the buckets storage when a key is configured.
    if cfg.buckets_kms_key_id:
        params["ServerSideEncryption"] = "aws:kms"
        params["SSEKMSKeyId"] = cfg.buckets_kms_key_id
    try:
        _s3_client().put_object(**params)
        return True
    except Exception:
        logger.exception("dealer-os: best-effort S3 archive failed for %s", key)
        return False


# Content types the browser may safely render inline. Anything else (notably
# text/html, image/svg+xml, xml, javascript) can execute script in the S3
# origin when previewed inline, so it is neutralized to a download.
# MIRRORS app/routers/buckets.py:_INLINE_SAFE_CONTENT_TYPES (read-only copy —
# keep the two lists in sync).
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


def _sanitize_content_type(content_type: str | None) -> str:
    """Coerce a stored/attacker-influenced content-type to a safe served type.
    MIRRORS app/routers/buckets.py:_sanitize_upload_content_type (read-only
    copy): executable/markup types become application/octet-stream so the
    object can never be served as active content."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return "application/octet-stream"
    if any(token in ct for token in ("html", "svg", "xml", "javascript", "script", "xhtml")):
        return "application/octet-stream"
    return ct


def presign_get(
    key: str,
    *,
    ttl: int = 900,
    disposition: str = "inline",
    content_type: str | None = None,
) -> str | None:
    """Presigned GET URL for an archived object — the preview/download bridge.

    Mirrors app/routers/buckets.py:_download_url: only known-inline-safe
    content types may render inline; anything else is forced to an
    attachment download served as application/octet-stream. Never raises —
    returns None when S3 is unconfigured or presigning fails (callers map
    that to a 503)."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return None
    params: dict = {"Bucket": cfg.s3_bucket, "Key": key}
    if (
        disposition == "inline"
        and content_type is not None
        and _sanitize_content_type(content_type) not in _INLINE_SAFE_CONTENT_TYPES
    ):
        disposition = "attachment"
        params["ResponseContentType"] = "application/octet-stream"
    params["ResponseContentDisposition"] = disposition
    try:
        return _s3_client().generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)
    except Exception:
        logger.exception("dealer-os: presign failed for %s", key)
        return None


def get_bytes(key: str) -> bytes | None:
    """Fetch archived bytes back for a re-extract. None if unavailable."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return None
    try:
        resp = _s3_client().get_object(Bucket=cfg.s3_bucket, Key=key)
        return resp["Body"].read()
    except Exception:
        logger.exception("dealer-os: S3 fetch failed for %s", key)
        return None


def presign_put(key: str, *, content_type: str, ttl: int = 300) -> dict[str, object] | None:
    """Create an encrypted direct-upload contract for a private S3 object."""
    cfg = get_settings()
    if not cfg.s3_bucket:
        return None
    params: dict[str, object] = {
        "Bucket": cfg.s3_bucket,
        "Key": key,
        "ContentType": content_type,
    }
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
        url = _s3_client().generate_presigned_url(
            "put_object", Params=params, ExpiresIn=ttl
        )
        return {"upload_url": url, "headers": headers, "s3_key": key}
    except Exception:
        logger.exception("dealer-os: upload presign failed for %s", key)
        return None
