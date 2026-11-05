"""Member lifecycle: provisioning, the questionnaire, review, the waiting period.

The status machine is the Rule 506(b) gate and lives here, not in a router:

    requested → questionnaire → under_review → waiting → approved
                                             ↘ (needs changes) → questionnaire
    any → suspended

``approved`` means the desk accepted the accreditation questionnaire AND the
waiting period has ended. ``gate_state`` is what both the portal and the admin
screen render; ``is_cleared`` is what every offering read checks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User

from .. import enums as E
from ..models import CpAccessRequest, CpIssuerSettings, CpMember, CpMemberEntity, CpQuestionnaire


async def issuer_settings(db: AsyncSession) -> CpIssuerSettings:
    row = (await db.execute(select(CpIssuerSettings).where(CpIssuerSettings.key == "default"))).scalar_one_or_none()
    if row is None:
        row = CpIssuerSettings(key="default")
        db.add(row)
        await db.flush()
    return row


async def member_for_user(db: AsyncSession, user_id: uuid.UUID) -> CpMember | None:
    return (
        await db.execute(
            select(CpMember).options(selectinload(CpMember.entities)).where(CpMember.user_id == user_id)
        )
    ).scalar_one_or_none()


async def ensure_member_for_user(
    db: AsyncSession,
    user: User,
    *,
    display_name: str | None = None,
    access_request: CpAccessRequest | None = None,
    invited_by_id: uuid.UUID | None = None,
) -> CpMember:
    """Create the pending member row for a capital-partner login, once.

    Called from app.deps on a portal sign-up and from the admin invite route.
    Idempotent: a second call returns the existing row.
    """
    existing = await member_for_user(db, user.id)
    if existing is not None:
        return existing
    member = CpMember(
        user_id=user.id,
        status=E.MemberStatus.REQUESTED,
        display_name=display_name or user.name,
        invited_by_id=invited_by_id,
        invited_at=datetime.now(UTC) if invited_by_id else None,
    )
    if access_request is not None:
        member.access_request_id = access_request.id
        member.investing_as = access_request.investing_as
        member.phone = access_request.phone
        member.capital_band = access_request.capital_band
        member.source = access_request.source
        if access_request.entity_name:
            member.entities.append(
                CpMemberEntity(
                    name=access_request.entity_name,
                    kind=E.EntityKind.FAMILY_OFFICE
                    if access_request.investing_as == E.InvestingAs.FAMILY_OFFICE
                    else E.EntityKind.OTHER,
                    is_default=True,
                )
            )
    db.add(member)
    await db.flush()
    return member


def waiting_period_end(started_at: datetime, days: int) -> datetime:
    return started_at + timedelta(days=max(0, int(days)))


def aware(value: datetime | None) -> datetime | None:
    """Postgres hands back aware datetimes; a naive one (SQLite in tests) is UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def is_cleared(member: CpMember | None, *, now: datetime | None = None) -> bool:
    """Approved and past the waiting period: offerings may be shown."""
    if member is None or member.status != E.MemberStatus.APPROVED:
        return False
    now = now or datetime.now(UTC)
    ends = aware(member.waiting_period_ends_at)
    return ends is None or ends <= now


def refresh_status(member: CpMember, *, now: datetime | None = None) -> None:
    """Move ``waiting`` to ``approved`` once the clock has run. Called on every
    read of the member so the row is never stale by more than one request."""
    now = now or datetime.now(UTC)
    ends = aware(member.waiting_period_ends_at)
    if member.status == E.MemberStatus.WAITING and ends and ends <= now:
        member.status = E.MemberStatus.APPROVED


def gate_state(member: CpMember, *, now: datetime | None = None) -> dict[str, Any]:
    """The four steps the portal and the public page draw, with real dates."""
    now = now or datetime.now(UTC)
    status = member.status
    done_q = status in {E.MemberStatus.UNDER_REVIEW, E.MemberStatus.WAITING, E.MemberStatus.APPROVED}
    approved = status == E.MemberStatus.APPROVED
    waiting = status == E.MemberStatus.WAITING
    ends = aware(member.waiting_period_ends_at)
    started = aware(member.relationship_established_at)
    days_left = None
    pct = None
    if waiting and ends and started:
        total = max((ends - started).total_seconds(), 1)
        elapsed = min(max((now - started).total_seconds(), 0), total)
        pct = round(100 * elapsed / total)
        days_left = max(0, (ends - now).days + (1 if (ends - now).seconds > 0 else 0))
    steps = [
        {"key": "request", "title": "Request access", "state": "done"},
        {
            "key": "questionnaire",
            "title": "Accreditation questionnaire",
            "state": "done" if done_q else ("now" if status in {E.MemberStatus.REQUESTED, E.MemberStatus.QUESTIONNAIRE} else "later"),
        },
        {
            "key": "review",
            "title": "Relationship review and waiting period",
            "state": "done" if approved else ("now" if status in {E.MemberStatus.UNDER_REVIEW, E.MemberStatus.WAITING} else "later"),
        },
        {"key": "access", "title": "Marketplace access", "state": "done" if approved else "later"},
    ]
    return {
        "status": status,
        "cleared": is_cleared(member, now=now),
        "steps": steps,
        "relationship_established_at": started,
        "waiting_period_days": member.waiting_period_days,
        "waiting_period_ends_at": ends,
        "waiting_days_left": days_left,
        "waiting_pct": pct,
        "suspended": status == E.MemberStatus.SUSPENDED,
    }


async def submit_questionnaire(
    db: AsyncSession,
    member: CpMember,
    *,
    answers: dict[str, Any],
    basis: str | None,
    investing_as: str | None,
    state_of_residence: str | None,
    entity_id: uuid.UUID | None,
    ip_address: str | None,
    user_agent: str | None,
) -> CpQuestionnaire:
    """A new evidence row every time; the member moves to under_review."""
    if member.status == E.MemberStatus.SUSPENDED:
        raise PermissionError("suspended")
    latest = (
        await db.execute(
            select(CpQuestionnaire.version)
            .where(CpQuestionnaire.member_id == member.id)
            .order_by(CpQuestionnaire.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    row = CpQuestionnaire(
        member_id=member.id,
        entity_id=entity_id,
        version=(latest or 0) + 1,
        answers=answers,
        basis=basis,
        investing_as=investing_as,
        state_of_residence=state_of_residence,
        submitted_at=datetime.now(UTC),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(row)
    await db.flush()
    member.last_questionnaire_id = row.id
    if investing_as:
        member.investing_as = investing_as
    if state_of_residence:
        member.state_of_residence = state_of_residence
    if basis:
        member.accreditation_basis = basis
    if member.status in {E.MemberStatus.REQUESTED, E.MemberStatus.QUESTIONNAIRE, E.MemberStatus.UNDER_REVIEW}:
        member.status = E.MemberStatus.UNDER_REVIEW
    await db.flush()
    return row


async def review_member(
    db: AsyncSession,
    member: CpMember,
    *,
    decision: str,
    reviewer_id: uuid.UUID,
    notes: str | None,
    relationship_established_at: datetime | None,
    waiting_period_days: int | None,
    accreditation_basis: str | None,
) -> CpMember:
    """approve | needs_changes | suspend | reinstate.

    Approval stamps the relationship date (defaults to now), computes the
    waiting-period end from the issuer default unless the desk sets the days,
    and parks the member in ``waiting``; ``refresh_status`` promotes it.
    """
    now = datetime.now(UTC)
    if decision == "approve":
        settings = await issuer_settings(db)
        started = relationship_established_at or now
        days = settings.default_waiting_period_days if waiting_period_days is None else int(waiting_period_days)
        member.relationship_established_at = started
        member.waiting_period_days = days
        member.waiting_period_ends_at = waiting_period_end(started, days)
        member.approved_at = now
        member.approved_by_id = reviewer_id
        if accreditation_basis:
            member.accreditation_basis = accreditation_basis
        member.status = E.MemberStatus.WAITING
        refresh_status(member, now=now)
        if member.last_questionnaire_id:
            q = await db.get(CpQuestionnaire, member.last_questionnaire_id)
            if q is not None and q.outcome == E.QuestionnaireOutcome.PENDING:
                q.outcome = E.QuestionnaireOutcome.ACCEPTED
                q.reviewed_at = now
                q.reviewed_by_id = reviewer_id
                q.review_notes = notes
    elif decision == "needs_changes":
        member.status = E.MemberStatus.QUESTIONNAIRE
        if member.last_questionnaire_id:
            q = await db.get(CpQuestionnaire, member.last_questionnaire_id)
            if q is not None:
                q.outcome = E.QuestionnaireOutcome.NEEDS_CHANGES
                q.reviewed_at = now
                q.reviewed_by_id = reviewer_id
                q.review_notes = notes
    elif decision == "suspend":
        member.status = E.MemberStatus.SUSPENDED
        member.suspended_at = now
        member.suspended_reason = notes
    elif decision == "reinstate":
        member.suspended_at = None
        member.suspended_reason = None
        member.status = E.MemberStatus.WAITING if member.approved_at else E.MemberStatus.REQUESTED
        refresh_status(member, now=now)
    else:
        raise ValueError(f"unknown decision {decision!r}")
    if notes:
        member.review_notes = notes
    await db.flush()
    return member
