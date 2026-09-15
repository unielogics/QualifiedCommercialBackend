"""Validation shared by every user-facing PDF upload path.

Browsers upload directly to S3 on most surfaces, so the completion request is
the first trusted point where the API can inspect the actual bytes.  A PDF is
considered locked only when pypdf cannot open it with the empty user password.
This deliberately permits the common bank-statement PDFs that use encryption
only to restrict editing or copying but open normally for the recipient.
"""

from __future__ import annotations

import logging
import zipfile
from io import BytesIO

import boto3
from botocore.config import Config
from pypdf import PdfReader
from pypdf.errors import WrongPasswordError
from starlette.concurrency import run_in_threadpool

from app.config import get_settings

log = logging.getLogger(__name__)

PASSWORD_PROTECTED_PDF_CODE = "password_protected_pdf"
PASSWORD_PROTECTED_PDF_MESSAGE = (
    "This PDF is password-protected. Remove the password and upload an unlocked copy."
)
UPLOAD_INSPECTION_UNAVAILABLE_CODE = "upload_inspection_unavailable"
UPLOAD_INSPECTION_UNAVAILABLE_MESSAGE = (
    "We could not verify this upload. Please try uploading the file again."
)
UPLOAD_TOO_LARGE_CODE = "upload_too_large"
UPLOAD_TOO_LARGE_MESSAGE = "This file is too large. Choose a smaller file and upload it again."
UPLOAD_SIZE_MISMATCH_CODE = "upload_size_mismatch"
UPLOAD_SIZE_MISMATCH_MESSAGE = (
    "The uploaded file did not match the selected file. Please upload it again."
)
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 60
MAX_ARCHIVE_PDF_BYTES = 40 * 1024 * 1024
MAX_ARCHIVE_PDF_TOTAL_BYTES = 80 * 1024 * 1024


class PasswordProtectedPDF(ValueError):
    """The supplied bytes are a PDF that requires a non-empty open password."""


class UploadInspectionUnavailable(RuntimeError):
    """The just-uploaded object could not be retrieved for validation."""


class UploadTooLarge(ValueError):
    """The actual S3 object exceeds the server-side completion cap."""


class UploadSizeMismatch(ValueError):
    """The actual object length differs from the browser-declared size."""


def pdf_requires_password(reader: PdfReader) -> bool:
    """Return true only for encryption that blocks opening without a password.

    ``PdfReader.is_encrypted`` is also true for permissions-only encryption.
    Those files open with an empty user password and are valid evidence, so an
    encryption-bit check alone would incorrectly reject many bank PDFs.
    """

    if not reader.is_encrypted:
        return False
    try:
        return not bool(reader.decrypt(""))
    except WrongPasswordError:
        # Future pypdf versions may raise instead of returning NOT_DECRYPTED.
        # This is the one exception that unambiguously means the empty open
        # password failed.
        return True
    except Exception:  # noqa: BLE001 - malformed/unsupported encryption is not a lock verdict
        # A parser or crypto-backend failure is not proof that the user must
        # supply a password. Leave it to the ordinary readability pipeline so
        # we never mislabel a damaged PDF as password-protected.
        return False


def looks_like_pdf(raw: bytes) -> bool:
    """Recognize a PDF header within the first 1 KiB allowed by the PDF spec."""

    return b"%PDF-" in raw[:1024]


def _looks_like_zip(raw: bytes) -> bool:
    return raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))


def validate_pdf_bytes(
    raw: bytes,
    *,
    file_name: str | None = None,
    content_type: str | None = None,
) -> None:
    """Reject a true open-password PDF; leave other/malformed files to type QA.

    Filename and content type are hints only.  Actual PDF magic wins, which
    prevents a renamed ``.bin`` upload from bypassing the lock check.
    """

    hinted_pdf = (file_name or "").casefold().endswith(".pdf") or (
        "application/pdf" in (content_type or "").casefold()
    )
    if not hinted_pdf and not looks_like_pdf(raw):
        return
    try:
        reader = PdfReader(BytesIO(raw), strict=False)
    except Exception:  # noqa: BLE001 - malformed PDFs are handled downstream
        # Reader construction failures do not establish an open-password gate.
        # In particular, never infer one from exception text containing words
        # such as "encrypt"; malformed PDFs continue to normal readability QA.
        return
    if pdf_requires_password(reader):
        raise PasswordProtectedPDF(PASSWORD_PROTECTED_PDF_MESSAGE)


def _validate_archive_pdf_entries(raw: bytes) -> None:
    """Inspect ordinary PDF members without expanding an unbounded archive."""

    try:
        archive = zipfile.ZipFile(BytesIO(raw))
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        return
    with archive:
        # Evidence ZIP handlers enforce their own tighter limits. This pass is
        # solely immediate lock feedback and never extracts more than a modest
        # bounded set into memory.
        extracted_bytes = 0
        for member in archive.infolist()[:MAX_ARCHIVE_ENTRIES]:
            if member.is_dir() or member.flag_bits & 0x1:
                continue
            # Match the broadest downstream ZIP intake. Anything larger is
            # skipped there and can never become an accepted child document.
            if member.file_size > MAX_ARCHIVE_PDF_BYTES:
                continue
            if extracted_bytes + member.file_size > MAX_ARCHIVE_PDF_TOTAL_BYTES:
                continue
            member_name = member.filename
            if not member_name.casefold().endswith(".pdf"):
                continue
            try:
                member_raw = archive.read(member)
            except Exception:  # noqa: BLE001 - archive QA reports this downstream
                continue
            extracted_bytes += len(member_raw)
            validate_pdf_bytes(
                member_raw,
                file_name=member_name,
                content_type="application/pdf",
            )


def validate_upload_bytes(
    raw: bytes,
    *,
    file_name: str | None = None,
    content_type: str | None = None,
) -> None:
    """Validate a direct PDF or PDFs contained in an ordinary ZIP upload."""

    validate_pdf_bytes(raw, file_name=file_name, content_type=content_type)
    normalized_name = (file_name or "").casefold()
    normalized_type = (content_type or "").casefold()
    if normalized_name.endswith(".zip") or "zip" in normalized_type or _looks_like_zip(raw):
        _validate_archive_pdf_entries(raw)


def _s3_client():
    cfg = get_settings()
    kwargs: dict = {
        "region_name": cfg.aws_region,
        "config": Config(signature_version="s3v4"),
    }
    if cfg.aws_access_key_id and cfg.aws_secret_access_key:
        kwargs["aws_access_key_id"] = cfg.aws_access_key_id
        kwargs["aws_secret_access_key"] = cfg.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def _read_body(response: dict, *, max_bytes: int) -> bytes:
    body = response["Body"]
    try:
        raw = body.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise UploadTooLarge(UPLOAD_TOO_LARGE_MESSAGE)
        return raw
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


def _validate_s3_pdf_upload_sync(
    *,
    s3_key: str,
    file_name: str | None = None,
    content_type: str | None = None,
    expected_size_bytes: int | None = None,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> None:
    """Synchronous implementation; the public API offloads it from the loop."""

    cfg = get_settings()
    if not cfg.s3_bucket:
        raise UploadInspectionUnavailable(UPLOAD_INSPECTION_UNAVAILABLE_MESSAGE)
    client = _s3_client()
    try:
        head = client.head_object(Bucket=cfg.s3_bucket, Key=s3_key)
        actual_size = int(head["ContentLength"])
    except Exception as exc:  # noqa: BLE001 - callers map to a retryable 503
        log.warning("upload validation could not inspect S3 object key=%s: %s", s3_key, exc)
        raise UploadInspectionUnavailable(UPLOAD_INSPECTION_UNAVAILABLE_MESSAGE) from exc
    if actual_size > max_bytes:
        raise UploadTooLarge(UPLOAD_TOO_LARGE_MESSAGE)
    if expected_size_bytes is not None and expected_size_bytes > 0 and actual_size != expected_size_bytes:
        raise UploadSizeMismatch(UPLOAD_SIZE_MISMATCH_MESSAGE)

    normalized_name = (file_name or "").casefold()
    normalized_type = (content_type or "").casefold()
    hinted_pdf = normalized_name.endswith(".pdf") or "application/pdf" in normalized_type
    hinted_zip = normalized_name.endswith(".zip") or "zip" in normalized_type
    try:
        if hinted_pdf or hinted_zip:
            response = client.get_object(Bucket=cfg.s3_bucket, Key=s3_key)
            raw = _read_body(response, max_bytes=max_bytes)
        else:
            response = client.get_object(
                Bucket=cfg.s3_bucket,
                Key=s3_key,
                Range="bytes=0-1023",
            )
            prefix = _read_body(response, max_bytes=1024)
            actual_content_type = str(response.get("ContentType") or "")
            actual_pdf = "application/pdf" in actual_content_type.casefold()
            actual_zip = "zip" in actual_content_type.casefold()
            if not (
                looks_like_pdf(prefix)
                or _looks_like_zip(prefix)
                or actual_pdf
                or actual_zip
            ):
                return
            response = client.get_object(Bucket=cfg.s3_bucket, Key=s3_key)
            raw = _read_body(response, max_bytes=max_bytes)
    except (PasswordProtectedPDF, UploadTooLarge, UploadSizeMismatch):
        raise
    except Exception as exc:  # noqa: BLE001 - callers map to a retryable 503
        log.warning("upload validation could not read S3 object key=%s: %s", s3_key, exc)
        raise UploadInspectionUnavailable(UPLOAD_INSPECTION_UNAVAILABLE_MESSAGE) from exc
    if len(raw) != actual_size:
        raise UploadSizeMismatch(UPLOAD_SIZE_MISMATCH_MESSAGE)
    validate_upload_bytes(raw, file_name=file_name, content_type=content_type)


async def validate_s3_pdf_upload(
    *,
    s3_key: str,
    file_name: str | None = None,
    content_type: str | None = None,
    expected_size_bytes: int | None = None,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> None:
    """Inspect the actual S3 object written by a presigned browser upload.

    Known PDFs are downloaded once. For an upload with opaque metadata, a
    one-KiB range probe detects renamed PDFs before downloading the full file.
    Any storage failure keeps the database row in its pre-completion state so a
    transient S3 outage can never turn an uninspected file into accepted data.
    """

    await run_in_threadpool(
        _validate_s3_pdf_upload_sync,
        s3_key=s3_key,
        file_name=file_name,
        content_type=content_type,
        expected_size_bytes=expected_size_bytes,
        max_bytes=max_bytes,
    )


def _discard_s3_upload_sync(s3_key: str | None) -> bool:
    """Best-effort cleanup of rejected bytes; rejected database state is primary."""

    if not s3_key:
        return True
    cfg = get_settings()
    if not cfg.s3_bucket:
        return False
    try:
        _s3_client().delete_object(Bucket=cfg.s3_bucket, Key=s3_key)
        return True
    except Exception:  # noqa: BLE001 - the row remains rejected even if cleanup retries later
        log.exception("could not delete rejected upload key=%s", s3_key)
        return False


async def discard_s3_upload(s3_key: str | None) -> bool:
    """Delete rejected upload bytes without blocking the request event loop."""

    return await run_in_threadpool(_discard_s3_upload_sync, s3_key)
