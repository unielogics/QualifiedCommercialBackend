"""Stream-zip a set of S3-hosted Documents into a single archive
back on S3.

Used by the Send-to-Lender flow when the operator picks the
`delivery="zip"` option — instead of dropping N download links in
the email body, we package the docs into one archive and put a
single 7-day presigned link in the email.

Cap is intentionally conservative (200 MB total) — Gmail's 25 MB
attachment limit isn't relevant since we're never attaching the
ZIP, but operator UX gets noticeably worse past ~200 MB and the
in-memory buffer makes it expensive in the Python process. If a
loan's package would exceed the cap the caller should fall back to
the links delivery mode.
"""

from __future__ import annotations

import io
import logging
import uuid
import zipfile
from dataclasses import dataclass

import boto3

from app.config import Settings, get_settings
from app.models.document import Document

log = logging.getLogger(__name__)

ZIP_MAX_BYTES = 200 * 1024 * 1024  # 200 MB
ZIP_PRESIGN_TTL_SECONDS = 7 * 86400  # 7 days


class DocumentZipError(RuntimeError):
    """Raised when packaging fails for a caller-fixable reason
    (size cap, missing object, etc.). Routers translate to 4xx."""


@dataclass
class ZipResult:
    s3_key: str
    download_url: str
    bytes_total: int
    files_packaged: int


def _s3():
    s = get_settings()
    return boto3.client(
        "s3",
        aws_access_key_id=s.aws_access_key_id or None,
        aws_secret_access_key=s.aws_secret_access_key or None,
        region_name=s.aws_region,
    )


def _safe_zip_name(doc: Document) -> str:
    """Strip path separators and any leading dots so the archive lays
    out flat. Falls back to the doc's UUID prefix when name is empty.
    """
    raw = (doc.name or f"doc-{str(doc.id)[:8]}").strip()
    cleaned = raw.replace("/", "_").replace("\\", "_")
    if cleaned.startswith("."):
        cleaned = "_" + cleaned[1:]
    return cleaned or f"doc-{str(doc.id)[:8]}"


def package_documents(
    *, deal_id: str, documents: list[Document]
) -> ZipResult:
    """Read each Document's S3 object, write into one in-memory ZIP,
    upload the archive to s3://{bucket}/loans/{deal_id}/lender-packages/{uuid}.zip,
    and return a presigned GET url valid for 7 days.

    Raises `DocumentZipError` when the running total exceeds
    `ZIP_MAX_BYTES` or when any source object can't be fetched."""
    settings: Settings = get_settings()
    if not settings.s3_bucket:
        raise DocumentZipError("S3 bucket not configured.")
    if not documents:
        raise DocumentZipError("No documents to package.")

    s3 = _s3()
    buf = io.BytesIO()
    bytes_total = 0
    seen_names: set[str] = set()

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for doc in documents:
            if not doc.s3_key:
                # Skip docs without an S3 object (request-only stubs)
                continue
            try:
                obj = s3.get_object(Bucket=settings.s3_bucket, Key=doc.s3_key)
            except Exception as exc:  # noqa: BLE001
                raise DocumentZipError(
                    f"Couldn't fetch document '{doc.name}' from S3 ({exc})"
                ) from exc
            body = obj["Body"].read()
            bytes_total += len(body)
            if bytes_total > ZIP_MAX_BYTES:
                raise DocumentZipError(
                    f"Package exceeded {ZIP_MAX_BYTES // (1024 * 1024)} MB. "
                    "Send fewer documents or use the Links delivery mode."
                )
            # Disambiguate same-name files inside the archive.
            base = _safe_zip_name(doc)
            name = base
            counter = 2
            while name in seen_names:
                stem, _, ext = base.rpartition(".")
                name = f"{stem} ({counter}).{ext}" if stem else f"{base} ({counter})"
                counter += 1
            seen_names.add(name)
            zf.writestr(name, body)

    buf.seek(0)
    archive_key = f"loans/{deal_id}/lender-packages/{uuid.uuid4().hex}.zip"
    try:
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=archive_key,
            Body=buf.getvalue(),
            ContentType="application/zip",
        )
    except Exception as exc:  # noqa: BLE001
        raise DocumentZipError(f"Couldn't upload package to S3 ({exc})") from exc

    download_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": archive_key},
        ExpiresIn=ZIP_PRESIGN_TTL_SECONDS,
    )
    log.info(
        "lender_zip: deal=%s files=%d bytes=%d key=%s",
        deal_id, len(seen_names), bytes_total, archive_key,
    )
    return ZipResult(
        s3_key=archive_key,
        download_url=download_url,
        bytes_total=bytes_total,
        files_packaged=len(seen_names),
    )
