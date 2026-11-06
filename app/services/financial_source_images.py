"""Bounded validation for image originals reviewed as selected-loan evidence."""
from __future__ import annotations

import asyncio
import hashlib
import warnings
from io import BytesIO

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/heic"})
MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_EDGE = 8000


def validated_image_media(raw: bytes) -> str | None:
    """Use real bytes, never a filename/MIME declaration, to authorize vision.

    Hashes always bind the original bytes. HEIC conversion, when available,
    remains the existing review reader's model-only derivative.
    """
    from app.services import bucket_ai

    media = bucket_ai.detect_document_content_type(raw)
    if media not in IMAGE_MEDIA_TYPES:
        return None
    if len(raw) > bucket_ai.MAX_FILE_BYTES:
        raise HTTPException(422, "This image is too large for automatic review. Upload a readable still image under 10 MB or a PDF.")
    if media == "image/heic":
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError as error:
            raise HTTPException(422, "Automatic HEIC review is unavailable. Upload a JPEG, PNG, or PDF copy; the original is retained.") from error
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_EDGE or width * height > MAX_IMAGE_PIXELS:
                    raise HTTPException(422, "This image's dimensions exceed automatic review limits. Upload a readable crop or PDF.")
                if getattr(image, "n_frames", 1) != 1:
                    raise HTTPException(422, "Animated or multi-frame images need separate still PNG/JPEG images or a PDF so no loan details are omitted.")
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise HTTPException(422, "This image could not be decoded safely. Upload a readable PNG, JPEG, GIF, WebP, or PDF copy.") from error
    return media


def jpeg_preview(raw: bytes) -> bytes:
    """Readable derivative only; the uploaded bytes and their hash stay intact."""
    if validated_image_media(raw) is None:
        raise HTTPException(422, "This source is not a supported image")
    with Image.open(BytesIO(raw)) as original:
        image = ImageOps.exif_transpose(original)
        if image.mode in {"RGBA", "LA"} or image.mode == "P" and "transparency" in image.info:
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
        image.thumbnail((3000, 3000))
        output = BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()


def _read_preview_source(file) -> bytes:
    """Read at most the review limit and pin a retained S3 version when known."""
    from app.routers.buckets import _bucket_storage_config, _s3_client
    from app.services.bucket_ai import MAX_FILE_BYTES

    bucket, _prefix, _kms = _bucket_storage_config()
    request = {"Bucket": bucket, "Key": file.s3_key}
    if getattr(file, "s3_version_id", None):
        request["VersionId"] = file.s3_version_id
    response = _s3_client().get_object(**request)
    stream = response["Body"]
    try:
        if int(response.get("ContentLength") or 0) > MAX_FILE_BYTES:
            raise HTTPException(422, "This image exceeds the automatic preview size limit")
        raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(422, "This image exceeds the automatic preview size limit")
        return raw
    finally:
        stream.close()


async def scoped_image_preview(db, profile, file_id, *, expected_content_hash=None) -> tuple[bytes, str]:
    from app.services import financial_workspace as workspace

    file = await workspace.source_file_for_profile(db, profile, file_id)
    recorded = getattr(file, "content_hash", None)
    if expected_content_hash and (not recorded or recorded.casefold() != expected_content_hash.casefold()):
        raise HTTPException(409, {"code": "evidence_source_changed", "message": "The cited image changed. Reload its current source before previewing."})
    try:
        raw = await asyncio.to_thread(_read_preview_source, file)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(502, "The original image could not be retrieved for preview") from error
    content_hash = hashlib.sha256(raw).hexdigest()
    if recorded and content_hash.casefold() != recorded.casefold():
        raise HTTPException(409, {"code": "evidence_source_changed", "message": "The image bytes no longer match their recorded source. Review the current document version."})
    return await asyncio.to_thread(jpeg_preview, raw), content_hash
