"""Who may see what in the Capital Partners API.

Three tiers, each a FastAPI dependency:

* ``MemberCtx`` (``require_member``): a capital-partner login with its member
  row, or an operator in preview. Enough for /capital/me and the questionnaire.
* ``require_cleared_member``: approved AND past the waiting period, or
  preview. The only way to read a listing, a document or a subscription.
  This is the Rule 506(b) gate; it lives here so no router can forget it.
* ``require_subscribing_member``: cleared and NOT preview. Operators look;
  they never subscribe or list a transfer on a member's behalf from here.

Admin routes use ``require_cp_admin`` (super admin only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user, require_role
from app.enums import Role
from app.models.user import User

from . import enums as E
from .models import CpMember
from .services import members as member_svc
from .services import membership_policy

PREVIEW_ROLES = membership_policy.PREVIEW_ROLES


@dataclass
class MemberCtx:
    user: User
    member: CpMember | None
    preview: bool

    @property
    def cleared(self) -> bool:
        return self.preview or member_svc.is_cleared(self.member)


async def require_member(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemberCtx:
    if user.role in PREVIEW_ROLES:
        return MemberCtx(user=user, member=None, preview=True)
    if user.role != membership_policy.MEMBER_ROLE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "not_a_member", "message": "This login does not open the Capital Partners portal."},
        )
    member = await member_svc.member_for_user(db, user.id)
    if member is None:
        # An invited user whose row was created before the member row existed.
        member = await member_svc.ensure_member_for_user(db, user, display_name=user.name)
        member = await member_svc.member_for_user(db, user.id)
    member_svc.refresh_status(member)
    if member.status == E.MemberStatus.SUSPENDED:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "suspended", "message": "This membership is suspended. Contact the desk."},
        )
    return MemberCtx(user=user, member=member, preview=False)


async def require_cleared_member(ctx: Annotated[MemberCtx, Depends(require_member)]) -> MemberCtx:
    if ctx.cleared:
        return ctx
    gate = member_svc.gate_state(ctx.member) if ctx.member else None
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={
            "code": "waiting_period" if ctx.member and ctx.member.status == E.MemberStatus.WAITING else "not_approved",
            "message": "Offerings are shown only after your accreditation is reviewed and the waiting period has passed.",
            "gate": {
                **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in (gate or {}).items() if k != "steps"},
                "steps": (gate or {}).get("steps", []),
            },
        },
    )


async def require_subscribing_member(ctx: Annotated[MemberCtx, Depends(require_cleared_member)]) -> MemberCtx:
    if not membership_policy.subscription_context_allowed(preview=ctx.preview):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "preview_only", "message": "Operators preview the portal; only members subscribe."},
        )
    return ctx


require_cp_admin = require_role(Role.SUPER_ADMIN)
CpAdmin = Annotated[User, Depends(require_cp_admin)]
Member = Annotated[MemberCtx, Depends(require_member)]
ClearedMember = Annotated[MemberCtx, Depends(require_cleared_member)]
SubscribingMember = Annotated[MemberCtx, Depends(require_subscribing_member)]
