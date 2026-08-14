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
