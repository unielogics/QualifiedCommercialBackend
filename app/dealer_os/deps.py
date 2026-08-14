"""Role guards + loaders for Dealer OS. Team = super_admin | loan_exec.

Stream 6 adds the `dealer` self-serve role: read-only dealer-scoped endpoints
accept team OR Role.DEALER, and a DEALER login only ever resolves the single
DealerBusiness whose dealer_user_id matches it (mismatch = 404, never 403, so
other dealers' ids stay unprobeable).
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


def require_team_or_dealer(user: User) -> None:
    if user.role not in _TEAM_ROLES and user.role != Role.DEALER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Team or dealer role required for Dealer OS"
        )


async def load_dealer(db: AsyncSession, dealer_id: UUID) -> DealerBusiness:
    dealer = await db.get(DealerBusiness, dealer_id)
    if dealer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer not found")
    return dealer


async def resolve_dealer_scope(db: AsyncSession, user: User, dealer_id: UUID) -> DealerBusiness:
    """Team roles load any dealer; a DEALER login only resolves the business
    linked to it via DealerBusiness.dealer_user_id. A mismatch is a 404 (same
    as a nonexistent id) so dealer ids can't be probed for existence."""
    dealer = await load_dealer(db, dealer_id)
    if user.role == Role.DEALER and dealer.dealer_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer not found")
    return dealer
