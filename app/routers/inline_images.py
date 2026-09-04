"""Reserving and confirming a pasted image.

Two endpoints and nothing else. The bytes go straight from the browser to S3 on
a presigned PUT, so there is no upload body here to size-limit or scan; the
size and type are checked before the URL is issued, and S3 refuses anything
that does not match the signed content type.

Reading images back is not here on purpose. A read belongs to whichever endpoint
already loaded the note or message and already decided this user may see it —
see the service docstring.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.services import inline_images

router = APIRouter(prefix="/inline-images", tags=["inline-images"])

#: Annotated rather than a Depends() default: a call in an argument default is
#: what B008 is about, and this module is new enough to not need the exemption
#: the older routers carry.
DbSession = Annotated[AsyncSession, Depends(get_db)]


class InlineImageUploadInit(BaseModel):
    subject_kind: str = Field(description="deal_note | bucket_note | dealer_message | appointment_activity")
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=80)
    size_bytes: int = Field(gt=0)


class InlineImageTicket(BaseModel):
    image_id: UUID
    #: None when S3 is not configured — the caller should report that the paste
    #: could not be stored rather than pretending it worked.
    upload_url: str | None
    filename: str
    mime_type: str
    size_bytes: int


class InlineImageRead(BaseModel):
    id: UUID
    filename: str
    mime_type: str
    size_bytes: int
    url: str | None


@router.post("/upload-init", response_model=InlineImageTicket)
async def start_inline_image_upload(
    payload: InlineImageUploadInit,
    user: CurrentUser,
    db: DbSession,
) -> InlineImageTicket:
    try:
        ticket = await inline_images.start_upload(
            db,
            subject_kind=payload.subject_kind,
            filename=payload.filename,
            mime_type=payload.mime_type,
            size_bytes=payload.size_bytes,
            user_id=user.id,
        )
    except inline_images.InlineImageError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return InlineImageTicket(**ticket)


@router.post("/{image_id}/complete", response_model=InlineImageRead)
async def complete_inline_image_upload(
    image_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> InlineImageRead:
    try:
        row = await inline_images.mark_ready(db, image_id, user.id)
    except inline_images.InlineImageError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return InlineImageRead(**inline_images.serialize(row))
