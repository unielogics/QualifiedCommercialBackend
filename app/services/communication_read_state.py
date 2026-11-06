"""Per-user received alerts and explicit, source-scoped message read updates."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select

from app.dealer_os.models import DealerRepInboxMessage, DealerRepInboxThread
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.email_message import EmailMessage
from app.models.notification import Notification
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.communication import FileCommunicationUnread
from app.services.communication_events import publish_communication_event


async def read_received_notifications(db, *, user_id, message_ids, thread_ids=(), at=None):
    """Only explicit source IDs clear alerts; a later arrival stays unread."""
    identifiers = {str(value) for value in message_ids}
    if not identifiers:
        return 0
    statement = select(Notification).where(Notification.recipient_user_id == user_id, Notification.read_at.is_(None),
        Notification.target_type == "communication_thread", Notification.meta["message_id"].astext.in_(identifiers))
    if thread_ids:
        statement = statement.where(Notification.target_id.in_(list(thread_ids)))
    rows = list((await db.execute(statement)).scalars().all())
    stamp = at or datetime.now(UTC)
    for row in rows:
        row.read_at = stamp
    if rows:
        await db.flush()
        await publish_communication_event(db, recipient_user_ids={user_id}, event_type="notification.read")
    return len(rows)


async def sms_alias_map(db, rows):
    """Alias one physical text only when its phone and file context agree."""
    from app.services.communication_scope import canonical_phone

    def source_key(row):
        phone = canonical_phone(getattr(row, "phone_e164", None))
        provider_id = getattr(row, "provider_message_id", None)
        if not phone or not provider_id or getattr(row, "direction", None) != "inbound":
            return None
        return row.provider, provider_id, phone

    grouped = {}
    ids = {}
    for row in rows:
        key = source_key(row)
        binding = tuple(str(getattr(row, field, None) or "") for field in ("profile_id", "intake_id", "client_id"))
        ids[str(row.id)] = repr((key, binding)) if key else str(row.id)
        if key:
            grouped.setdefault(key, []).append(row)
    if not grouped:
        return ids
    mirrors = list((await db.execute(select(DealerRepInboxMessage).where(
        DealerRepInboxMessage.channel == "sms", DealerRepInboxMessage.direction == "inbound",
        DealerRepInboxMessage.provider_message_id.in_({key[1] for key in grouped})))).scalars().all())
    mirrors = [row for row in mirrors if (row.provider, row.provider_message_id, canonical_phone(row.sender)) in grouped]
    if not mirrors:
        return ids
    threads = {row.id: row for row in (await db.execute(select(DealerRepInboxThread).where(
        DealerRepInboxThread.id.in_({row.thread_id for row in mirrors})))).scalars().all()}
    profile_ids = {value for row in [*rows, *mirrors, *threads.values()]
                   if (value := getattr(row, "profile_id", None)) is not None}
    profiles = {}
    if profile_ids:
        profile_rows = (await db.execute(select(ApplicationProfile.id, ApplicationProfile.intake_id,
            ApplicationProfile.primary_bucket_id, ApplicationProfile.client_id, ApplicationProfile.dealer_id,
            PublicUnderwritingIntake.bucket_id).outerjoin(PublicUnderwritingIntake,
                PublicUnderwritingIntake.id == ApplicationProfile.intake_id).where(ApplicationProfile.id.in_(profile_ids)))).all()
        profiles = {row[0]: row for row in profile_rows}
    by_source = {}
    for row in mirrors:
        by_source.setdefault((row.provider, row.provider_message_id, canonical_phone(row.sender)), []).append(row)
    for key, members in by_source.items():
        contexts = set()
        invalid = False
        for mirror in members:
            thread = threads.get(mirror.thread_id)
            if thread is None:
                invalid = True
                continue
            for field in ("profile_id", "dealer_id", "owner_user_id"):
                a, b = getattr(mirror, field, None), getattr(thread, field, None)
                if a is not None and b is not None and a != b:
                    invalid = True
            profile_id = mirror.profile_id or thread.profile_id
            dealer_id = mirror.dealer_id or thread.dealer_id
            anchor = profiles.get(profile_id)
            if profile_id is not None and (anchor is None or (dealer_id is not None and anchor[4] != dealer_id)):
                invalid = True
            contexts.add((thread.id, profile_id, dealer_id, thread.owner_user_id))
        # Even an otherwise compatible mirror cannot choose among two files.
        if invalid or len(contexts) != 1:
            continue
        thread_id, profile_id, _dealer_id, _owner_id = next(iter(contexts))
        anchor = profiles.get(profile_id)
        compatible = []
        for source in grouped[key]:
            source_profile = getattr(source, "profile_id", None)
            source_intake = getattr(source, "intake_id", None)
            source_client = getattr(source, "client_id", None)
            if source_profile is not None and source_profile != profile_id:
                continue
            if source_intake is not None and (anchor is None or anchor[1] != source_intake or anchor[2] != anchor[5]):
                continue
            if source_client is not None and (anchor is None or anchor[3] != source_client):
                continue
            compatible.append(source)
        if compatible:
            physical = repr((*key, str(thread_id)))
            ids.update({str(row.id): physical for row in [*compatible, *members]})
    return ids


async def sms_source_ids(db, rows):
    return set(await sms_alias_map(db, rows))


async def sms_unread_by_phone(db, user_id):
    """Per-recipient SMS unread alerts, across the full notification history."""
    from app.services.communication_scope import canonical_phone

    rows = (await db.execute(select(Notification.target_id, func.count()).where(
        Notification.recipient_user_id == user_id, Notification.event_type == "sms_received",
        Notification.read_at.is_(None), Notification.target_type == "communication_thread")
        .group_by(Notification.target_id))).all()
    result = {}
    for thread_id, count in rows:
        if str(thread_id or "").startswith("sms:phone:"):
            phone = canonical_phone(str(thread_id).split(":", 2)[2])
            if phone:
                result[phone] = result.get(phone, 0) + int(count or 0)
    return result


async def own_email_source_aliases(db, *, user_id, rep_rows):
    provider_ids = {row.provider_message_id for row in rep_rows if row.provider == "gmail" and row.provider_message_id}
    if not provider_ids:
        return []
    candidates = list((await db.execute(select(EmailMessage).where(EmailMessage.owner_user_id == user_id,
        EmailMessage.gmail_message_id.in_(provider_ids), EmailMessage.direction == "inbound"))).scalars().all())
    mirrors = list((await db.execute(select(DealerRepInboxMessage).where(DealerRepInboxMessage.owner_user_id == user_id,
        DealerRepInboxMessage.provider == "gmail", DealerRepInboxMessage.provider_message_id.in_(provider_ids)))).scalars().all())
    ambiguous = {provider for provider in provider_ids if len({row.thread_id for row in mirrors if row.provider_message_id == provider}) > 1
                 or sum(row.gmail_message_id == provider for row in candidates) > 1}
    return [row for row in candidates if row.gmail_message_id not in ambiguous]


async def mark_rep_messages_seen(db, *, thread, user_id, rows, at=None):
    """An operator never changes another mailbox owner's message read state."""
    stamp = at or datetime.now(UTC)
    if thread.owner_user_id == user_id:
        for row in rows:
            if row.direction == "inbound":
                row.read_at = stamp
        await db.flush()
        thread.unread_count = int((await db.execute(select(func.count()).select_from(DealerRepInboxMessage).where(
            DealerRepInboxMessage.thread_id == thread.id, DealerRepInboxMessage.direction == "inbound",
            DealerRepInboxMessage.read_at.is_(None)))).scalar_one() or 0)
        aliases = await own_email_source_aliases(db, user_id=user_id, rep_rows=rows)
        for row in aliases:
            row.is_read = True
    else:
        aliases = []
    await read_received_notifications(db, user_id=user_id,
        message_ids=[*(row.id for row in rows), *(row.id for row in aliases)], at=stamp)


async def mark_mailbox_messages_seen(db, *, user_id, rows, at=None):
    """Read a mailbox source and its one unambiguous provider-backed mirror."""
    stamp = at or datetime.now(UTC)
    for row in rows:
        row.is_read = True
    provider_ids = {row.gmail_message_id for row in rows if row.gmail_message_id}
    if provider_ids:
        sources = list((await db.execute(select(EmailMessage).where(EmailMessage.owner_user_id == user_id,
            EmailMessage.gmail_message_id.in_(provider_ids)))).scalars().all())
        provider_ids = {provider for provider in provider_ids if sum(row.gmail_message_id == provider for row in sources) == 1}
    mirrors = list((await db.execute(select(DealerRepInboxMessage).where(
        DealerRepInboxMessage.owner_user_id == user_id, DealerRepInboxMessage.provider == "gmail",
        DealerRepInboxMessage.direction == "inbound", DealerRepInboxMessage.provider_message_id.in_(provider_ids)))).scalars().all()) if provider_ids else []
    by_provider = {}
    for row in mirrors:
        by_provider.setdefault(row.provider_message_id, []).append(row)
    by_thread = {}
    for members in by_provider.values():
        if len({row.thread_id for row in members}) == 1:
            for row in members:
                by_thread.setdefault(row.thread_id, []).append(row)
    for thread_id, members in by_thread.items():
        thread = await db.get(DealerRepInboxThread, thread_id)
        if thread is not None and thread.owner_user_id == user_id:
            await mark_rep_messages_seen(db, thread=thread, user_id=user_id, rows=members, at=stamp)
    await read_received_notifications(db, user_id=user_id, message_ids=[row.id for row in rows], at=stamp)


async def load_file_scope(db, user, intake_id):
    # Matches application_communications and the admin client-thread workspace.
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    intake = await db.get(PublicUnderwritingIntake, intake_id)
    if intake is None:
        raise HTTPException(404, "Application file not found")
    profiles = list((await db.execute(select(ApplicationProfile).where(
        ApplicationProfile.intake_id == intake.id, ApplicationProfile.primary_bucket_id == intake.bucket_id))).scalars().all())
    return intake, profiles[0] if len(profiles) == 1 else None


async def file_unread(db, *, user, intake, profile):
    from app.services.communication_scope import intake_sms_rows
    from app.services.file_communication_unread import email_counts, sms_counts

    sms_rows = [row for row in await intake_sms_rows(db, intake, limit=None) if row.direction == "inbound"]
    sms = (await sms_counts(db, user_id=user.id, rows_by_intake={intake.id: sms_rows}))[intake.id]
    emails = await email_counts(db, user_id=user.id, profile_ids=[profile.id] if profile else [])
    email = int(emails.get(profile.id, 0)) if profile else 0
    return FileCommunicationUnread(intake_id=intake.id, profile_id=profile.id if profile else None,
        sms=sms, email=email, total=sms + email, as_of=datetime.now(UTC))


async def file_seen(db, *, user, intake, profile, payload):
    from app.services.communication_scope import intake_sms_rows

    requested = set(payload.message_ids)
    if payload.channel == "sms":
        rows = await intake_sms_rows(db, intake, ids=list(requested)) if requested else []
        if {row.id for row in rows} != requested:
            raise HTTPException(404, "One or more rendered text messages do not belong to this application file")
        rows = [row for row in rows if row.direction == "inbound"]
        await read_received_notifications(db, user_id=user.id, message_ids=await sms_source_ids(db, rows))
    else:
        if profile is None or payload.thread_id is None:
            raise HTTPException(422, "Choose the exact file email thread that was viewed")
        thread = (await db.execute(select(DealerRepInboxThread).where(DealerRepInboxThread.id == payload.thread_id,
            DealerRepInboxThread.profile_id == profile.id, DealerRepInboxThread.channel == "email"))).scalar_one_or_none()
        if thread is None:
            raise HTTPException(404, "File email thread not found")
        rows = list((await db.execute(select(DealerRepInboxMessage).where(DealerRepInboxMessage.thread_id == thread.id,
            DealerRepInboxMessage.id.in_(requested)))).scalars().all()) if requested else []
        if {row.id for row in rows} != requested:
            raise HTTPException(404, "One or more rendered email messages do not belong to this file thread")
        await mark_rep_messages_seen(db, thread=thread, user_id=user.id, rows=[row for row in rows if row.direction == "inbound"])
    await db.flush()
    return await file_unread(db, user=user, intake=intake, profile=profile)
