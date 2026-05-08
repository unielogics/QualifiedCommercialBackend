"""Per-user push-token registration.

Mobile (Expo) calls POST /devices/push-tokens after the borrower
grants notification permission. We upsert by `(user_id, token)` so
re-registration on app reopen is idempotent. DELETE removes a token
on logout / uninstall.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.models.device_token import DeviceToken

router = APIRouter(prefix="/devices", tags=["devices"])
log = logging.getLogger(__name__)


class PushTokenRegisterRequest(BaseModel):
    token: str = Field(min_length=1, max_length=255)
    platform: str = Field(default="expo", max_length=16)


class PushTokenDeregisterRequest(BaseModel):
    token: str = Field(min_length=1, max_length=255)


class PushTokenAck(BaseModel):
    ok: bool = True


@router.post(
    "/push-tokens",
    response_model=PushTokenAck,
    status_code=status.HTTP_201_CREATED,
)
async def register_push_token(
    payload: PushTokenRegisterRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PushTokenAck:
    """Idempotent. If `(user_id, token)` already exists, just refresh
    `updated_at` and the platform marker; otherwise insert."""
    existing = (
        await db.execute(
            select(DeviceToken).where(
                DeviceToken.user_id == user.id,
                DeviceToken.token == payload.token.strip(),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.platform = payload.platform.strip() or "expo"
        await db.commit()
        return PushTokenAck()
    row = DeviceToken(
        user_id=user.id,
        token=payload.token.strip(),
        platform=(payload.platform.strip() or "expo")[:16],
    )
    db.add(row)
    await db.commit()
    log.info("push-token registered user=%s platform=%s", user.id, row.platform)
    return PushTokenAck()


@router.delete("/push-tokens", status_code=status.HTTP_204_NO_CONTENT)
async def deregister_push_token(
    payload: PushTokenDeregisterRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Idempotent — removes the row by `(user_id, token)`. Used on
    logout, app uninstall, or when the mobile detects a rotated
    token and wants to drop the old one."""
    await db.execute(
        delete(DeviceToken).where(
            DeviceToken.user_id == user.id,
            DeviceToken.token == payload.token.strip(),
        )
    )
    await db.commit()
