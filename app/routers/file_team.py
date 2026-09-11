# ruff: noqa: B008
"""The team on a file, and its timeline.

Mounted before the application-profiles router on purpose: `/find` and
`/team/candidates` are literal paths that a `/{profile_id}` route would
otherwise try to parse as ids.

Reads are gated by `seat_or_visible` — the file's own visibility rules or a
seat on it. Writes (agents, underwriters, company) are the desk's. The client's view
of the timeline comes through the PIN room, never through a login-less id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.notification import Notification
from app.models.referral_partner_company import KIND_HOUSE, ReferralPartnerCompany
from app.models.user import User
from app.routers.application_profiles import _public_application_room, _require_underwriting_actor
from app.schemas.application_profile import ApplicationRoomAccess
from app.services import application_profiles as profiles
from app.services import file_events, file_team

router = APIRouter(prefix="/application-profiles", tags=["file-team"])

_SOURCE_KINDS = ("loan", "intake", "dealer", "deal", "bucket")


class UnderwriterAdd(BaseModel):
    user_id: UUID


class CompanySet(BaseModel):
    company_id: UUID | None = None


class TimelineRead(BaseModel):
    tier: str
    events: list[dict[str, Any]] = Field(default_factory=list)
    unread_count: int = 0


# ── helpers ─────────────────────────────────────────────────────────────────


def _is_operator(user: User) -> bool:
    return user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC)


async def _readable_profile(db: AsyncSession, profile_id: UUID, user: User) -> ApplicationProfile:
    profile = await db.get(ApplicationProfile, profile_id)
    if profile is None or not await file_team.seat_or_visible(db, profile, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application file not found")
    return profile


async def _desk_profile(db: AsyncSession, profile_id: UUID, user: User) -> ApplicationProfile:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_underwriting_actor(user)
    return profile


# ── find ────────────────────────────────────────────────────────────────────


@router.get("/find")
async def find_file(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    source_kind: str = Query(...),
    source_id: UUID = Query(...),
) -> dict[str, Any]:
    """The file behind a source the caller may see, without creating one.
    404 when the source is invisible or nobody has opened it as a file yet."""
    if source_kind not in _SOURCE_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown source kind")
    if source_kind == "bucket":
        profile = await profiles.find_profile(db, bucket_id=source_id)
        if profile is None or not await file_team.seat_or_visible(db, profile, user):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No file record yet")
        return {"id": str(profile.id)}
    source = await profiles._load_source(db, source_kind, source_id, user)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    profile = await profiles.find_profile(db, **{f"{source_kind}_id": source_id})
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No file record yet")
    return {"id": str(profile.id)}


# ── candidates ──────────────────────────────────────────────────────────────


@router.get("/team/candidates")
async def team_candidates(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not _is_operator(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    candidate_roles = file_team.AGENT_ROLES | file_team.UNDERWRITER_ROLES
    users = (
        await db.execute(
            select(User)
            .where(User.role.in_([r.value for r in candidate_roles]), User.deleted_at.is_(None))
            .order_by(User.name.asc())
        )
    ).scalars().all()
    companies = (
        await db.execute(
            select(ReferralPartnerCompany).where(ReferralPartnerCompany.kind != KIND_HOUSE).order_by(ReferralPartnerCompany.name.asc())
        )
    ).scalars().all()
    return {
        "agents": [
            {"user_id": str(u.id), "name": u.name, "email": u.email, "role": file_team._role_value(u.role)}
            for u in users
            if u.role in file_team.AGENT_ROLES
        ],
        "underwriters": [
            {"user_id": str(u.id), "name": u.name, "email": u.email, "role": file_team._role_value(u.role)}
            for u in users
            if u.role in file_team.UNDERWRITER_ROLES
        ],
        "companies": [{"id": str(c.id), "name": c.name, "kind": c.kind} for c in companies],
    }


# ── the team ────────────────────────────────────────────────────────────────


@router.get("/{profile_id}/team")
async def read_team(profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    profile = await _readable_profile(db, profile_id, user)
    # Only the desk's read persists a derived seat; everyone else reads it in memory.
    team = await file_team.team_for(db, profile, persist=_is_operator(user))
    if _is_operator(user):
        await db.commit()
    return {**file_team.team_read(team, for_client=not _is_operator(user) and file_events.tier_for_role(user.role) == file_events.VISIBILITY_CLIENT), "can_edit": _is_operator(user)}


@router.post("/{profile_id}/team/agents")
async def add_agent(profile_id: UUID, payload: UnderwriterAdd, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    profile = await _desk_profile(db, profile_id, user)
    team = await file_team.add_agent(db, profile, payload.user_id, user)
    await db.commit()
    return {**file_team.team_read(team, for_client=False), "can_edit": True}


@router.delete("/{profile_id}/team/agents/{user_id}")
async def remove_agent(profile_id: UUID, user_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    profile = await _desk_profile(db, profile_id, user)
    team = await file_team.remove_agent(db, profile, user_id, user)
    await db.commit()
    return {**file_team.team_read(team, for_client=False), "can_edit": True}


@router.post("/{profile_id}/team/underwriters")
async def add_underwriter(profile_id: UUID, payload: UnderwriterAdd, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    profile = await _desk_profile(db, profile_id, user)
    team = await file_team.add_underwriter(db, profile, payload.user_id, user)
    await db.commit()
    return {**file_team.team_read(team, for_client=False), "can_edit": True}


@router.delete("/{profile_id}/team/underwriters/{user_id}")
async def remove_underwriter(profile_id: UUID, user_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    profile = await _desk_profile(db, profile_id, user)
    team = await file_team.remove_underwriter(db, profile, user_id, user)
    await db.commit()
    return {**file_team.team_read(team, for_client=False), "can_edit": True}


@router.put("/{profile_id}/team/company")
async def set_company(profile_id: UUID, payload: CompanySet, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    profile = await _desk_profile(db, profile_id, user)
    team = await file_team.set_company(db, profile, payload.company_id, user)
    await db.commit()
    return {**file_team.team_read(team, for_client=False), "can_edit": True}


# ── the timeline ────────────────────────────────────────────────────────────


@router.get("/{profile_id}/timeline", response_model=TimelineRead)
async def read_timeline(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    before: datetime | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> TimelineRead:
    profile = await _readable_profile(db, profile_id, user)
    tier = file_events.tier_for_role(user.role)
    events = await file_events.list_events(db, profile.id, tier=tier, before=before, since=since, limit=limit)
    return TimelineRead(
        tier=tier,
        events=[file_events.event_read(e) for e in events],
        unread_count=await file_events.unread_count(db, user.id, profile.id),
    )


@router.get("/me/file-updates", response_model=TimelineRead)
async def my_file_updates(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    before: datetime | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> TimelineRead:
    """One feed across every file the caller is on, at their tier — the shape
    a mobile app or another platform reads instead of polling files one by one."""
    tier = file_events.tier_for_role(user.role)
    events = await file_events.list_events_for_user(db, user, before=before, since=since, limit=limit)
    unread = (
        await db.execute(
            select(Notification.id).where(
                Notification.recipient_user_id == user.id,
                Notification.target_type == file_events.TARGET_TYPE,
                Notification.read_at.is_(None),
            )
        )
    ).scalars().all()
    return TimelineRead(tier=tier, events=[file_events.event_read(e) for e in events], unread_count=len(unread))


@router.post("/{profile_id}/timeline/seen", status_code=status.HTTP_204_NO_CONTENT)
async def mark_timeline_seen(profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> None:
    profile = await _readable_profile(db, profile_id, user)
    await file_events.mark_seen(db, user.id, profile.id)
    await db.commit()


@router.post("/public/room/{token}/timeline", response_model=TimelineRead)
async def public_room_timeline(
    token: str,
    payload: ApplicationRoomAccess,
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> TimelineRead:
    """The client's tier of the file's timeline, from the PIN room."""
    _link, profile = await _public_application_room(db, token, payload.passcode, request)
    events = await file_events.list_events(db, profile.id, tier=file_events.VISIBILITY_CLIENT, limit=limit)
    return TimelineRead(tier=file_events.VISIBILITY_CLIENT, events=[file_events.event_read(e) for e in events], unread_count=0)
