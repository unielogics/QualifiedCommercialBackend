"""Who is on a file.

Any number of agent and underwriter seats, plus one company. The first agent
is still derived from the ownership pointers the system already keeps and
moves with the existing reassign actions. The desk can add collaborators
without replacing that primary ownership source. Underwriters and the
company are also set by the desk.

Seats decide who is told about the file's timeline and who may read it
(`seat_or_visible`). They never widen what a person may open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerBusiness
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.broker import Broker
from app.models.client import Client
from app.models.file_team_member import SEAT_AGENT, SEAT_UNDERWRITER, FileTeamMember
from app.models.loan import Loan
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.referral_partner_company import KIND_HOUSE, ReferralPartnerCompany
from app.models.user import User
from app.services import application_profiles as profiles

log = logging.getLogger(__name__)

AGENT_ROLES = frozenset({Role.BROKER, Role.FIELD_REP, Role.DEALER_PARTNER})
UNDERWRITER_ROLES = frozenset({Role.LOAN_EXEC, Role.SUPER_ADMIN})
DESK_ROLES = UNDERWRITER_ROLES


@dataclass
class Member:
    user_id: UUID
    name: str | None
    email: str | None
    role: str
    seat: str
    derived_from: str | None = None


@dataclass
class CompanyRef:
    id: UUID
    name: str
    kind: str
    notice_email: str | None
    derived: bool


@dataclass
class Team:
    agent: Member | None = None
    agents: list[Member] = field(default_factory=list)
    underwriters: list[Member] = field(default_factory=list)
    company: CompanyRef | None = None

    def agent_members(self) -> list[Member]:
        agents = list(self.agents)
        if self.agent and all(member.user_id != self.agent.user_id for member in agents):
            agents.insert(0, self.agent)
        return agents

    def user_ids(self, *, agent: bool = True, underwriters: bool = True) -> set[UUID]:
        ids: set[UUID] = set()
        if agent:
            ids.update(member.user_id for member in self.agent_members())
        if underwriters:
            ids.update(m.user_id for m in self.underwriters)
        return ids


def _role_value(role: Any) -> str:
    return role.value if hasattr(role, "value") else str(role)


def _member(user: User, seat: str, derived_from: str | None = None) -> Member:
    return Member(user_id=user.id, name=user.name, email=user.email, role=_role_value(user.role), seat=seat, derived_from=derived_from)


async def _live_user(db: AsyncSession, user_id: UUID | None) -> User | None:
    if user_id is None:
        return None
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        return None
    return user


# ── derivation ──────────────────────────────────────────────────────────────


async def derive_agent(db: AsyncSession, profile: ApplicationProfile) -> tuple[UUID | None, str | None]:
    """The agent the ownership pointers already name, and which pointer.

    Order: the dealer partner on an intake, the field rep on a dealer file, the
    client's current agent, the broker on the loan or client, and finally the
    person who created the intake when they are an agent role. Nothing here
    is written; `team_for(persist=True)` does that.
    """
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    if intake is not None and intake.broker_id and await _live_user(db, intake.broker_id):
        return intake.broker_id, "intake.broker_id"
    dealer = await db.get(DealerBusiness, profile.dealer_id) if profile.dealer_id else None
    if dealer is not None and dealer.owner_user_id and await _live_user(db, dealer.owner_user_id):
        return dealer.owner_user_id, "dealer.owner_user_id"
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    if client is not None and client.current_agent_id and await _live_user(db, client.current_agent_id):
        return client.current_agent_id, "client.current_agent_id"
    loan = await db.get(Loan, profile.loan_id) if profile.loan_id else None
    for broker_id, label in (
        (loan.broker_id if loan is not None else None, "loan.broker_id"),
        (client.broker_id if client is not None else None, "client.broker_id"),
    ):
        if broker_id:
            broker = await db.get(Broker, broker_id)
            if broker is not None and broker.user_id and await _live_user(db, broker.user_id):
                return broker.user_id, label
    if intake is not None and intake.source_user_id:
        creator = await _live_user(db, intake.source_user_id)
        if creator is not None and creator.role in AGENT_ROLES:
            return creator.id, "intake.source_user_id"
    return None, None


async def derive_company(db: AsyncSession, agent_user_id: UUID | None) -> ReferralPartnerCompany | None:
    """The agent's referral partner company; the house is transparent."""
    user = await _live_user(db, agent_user_id)
    if user is None or not user.referral_partner_company_id:
        return None
    company = await db.get(ReferralPartnerCompany, user.referral_partner_company_id)
    if company is None or company.kind == KIND_HOUSE:
        return None
    return company


# ── reading ─────────────────────────────────────────────────────────────────


async def _seat_rows(db: AsyncSession, profile_id: UUID) -> list[FileTeamMember]:
    return list(
        (
            await db.execute(
                select(FileTeamMember)
                .where(FileTeamMember.profile_id == profile_id)
                .order_by(FileTeamMember.created_at.asc())
            )
        ).scalars().all()
    )


async def team_for(db: AsyncSession, profile: ApplicationProfile, *, persist: bool = False) -> Team:
    """Read a file team and keep its ownership-derived primary agent current."""
    team = Team()
    rows = await _seat_rows(db, profile.id)
    agent_rows = [row for row in rows if row.seat == SEAT_AGENT]
    derived_rows = [row for row in agent_rows if row.derived_from is not None]
    if derived_rows and not persist:
        derived_user_id = derived_rows[0].user_id
        derived_source = derived_rows[0].derived_from
    else:
        derived_user_id, derived_source = await derive_agent(db, profile)

    if persist:
        target = next((row for row in agent_rows if row.user_id == derived_user_id), None)
        stale = [row for row in derived_rows if row is not target]
        for row in stale:
            await db.delete(row)
        if stale:
            await db.flush()
        if derived_user_id is not None:
            if target is None:
                target = FileTeamMember(
                    profile_id=profile.id,
                    user_id=derived_user_id,
                    seat=SEAT_AGENT,
                    derived_from=derived_source,
                )
                db.add(target)
                agent_rows.append(target)
            else:
                target.derived_from = derived_source
            await db.flush()

    primary: Member | None = None
    if derived_user_id is not None:
        user = await _live_user(db, derived_user_id)
        if user is not None:
            primary = _member(user, SEAT_AGENT, derived_source)
            team.agents.append(primary)

    for row in agent_rows:
        if row.user_id == derived_user_id or row in derived_rows:
            continue
        user = await _live_user(db, row.user_id)
        if user is not None:
            team.agents.append(_member(user, SEAT_AGENT, row.derived_from))
    team.agent = primary or (team.agents[0] if team.agents else None)

    for row in rows:
        if row.seat != SEAT_UNDERWRITER:
            continue
        user = await _live_user(db, row.user_id)
        if user is not None:
            team.underwriters.append(_member(user, SEAT_UNDERWRITER))

    company: ReferralPartnerCompany | None = None
    derived = profile.company_set_by_user_id is None
    if profile.company_id:
        company = await db.get(ReferralPartnerCompany, profile.company_id)
    elif derived and team.agent is not None:
        company = await derive_company(db, team.agent.user_id)
        if company is not None and persist:
            profile.company_id = company.id
            await db.flush()
    if company is not None:
        team.company = CompanyRef(id=company.id, name=company.name, kind=company.kind, notice_email=company.notice_email, derived=derived)
    return team


async def holds_seat(db: AsyncSession, profile_id: UUID, user_id: UUID) -> bool:
    row = (
        await db.execute(
            select(FileTeamMember.id)
            .where(FileTeamMember.profile_id == profile_id, FileTeamMember.user_id == user_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def seat_or_visible(db: AsyncSession, profile: ApplicationProfile, user: User) -> bool:
    """The gate for the roster and the timeline only: the file's own
    visibility rules, or a seat on it."""
    if await profiles._profile_is_visible(db, profile, user):
        return True
    if user.role == Role.VENDOR:
        return False
    return await holds_seat(db, profile.id, user.id)


def team_read(team: Team, *, for_client: bool) -> dict[str, Any]:
    """The roster as an API shape. Clients receive agent names only."""
    agents = team.agent_members()
    if for_client:
        return {
            "agent": {"name": agents[0].name} if agents else None,
            "agents": [{"name": member.name} for member in agents],
            "underwriters": [],
            "company": None,
        }
    return {
        "agent": (
            {
                "user_id": str(agents[0].user_id),
                "name": agents[0].name,
                "email": agents[0].email,
                "role": agents[0].role,
                "derived_from": agents[0].derived_from,
            }
            if agents
            else None
        ),
        "agents": [
            {
                "user_id": str(member.user_id),
                "name": member.name,
                "email": member.email,
                "role": member.role,
                "derived_from": member.derived_from,
            }
            for member in agents
        ],
        "underwriters": [
            {"user_id": str(m.user_id), "name": m.name, "email": m.email, "role": m.role} for m in team.underwriters
        ],
        "company": (
            {"id": str(team.company.id), "name": team.company.name, "kind": team.company.kind, "derived": team.company.derived}
            if team.company
            else None
        ),
    }


# ── writing ─────────────────────────────────────────────────────────────────


async def _emit_team_changed(db: AsyncSession, profile: ApplicationProfile, *, title: str, actor: User | None, meta: dict[str, Any]) -> None:
    from app.services import file_events

    await file_events.emit(
        db,
        profile=profile,
        kind="team.changed",
        visibility=file_events.VISIBILITY_TEAM,
        title=title,
        actor=actor,
        target_type="file_team",
        target_id=str(profile.id),
        meta=meta,
    )


async def refresh_agent_seat(db: AsyncSession, profile: ApplicationProfile, *, actor: User | None = None) -> tuple[UUID | None, UUID | None]:
    """Refresh only the ownership-derived primary; manual agents remain."""
    rows = await _seat_rows(db, profile.id)
    agent_rows = [row for row in rows if row.seat == SEAT_AGENT]
    derived_rows = [row for row in agent_rows if row.derived_from is not None]
    previous = derived_rows[0] if derived_rows else None
    previous_id = previous.user_id if previous is not None else None
    user_id, source = await derive_agent(db, profile)
    target = next((row for row in agent_rows if row.user_id == user_id), None)
    stale = [row for row in derived_rows if row is not target]
    for row in stale:
        await db.delete(row)
    if stale:
        await db.flush()
    if user_id is not None:
        if target is None:
            db.add(FileTeamMember(profile_id=profile.id, user_id=user_id, seat=SEAT_AGENT, derived_from=source, assigned_by_user_id=actor.id if actor else None))
        else:
            target.derived_from = source
        await db.flush()
    if profile.company_set_by_user_id is None:
        company = await derive_company(db, user_id)
        profile.company_id = company.id if company is not None else None
    if previous_id == user_id:
        return previous_id, user_id
    user = await _live_user(db, user_id)
    await _emit_team_changed(
        db,
        profile,
        title=f"{user.name or user.email} is now the agent on this file" if user else "The agent on this file was cleared",
        actor=actor,
        meta={"seat": SEAT_AGENT, "previous_user_id": str(previous_id) if previous_id else None, "user_id": str(user_id) if user_id else None, "derived_from": source},
    )
    return previous_id, user_id


async def add_agent(db: AsyncSession, profile: ApplicationProfile, user_id: UUID, actor: User) -> Team:
    user = await _live_user(db, user_id)
    if user is None or user.role not in AGENT_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick an active broker, field rep, or dealer partner")
    existing = (
        await db.execute(
            select(FileTeamMember).where(
                FileTeamMember.profile_id == profile.id,
                FileTeamMember.user_id == user_id,
                FileTeamMember.seat == SEAT_AGENT,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(FileTeamMember(profile_id=profile.id, user_id=user_id, seat=SEAT_AGENT, assigned_by_user_id=actor.id))
        await db.flush()
        await profiles.log_profile_action(
            db,
            profile,
            actor,
            "file_team.agent_added",
            f"Added {user.name or user.email} as an agent",
            target_type="file_team",
            target_id=user_id,
        )
        await _emit_team_changed(
            db,
            profile,
            title=f"{user.name or user.email} was added as an agent",
            actor=actor,
            meta={"seat": SEAT_AGENT, "user_id": str(user_id)},
        )
    return await team_for(db, profile, persist=True)


async def remove_agent(db: AsyncSession, profile: ApplicationProfile, user_id: UUID, actor: User) -> Team:
    row = (
        await db.execute(
            select(FileTeamMember).where(
                FileTeamMember.profile_id == profile.id,
                FileTeamMember.user_id == user_id,
                FileTeamMember.seat == SEAT_AGENT,
            )
        )
    ).scalar_one_or_none()
    if row is not None and row.derived_from is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This is the ownership-derived primary agent. Change the file's ownership assignment instead.",
        )
    if row is not None:
        user = await db.get(User, user_id)
        label = (user.name or user.email) if user else "An agent"
        await db.delete(row)
        await db.flush()
        await profiles.log_profile_action(
            db,
            profile,
            actor,
            "file_team.agent_removed",
            f"Removed {label} as an agent",
            target_type="file_team",
            target_id=user_id,
        )
        await _emit_team_changed(
            db,
            profile,
            title=f"{label} was removed as an agent",
            actor=actor,
            meta={"seat": SEAT_AGENT, "user_id": str(user_id), "removed": True},
        )
    return await team_for(db, profile, persist=True)


async def add_underwriter(db: AsyncSession, profile: ApplicationProfile, user_id: UUID, actor: User) -> Team:
    user = await _live_user(db, user_id)
    if user is None or user.role not in UNDERWRITER_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick an underwriter or super admin on the team")
    existing = (
        await db.execute(
            select(FileTeamMember).where(
                FileTeamMember.profile_id == profile.id,
                FileTeamMember.user_id == user_id,
                FileTeamMember.seat == SEAT_UNDERWRITER,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(FileTeamMember(profile_id=profile.id, user_id=user_id, seat=SEAT_UNDERWRITER, assigned_by_user_id=actor.id))
        await db.flush()
        await profiles.log_profile_action(
            db, profile, actor, "file_team.underwriter_added", f"Added {user.name or user.email} as an underwriter",
            target_type="file_team", target_id=user_id,
        )
        await _emit_team_changed(db, profile, title=f"{user.name or user.email} was added as an underwriter", actor=actor, meta={"seat": SEAT_UNDERWRITER, "user_id": str(user_id)})
    return await team_for(db, profile, persist=True)


async def remove_underwriter(db: AsyncSession, profile: ApplicationProfile, user_id: UUID, actor: User) -> Team:
    row = (
        await db.execute(
            select(FileTeamMember).where(
                FileTeamMember.profile_id == profile.id,
                FileTeamMember.user_id == user_id,
                FileTeamMember.seat == SEAT_UNDERWRITER,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        user = await db.get(User, user_id)
        label = (user.name or user.email) if user else "An underwriter"
        await db.delete(row)
        await db.flush()
        await profiles.log_profile_action(
            db, profile, actor, "file_team.underwriter_removed", f"Removed {label} as an underwriter",
            target_type="file_team", target_id=user_id,
        )
        await _emit_team_changed(db, profile, title=f"{label} was removed as an underwriter", actor=actor, meta={"seat": SEAT_UNDERWRITER, "user_id": str(user_id), "removed": True})
    return await team_for(db, profile, persist=True)


async def set_company(db: AsyncSession, profile: ApplicationProfile, company_id: UUID | None, actor: User) -> Team:
    company: ReferralPartnerCompany | None = None
    if company_id is not None:
        company = await db.get(ReferralPartnerCompany, company_id)
        if company is None or company.kind == KIND_HOUSE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick a referral partner company; the house is never a company on a file")
    if profile.company_id == company_id and profile.company_set_by_user_id is not None:
        return await team_for(db, profile, persist=True)
    profile.company_id = company_id
    profile.company_set_by_user_id = actor.id
    await db.flush()
    label = company.name if company else "no company"
    await profiles.log_profile_action(
        db, profile, actor, "file_team.company_set", f"Set the company on the file to {label}",
        target_type="file_team", target_id=company_id or profile.id,
    )
    await _emit_team_changed(db, profile, title=f"Company on the file: {label}", actor=actor, meta={"company_id": str(company_id) if company_id else None})
    return await team_for(db, profile, persist=True)
