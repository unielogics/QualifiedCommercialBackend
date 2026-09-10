"""The file's timeline: one line per thing that happened, and who is told.

`emit` is the only writer. A hook at a write path calls it after the write,
inside the same transaction; it appends a `file_events` row, tells the
seats in-app and by push at once through `notify_users`, and leaves the row
`pending` for the one-minute drain, which batches email notices into one
per person per file. It runs inside a savepoint and swallows its own errors,
so a failure in the timeline can never fail the write it describes — and it
never commits.

Tiers: `client` is read by everyone on the file including the client; `team`
by the agent, the underwriters, the company (by email) and the desk; `desk`
by underwriters and the desk. Rows carry what happened, never what was said.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.file_event import (
    NOTICE_NONE,
    NOTICE_PENDING,
    NOTICE_SENT,
    NOTICE_SKIPPED,
    VISIBILITIES,
    VISIBILITY_CLIENT,
    VISIBILITY_DESK,
    VISIBILITY_TEAM,
    FileEvent,
)
from app.models.notification import Notification
from app.models.user import User
from app.services import file_contacts, file_links, file_team
from app.services.application_profiles import find_profile

log = logging.getLogger(__name__)

TARGET_TYPE = "file_event"
DRAIN_DELAY = timedelta(seconds=90)

_TIER_RANK = {VISIBILITY_CLIENT: 0, VISIBILITY_TEAM: 1, VISIBILITY_DESK: 2}


def tier_for_role(role: Any) -> str:
    value = role.value if hasattr(role, "value") else str(role)
    if value in (Role.SUPER_ADMIN.value, Role.LOAN_EXEC.value):
        return VISIBILITY_DESK
    if value in (Role.BROKER.value, Role.FIELD_REP.value, Role.DEALER_PARTNER.value, Role.REGIONAL_MANAGER.value):
        return VISIBILITY_TEAM
    return VISIBILITY_CLIENT


def visible_at(tier: str) -> list[str]:
    rank = _TIER_RANK.get(tier, 0)
    return [v for v, r in _TIER_RANK.items() if r <= rank]


# ── writing ─────────────────────────────────────────────────────────────────


async def emit(
    db: AsyncSession,
    *,
    kind: str,
    visibility: str,
    title: str,
    profile: ApplicationProfile | None = None,
    loan_id: UUID | None = None,
    intake_id: UUID | None = None,
    dealer_id: UUID | None = None,
    deal_id: UUID | None = None,
    bucket_id: UUID | None = None,
    body: str | None = None,
    actor: Any = None,
    actor_label: str | None = None,
    target_type: str | None = None,
    target_id: Any = None,
    meta: dict[str, Any] | None = None,
    already_notified: Any = (),
) -> FileEvent | None:
    """Append one line to the file's timeline and tell the seats.

    `actor` is a User or a user id; the actor is never notified. Ids in
    `already_notified` are people a legacy fan-out at the same site already
    reached, so they are not told twice. Returns None — silently, at debug —
    when the source has no file yet, and never raises.
    """
    if visibility not in VISIBILITIES:
        raise ValueError(f"unknown visibility {visibility!r}")
    try:
        async with db.begin_nested():
            if profile is None:
                profile = await find_profile(
                    db, loan_id=loan_id, intake_id=intake_id, dealer_id=dealer_id, deal_id=deal_id, bucket_id=bucket_id
                )
            if profile is None:
                log.debug("file_events.emit: no profile for %s (loan=%s intake=%s dealer=%s bucket=%s)", kind, loan_id, intake_id, dealer_id, bucket_id)
                return None
            actor_id = _actor_id(actor)
            label = actor_label or _actor_label(actor)
            row = FileEvent(
                profile_id=profile.id,
                kind=kind,
                visibility=visibility,
                actor_user_id=actor_id,
                actor_label=(label or None) and label[:120],
                title=title[:200],
                body=body,
                target_type=target_type,
                target_id=str(target_id)[:80] if target_id is not None else None,
                meta=meta or {},
                notice_status=NOTICE_NONE if visibility == VISIBILITY_DESK else NOTICE_PENDING,
            )
            db.add(row)
            await db.flush()
            await _notify(db, profile, row, actor_id=actor_id, already_notified={i for i in already_notified if i})
            return row
    except Exception:  # noqa: BLE001
        log.exception("file_events.emit failed kind=%s", kind)
        return None


def _actor_id(actor: Any) -> UUID | None:
    if actor is None:
        return None
    if isinstance(actor, UUID):
        return actor
    return getattr(actor, "id", None)


def _actor_label(actor: Any) -> str | None:
    if actor is None or isinstance(actor, UUID):
        return None
    return getattr(actor, "name", None) or getattr(actor, "email", None)


async def _notify(
    db: AsyncSession, profile: ApplicationProfile, event: FileEvent, *, actor_id: UUID | None, already_notified: set[UUID]
) -> None:
    from app.services.notifications import notify_users

    team = await file_team.team_for(db, profile)
    sources = await file_contacts.load_sources(db, profile)
    business = file_contacts.business_label(sources)
    recipients: set[UUID] = set()
    if event.visibility == VISIBILITY_CLIENT:
        client = await file_contacts.client_recipient(db, profile, sources)
        if client.user_id:
            recipients.add(client.user_id)
        recipients |= team.user_ids()
    elif event.visibility == VISIBILITY_TEAM:
        recipients |= team.user_ids()
    else:
        # Desk tier: the underwriters on the file. With none assigned the row
        # stays on the timeline and nobody is pinged — the desk already hears
        # about every file through the upload broadcast and the digest.
        recipients |= team.user_ids(agent=False)
    recipients.discard(actor_id)  # type: ignore[arg-type]
    recipients -= already_notified
    if not recipients:
        return
    users = (
        await db.execute(select(User).where(User.id.in_(recipients), User.deleted_at.is_(None)))
    ).scalars().all()
    groups: dict[str, set[UUID]] = {}
    for user in users:
        groups.setdefault(file_links.audience_for_role(user.role), set()).add(user.id)
    for audience, ids in groups.items():
        await notify_users(
            db,
            recipient_ids=ids,
            event_type=f"file.{event.kind}",
            title=event.title,
            body=event.body or business,
            category="file",
            priority="medium",
            target_type=TARGET_TYPE,
            target_id=str(profile.id),
            deep_link=file_links.for_audience(profile, audience),
            meta={"profile_id": str(profile.id), "kind": event.kind, "event_id": str(event.id)},
            batch_key=f"file:{profile.id}",
            push=event.visibility != VISIBILITY_DESK,
            email=False,
            actor_user_id=actor_id,
        )
    # One stream event naming the file, for any client that keeps a timeline
    # open — a console today, a mobile app or another platform tomorrow.
    from app.services.communication_events import publish_communication_event

    await publish_communication_event(
        db,
        recipient_user_ids={u.id for u in users},
        event_type="file_event.created",
        profile_id=profile.id,
        dealer_id=profile.dealer_id,
    )


# ── reading ─────────────────────────────────────────────────────────────────


async def list_events(
    db: AsyncSession,
    profile_id: UUID,
    *,
    tier: str,
    before: datetime | None = None,
    since: datetime | None = None,
    limit: int = 50,
) -> list[FileEvent]:
    """Newest first. `before` pages back; `since` fetches what arrived after
    a timestamp, for a client that polls."""
    stmt = (
        select(FileEvent)
        .where(FileEvent.profile_id == profile_id, FileEvent.visibility.in_(visible_at(tier)))
        .order_by(FileEvent.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    if before is not None:
        stmt = stmt.where(FileEvent.created_at < before)
    if since is not None:
        stmt = stmt.where(FileEvent.created_at > since)
    return list((await db.execute(stmt)).scalars().all())


async def profile_ids_for_user(db: AsyncSession, user: User) -> set[UUID]:
    """Every file this person is on: their seats, plus — for a client login —
    the files whose client record is theirs."""
    from app.models.client import Client
    from app.models.file_team_member import FileTeamMember

    ids: set[UUID] = set(
        (await db.execute(select(FileTeamMember.profile_id).where(FileTeamMember.user_id == user.id))).scalars().all()
    )
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role == Role.CLIENT.value:
        client_ids = list((await db.execute(select(Client.id).where(Client.user_id == user.id))).scalars().all())
        if client_ids:
            ids.update(
                (await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.client_id.in_(client_ids)))).scalars().all()
            )
    elif role == Role.DEALER.value:
        from app.dealer_os.models import DealerBusiness

        dealer_ids = list((await db.execute(select(DealerBusiness.id).where(DealerBusiness.dealer_user_id == user.id))).scalars().all())
        if dealer_ids:
            ids.update(
                (await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.dealer_id.in_(dealer_ids)))).scalars().all()
            )
    return ids


async def list_events_for_user(
    db: AsyncSession, user: User, *, before: datetime | None = None, since: datetime | None = None, limit: int = 50
) -> list[FileEvent]:
    """The person's feed across every file they are on, at their tier."""
    profile_ids = await profile_ids_for_user(db, user)
    if not profile_ids:
        return []
    stmt = (
        select(FileEvent)
        .where(FileEvent.profile_id.in_(profile_ids), FileEvent.visibility.in_(visible_at(tier_for_role(user.role))))
        .order_by(FileEvent.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    if before is not None:
        stmt = stmt.where(FileEvent.created_at < before)
    if since is not None:
        stmt = stmt.where(FileEvent.created_at > since)
    return list((await db.execute(stmt)).scalars().all())


async def unread_count(db: AsyncSession, user_id: UUID, profile_id: UUID) -> int:
    rows = (
        await db.execute(
            select(Notification.id).where(
                Notification.recipient_user_id == user_id,
                Notification.target_type == TARGET_TYPE,
                Notification.target_id == str(profile_id),
                Notification.read_at.is_(None),
            )
        )
    ).scalars().all()
    return len(rows)


async def mark_seen(db: AsyncSession, user_id: UUID, profile_id: UUID) -> None:
    await db.execute(
        update(Notification)
        .where(
            Notification.recipient_user_id == user_id,
            Notification.target_type == TARGET_TYPE,
            Notification.target_id == str(profile_id),
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    await db.flush()


#: The wire shape's version. Bump when a field changes meaning, never for an
#: added optional field, so a mobile app can pin what it understands.
SCHEMA = "file_event.v1"


def event_read(event: FileEvent) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "id": str(event.id),
        "profile_id": str(event.profile_id),
        "kind": event.kind,
        "visibility": event.visibility,
        "title": event.title,
        "body": event.body,
        "actor_label": event.actor_label,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "meta": event.meta or {},
        "created_at": event.created_at,
    }


# ── the email drain ─────────────────────────────────────────────────────────


@dataclass
class _Recipient:
    kind: str  # agent | underwriter | client | company
    email: str
    name: str | None
    tier: str
    user_id: UUID | None
    titles_only: bool
    link: str | None


async def _settings(db: AsyncSession):
    from app.models.app_settings import AppSettings
    from app.schemas.settings import AppSettingsData

    row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    return AppSettingsData.model_validate(row.data if row else {}).file_updates


async def _room_link(db: AsyncSession, profile: ApplicationProfile) -> str | None:
    if not profile.primary_bucket_id:
        return None
    from app.dealer_os.services import client_room

    link = await client_room.active_link(db, profile.primary_bucket_id)
    return file_links.room_link(link.token) if link is not None and link.token else None


async def _recipients_for_email(db: AsyncSession, profile: ApplicationProfile, team: file_team.Team, sources, settings) -> list[_Recipient]:
    out: list[_Recipient] = []
    if settings.team_email_enabled:
        if team.agent and team.agent.email:
            out.append(_Recipient("agent", team.agent.email, team.agent.name, VISIBILITY_TEAM, team.agent.user_id, False, file_links.for_audience(profile, file_links.audience_for_role(team.agent.role))))
        for member in team.underwriters:
            if member.email:
                out.append(_Recipient("underwriter", member.email, member.name, VISIBILITY_DESK, member.user_id, False, file_links.for_audience(profile, file_links.AUDIENCE_DESK)))
    if settings.client_email_enabled:
        client = await file_contacts.client_recipient(db, profile, sources)
        email = client.email
        link: str | None
        if client.user_id:
            user = await db.get(User, client.user_id)
            if user is not None and user.deleted_at is None and user.email:
                email = user.email
            link = file_links.for_audience(profile, file_links.AUDIENCE_CLIENT)
        else:
            link = await _room_link(db, profile)
        if email:
            out.append(_Recipient("client", email, client.name, VISIBILITY_CLIENT, client.user_id, False, link))
    if settings.company_email_enabled and team.company and team.company.notice_email:
        out.append(_Recipient("company", team.company.notice_email, team.company.name, VISIBILITY_TEAM, None, True, None))
    return out


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _fmt_when(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%b %d, %I:%M %p UTC")


def render_digest(business: str, events: list[FileEvent], *, titles_only: bool, link: str | None) -> tuple[str, str, str]:
    """(subject, text, html) — what happened, in order, and where to look."""
    count = len(events)
    subject = f"{count} update{'s' if count != 1 else ''} on {business}"
    ordered = sorted(events, key=lambda e: e.created_at)
    lines: list[str] = []
    items: list[str] = []
    for event in ordered:
        line = f"{_fmt_when(event.created_at)} — {event.title}"
        detail = "" if titles_only or not event.body else f"\n    {event.body}"
        lines.append(f"• {line}{detail}")
        items.append(
            f"<li><strong>{html.escape(event.title)}</strong> <span style=\"color:#64748b\">{html.escape(_fmt_when(event.created_at))}</span>"
            + ("" if titles_only or not event.body else f"<br><span>{html.escape(event.body)}</span>")
            + "</li>"
        )
    footer = f"Open the file: {link}" if link else "Reach your Qualified Commercial contact for anything that needs a reply."
    text = f"Updates on {business}:\n\n" + "\n".join(lines) + f"\n\n{footer}\n"
    html_body = (
        f"<p>Updates on <strong>{html.escape(business)}</strong>:</p><ul>{''.join(items)}</ul>"
        + (f'<p><a href="{html.escape(link)}">Open the file</a></p>' if link else f"<p style=\"color:#64748b\">{html.escape(footer)}</p>")
    )
    return subject, text, html_body


async def drain_notices(db: AsyncSession, *, limit: int = 300) -> int:
    """Batch pending events into one email per person per file. Returns the
    number of emails accepted. Marks events in the same transaction as the
    outbox rows, so a double-fire duplicates a notice rather than losing one."""
    from app.services.messaging.outbox import Draft, Subject, deliver_email

    settings = await _settings(db)
    cutoff = datetime.now(UTC) - DRAIN_DELAY
    events = list(
        (
            await db.execute(
                select(FileEvent)
                .where(FileEvent.notice_status == NOTICE_PENDING, FileEvent.created_at <= cutoff)
                .order_by(FileEvent.created_at.asc())
                .limit(limit)
            )
        ).scalars().all()
    )
    by_profile: dict[UUID, list[FileEvent]] = {}
    for event in events:
        by_profile.setdefault(event.profile_id, []).append(event)
    accepted = 0
    for profile_id, batch in by_profile.items():
        now = datetime.now(UTC)
        try:
            profile = await db.get(ApplicationProfile, profile_id)
            outcomes: list[dict[str, Any]] = []
            if profile is None:
                outcomes.append({"kind": "none", "ok": False, "detail": "no profile"})
            else:
                team = await file_team.team_for(db, profile)
                sources = await file_contacts.load_sources(db, profile)
                business = file_contacts.business_label(sources)
                for recipient in await _recipients_for_email(db, profile, team, sources, settings):
                    visible = [e for e in batch if e.visibility in visible_at(recipient.tier)]
                    if len(visible) == 1 and recipient.user_id and visible[0].actor_user_id == recipient.user_id:
                        continue
                    if not visible:
                        continue
                    subject, text, html_body = render_digest(business, visible, titles_only=recipient.titles_only, link=recipient.link)
                    outcome = await deliver_email(
                        db,
                        Draft(to=recipient.email, subject=subject, body_text=text, body_html=html_body),
                        context="file_updates",
                        template_key="file_updates_digest",
                        subject=Subject(
                            profile_id=profile.id,
                            intake_id=profile.intake_id,
                            loan_id=profile.loan_id,
                            dealer_id=profile.dealer_id,
                            client_id=profile.client_id,
                        ),
                    )
                    outcomes.append({"kind": recipient.kind, "to": _mask(recipient.email), "ok": outcome.ok, "detail": (outcome.detail or "")[:120], "events": len(visible)})
                    if outcome.ok:
                        accepted += 1
            any_ok = any(o.get("ok") for o in outcomes)
            for event in batch:
                event.notice_status = NOTICE_SENT if any_ok else NOTICE_SKIPPED
                event.notice_recipients = outcomes
                event.notice_at = now
            await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("file_updates drain failed profile=%s", profile_id)
            await db.rollback()
    return accepted
