"""LenderThread attachment service.

Three insertion paths into `message_attachments`:

  1. POST /loans/{id}/lender-thread/attachment/upload-init
     → reserves an `outbound_upload` attachment row in status='staged',
       returns a presigned S3 PUT URL the browser uses to ship the
       file directly to S3.
  2. POST /loans/{id}/lender-thread/attachment/upload-complete
     → flips the row's status confirmation flag (kept as 'staged'
       until actually attached to a send).
  3. POST /loans/{id}/lender-thread/attachment/from-doc
     → creates a `system_doc_ref` row pointing at an existing
       Document.s3_key. No S3 copy — the row re-references.

When the operator hits "Send" on the reply composer the handler
calls `commit_attachments_to_message(...)` which sets message_id on
the staged rows and flips status='committed'. That's the moment
the attachments officially become part of the thread.

Inbound is handled separately by inbound_poller, which calls
`ingest_inbound_attachment(...)` directly with message_id set.
All attachments downloaded from Gmail land in S3 under
`lender-attachments/{loan_id}/{gmail_id}/{filename}`.

GETs go through `presign_download(...)` so we can rotate S3
credentials without breaking the UI — the browser never sees raw
s3_keys.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any
from uuid import UUID, uuid4

import boto3
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.document import Document
from app.models.loan import Loan
from app.models.message import Message
from app.models.message_attachment import MessageAttachment

log = logging.getLogger(__name__)

# Hard caps. Gmail's per-message limit is 25 MB INCLUDING base64
# expansion; we cap the raw file at 18 MB to leave headroom.
MAX_FILE_BYTES = 18 * 1024 * 1024
PRESIGN_PUT_TTL = 900  # 15 min
PRESIGN_GET_TTL = 3600  # 1 hour — operator may open in audit drawer


class AttachmentError(ValueError):
    """Caller-fixable problem. Routers map to HTTP 400."""


# ---------------------------------------------------------------------------
# Outbound — composer flow
# ---------------------------------------------------------------------------


def _s3_client():
    """Boto3 S3 client. Prefers explicit env keys when set, otherwise
    falls through to the default credential chain (EC2 instance role,
    ECS task role, ~/.aws/credentials, etc.) — production uses an
    instance role here so the explicit keys are blank by design."""
    s = get_settings()
    kwargs: dict[str, str] = {"region_name": s.aws_region}
    if s.aws_access_key_id and s.aws_secret_access_key:
        kwargs["aws_access_key_id"] = s.aws_access_key_id
        kwargs["aws_secret_access_key"] = s.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def _safe_filename(raw: str) -> str:
    """Strip path separators and control chars so a malicious filename
    can't break out of the prefix."""
    out = re.sub(r"[\\/\x00-\x1f]", "_", raw).strip()
    return out[:200] or "untitled"


async def init_outbound_upload(
    db: AsyncSession,
    *,
    loan_id: UUID,
    filename: str,
    mime_type: str,
    size_bytes: int,
    uploaded_by: UUID,
) -> dict[str, Any]:
    """Reserve a staged outbound attachment row and return the
    presigned PUT URL for the browser to ship the bytes directly to
    S3. Subsequent /upload-complete flips the row to a sendable
    state."""
    if size_bytes <= 0 or size_bytes > MAX_FILE_BYTES:
        raise AttachmentError(
            f"File size {size_bytes} bytes is outside the allowed range "
            f"(0, {MAX_FILE_BYTES}]."
        )
    loan = (await db.execute(select(Loan).where(Loan.id == loan_id))).scalar_one_or_none()
    if loan is None:
        raise AttachmentError("Loan not found")

    settings = get_settings()
    safe = _safe_filename(filename)
    s3_key = f"lender-attachments/{loan.deal_id}/{uuid4()}-{safe}"
    att = MessageAttachment(
        loan_id=loan.id,
        message_id=None,
        document_id=None,
        filename=safe,
        mime_type=mime_type or "application/octet-stream",
        size_bytes=size_bytes,
        s3_key=s3_key,
        source="outbound_upload",
        direction="outbound",
        status="staged",
        uploaded_by=uploaded_by,
    )
    db.add(att)
    await db.flush()

    upload_url: str | None = None
    if settings.s3_bucket:
        upload_url = _s3_client().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": s3_key,
                "ContentType": att.mime_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=PRESIGN_PUT_TTL,
        )

    return {
        "attachment_id": str(att.id),
        "upload_url": upload_url,
        "s3_key": s3_key,
        "filename": att.filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
    }


async def from_existing_document(
    db: AsyncSession,
    *,
    loan_id: UUID,
    document_id: UUID,
    uploaded_by: UUID,
) -> MessageAttachment:
    """Create an attachment that points at an existing Document on the
    loan vault. No S3 copy — the new row re-uses the Document's
    s3_key so we don't double-store the file."""
    doc = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if doc is None:
        raise AttachmentError("Document not found")
    if doc.loan_id != loan_id:
        raise AttachmentError("Document belongs to a different loan")
    if not doc.s3_key:
        raise AttachmentError(
            "Document has no s3_key yet — wait for the upload to complete."
        )
    att = MessageAttachment(
        loan_id=loan_id,
        message_id=None,
        document_id=doc.id,
        filename=doc.name or "document",
        mime_type="application/octet-stream",  # docs vault doesn't store mime; fine for Gmail
        size_bytes=0,  # unknown without a HEAD; gmail tolerates 0 here
        s3_key=doc.s3_key,
        source="system_doc_ref",
        direction="outbound",
        status="staged",
        uploaded_by=uploaded_by,
    )
    db.add(att)
    await db.flush()
    return att


async def commit_attachments_to_message(
    db: AsyncSession,
    *,
    loan_id: UUID,
    message_id: UUID,
    attachment_ids: list[UUID],
) -> list[MessageAttachment]:
    """Link a batch of staged attachments to a freshly-sent Message
    row. Skips any IDs that don't belong to this loan or that are
    already committed elsewhere — caller-side input validation has
    already happened."""
    if not attachment_ids:
        return []
    rows = (
        await db.execute(
            select(MessageAttachment).where(
                MessageAttachment.id.in_(attachment_ids),
                MessageAttachment.loan_id == loan_id,
            )
        )
    ).scalars().all()
    out: list[MessageAttachment] = []
    for r in rows:
        if r.status == "committed" and r.message_id is not None:
            # Already attached elsewhere — protect against double-link
            log.warning(
                "attachment %s already committed to message %s; skipping",
                r.id, r.message_id,
            )
            continue
        r.message_id = message_id
        r.status = "committed"
        out.append(r)
    await db.flush()
    return out


async def fetch_for_message(
    db: AsyncSession,
    *,
    message_id: UUID,
) -> list[MessageAttachment]:
    return (
        await db.execute(
            select(MessageAttachment)
            .where(MessageAttachment.message_id == message_id)
            .where(MessageAttachment.status == "committed")
            .order_by(MessageAttachment.created_at.asc())
        )
    ).scalars().all()


async def presign_download(attachment: MessageAttachment) -> str | None:
    """Return a short-lived signed GET URL the browser can hit to
    download the file. None if S3 isn't configured."""
    settings = get_settings()
    if not (settings.s3_bucket and attachment.s3_key):
        return None
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": attachment.s3_key},
        ExpiresIn=PRESIGN_GET_TTL,
    )


# ---------------------------------------------------------------------------
# Outbound — Gmail send helper
# ---------------------------------------------------------------------------


async def materialize_attachments_for_send(
    attachments: list[MessageAttachment],
) -> list[dict[str, Any]]:
    """Download attachment bytes from S3 and return a list of dicts the
    Gmail client's build_message can append as MIME parts.

    Returns a list of {filename, mime_type, data: bytes}. Skips any
    attachment whose s3 fetch fails so a single bad file doesn't
    block the rest of the send (logged)."""
    settings = get_settings()
    if not settings.s3_bucket:
        return []
    s3 = _s3_client()
    out: list[dict[str, Any]] = []
    for att in attachments:
        try:
            resp = s3.get_object(Bucket=settings.s3_bucket, Key=att.s3_key)
            data = resp["Body"].read()
            out.append(
                {
                    "filename": att.filename,
                    "mime_type": att.mime_type or "application/octet-stream",
                    "data": data,
                }
            )
        except Exception as exc:  # noqa: BLE001 — never crash the send
            log.warning(
                "attachment %s s3 fetch failed (skipping): %s",
                att.id, exc,
            )
    return out


# ---------------------------------------------------------------------------
# Inbound — Gmail poller flow
# ---------------------------------------------------------------------------


async def ingest_inbound_attachment(
    db: AsyncSession,
    *,
    loan_id: UUID,
    message_id: UUID,
    gmail_id: str,
    filename: str,
    mime_type: str,
    data: bytes,
) -> MessageAttachment | None:
    """Upload the bytes to S3 and create the attachment row in one go.
    Called by the inbound poller for each MIME attachment on an
    inbound lender email.

    Returns None if S3 isn't configured (we don't store the data
    anywhere else; the Message body's text content still lands so
    the operator at least has the conversation)."""
    settings = get_settings()
    if not settings.s3_bucket:
        log.warning(
            "ingest_inbound_attachment: S3 not configured; skipping %s",
            filename,
        )
        return None
    if not data:
        return None
    if len(data) > MAX_FILE_BYTES:
        log.warning(
            "ingest_inbound_attachment: %s exceeds %d bytes (got %d); "
            "skipping to keep the system honest",
            filename, MAX_FILE_BYTES, len(data),
        )
        return None

    loan = (await db.execute(select(Loan).where(Loan.id == loan_id))).scalar_one_or_none()
    if loan is None:
        return None
    safe = _safe_filename(filename)
    s3_key = f"lender-attachments/{loan.deal_id}/inbound/{gmail_id}/{uuid4()}-{safe}"
    try:
        _s3_client().put_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
            Body=data,
            ContentType=mime_type or "application/octet-stream",
            ServerSideEncryption="AES256",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("S3 put_object failed for %s: %s", s3_key, exc)
        return None

    att = MessageAttachment(
        loan_id=loan_id,
        message_id=message_id,
        document_id=None,
        filename=safe,
        mime_type=mime_type or "application/octet-stream",
        size_bytes=len(data),
        s3_key=s3_key,
        source="inbound_lender",
        direction="inbound",
        status="committed",
        uploaded_by=None,
    )
    db.add(att)
    await db.flush()
    return att


def decode_b64url(data: str) -> bytes:
    """Gmail attachment data field is URL-safe base64 without padding."""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)
