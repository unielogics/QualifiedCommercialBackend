"""Lender CRUD — super-admin only.

Backs the desktop /admin/lenders page. The list endpoint also
supports `?product=<LoanType>` filtering so the loan page's
Connect-Lender dropdown can show only matching active lenders.

Soft-delete is the default: operators flip `is_active=False` to
hide a lender from new connections without orphaning historical
LoanParticipant rows / Activity log entries that reference it.
Hard-delete is rejected if any loan still references the lender.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import LoanType, Role
from app.models.lender import Lender
from app.models.loan import Loan
from app.schemas.lender import LenderCreate, LenderRead, LenderUpdate

router = APIRouter(prefix="/lenders", tags=["lenders"])


def _require_super_admin(user) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


@router.get("", response_model=list[LenderRead])
async def list_lenders(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    product: LoanType | None = Query(default=None),
    active_only: bool = Query(default=False),
) -> list[LenderRead]:
    """List lenders. `product` filters to lenders whose `products`
    array contains that LoanType (used by the Connect-Lender
    dropdown). `active_only=True` hides soft-deleted rows."""
    _require_super_admin(user)
    stmt = select(Lender).order_by(Lender.name.asc())
    if active_only:
        stmt = stmt.where(Lender.is_active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    if product is not None:
        rows = [r for r in rows if product.value in (r.products or [])]
    return [LenderRead.model_validate(r) for r in rows]


@router.post("", response_model=LenderRead, status_code=status.HTTP_201_CREATED)
async def create_lender(
    payload: LenderCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderRead:
    _require_super_admin(user)
    # Pydantic enums serialize to their value via model_dump(); JSONB
    # column expects a list of strings.
    data = payload.model_dump()
    data["products"] = [p.value if hasattr(p, "value") else str(p) for p in (data.get("products") or [])]
    row = Lender(**data)
    db.add(row)
    try:
        await db.flush()
    except Exception as exc:  # likely unique-violation on name
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Could not create lender (name already taken?): {exc}",
        ) from exc
    await db.commit()
    await db.refresh(row)
    return LenderRead.model_validate(row)


@router.get("/{lender_id}", response_model=LenderRead)
async def get_lender(
    lender_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderRead:
    _require_super_admin(user)
    row = (await db.execute(select(Lender).where(Lender.id == lender_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lender not found")
    return LenderRead.model_validate(row)


@router.patch("/{lender_id}", response_model=LenderRead)
async def update_lender(
    lender_id: UUID,
    payload: LenderUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderRead:
    _require_super_admin(user)
    row = (await db.execute(select(Lender).where(Lender.id == lender_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lender not found")
    patch = payload.model_dump(exclude_unset=True)
    if "products" in patch and patch["products"] is not None:
        patch["products"] = [p.value if hasattr(p, "value") else str(p) for p in patch["products"]]
    for k, v in patch.items():
        setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return LenderRead.model_validate(row)


@router.delete("/{lender_id}", status_code=status.HTTP_200_OK)
async def delete_lender(
    lender_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    hard: bool = Query(default=False),
) -> dict[str, str]:
    """Soft-delete by default (sets is_active=False). Pass ?hard=true
    to actually drop the row — only allowed when no Loan still
    references this lender via `loans.lender_id`."""
    _require_super_admin(user)
    row = (await db.execute(select(Lender).where(Lender.id == lender_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lender not found")
    if hard:
        in_use = (
            await db.execute(select(Loan.id).where(Loan.lender_id == lender_id).limit(1))
        ).scalar_one_or_none()
        if in_use is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Lender is connected to one or more loans. Disconnect those loans first, "
                "or soft-delete with `is_active=false`.",
            )
        await db.delete(row)
        await db.commit()
        return {"status": "deleted", "mode": "hard"}
    row.is_active = False
    await db.commit()
    return {"status": "deleted", "mode": "soft"}
