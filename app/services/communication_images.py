"""Validated images for operator-authored email and text messages.

Uploads belong to their author until a send succeeds. Each delivered message
gets its own reference to immutable validated bytes, so a partial email/SMS
failure can be retried without losing the other channel's attachment history.
"""
from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models.inline_image import InlineImage
from app.services import inline_images

SUBJECT_KIND = "outbound_message"
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 6 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/heic", "image/heif"})
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp", "HEIF": "image/heic"}


@dataclass(frozen=True)
class OutboundImage:
    row: InlineImage
    data: bytes

    @property
    def attachment(self) -> tuple[str, bytes, str]:
        return self.row.filename, self.data, self.row.mime_type


def _read_bytes(row: InlineImage) -> bytes:
    settings = get_settings()
    if not settings.s3_bucket:
        raise inline_images.InlineImageError("Image storage is unavailable. Your message has not been sent.")
    response = inline_images._s3_client().get_object(Bucket=settings.s3_bucket, Key=row.s3_key)
    stream = response["Body"]
    try:
        if int(response.get("ContentLength") or 0) > MAX_IMAGE_BYTES:
            raise inline_images.InlineImageError("An image exceeds the 10 MB limit.")
        raw = stream.read(MAX_IMAGE_BYTES + 1)
    finally:
        stream.close()
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise inline_images.InlineImageError("An image is empty or exceeds the 10 MB limit.")
    if len(raw) != row.size_bytes:
        raise inline_images.InlineImageError("An uploaded image changed. Remove it and attach it again before sending.")
    return raw


def _validated_bytes(raw: bytes, declared_type: str, filename: str) -> tuple[bytes, str, str]:
    if declared_type in {"image/heic", "image/heif"}:
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError as exc:
            raise inline_images.InlineImageError("HEIC conversion is unavailable. Attach a JPEG or PNG copy.") from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                media = _FORMATS.get(image.format or "")
                width, height = image.size
                frames = getattr(image, "n_frames", 1)
                if media is None or width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS or max(width, height) > 12_000:
                    raise inline_images.InlineImageError("This image type or its dimensions are not supported. Use a readable PNG or JPEG.")
                if frames > 200 or width * height * frames > 128_000_000:
                    raise inline_images.InlineImageError("This animated image is too large. Attach a still image instead.")
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
                if media == "image/heic":
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    image.thumbnail((4000, 4000))
                    output = BytesIO()
                    image.save(output, format="JPEG", quality=92)
                    raw = output.getvalue()
                    media = "image/jpeg"
                    filename = str(PurePosixPath(filename).with_suffix(".jpg"))
    except inline_images.InlineImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise inline_images.InlineImageError("This image could not be read safely. Attach a PNG, JPEG, GIF, WebP, or HEIC image.") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise inline_images.InlineImageError("The converted image exceeds the 10 MB limit.")
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
    if media in extensions and PurePosixPath(filename).suffix.lower() not in {extensions[media], ".jpeg" if media == "image/jpeg" else extensions[media]}:
        filename = str(PurePosixPath(filename).with_suffix(extensions[media]))
    return raw, media, filename


async def validate_upload(db, row: InlineImage) -> None:
    """Promote decoded bytes to a key the browser's PUT ticket cannot overwrite."""
    try:
        raw = await asyncio.to_thread(_read_bytes, row)
        data, media, filename = await asyncio.to_thread(_validated_bytes, raw, row.mime_type, row.filename)
        key = f"outbound-images/{row.id}/{uuid4()}-{inline_images._safe_filename(filename)}"
        await asyncio.to_thread(inline_images._s3_client().put_object,
                                Bucket=get_settings().s3_bucket, Key=key, Body=data,
                                ContentType=media, ServerSideEncryption="AES256")
    except inline_images.InlineImageError:
        raise
    except Exception as exc:
        raise inline_images.InlineImageError("The image upload could not be verified. Try again before sending.") from exc
    # The browser's PUT ticket remains valid briefly. Retain a hidden staged
    # record so the ordinary 24-hour orphan sweep deletes that temporary key
    # only after every upload ticket has expired.
    db.add(InlineImage(subject_kind="outbound_upload_staging", subject_id=None,
                       s3_key=row.s3_key, filename=row.filename, mime_type=row.mime_type,
                       size_bytes=row.size_bytes, uploaded_by_user_id=row.uploaded_by_user_id,
                       status="staged"))
    row.s3_key = key
    row.filename, row.mime_type, row.size_bytes = filename, media, len(data)


async def load(db, image_ids: list[UUID], user_id: UUID) -> list[OutboundImage]:
    if not image_ids:
        return []
    if len(image_ids) > MAX_IMAGES or len(set(image_ids)) != len(image_ids):
        raise inline_images.InlineImageError("Attach up to 10 different images to one message.")
    rows = list((await db.execute(select(InlineImage).where(
        InlineImage.id.in_(image_ids), InlineImage.uploaded_by_user_id == user_id,
        InlineImage.subject_kind == SUBJECT_KIND, InlineImage.status == "ready",
        InlineImage.subject_id.is_(None),
    ).with_for_update())).scalars().all())
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(image_ids) or any(
        row.uploaded_by_user_id != user_id or row.subject_kind != SUBJECT_KIND or row.status != "ready" or row.subject_id is not None
        for row in rows
    ):
        raise inline_images.InlineImageError("An image is unavailable or belongs to another author. Remove it and attach it again.")
    if sum(row.size_bytes for row in rows) > MAX_TOTAL_BYTES:
        raise inline_images.InlineImageError("Image attachments must total no more than 6 MB per message.")
    images = []
    try:
        for image_id in image_ids:
            row = by_id[image_id]
            if not row.s3_key.startswith(f"outbound-images/{row.id}/"):
                raise inline_images.InlineImageError("An image upload has not been verified. Attach it again before sending.")
            images.append(OutboundImage(row, await asyncio.to_thread(_read_bytes, row)))
    except inline_images.InlineImageError:
        raise
    except Exception as exc:
        raise inline_images.InlineImageError("An attached image could not be loaded. Your message has not been sent.") from exc
    return images


async def bind(db, images: list[OutboundImage], *, subject_kind: str, subject_id: str) -> None:
    """References are message-local; failed channels retain the original upload."""
    if not images:
        return
    for image in images:
        source = image.row
        db.add(InlineImage(
            subject_kind=subject_kind, subject_id=str(subject_id), s3_key=source.s3_key,
            filename=source.filename, mime_type=source.mime_type, size_bytes=source.size_bytes,
            uploaded_by_user_id=source.uploaded_by_user_id, status="ready", attached_at=datetime.now(UTC),
        ))
    await db.flush()


async def load_bound(db, subject_kind: str, subject_id: str, user_id: UUID | None = None) -> list[OutboundImage]:
    """Replay images after the caller authorizes this exact stored message."""
    statement = select(InlineImage).where(InlineImage.subject_kind == subject_kind,
                                         InlineImage.subject_id == str(subject_id), InlineImage.status == "ready")
    if user_id is not None:
        statement = statement.where(InlineImage.uploaded_by_user_id == user_id)
    rows = list((await db.execute(statement.order_by(InlineImage.created_at, InlineImage.id))).scalars().all())
    if len(rows) > MAX_IMAGES or sum(row.size_bytes for row in rows) > MAX_TOTAL_BYTES:
        raise inline_images.InlineImageError("These stored images exceed the current message attachment limits.")
    try:
        images = []
        for row in rows:
            if not row.s3_key.startswith("outbound-images/"):
                raise inline_images.InlineImageError("A stored image cannot be re-sent automatically. Attach it again.")
            images.append(OutboundImage(row, await asyncio.to_thread(_read_bytes, row)))
        return images
    except inline_images.InlineImageError:
        raise
    except Exception as exc:
        raise inline_images.InlineImageError("The stored images could not be loaded. No new message was sent.") from exc


def attachment_bytes(images: list[OutboundImage]) -> list[tuple[str, bytes, str]]:
    return [image.attachment for image in images]


async def reserve_sms_shares(db, images: list[OutboundImage]) -> list[InlineImage]:
    shares = []
    for image in images:
        source = image.row
        row = InlineImage(id=uuid4(), subject_kind="sms_image_share", subject_id=None,
                          s3_key=source.s3_key, filename=source.filename, mime_type=source.mime_type,
                          size_bytes=source.size_bytes, uploaded_by_user_id=source.uploaded_by_user_id,
                          status="staged")
        shares.append(row)
    if shares:
        # Delivery links have their own durable preparation. A later failure
        # recording message history must not destroy a link already texted.
        async with SessionLocal() as share_db:
            share_db.add_all(shares)
            await share_db.commit()
    return shares


def sms_share_url(row: InlineImage) -> str:
    if not sms_share_available():
        raise inline_images.InlineImageError("Image links require a public HTTPS API address. Send the images by email instead.")
    return f"{get_settings().public_api_url.rstrip('/')}/api/v1/communications/images/{row.id}"


def sms_share_available() -> bool:
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(get_settings().public_api_url or "")
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.query or parsed.fragment or any(char.isspace() for char in parsed.netloc):
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


async def activate_sms_shares(shares: list[InlineImage], message_id: UUID | None = None) -> None:
    if not shares:
        return
    ids = [row.id for row in shares]
    async with SessionLocal() as share_db:
        rows = list((await share_db.execute(select(InlineImage).where(
            InlineImage.id.in_(ids), InlineImage.subject_kind == "sms_image_share",
        ).with_for_update())).scalars().all())
        if len(rows) != len(ids):
            raise inline_images.InlineImageError("Image link preparation is incomplete.")
        for row in rows:
            if row.status == "shared":
                if message_id is not None and row.subject_id is None:
                    row.subject_id = str(message_id)
                continue
            row.status = "shared"
            row.subject_id = str(message_id) if message_id is not None else None
            row.attached_at = datetime.now(UTC)
        await share_db.commit()
