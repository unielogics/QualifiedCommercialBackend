"""The bureau's report page, kept.

A soft pull used to leave behind a score and a JSON parse of the report and
nothing else: the page itself lived only on iSoftPull's site, behind a login
that expires. The desk could not open what the decision was made on. The
HTML is archived under the firm's own storage prefix, encrypted with the
buckets key, outside every client room, and handed to the desk by short
links.
"""

from __future__ import annotations

import logging
from uuid import UUID

import boto3
from botocore.config import Config

from app.config import get_settings

log = logging.getLogger(__name__)

REPORT_LINK_SECONDS = 60


def _client():
    cfg = get_settings()
    kwargs: dict[str, object] = {"region_name": cfg.aws_region, "config": Config(signature_version="s3v4")}
    if cfg.aws_access_key_id and cfg.aws_secret_access_key:
        kwargs["aws_access_key_id"] = cfg.aws_access_key_id
        kwargs["aws_secret_access_key"] = cfg.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def report_key(pull_id: UUID | str) -> str:
    prefix = get_settings().buckets_s3_prefix.strip("/")
    return f"{prefix}/credit-reports/{pull_id}/report.html"


def archive_report_html(pull_id: UUID | str, html: str) -> str | None:
    """Store the report page; the key on success, None (logged) on failure."""
    cfg = get_settings()
    if not html or not cfg.s3_bucket or not cfg.buckets_kms_key_id:
        return None
    key = report_key(pull_id)
    try:
        _client().put_object(
            Bucket=cfg.s3_bucket,
            Key=key,
            Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=cfg.buckets_kms_key_id,
        )
    except Exception:  # noqa: BLE001 - the pull stands even if the archive does not
        log.exception("credit report archive failed for pull %s", pull_id)
        return None
    return key


def report_download_url(key: str) -> str:
    """A short-lived link to the archived page for an already-authorised desk user."""
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().s3_bucket, "Key": key, "ResponseContentType": "text/html; charset=utf-8"},
        ExpiresIn=REPORT_LINK_SECONDS,
    )
