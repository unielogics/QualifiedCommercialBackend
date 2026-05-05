"""Settings router — singleton row, super-admin write.

Read access is open to any authenticated user (the UI displays the values).
Write access is restricted to SUPER_ADMIN.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser, require_role
from app.enums import Role
from app.models.activity import Activity
from app.models.app_settings import AppSettings
from app.schemas.settings import AppSettingsData, AppSettingsRead, AppSettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


async def _get_or_create(db: AsyncSession) -> AppSettings:
    row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    if row is None:
        row = AppSettings(id=uuid.uuid4(), singleton=True, data=AppSettingsData().model_dump(mode="json"))
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


def _coerce(data: dict[str, Any]) -> AppSettingsData:
    """Tolerate older partial blobs by letting Pydantic fill defaults."""
    return AppSettingsData.model_validate(data or {})


@router.get("", response_model=AppSettingsRead)
async def get_settings(_: CurrentUser, db: AsyncSession = Depends(get_db)) -> AppSettingsRead:
    row = await _get_or_create(db)
    return AppSettingsRead(data=_coerce(row.data))


@router.patch(
    "",
    response_model=AppSettingsRead,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def update_settings(
    payload: AppSettingsUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AppSettingsRead:
    row = await _get_or_create(db)

    # Section-level merge — replace whole sections that the caller sent,
    # leave the rest untouched. Keeps the wire format simple.
    current = _coerce(row.data).model_dump(mode="json")
    patch = payload.model_dump(mode="json", exclude_none=True)
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No settings sections supplied.")
    current.update(patch)

    row.data = current

    db.add(
        Activity(
            loan_id=None,
            actor_id=user.id,
            actor_label=user.role.value if hasattr(user.role, "value") else str(user.role),
            kind="settings.updated",
            summary=f"Updated settings sections: {', '.join(patch.keys())}",
            payload=patch,
        )
    )

    await db.flush()
    await db.refresh(row)
    return AppSettingsRead(data=_coerce(row.data))
