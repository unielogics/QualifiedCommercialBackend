"""Users router — operator-team listing + invite/edit/revoke."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user, require_role
from app.enums import Role
from app.models.user import User
from app.services import clerk as clerk_service

router = APIRouter(prefix="/users", tags=["users"])


class UserRead(BaseModel):
    id: UUID
    email: EmailStr | str
    name: str
    role: Role
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserInvite(BaseModel):
    email: EmailStr
    name: str
    role: Role


class UserPatch(BaseModel):
    role: Role | None = None
    name: str | None = None


@router.get(
    "",
    response_model=list[UserRead],
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def list_users(db: AsyncSession = Depends(get_db)) -> list[UserRead]:
    """List every operator-team user. Super-admin only.

    Excludes CLIENT-role users (Team is the operator team) and soft-deleted rows.
    """
    rows = (
        await db.execute(
            select(User)
            .where(User.role != Role.CLIENT, User.deleted_at.is_(None))
            .order_by(User.name)
        )
    ).scalars().all()
    return [UserRead.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def invite_user(
    body: UserInvite,
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Invite a new operator-team member.

    Creates a local User row immediately so the team list updates, then sends
    a Clerk invitation email (best-effort — invite still completes if Clerk
    isn't configured locally). Blocks role=CLIENT — borrowers are created via
    /clients.
    """
    if body.role == Role.CLIENT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "CLIENT role belongs to /clients — use that endpoint to create borrowers.",
        )

    existing = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if existing is not None and existing.deleted_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists.")
    if existing is not None and existing.deleted_at is not None:
        # Resurrect a soft-deleted row instead of failing on the unique index.
        existing.deleted_at = None
        existing.name = body.name
        existing.role = body.role
        existing.clerk_id = None  # force re-bind on next sign-in
        user = existing
    else:
        user = User(
            email=body.email.lower(),
            name=body.name,
            role=body.role,
            clerk_id=None,  # bound on first sign-in via JIT provision
        )
        db.add(user)

    await db.flush()
    await db.refresh(user)

    # Fire-and-forget Clerk invitation. No-op when CLERK_SECRET_KEY is unset.
    await clerk_service.invite_user(email=body.email, name=body.name, role=body.role)

    return UserRead.model_validate(user)


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def update_user(
    user_id: UUID,
    body: UserPatch,
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if body.role is not None:
        if body.role == Role.CLIENT:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Use /clients to convert a user to a borrower.",
            )
        user.role = body.role
    if body.name is not None:
        user.name = body.name
    await db.flush()
    await db.refresh(user)
    return UserRead.model_validate(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
) -> None:
    """Soft-delete a team member. Self-delete is blocked."""
    if current.id == user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You can't remove your own super-admin account."
        )
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    # Best-effort revoke in Clerk so the invited user can't sign in afterward.
    if user.clerk_id:
        await clerk_service.revoke_user(user.clerk_id)
