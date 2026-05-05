"""Auth — Clerk owns sessions; backend exposes /me."""

from __future__ import annotations

from fastapi import APIRouter

from app.deps import CurrentUser
from app.enums import Role
from app.schemas.common import ORMModel

router = APIRouter(prefix="/auth", tags=["auth"])


class MeResponse(ORMModel):
    id: str
    clerk_id: str
    email: str
    name: str
    role: Role


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser) -> MeResponse:
    return MeResponse(
        id=str(user.id),
        clerk_id=user.clerk_id,
        email=user.email,
        name=user.name,
        role=user.role,
    )
