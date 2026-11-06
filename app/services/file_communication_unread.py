"""Read-only, page-sized communication counts using the file's exact sources."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, literal, select, union_all

from app.dealer_os.models import DealerRepInboxMessage, DealerRepInboxThread
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.notification import Notification
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.sms_message import SmsMessage
from app.schemas.communication import FileCommunicationUnread
from app.services.communication_scope import intake_sms_clause


async def sms_counts(db, *, user_id, rows_by_intake):
    from app.services import communication_read_state

    messages = {row.id: row for rows in rows_by_intake.values() for row in rows}
    aliases = await communication_read_state.sms_alias_map(db, list(messages.values()))
    unread = set()
    if aliases:
        notices = (await db.execute(select(Notification.meta["message_id"].astext).where(
            Notification.recipient_user_id == user_id, Notification.read_at.is_(None),
            Notification.event_type == "sms_received",
            Notification.meta["message_id"].astext.in_(list(aliases)),
        ))).scalars().all()
        unread = {aliases[value] for value in notices if value in aliases}
    return {intake_id: len({aliases[str(row.id)] for row in rows} & unread)
            for intake_id, rows in rows_by_intake.items()}


async def email_counts(db, *, user_id, profile_ids):
    if not profile_ids:
        return {}
    return dict((await db.execute(select(DealerRepInboxThread.profile_id, func.count())
        .join(DealerRepInboxMessage, DealerRepInboxMessage.thread_id == DealerRepInboxThread.id).where(
            DealerRepInboxThread.profile_id.in_(profile_ids), DealerRepInboxThread.owner_user_id == user_id,
            DealerRepInboxThread.channel == "email", DealerRepInboxMessage.direction == "inbound",
            DealerRepInboxMessage.read_at.is_(None),
        ).group_by(DealerRepInboxThread.profile_id))).all())


async def unread_batch(db, *, user, intake_ids):
    # This matches the single file-count and file-acknowledgment role gate.
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    identifiers = list(dict.fromkeys(intake_ids))
    if not identifiers:
        return []
    intakes = list((await db.execute(select(PublicUnderwritingIntake.id, PublicUnderwritingIntake.bucket_id,
        PublicUnderwritingIntake.client_id, PublicUnderwritingIntake.phone)
        .where(PublicUnderwritingIntake.id.in_(identifiers)))).all())
    by_id = {row.id: row for row in intakes}
    if set(by_id) != set(identifiers):
        raise HTTPException(404, "One or more application files were not found")
    # Load only verified intake/bucket lineage; a stale profile cannot expose
    # mail owned by a different file. An ambiguous profile has no email scope.
    profiles = (await db.execute(select(ApplicationProfile.id, ApplicationProfile.intake_id,
        ApplicationProfile.primary_bucket_id).where(ApplicationProfile.intake_id.in_(identifiers)))).all()
    profile_candidates = {key: [] for key in identifiers}
    for profile in profiles:
        if profile.primary_bucket_id == by_id[profile.intake_id].bucket_id:
            profile_candidates[profile.intake_id].append(profile.id)
    profile_ids = {key: values[0] for key, values in profile_candidates.items() if len(values) == 1}

    # Every branch uses the same predicate as the single-file conversation.
    # A bounded UNION performs one source query for all visible files instead
    # of issuing a query for each row or applying a global message scan cap.
    bindings = union_all(*[select(
        literal(row.id, type_=SmsMessage.intake_id.type).label("intake_id"), SmsMessage.id.label("message_id"),
    ).where(SmsMessage.direction == "inbound", intake_sms_clause(row)) for row in intakes]).subquery()
    bound_rows = (await db.execute(select(bindings.c.intake_id, SmsMessage)
        .join(SmsMessage, SmsMessage.id == bindings.c.message_id))).all()
    rows_by_intake = {key: [] for key in identifiers}
    for intake_id, message in bound_rows:
        rows_by_intake[intake_id].append(message)
    sms = await sms_counts(db, user_id=user.id, rows_by_intake=rows_by_intake)
    email = await email_counts(db, user_id=user.id, profile_ids=list(profile_ids.values()))
    stamp = datetime.now(UTC)
    return [FileCommunicationUnread(intake_id=key, profile_id=profile_ids.get(key),
        sms=sms[key], email=int(email.get(profile_ids.get(key), 0)),
        total=sms[key] + int(email.get(profile_ids.get(key), 0)), as_of=stamp) for key in identifiers]
