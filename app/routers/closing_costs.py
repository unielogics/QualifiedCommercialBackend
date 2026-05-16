"""Closing-cost tiers — global config; SUPER_ADMIN write.

Backs the desktop /admin/closing-costs grid. The Deal Analyzer reads
this table (any authenticated user) to derive the closing percentage
for a deal; only SUPER_ADMIN can edit it.

The grid uses the bulk PUT (replace the whole table in one
transaction — matches the Excel-grid Save). Single-row CRUD is kept
for flexibility / API completeness.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser, require_role
from app.enums import Role
from app.models.activity import Activity
from app.models.closing_cost_tier import ClosingCostTier
from app.schemas.closing_cost import (
    ClosingCostTierCreate,
    ClosingCostTierRead,
    ClosingCostTiersReplace,
    ClosingCostTierUpdate,
)

router = APIRouter(prefix="/closing-costs", tags=["closing-costs"])


def _log(db: AsyncSession, user, kind: str, summary: str, payload: dict | None = None) -> None:
    db.add(
        Activity(
            loan_id=None,
            actor_id=user.id,
            actor_label=user.role.value if hasattr(user.role, "value") else str(user.role),
            kind=kind,
            summary=summary,
            payload=payload,
        )
    )


@router.get("", response_model=list[ClosingCostTierRead])
async def list_tiers(
    _: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ClosingCostTierRead]:
    """List tiers ordered by sort_order. Any authenticated user — the
    analyzer (CLIENT on mobile, operator on web) reads this."""
    rows = (
        await db.execute(
            select(ClosingCostTier).order_by(
                ClosingCostTier.sort_order.asc(), ClosingCostTier.from_amount.asc()
            )
        )
    ).scalars().all()
    return [ClosingCostTierRead.model_validate(r) for r in rows]


@router.put(
    "",
    response_model=list[ClosingCostTierRead],
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def replace_tiers(
    payload: ClosingCostTiersReplace,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ClosingCostTierRead]:
    """Bulk replace the whole table in one transaction."""
    await db.execute(delete(ClosingCostTier))
    for i, t in enumerate(payload.tiers):
        data = t.model_dump()
        if data.get("sort_order") in (None, 0):
            data["sort_order"] = i
        db.add(ClosingCostTier(**data))
    _log(
        db, user, "closing_costs.replaced",
        f"Replaced closing-cost table ({len(payload.tiers)} tiers)",
        {"count": len(payload.tiers)},
    )
    await db.flush()
    rows = (
        await db.execute(
            select(ClosingCostTier).order_by(
                ClosingCostTier.sort_order.asc(), ClosingCostTier.from_amount.asc()
            )
        )
    ).scalars().all()
    return [ClosingCostTierRead.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=ClosingCostTierRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def create_tier(
    payload: ClosingCostTierCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClosingCostTierRead:
    row = ClosingCostTier(**payload.model_dump())
    db.add(row)
    _log(db, user, "closing_costs.created", "Added a closing-cost tier")
    await db.flush()
    await db.refresh(row)
    return ClosingCostTierRead.model_validate(row)


@router.patch(
    "/{tier_id}",
    response_model=ClosingCostTierRead,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def update_tier(
    tier_id: UUID,
    payload: ClosingCostTierUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClosingCostTierRead:
    row = (
        await db.execute(select(ClosingCostTier).where(ClosingCostTier.id == tier_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tier not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    _log(db, user, "closing_costs.updated", "Updated a closing-cost tier")
    await db.flush()
    await db.refresh(row)
    return ClosingCostTierRead.model_validate(row)


@router.delete(
    "/{tier_id}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def delete_tier(
    tier_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    row = (
        await db.execute(select(ClosingCostTier).where(ClosingCostTier.id == tier_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tier not found")
    await db.delete(row)
    _log(db, user, "closing_costs.deleted", "Removed a closing-cost tier")
    await db.flush()
    return {"status": "deleted"}
