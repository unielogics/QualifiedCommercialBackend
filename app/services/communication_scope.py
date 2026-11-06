"""Exact SMS identity and row scopes shared by conversations and unread views."""
from __future__ import annotations

import re

from sqlalchemy import case, exists, func, literal, or_, select
from sqlalchemy import false as sql_false

from app.dealer_os.models import DealerBusiness, DealerRepContact
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.sms_message import SmsMessage
from app.scoping import scope_client_query

SMS_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER, Role.FIELD_REP}
GLOBAL_SMS_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}


def canonical_phone(value):
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def phone_identity_sql(column):
    """NANP formatting variants compare equal; international tails never do."""
    raw = func.trim(func.coalesce(column, ""))
    digits = func.regexp_replace(raw, r"\D", "", "g")
    return case((raw.like("+%") & func.length(digits).between(8, 15), literal("+") + digits),
                (func.length(digits) == 10, literal("+1") + digits),
                ((func.length(digits) == 11) & digits.like("1%"), literal("+") + digits),
                else_=None)


async def scoped_sms_query(db, user, phone=None):
    """Reapply the viewer's row scope even after a thread has been authorized."""
    query = select(SmsMessage)
    if user.role not in SMS_ROLES:
        query = query.where(sql_false())
    elif user.role == Role.BROKER:
        query = query.where(SmsMessage.client_id.in_(scope_client_query(user, select(Client.id))))
    elif user.role == Role.FIELD_REP:
        number = phone_identity_sql(SmsMessage.phone_e164)
        owned_numbers = select(phone_identity_sql(DealerRepContact.phone_e164)).where(DealerRepContact.owner_user_id == user.id)
        other_owner = exists(select(DealerRepContact.id).where(
            phone_identity_sql(DealerRepContact.phone_e164) == number,
            or_(DealerRepContact.owner_user_id != user.id, DealerRepContact.owner_user_id.is_(None)),
        ).correlate(SmsMessage))
        # Explicit file ownership may authorize a bound ledger row. A shared
        # phone alone never proves access to another file's bound messages.
        own_profile = exists(select(ApplicationProfile.id).join(DealerBusiness, DealerBusiness.id == ApplicationProfile.dealer_id).where(
            ApplicationProfile.id == SmsMessage.profile_id, DealerBusiness.owner_user_id == user.id,
            or_(SmsMessage.intake_id.is_(None), ApplicationProfile.intake_id == SmsMessage.intake_id),
            or_(SmsMessage.client_id.is_(None), ApplicationProfile.client_id == SmsMessage.client_id),
        ).correlate(SmsMessage))
        own_intake = exists(select(ApplicationProfile.id).join(DealerBusiness, DealerBusiness.id == ApplicationProfile.dealer_id)
            .join(PublicUnderwritingIntake, PublicUnderwritingIntake.id == ApplicationProfile.intake_id).where(
                ApplicationProfile.intake_id == SmsMessage.intake_id, SmsMessage.profile_id.is_(None),
                ApplicationProfile.primary_bucket_id == PublicUnderwritingIntake.bucket_id,
                DealerBusiness.owner_user_id == user.id,
                or_(SmsMessage.client_id.is_(None), PublicUnderwritingIntake.client_id == SmsMessage.client_id),
            ).correlate(SmsMessage))
        unbound = (SmsMessage.client_id.is_(None) & SmsMessage.intake_id.is_(None) & SmsMessage.profile_id.is_(None)
                   & number.in_(owned_numbers) & ~other_owner)
        query = query.where(or_(own_profile, own_intake, unbound))
    if phone is not None:
        number = canonical_phone(phone)
        query = query.where(phone_identity_sql(SmsMessage.phone_e164) == number if number else sql_false())
    return query


async def intake_sms_rows(db, intake, *, ids=None, limit=200):
    """Explicit binding wins; unbound legacy rows require a unique intake phone.

    A contrary intake/profile/client binding never flows through phone fallback.
    Keep newest 200 for display, then return them chronologically. An explicit
    IDs read is unbounded by that window for safe seen/reconciliation checks.
    """
    profiles = select(ApplicationProfile.id).where(ApplicationProfile.intake_id == intake.id,
        ApplicationProfile.primary_bucket_id == intake.bucket_id)
    client_id = getattr(intake, "client_id", None)
    client_ok = or_(SmsMessage.client_id.is_(None), SmsMessage.client_id == client_id) if client_id else SmsMessage.client_id.is_(None)
    profile_ok = or_(SmsMessage.profile_id.is_(None), SmsMessage.profile_id.in_(profiles))
    intake_ok = or_(SmsMessage.intake_id.is_(None), SmsMessage.intake_id == intake.id)
    bound = or_(SmsMessage.intake_id == intake.id, SmsMessage.profile_id.in_(profiles)) & profile_ok & intake_ok & client_ok
    number = canonical_phone(intake.phone)
    fallback = sql_false()
    if number:
        unique_intake = select(func.count(PublicUnderwritingIntake.id)).where(
            phone_identity_sql(PublicUnderwritingIntake.phone) == number).scalar_subquery() == 1
        fallback = (phone_identity_sql(SmsMessage.phone_e164) == number) & unique_intake & client_ok & SmsMessage.profile_id.is_(None) & SmsMessage.intake_id.is_(None)
    query = select(SmsMessage).where(or_(bound, fallback))
    if ids is not None:
        if not ids:
            return []
        query = query.where(SmsMessage.id.in_(ids))
    query = query.order_by(SmsMessage.created_at.desc(), SmsMessage.id.desc())
    if ids is None and limit is not None:
        query = query.limit(limit)
    rows = list((await db.execute(query)).scalars().all())
    return list(reversed(rows))
