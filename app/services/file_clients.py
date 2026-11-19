"""Every file has a client, and a client can be read back as its files.

A file — an AI intake under Files — is somebody's request for funding, and the
client record is that somebody. Most of the ways a file is opened already
found or made one, each with its own copy of the rule; a foreclosure rescue
never did, and a file opened by the desk without one was given a brand-new
record even when the person was already in the book. This module is the one
rule those paths share, the repair that applies it to files that predate it,
and the read side the client screen uses: the files that belong to a client
and every text and email exchanged about them.

The match is deliberately narrow. A client is the same person only when the
email is the same; a shared phone number or a similar name never joins two
records. When several records carry the email, the one that came from the same
kind of intake wins, then the newest — the rule `_find_or_create_client` has
always applied, so a file opened today and a file repaired tonight land on the
same record.

A professional who submits a foreclosure rescue for an owner is not the
client. The client is the owner they named, and the record says the firm may
not reach out to them directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket
from app.models.client import Client
from app.models.file_event import FileEvent
from app.models.message_send import MessageSend
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.sms_message import SmsMessage
from app.models.user import User

log = logging.getLogger(__name__)

#: Who may read a client's files and the messages about them: the desk. An
#: agent's own book keeps the screens it already had.
DESK_ROLES = frozenset({Role.SUPER_ADMIN, Role.LOAN_EXEC})

#: The channel a client record is stamped with when a file of this kind is the
#: first thing the firm knows about them, in the words the intake routes
#: already write. It is also the tie-break when one email is on several
#: records: the record that came from the same kind of file is preferred.
SOURCE_CHANNEL_BY_VARIANT: dict[str, str] = {
    "dealer_gatekeeper_v1": "dealer_ai_intake",
    "main_street_v1": "main_street_intake",
    "mca_refi_v1": "mca_refinance",
    "real_estate_dscr_v1": "funding_review",
    "commercial_foreclosure_bailout_v1": "foreclosure_rescue",
}
DEFAULT_SOURCE_CHANNEL = "ai_intake"

#: `Client.contact_permission` for an owner a professional submitted for. The
#: intake link route already refuses to message a record saved this way.
NO_DIRECT_CONTACT = "save_lead_only"

SWEEP_LIMIT = 200


def clean_email(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if "@" in text else None


def _clean(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] or None


def source_channel_for(intake: PublicUnderwritingIntake) -> str:
    return SOURCE_CHANNEL_BY_VARIANT.get(str(intake.variant or ""), DEFAULT_SOURCE_CHANNEL)


@dataclass(frozen=True)
class ClientIdentity:
    """Who the file is for, read off the file."""

    name: str
    email: str | None
    phone: str | None
    business_name: str | None
    #: False when the person on the file is a professional acting for the
    #: client, so nothing on the file is the client's consent to be contacted.
    direct: bool = True


def identity_for(intake: PublicUnderwritingIntake) -> ClientIdentity:
    """The client a file belongs to.

    Ordinarily the person who filled the intake in. On a foreclosure rescue
    sent by an attorney or a broker the intake's contact is that professional,
    and the client is the owner they named in the request.
    """

    business = _clean(intake.business_name, 180)
    state = intake.intake_state if isinstance(intake.intake_state, dict) else {}
    rescue = state.get("foreclosure_rescue")
    if getattr(intake, "client_contact_suppressed", False) and isinstance(rescue, dict):
        name = (
            _clean(rescue.get("borrower_name"), 160)
            or _clean(rescue.get("holding_entity"), 160)
            or business
            or "Owner not named"
        )
        return ClientIdentity(
            name=name,
            email=clean_email(rescue.get("client_email")),
            phone=_clean(rescue.get("client_phone"), 32),
            business_name=_clean(rescue.get("holding_entity"), 180) or business,
            direct=False,
        )
    return ClientIdentity(
        name=_clean(intake.full_name, 160) or business or "Name not given",
        email=clean_email(intake.email),
        phone=_clean(intake.phone, 32),
        business_name=business,
        direct=not getattr(intake, "client_contact_suppressed", False),
    )


async def match_client(db: AsyncSession, identity: ClientIdentity, source_channel: str) -> Client | None:
    """The record already in the book for this person, if there is one."""

    if not identity.email:
        return None
    candidates = list(
        (
            await db.execute(
                select(Client)
                .where(func.lower(Client.email) == identity.email)
                .order_by(Client.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return None
    return next((row for row in candidates if row.source_channel == source_channel), candidates[0])


@dataclass
class Ensured:
    client: Client
    #: "kept" — the file already had it; "linked" — an existing record was
    #: attached; "created" — a new record was made for this file.
    outcome: str


async def ensure_client(db: AsyncSession, intake: PublicUnderwritingIntake) -> Ensured:
    """Give the file its client, finding the record before making one.

    Idempotent and flush-only: the caller owns the transaction. The file's
    application profile is brought along, so the two can never name different
    clients because one of them was written first.
    """

    if intake.client_id is not None:
        client = await db.get(Client, intake.client_id)
        if client is not None:
            await _carry_to_profile(db, intake, client.id)
            return Ensured(client, "kept")

    identity = identity_for(intake)
    channel = source_channel_for(intake)
    client = await match_client(db, identity, channel)
    outcome = "linked"
    if client is None:
        from app.services.payment_authorization import primary_super_admin

        owner = await primary_super_admin(db)
        client = Client(
            name=identity.name,
            email=identity.email,
            phone=identity.phone,
            stage="lead",
            referral_source=_clean(intake.referral_source, 160) or channel,
            source_channel=channel,
            originating_agent_id=owner.id if owner else None,
            current_agent_id=owner.id if owner else None,
            lead_source="other",
            lead_temperature="warm",
            financing_support_needed="yes",
            relationship_context="new_lead",
            contact_permission=None if identity.direct else NO_DIRECT_CONTACT,
            client_experience_mode="self_directed",
            client_experience_mode_reason=channel,
            client_experience_mode_locked_by="firm",
            lead_intake={
                "source": channel,
                "business_name": identity.business_name,
                "created_from_file": str(intake.id),
            },
        )
        db.add(client)
        await db.flush()
        outcome = "created"
    else:
        # Fill what the record lacks; never overwrite what somebody entered.
        if not client.phone and identity.phone:
            client.phone = identity.phone
        if identity.name and (not client.name or client.name.strip().lower() == (identity.email or "")):
            client.name = identity.name
        if not client.source_channel:
            client.source_channel = channel
    intake.client_id = client.id
    await _carry_to_profile(db, intake, client.id)
    await db.flush()
    return Ensured(client, outcome)


async def _carry_to_profile(db: AsyncSession, intake: PublicUnderwritingIntake, client_id: UUID) -> None:
    profile = (
        await db.execute(
            select(ApplicationProfile).where(
                ApplicationProfile.intake_id == intake.id, ApplicationProfile.client_id.is_(None)
            )
        )
    ).scalar_one_or_none()
    if profile is not None:
        profile.client_id = client_id


# ── the repair ──────────────────────────────────────────────────────────────


def _live_file_without_client():
    """A file the desk is still working that names no client.

    Archived files and files flagged for removal are left alone: making a
    record for something that was thrown away fills the book with entries
    nobody can remove. Restoring or opening such a file gives it its client.
    """

    return and_(
        PublicUnderwritingIntake.client_id.is_(None),
        PublicUnderwritingIntake.archived_at.is_(None),
        PublicUnderwritingIntake.delete_requested_at.is_(None),
        Bucket.archived_at.is_(None),
    )


async def count_unlinked(db: AsyncSession) -> int:
    return int(
        (
            await db.execute(
                select(func.count(PublicUnderwritingIntake.id))
                .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
                .where(_live_file_without_client())
            )
        ).scalar_one()
        or 0
    )


async def sweep(db: AsyncSession, *, limit: int = SWEEP_LIMIT) -> dict[str, int]:
    """Give every live file that has no client its client. Oldest first.

    Each file is its own savepoint, so one that cannot be repaired is counted
    and skipped rather than undoing the others. Flush-only; the caller commits.
    """

    report = {"checked": 0, "linked": 0, "created": 0, "failed": 0, "profiles": 0}
    intakes = list(
        (
            await db.execute(
                select(PublicUnderwritingIntake)
                .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
                .where(_live_file_without_client())
                .order_by(PublicUnderwritingIntake.created_at.asc())
                .limit(max(1, limit))
            )
        )
        .scalars()
        .all()
    )
    for intake in intakes:
        report["checked"] += 1
        try:
            async with db.begin_nested():
                ensured = await ensure_client(db, intake)
            if ensured.outcome in report:
                report[ensured.outcome] += 1
        except Exception:  # noqa: BLE001
            report["failed"] += 1
            log.exception("file_clients: could not give file %s a client", intake.id)

    # A profile opened before its file had a client keeps saying it has none.
    stale = list(
        (
            await db.execute(
                select(ApplicationProfile, PublicUnderwritingIntake.client_id)
                .join(PublicUnderwritingIntake, PublicUnderwritingIntake.id == ApplicationProfile.intake_id)
                .where(ApplicationProfile.client_id.is_(None), PublicUnderwritingIntake.client_id.is_not(None))
                .limit(max(1, limit))
            )
        ).all()
    )
    for profile, client_id in stale:
        profile.client_id = client_id
        report["profiles"] += 1
    if stale:
        await db.flush()
    return report


async def run_scheduled_sweep() -> dict[str, int] | None:
    """The scheduler's tick. Opens its own session like every other job."""

    from app.db import SessionLocal

    async with SessionLocal() as db:
        report = await sweep(db)
        if report["linked"] or report["created"] or report["profiles"]:
            await db.commit()
            log.info("file_clients sweep: %s", report)
        elif report["failed"]:
            await db.rollback()
            log.warning("file_clients sweep: %s", report)
        return report


# ── a client's files ────────────────────────────────────────────────────────


async def intake_ids_for_client(db: AsyncSession, client_id: UUID) -> list[UUID]:
    return list(
        (
            await db.execute(
                select(PublicUnderwritingIntake.id).where(PublicUnderwritingIntake.client_id == client_id)
            )
        )
        .scalars()
        .all()
    )


async def profile_ids_for_client(db: AsyncSession, client_id: UUID, intake_ids: list[UUID]) -> list[UUID]:
    clause = ApplicationProfile.client_id == client_id
    if intake_ids:
        clause = or_(clause, ApplicationProfile.intake_id.in_(intake_ids))
    return list((await db.execute(select(ApplicationProfile.id).where(clause))).scalars().all())


async def files_for_client(db: AsyncSession, client_id: UUID) -> list[dict[str, Any]]:
    """The client's files, newest activity first, in the directory's own words.

    The counts and the state come from the helpers the Files directory uses,
    so a file reads the same here as it does there. Archived and closed files
    are included: a client's history is part of the client.
    """

    from sqlalchemy.orm import selectinload

    from app.routers import dealer_ai_intake as intake_core
    from app.services import funding_requests, intake_lifecycle

    rows = (
        await db.execute(
            select(PublicUnderwritingIntake, Bucket)
            .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
            .options(selectinload(Bucket.files), selectinload(Bucket.requested_documents))
            .where(PublicUnderwritingIntake.client_id == client_id)
            .order_by(PublicUnderwritingIntake.updated_at.desc())
        )
    ).all()
    if not rows:
        return []
    intake_ids = [intake.id for intake, _bucket in rows]
    profiles = {
        intake_id: (profile_id, underwriting_status)
        for intake_id, profile_id, underwriting_status in (
            await db.execute(
                select(
                    ApplicationProfile.intake_id,
                    ApplicationProfile.id,
                    ApplicationProfile.underwriting_status,
                ).where(ApplicationProfile.intake_id.in_(intake_ids))
            )
        ).all()
    }
    files: list[dict[str, Any]] = []
    for intake, bucket in rows:
        profile_id, underwriting_status = profiles.get(intake.id, (None, None))
        state, reason = intake_lifecycle.intake_workspace_state(
            bucket_archived_at=bucket.archived_at,
            intake_status=intake.status,
            outcome_status=intake.outcome_status,
            foreclosure_status=intake.foreclosure_rescue_status,
            underwriting_status=underwriting_status,
        )
        documents = [
            item
            for item in intake_core._active_files(bucket)
            if not intake_core._is_package_readiness_output(item)
        ]
        amount = funding_requests.intake_requested_amount(intake)
        files.append(
            {
                "id": intake.id,
                "profile_id": profile_id,
                "bucket_id": bucket.id,
                "variant": intake.variant,
                "full_name": intake.full_name,
                "business_name": intake.business_name,
                "loan_purpose": intake.loan_purpose,
                "status": intake.status,
                "outcome_status": intake.outcome_status,
                "underwriting_status": underwriting_status,
                "workspace_state": state,
                "workspace_state_reason": reason,
                "requested_loan_amount": float(amount) if amount is not None else None,
                "file_count": len(documents),
                "missing_required_count": len(intake_core._missing_required_docs(bucket)),
                "created_at": intake.created_at,
                "updated_at": intake.updated_at,
                "last_message_at": intake.last_message_at,
                "archived_at": bucket.archived_at,
            }
        )
    return files


# ── a client's messages ─────────────────────────────────────────────────────


def _as_utc(value: datetime) -> datetime:
    """Three ledgers, one clock: a timestamp stored without a zone is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _snippet(value: str | None, limit: int = 220) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _unbound_phone_clause(db: AsyncSession, client: Client):
    """Texts filed under no client and no file, on this client's own number.

    Only when the number is this client's alone: a number two records share
    proves nothing about which of them a text belongs to.
    """

    from app.services.communication_scope import canonical_phone, phone_identity_sql
    from app.services.sms.ledger import client_candidates_for_phone

    number = canonical_phone(client.phone)
    if not number:
        return None
    holders = await client_candidates_for_phone(db, number)
    if len(holders) != 1 or holders[0].id != client.id:
        return None
    return and_(
        SmsMessage.client_id.is_(None),
        SmsMessage.intake_id.is_(None),
        SmsMessage.profile_id.is_(None),
        phone_identity_sql(SmsMessage.phone_e164) == number,
    )


async def communications_for_client(
    db: AsyncSession,
    client: Client,
    *,
    viewer: User,
    limit: int = 200,
) -> dict[str, Any]:
    """Every text and email with this client or about their files, newest first.

    Three ledgers are read where they already live: `sms_messages` for texts in
    both directions, `message_sends` for every email the system sent, and the
    file email threads for what the client wrote back. A row is the client's
    when it names the client, one of their files, or one of those files'
    profiles. Email bodies are not copied here: a sent email carries its
    subject and outcome and is opened through the communications log, which
    decides who may read it.
    """

    from app.dealer_os.models import DealerRepInboxMessage, DealerRepInboxThread

    limit = max(1, min(limit, 500))
    intake_ids = await intake_ids_for_client(db, client.id)
    profile_ids = await profile_ids_for_client(db, client.id, intake_ids)

    titles: dict[UUID, str] = {}
    profile_intake: dict[UUID, UUID] = {}
    if intake_ids:
        for intake_id, business, full_name in (
            await db.execute(
                select(
                    PublicUnderwritingIntake.id,
                    PublicUnderwritingIntake.business_name,
                    PublicUnderwritingIntake.full_name,
                ).where(PublicUnderwritingIntake.id.in_(intake_ids))
            )
        ).all():
            titles[intake_id] = business or full_name
    if profile_ids:
        for profile_id, intake_id in (
            await db.execute(
                select(ApplicationProfile.id, ApplicationProfile.intake_id).where(
                    ApplicationProfile.id.in_(profile_ids)
                )
            )
        ).all():
            if intake_id is not None:
                profile_intake[profile_id] = intake_id

    def file_of(intake_id: UUID | None, profile_id: UUID | None) -> dict[str, Any]:
        resolved = intake_id if intake_id in titles else profile_intake.get(profile_id) if profile_id else None
        return {
            "intake_id": str(resolved) if resolved else None,
            "file_title": titles.get(resolved) if resolved else None,
        }

    def bound(model) -> Any:
        clauses = [model.client_id == client.id]
        if intake_ids:
            clauses.append(model.intake_id.in_(intake_ids))
        if profile_ids:
            clauses.append(model.profile_id.in_(profile_ids))
        return or_(*clauses)

    sms_clause = bound(SmsMessage)
    unbound = await _unbound_phone_clause(db, client)
    if unbound is not None:
        sms_clause = or_(sms_clause, unbound)
    sms_rows = list(
        (
            await db.execute(
                select(SmsMessage).where(sms_clause).order_by(SmsMessage.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    email_rows = list(
        (
            await db.execute(
                select(MessageSend)
                .where(MessageSend.channel == "email", bound(MessageSend))
                .order_by(MessageSend.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    inbound_rows: list[Any] = []
    if profile_ids:
        inbound_rows = list(
            (
                await db.execute(
                    select(DealerRepInboxMessage, DealerRepInboxThread.subject)
                    .join(DealerRepInboxThread, DealerRepInboxThread.id == DealerRepInboxMessage.thread_id)
                    .where(
                        DealerRepInboxMessage.profile_id.in_(profile_ids),
                        DealerRepInboxMessage.channel == "email",
                        DealerRepInboxMessage.direction == "inbound",
                    )
                    .order_by(DealerRepInboxMessage.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )

    actor_ids = {row.actor_user_id for row in email_rows if row.actor_user_id}
    actor_names: dict[UUID, str] = {}
    if actor_ids:
        for user in (await db.execute(select(User).where(User.id.in_(actor_ids)))).scalars().all():
            actor_names[user.id] = user.name or user.email

    is_super_admin = viewer.role == Role.SUPER_ADMIN
    items: list[dict[str, Any]] = []
    for row in sms_rows:
        items.append(
            {
                "id": f"sms:{row.id}",
                "channel": "sms",
                "direction": row.direction,
                "occurred_at": row.created_at,
                "counterpart": row.phone_e164,
                "subject": None,
                "snippet": _snippet(row.body) or _snippet(row.detail),
                "status": row.status,
                "detail": row.detail or "",
                "context": row.context or "",
                "actor_name": None,
                "delivered_at": row.delivered_at,
                "opened_at": None,
                "can_open": False,
                **file_of(row.intake_id, row.profile_id),
            }
        )
    for row in email_rows:
        items.append(
            {
                "id": f"send:{row.id}",
                "channel": "email",
                "direction": row.direction or "outbound",
                "occurred_at": row.created_at,
                "counterpart": row.to_email,
                "subject": row.subject,
                "snippet": None,
                "status": row.status,
                "detail": row.detail or "",
                "context": row.context or "",
                # A person's name or nothing: "sent by system" tells the desk nothing
                # the context does not already say.
                "actor_name": actor_names.get(row.actor_user_id),
                "delivered_at": row.delivered_at,
                "opened_at": row.opened_at,
                # The same ownership rule the communications log applies.
                "can_open": bool(row.body_text_enc or row.body_html_enc)
                and (is_super_admin or viewer.id in (row.owner_user_id, row.actor_user_id)),
                **file_of(row.intake_id, row.profile_id),
            }
        )
    for message, thread_subject in inbound_rows:
        items.append(
            {
                "id": f"reply:{message.id}",
                "channel": "email",
                "direction": "inbound",
                "occurred_at": message.created_at,
                "counterpart": getattr(message, "sender", None),
                "subject": message.subject or thread_subject,
                "snippet": _snippet(message.body),
                "status": "received",
                "detail": "",
                "context": "reply",
                "actor_name": None,
                "delivered_at": None,
                "opened_at": None,
                "can_open": False,
                **file_of(None, message.profile_id),
            }
        )
    items.sort(key=lambda item: _as_utc(item["occurred_at"]), reverse=True)
    counts = {"sms": 0, "email": 0}
    for item in items:
        counts[item["channel"]] += 1
    return {"items": items[:limit], "counts": counts, "file_count": len(intake_ids)}


# ── the desk's view across every file ───────────────────────────────────────

#: The canonical underwriting lifecycle, in the order a file moves through it.
LIFECYCLE_ORDER = (
    "submitted",
    "collecting_docs",
    "in_underwriting",
    "term_sheet_provided",
    "approved",
    "closed_won",
)

#: Timeline kinds grouped the way the desk filters them.
ACTIVITY_GROUPS: dict[str, tuple[str, ...]] = {
    "documents": ("document.", "requirement.", "forms."),
    "messages": ("message.", "sms.", "offer."),
    "status": ("status.", "review.", "team.", "note."),
}


def activity_group(kind: str) -> str:
    for group, prefixes in ACTIVITY_GROUPS.items():
        if kind.startswith(prefixes):
            return group
    return "other"


async def desk_summary(db: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """The numbers at the top of the desk: live files, where they stand, and
    whether any of them still lacks a client."""

    from app.models.bucket import BucketFile
    from app.routers.dealer_ai_intake import _closed_intake_lifecycle_clause

    now = now or datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    live = and_(Bucket.archived_at.is_(None), ~_closed_intake_lifecycle_clause())
    base = (
        select(PublicUnderwritingIntake.id)
        .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
        .where(live)
    )

    async def count(*where) -> int:
        scoped = base.where(*where) if where else base
        stmt = select(func.count()).select_from(scoped.subquery())
        return int((await db.execute(stmt)).scalar_one() or 0)

    # Aliased: the lifecycle clause has its own correlated look at the profile,
    # which would otherwise be folded into this join.
    profile = aliased(ApplicationProfile)
    # A literal, and grouped by its label: with a bound value Postgres sees the
    # selected expression and the grouped one as two different expressions.
    stage = func.coalesce(profile.underwriting_status, literal_column("'submitted'")).label("stage")
    stage_rows = (
        await db.execute(
            select(stage, func.count(PublicUnderwritingIntake.id))
            .select_from(PublicUnderwritingIntake)
            .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
            .outerjoin(profile, profile.intake_id == PublicUnderwritingIntake.id)
            .where(live)
            .group_by(stage)
        )
    ).all()
    by_stage = {str(key): int(total) for key, total in stage_rows}
    stages = [{"key": key, "count": by_stage.pop(key, 0)} for key in LIFECYCLE_ORDER]
    stages += [{"key": key, "count": total} for key, total in sorted(by_stage.items())]

    has_document = (
        select(BucketFile.id)
        .where(BucketFile.bucket_id == PublicUnderwritingIntake.bucket_id, BucketFile.deleted_at.is_(None))
        .exists()
    )
    return {
        "generated_at": now,
        "active": await count(),
        "new_7d": await count(PublicUnderwritingIntake.created_at >= week_ago),
        "no_documents": await count(~has_document),
        "without_client": await count(PublicUnderwritingIntake.client_id.is_(None)),
        "stages": stages,
    }


async def desk_activity(
    db: AsyncSession,
    *,
    group: str | None = None,
    before: datetime | None = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """What happened on the files, newest first, with the file and the client
    each line belongs to. Only live files that came through an intake: the
    same files the summary counts. A file that closed (lost, denied,
    withdrawn) or whose room was archived keeps its history on its own
    timeline and leaves the desk's."""

    from app.routers.dealer_ai_intake import _closed_intake_lifecycle_clause

    # Aliased: the lifecycle clause has its own correlated look at the profile.
    profile = aliased(ApplicationProfile)
    stmt = (
        select(FileEvent, PublicUnderwritingIntake, Client.name)
        .join(profile, profile.id == FileEvent.profile_id)
        .join(PublicUnderwritingIntake, PublicUnderwritingIntake.id == profile.intake_id)
        .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
        .outerjoin(Client, Client.id == PublicUnderwritingIntake.client_id)
        .where(Bucket.archived_at.is_(None), ~_closed_intake_lifecycle_clause())
        .order_by(FileEvent.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    if before is not None:
        stmt = stmt.where(FileEvent.created_at < before)
    prefixes = ACTIVITY_GROUPS.get(group or "")
    if prefixes:
        stmt = stmt.where(or_(*(FileEvent.kind.like(f"{prefix}%") for prefix in prefixes)))
    items: list[dict[str, Any]] = []
    for event, intake, client_name in (await db.execute(stmt)).all():
        items.append(
            {
                "id": str(event.id),
                "occurred_at": event.created_at,
                "kind": event.kind,
                "group": activity_group(event.kind),
                "title": event.title,
                "body": event.body,
                "actor_label": event.actor_label,
                "visibility": event.visibility,
                "profile_id": str(event.profile_id),
                "intake_id": str(intake.id),
                "file_title": intake.business_name or intake.full_name,
                "client_id": str(intake.client_id) if intake.client_id else None,
                "client_name": client_name,
            }
        )
    return items
