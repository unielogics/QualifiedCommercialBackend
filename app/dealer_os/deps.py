"""Role guards + loaders for Dealer OS. Team = super_admin | loan_exec.

The `dealer` self-serve role lands in Stream 6; until then every endpoint is
team-gated so the isolation contract's three touch points stay exactly three
(no Role enum edit yet).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.user import User

from .models import DealerBusiness

_TEAM_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}


def require_team(user: User) -> None:
    if user.role not in _TEAM_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team role required for Dealer OS")


async def load_dealer(db: AsyncSession, dealer_id: UUID) -> DealerBusiness:
    dealer = await db.get(DealerBusiness, dealer_id)
    if dealer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer not found")
    return dealer
