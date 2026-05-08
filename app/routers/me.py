"""Per-user personal settings endpoints.

Today this is just the broker-settings overlay (`brokers.settings_data`,
alembic 0023) — the agent's checklist additions/disables, AI cadence
overrides, and personal letterhead. Replaces the v1 localStorage
persistence on the desktop /agent-settings page.

Scoped to `Role.BROKER` — super-admins use the firm-wide /settings
page, not this one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.app_settings import AppSettings
from app.models.broker import Broker
from app.schemas.broker_settings import AgentSettingsData, AgentSettingsRead
from app.services.loan_intake_automation import _coerce_settings

router = APIRouter(prefix="/me", tags=["me"])
log = logging.getLogger(__name__)


async def _broker_for_user(db: AsyncSession, user_id) -> Broker | None:
    return (
        await db.execute(select(Broker).where(Broker.user_id == user_id))
    ).scalar_one_or_none()


@router.get("/broker-settings", response_model=AgentSettingsRead)
async def get_broker_settings(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> AgentSettingsRead:
    """Returns the calling broker's overlay data, or empty defaults
    when the row is fresh / no overlay configured yet."""
    if user.role != Role.BROKER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Broker-settings is broker-only — super-admins use /settings.",
        )
    broker = await _broker_for_user(db, user.id)
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile for this user")
    raw = broker.settings_data
    data = AgentSettingsData() if not raw else AgentSettingsData.model_validate(raw)
    return AgentSettingsRead(data=data)


@router.put("/broker-settings", response_model=AgentSettingsRead)
async def put_broker_settings(
    payload: AgentSettingsData,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AgentSettingsRead:
    """Whole-document replacement — the desktop sends the full
    overlay every save. Validates that:
      - `disabled_firm_items` references real firm item names
        (otherwise it's dead config and the agent's intent is wrong)
      - `extra_items[*].name` doesn't collide with firm item names
        (avoid silent shadowing)
    """
    if user.role != Role.BROKER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Broker-settings is broker-only.",
        )
    broker = await _broker_for_user(db, user.id)
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile for this user")

    # Load firm settings to validate against.
    settings_row = (
        await db.execute(select(AppSettings).limit(1))
    ).scalar_one_or_none()
    firm = _coerce_settings(settings_row)

    errors: list[str] = []
    for key, overlay in (payload.checklists or {}).items():
        # Key shape: "<loan_type>:<side>"
        if ":" not in key:
            errors.append(f"checklist key {key!r} must be 'loan_type:side'")
            continue
        loan_type, side = key.split(":", 1)
        firm_chk = firm.checklists.get(loan_type)
        firm_names = {it.name for it in (firm_chk.docs if firm_chk else [])}
        # disabled_firm_items must reference real firm items
        for n in overlay.disabled_firm_items:
            if n not in firm_names:
                errors.append(
                    f"checklist[{key}].disabled_firm_items: "
                    f"{n!r} doesn't match any firm item for {loan_type}"
                )
        # extra_items names cannot collide with firm names
        for it in overlay.extra_items:
            if it.name in firm_names:
                errors.append(
                    f"checklist[{key}].extra_items: {it.name!r} "
                    f"already exists in the firm checklist (rename your item)"
                )
            if it.side not in ("buyer", "seller", "both"):
                errors.append(
                    f"checklist[{key}].extra_items[{it.name!r}].side "
                    f"must be buyer | seller | both"
                )
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"errors": errors},
        )

    broker.settings_data = payload.model_dump(mode="json")
    await db.commit()
    await db.refresh(broker)
    return AgentSettingsRead(
        data=AgentSettingsData.model_validate(broker.settings_data or {})
    )
