"""Pasting a picture into a note or an internal message.

The upload is a presigned PUT, the same shape lender attachments already use:
the browser ships bytes straight to S3 and they never pass through the API. What
this module owns is everything around that —

  start_upload   validates the type and size, reserves a `staged` row, hands
                 back the PUT URL.
  mark_ready     the browser says the bytes landed.
  attach         binds ready rows to the note or message that now exists.
                 Only the author's own rows, and only ones not already bound,
                 so an image id guessed from somewhere else cannot be pulled
                 into a different file.
  hydrate        what a read endpoint calls to turn subject ids into signed
                 view URLs.

Authorisation is deliberately NOT re-implemented here. `hydrate` is called from
inside the endpoint that already loaded the note or message for this user, so
the check that let them see the text is the same check that lets them see the
picture. A parallel ACL would be a second thing to keep in step.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import boto3
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.inline_image import InlineImage

log = logging.getLogger(__name__)

#: Closed set. A typo in a caller cannot quietly open a new namespace whose
#: rows nothing will ever read back.
SUBJECT_KINDS = frozenset(
    {
        "deal_note",
        "bucket_note",
        "dealer_message",
        "appointment_activity",
        # Not pasted by anyone here: a picture the client texted us, stored by
        # store_bytes when the relay hands it over. Same table because the read
        # path and the rendering are identical.
        "sms_message",
    }
)

#: Formats a browser actually puts on the clipboard for a screenshot, plus the
#: two everyone drags in from disk. Deliberately no SVG: it is a script carrier,
#: and these are rendered inline.
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

MAX_IMAGE_BYTES = 10 * 1024 * 1024
PRESIGN_PUT_TTL = 900  # 15 min — a paste-and-upload, not a background job.
PRESIGN_GET_TTL = 3600


class InlineImageError(ValueError):
    """Caller-fixable problem. Routers map this to HTTP 400."""


def _s3_client():
    s = get_settings()
    kwargs: dict[str, str] = {"region_name": s.aws_region}
    if s.aws_access_key_id and s.aws_secret_access_key:
        kwargs["aws_access_key_id"] = s.aws_access_key_id
        kwargs["aws_secret_access_key"] = s.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def _safe_filename(raw: str) -> str:
    """Reduce a name to one flat path segment.

    Separators and control characters become underscores, so whatever arrives
    contributes a single segment under the prefix and cannot walk out of it. A
    leading dot goes too: it is never meaningful here and a name starting "../"
    reads alarmingly in a log even when it is inert.
    """
    out = re.sub(r"[\\/\x00-\x1f]", "_", raw or "").strip().lstrip(".")
    return out[:200] or "pasted-image"


async def start_upload(
    db: AsyncSession,
    *,
    subject_kind: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    user_id: UUID | None,
) -> dict[str, Any]:
    if subject_kind not in SUBJECT_KINDS:
        raise InlineImageError(f"Unknown subject kind {subject_kind!r}.")
    normalized = (mime_type or "").split(";")[0].strip().lower()
    if normalized not in ALLOWED_MIME:
        raise InlineImageError(
            "That file type cannot be pasted here. Use a PNG, JPEG, GIF, or WebP image."
        )
    if size_bytes <= 0 or size_bytes > MAX_IMAGE_BYTES:
        raise InlineImageError(
            f"Images must be under {MAX_IMAGE_BYTES // (1024 * 1024)} MB."
        )

    settings = get_settings()
    safe = _safe_filename(filename)
    row = InlineImage(
        subject_kind=subject_kind,
        subject_id=None,
        s3_key=f"inline-images/{subject_kind}/{uuid4()}-{safe}",
        filename=safe,
        mime_type=normalized,
        size_bytes=size_bytes,
        uploaded_by_user_id=user_id,
        status="staged",
    )
    db.add(row)
    await db.flush()

    upload_url: str | None = None
    if settings.s3_bucket:
        upload_url = _s3_client().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": row.s3_key,
                "ContentType": row.mime_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=PRESIGN_PUT_TTL,
        )
    return {
        "image_id": row.id,
        "upload_url": upload_url,
        "filename": row.filename,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
    }


async def mark_ready(db: AsyncSession, image_id: UUID, user_id: UUID | None) -> InlineImage:
    row = await db.get(InlineImage, image_id)
    # Scoped to the uploader: confirming someone else's upload is not a thing
    # any caller needs to do, so it is not a thing this allows.
    if row is None or (user_id is not None and row.uploaded_by_user_id != user_id):
        raise InlineImageError("That upload could not be found.")
    row.status = "ready"
    return row


async def attach(
    db: AsyncSession,
    *,
    image_ids: list[UUID],
    subject_kind: str,
    subject_id: str,
    user_id: UUID | None,
) -> list[InlineImage]:
    """Bind the author's ready uploads to the thing they just wrote.

    Silently skips anything that is not theirs, not ready, of the wrong kind, or
    already bound elsewhere. A note posting should not fail because one image of
    four went missing — the note is the thing the person wanted saved.
    """
    if not image_ids:
        return []
    if subject_kind not in SUBJECT_KINDS:
        raise InlineImageError(f"Unknown subject kind {subject_kind!r}.")

    rows = (
        (
            await db.execute(
                select(InlineImage).where(
                    InlineImage.id.in_(image_ids),
                    InlineImage.subject_kind == subject_kind,
                    InlineImage.subject_id.is_(None),
                    InlineImage.status == "ready",
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    attached: list[InlineImage] = []
    for row in rows:
        if user_id is not None and row.uploaded_by_user_id != user_id:
            continue
        row.subject_id = str(subject_id)
        row.attached_at = now
        attached.append(row)
    if len(attached) != len(image_ids):
        log.info(
            "inline images: attached %d of %d requested kind=%s",
            len(attached), len(image_ids), subject_kind,
        )
    return attached



async def store_bytes(
    db: AsyncSession,
    *,
    subject_kind: str,
    subject_id: str,
    filename: str,
    mime_type: str,
    data: bytes,
    uploaded_by_user_id: UUID | None = None,
) -> InlineImage | None:
    """Store bytes we already hold, bound immediately.

    The presigned-PUT flow exists because a browser should not push a file
    through our API. That reasoning does not apply to an MMS the relay has
    already downloaded from the handset — there is no browser, and the bytes
    are in hand. Returns None when the type is not one we render or object
    storage is not configured, because a dropped picture must not fail the text
    that carried it.
    """
    normalized = (mime_type or "").split(";")[0].strip().lower()
    if subject_kind not in SUBJECT_KINDS or normalized not in ALLOWED_MIME:
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    settings = get_settings()
    if not settings.s3_bucket:
        return None

    safe = _safe_filename(filename)
    key = f"inline-images/{subject_kind}/{uuid4()}-{safe}"
    try:
        _s3_client().put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=data,
            ContentType=normalized,
            ServerSideEncryption="AES256",
        )
    except Exception:  # noqa: BLE001
        # The message itself is the record that matters; losing its picture is
        # bad but losing the reply would be worse.
        log.exception("inline images: could not store %s bytes for %s", len(data), subject_kind)
        return None

    row = InlineImage(
        subject_kind=subject_kind,
        subject_id=str(subject_id),
        s3_key=key,
        filename=safe,
        mime_type=normalized,
        size_bytes=len(data),
        uploaded_by_user_id=uploaded_by_user_id,
        status="ready",
        attached_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row

def view_url(row: InlineImage) -> str | None:
    """Short-lived signed GET. The browser never sees a raw S3 key."""
    settings = get_settings()
    if not (settings.s3_bucket and row.s3_key):
        return None
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": row.s3_key},
        ExpiresIn=PRESIGN_GET_TTL,
    )


def serialize(row: InlineImage) -> dict[str, Any]:
    return {
        "id": row.id,
        "filename": row.filename,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "url": view_url(row),
    }


async def hydrate(
    db: AsyncSession, subject_kind: str, subject_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Signed image payloads per subject id, for a read endpoint to fold in.

    Call this from inside a handler that has already authorised the subject for
    this user — see the module docstring.
    """
    wanted = [str(value) for value in subject_ids if value]
    if not wanted:
        return {}
    rows = (
        (
            await db.execute(
                select(InlineImage)
                .where(
                    InlineImage.subject_kind == subject_kind,
                    InlineImage.subject_id.in_(wanted),
                    InlineImage.status == "ready",
                )
                .order_by(InlineImage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row.subject_id), []).append(serialize(row))
    return out
