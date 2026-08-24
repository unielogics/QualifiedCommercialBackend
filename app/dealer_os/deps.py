"""Role guards + loaders for Dealer OS. Team = super_admin | loan_exec.

Stream 6 adds the `dealer` self-serve role: read-only dealer-scoped endpoints
accept team OR Role.DEALER, and a DEALER login only ever resolves the single
DealerBusiness whose dealer_user_id matches it (mismatch = 404, never 403, so
other dealers' ids stay unprobeable).

The field-rep role follows the same shape, scoped on owner_user_id instead.

READ THIS BEFORE ADDING A ROLE. The guards here are decoupled from the
scoping: a guard admits by role, while scoping happens separately depending on
whether a handler called load_dealer (no scoping at all) or resolve_dealer_scope.
Sixty-odd handlers pair require_team with load_dealer, so adding a role to
_TEAM_ROLES hands it every client file in the system, instantly and silently.
FIELD_REP is therefore deliberately NOT in _TEAM_ROLES, and every role that
resolve_dealer_scope does not know by name falls through to unrestricted access.
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team role required for Capital OS")


def require_super_admin(user: User) -> None:
    """Desk-policy writes (program settings) are super-admin only."""
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


def require_dealer(user: User) -> None:
    """Client-owned actions that staff must never perform on the client's behalf."""
    if user.role != Role.DEALER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Client role required for this action"
        )


def require_team_or_dealer(user: User) -> None:
    if user.role not in _TEAM_ROLES and user.role != Role.DEALER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Team or client role required for Capital OS"
        )


def require_team_or_rep(user: User) -> None:
    """Field reps and the team. A rep is confined to their own book by
    resolve_dealer_scope and by the one list filter; this only decides who may
    knock."""
    if user.role not in _TEAM_ROLES and user.role != Role.FIELD_REP:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Rep or team role required for Capital OS"
        )


def require_team_or_dealer_or_rep(user: User) -> None:
    """Every role with a legitimate view of a client file: the team, the
    client themselves, and the rep who owns it."""
    if user.role not in _TEAM_ROLES and user.role not in (Role.DEALER, Role.FIELD_REP):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "No access to Capital OS"
        )


def require_field_rep(user: User) -> None:
    if user.role != Role.FIELD_REP:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Field-rep role required")


def is_rep(user: User) -> bool:
    return user.role == Role.FIELD_REP


async def load_dealer(db: AsyncSession, dealer_id: UUID) -> DealerBusiness:
    dealer = await db.get(DealerBusiness, dealer_id)
    if dealer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    return dealer


async def resolve_dealer_scope(db: AsyncSession, user: User, dealer_id: UUID) -> DealerBusiness:
    """Team roles load any dealer. A DEALER login resolves only the business
    linked to it via dealer_user_id; a FIELD_REP only the files they own via
    owner_user_id. A mismatch is a 404 (same as a nonexistent id) so ids can't
    be probed for existence.

    Deny by default: a role that is neither a team role nor a known scoped role
    gets 403 rather than falling through to unrestricted access, which is what
    the earlier shape did."""
    dealer = await load_dealer(db, dealer_id)
    if user.role == Role.DEALER:
        if dealer.dealer_user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    elif user.role == Role.FIELD_REP:
        if dealer.owner_user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    elif user.role not in _TEAM_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No access to this client")
    return dealer
