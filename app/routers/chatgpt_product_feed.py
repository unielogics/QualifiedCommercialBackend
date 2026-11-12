"""Super-admin-only controls; no secret ever travels through generic settings."""

# ruff: noqa: B008

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.user import User
from app.schemas.chatgpt_product_feed import (
    FeedSettings,
    KeyPairRequest,
    PreviewRequest,
    SyncRequest,
)
from app.services import chatgpt_product_feed as service

router = APIRouter(prefix="/admin/chatgpt-product-feed", tags=["chatgpt-product-feed"], dependencies=[Depends(require_role(Role.SUPER_ADMIN))])


@router.get("/settings")
async def settings(db: AsyncSession = Depends(get_db)):
    return await service.settings_view(db)


@router.put("/settings")
async def update_settings(payload: FeedSettings, db: AsyncSession = Depends(get_db)):
    return await service.save_settings(db, payload)


@router.post("/key-pair")
async def key_pair(payload: KeyPairRequest, user: User = Depends(require_role(Role.SUPER_ADMIN)), db: AsyncSession = Depends(get_db)):
    return await service.key_pair(db, rotate=payload.rotate, user_id=user.id)


@router.post("/preview")
async def preview(payload: PreviewRequest | None = None, db: AsyncSession = Depends(get_db)):
    return await service.preview(db, payload.settings.model_dump(mode="json") if payload and payload.settings else None)


@router.get("/download")
async def download(db: AsyncSession = Depends(get_db)):
    _, content = await service.export_snapshot(db)
    row = await service.config_row(db)
    filename = FeedSettings.model_validate(row.data).filename
    return Response(content=content, media_type="application/gzip", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.post("/test-connection")
async def test_connection(db: AsyncSession = Depends(get_db)):
    return await service.test_saved_connection(db)


@router.post("/sync", status_code=202)
async def sync(payload: SyncRequest | None = None, db: AsyncSession = Depends(get_db)):
    return await service.queue_sync(db, idempotency_key=payload.idempotency_key if payload else None)


@router.get("/runs")
async def runs(db: AsyncSession = Depends(get_db)):
    return await service.list_runs(db)
