"""Bounded storage checks used before retained Bucket records are restored.

Restoration is a claim that the original evidence is usable again.  A database
row alone cannot support that claim, so these helpers verify the exact S3
object version before callers clear ``archived_at`` or ``deleted_at``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings

logger = logging.getLogger(__name__)

_HEAD_CONCURRENCY = 8
_CLIENT_SETUP_TIMEOUT_SECONDS = 8.0
_RESTORE_CHECK_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True)
class StoredBucketObject:
    file_id: UUID
    file_name: str
    s3_key: str
    version_id: str | None = None

    @classmethod
    def from_file(cls, file: Any) -> StoredBucketObject:
        return cls(
            file_id=file.id,
            file_name=file.file_name,
            s3_key=file.s3_key,
            version_id=getattr(file, "s3_version_id", None),
        )


class RetainedObjectMissingError(RuntimeError):
    def __init__(self, objects: Iterable[StoredBucketObject]):
        self.objects = tuple(objects)
        super().__init__("One or more retained files are missing from storage")


class RetainedObjectVerificationError(RuntimeError):
    pass


def _bounded_s3_client():
    cfg = get_settings()
    if not cfg.s3_bucket:
        raise RetainedObjectVerificationError("Bucket storage is not configured")
    kwargs: dict[str, Any] = {
        "region_name": cfg.aws_region,
        "config": Config(
            signature_version="s3v4",
            connect_timeout=4,
            read_timeout=6,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    }
    if cfg.aws_access_key_id and cfg.aws_secret_access_key:
        kwargs["aws_access_key_id"] = cfg.aws_access_key_id
        kwargs["aws_secret_access_key"] = cfg.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def _head_object(client: Any, bucket_name: str, ref: StoredBucketObject) -> str:
    params: dict[str, str] = {"Bucket": bucket_name, "Key": ref.s3_key}
    if ref.version_id:
        params["VersionId"] = ref.version_id
    try:
        client.head_object(**params)
    except ClientError as exc:
        response = exc.response or {}
        error = response.get("Error") or {}
        code = str(error.get("Code") or "")
        http_status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"} or http_status == 404:
            return "missing"
        logger.warning(
            "retained Bucket object verification failed file_id=%s code=%s status=%s",
            ref.file_id,
            code or "unknown",
            http_status,
        )
        return "unavailable"
    except Exception:  # noqa: BLE001 - storage SDK/network boundary
        logger.exception(
            "retained Bucket object verification failed file_id=%s",
            ref.file_id,
        )
        return "unavailable"
    return "available"


async def verify_bucket_file_objects(files: Iterable[Any]) -> None:
    """Verify exact stored versions without blocking the async event loop.

    Missing objects are a recoverable data conflict (HTTP 409 at the route).
    Provider/configuration failures are distinguished so callers can return an
    actionable 502 and leave the archived/deleted state untouched.
    """

    refs = tuple(StoredBucketObject.from_file(file) for file in files)
    if not refs:
        return
    if any(not ref.s3_key for ref in refs):
        raise RetainedObjectMissingError(ref for ref in refs if not ref.s3_key)

    cfg = get_settings()
    try:
        client = await asyncio.wait_for(
            asyncio.to_thread(_bounded_s3_client),
            timeout=_CLIENT_SETUP_TIMEOUT_SECONDS,
        )
    except RetainedObjectVerificationError:
        raise
    except TimeoutError as exc:
        raise RetainedObjectVerificationError(
            "Timed out connecting to retained file storage"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - SDK/configuration boundary
        logger.exception("could not create S3 client for retained file verification")
        raise RetainedObjectVerificationError(
            "Retained file storage could not be reached"
        ) from exc

    semaphore = asyncio.Semaphore(_HEAD_CONCURRENCY)

    async def check(ref: StoredBucketObject) -> tuple[StoredBucketObject, str]:
        async with semaphore:
            state = await asyncio.to_thread(
                _head_object,
                client,
                cfg.s3_bucket,
                ref,
            )
            return ref, state

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(check(ref) for ref in refs)),
            timeout=_RESTORE_CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise RetainedObjectVerificationError(
            "Timed out verifying retained file storage"
        ) from exc

    missing = [ref for ref, state in results if state == "missing"]
    if missing:
        raise RetainedObjectMissingError(missing)
    if any(state != "available" for _ref, state in results):
        raise RetainedObjectVerificationError(
            "Retained file storage could not be verified"
        )

