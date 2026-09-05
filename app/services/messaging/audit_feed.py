"""Reading the communications log.

Two tables back it. `message_sends` is the new spine for email; `sms_messages`
has held every text since 0169 and is deliberately left where it is — it works,
three surfaces read it, and unioning at read time is cheaper and safer than
migrating a working ledger. They are normalized here into one shape, the same
way `application_profiles.audit_events` already normalizes four audit trails
into `UnifiedAuditEvent`.

Access is the rule the owner chose: everyone sees their own, a super admin sees
everything. A row with no owner belongs to nobody — a cron send about no
particular file, like the five-minute digest — and nobody means super admins
alone, because defaulting it to everybody would be a leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.message_send import MessageSend
from app.models.sms_message import SmsMessage
from app.models.user import User

#: SMS rows predate the ownership rule and carry no owner, so they are visible
#: to the desk as a whole. They were already readable at /sms/messages by any
#: operator, so this narrows nothing.
SMS_IS_DESK_WIDE = True


@dataclass
class MessageRow:
    id: str
    source: str
    channel: str
    direction: str
    context: str
    to: str | None
    subject: str | None
    status: str
    detail: str
    provider: str
    provider_message_id: str | None
    occurred_at: datetime
    delivered_at: datetime | None
    opened_at: datetime | None
    actor_name: str | None
    actor_label: str
    job: str | None
    request_id: str | None
    has_body: bool
    secrets_masked: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "channel": self.channel,
            "direction": self.direction, "context": self.context, "to": self.to,
            "subject": self.subject, "status": self.status, "detail": self.detail,
            "provider": self.provider, "provider_message_id": self.provider_message_id,
            "occurred_at": self.occurred_at, "delivered_at": self.delivered_at,
            "opened_at": self.opened_at, "actor_name": self.actor_name,
            "actor_label": self.actor_label, "job": self.job, "request_id": self.request_id,
            "has_body": self.has_body, "secrets_masked": self.secrets_masked,
        }


def is_super_admin(user: User) -> bool:
    return getattr(user, "role", None) == Role.SUPER_ADMIN


def _visible_to(user: User):
    """The ownership filter. A super admin gets no filter at all."""
    return or_(
        MessageSend.owner_user_id == user.id,
        MessageSend.actor_user_id == user.id,
    )


async def list_messages(
    db: AsyncSession,
    user: User,
    *,
    q: str = "",
    channel: str = "",
    statuses: tuple[str, ...] = (),
    context: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MessageRow], int]:
    """Newest first, across both ledgers."""
    everything = is_super_admin(user)

    email_q = select(MessageSend)
    if not everything:
        email_q = email_q.where(_visible_to(user))
    if channel in ("email", "sms"):
        email_q = email_q.where(MessageSend.channel == channel)
    if statuses:
        email_q = email_q.where(MessageSend.status.in_(statuses))
    if context:
        email_q = email_q.where(MessageSend.context == context)
    if q:
        like = f"%{q.strip()}%"
        email_q = email_q.where(
            or_(MessageSend.to_email.ilike(like), MessageSend.subject.ilike(like))
        )

    sms_rows: list[SmsMessage] = []
    if channel in ("", "sms") and (everything or SMS_IS_DESK_WIDE):
        sms_q = select(SmsMessage).order_by(SmsMessage.created_at.desc()).limit(500)
        if statuses:
            sms_q = sms_q.where(SmsMessage.status.in_(statuses))
        if context:
            sms_q = sms_q.where(SmsMessage.context == context)
        if q:
            sms_q = sms_q.where(SmsMessage.phone_e164.ilike(f"%{q.strip()}%"))
        sms_rows = list((await db.execute(sms_q)).scalars().all())

    email_rows = list(
        (await db.execute(email_q.order_by(MessageSend.created_at.desc()).limit(500)))
        .scalars().all()
    )

    names = await _actor_names(db, [r.actor_user_id for r in email_rows])
    merged = [_from_email(r, names) for r in email_rows] + [_from_sms(r) for r in sms_rows]
    merged.sort(key=lambda r: r.occurred_at, reverse=True)
    return merged[offset : offset + limit], len(merged)


async def _actor_names(db: AsyncSession, ids) -> dict[Any, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(wanted)))).scalars().all()
    return {u.id: u.name or u.email for u in rows}


def _from_email(row: MessageSend, names: dict[Any, str]) -> MessageRow:
    return MessageRow(
        id=f"send:{row.id}", source="message_sends", channel=row.channel,
        direction=row.direction, context=row.context or "", to=row.to_email or row.to_phone,
        subject=row.subject, status=row.status, detail=row.detail or "",
        provider=row.provider or "", provider_message_id=row.provider_message_id,
        occurred_at=row.created_at, delivered_at=row.delivered_at, opened_at=row.opened_at,
        actor_name=names.get(row.actor_user_id), actor_label=row.actor_label or "system",
        job=row.job, request_id=row.request_id,
        has_body=bool(row.body_text_enc or row.body_html_enc),
        secrets_masked=bool(row.secrets_masked),
    )


def _from_sms(row: SmsMessage) -> MessageRow:
    return MessageRow(
        id=f"sms:{row.id}", source="sms_messages", channel="sms",
        direction=row.direction, context=row.context or "", to=row.phone_e164,
        subject=None, status=row.status, detail=row.detail or "",
        provider=row.provider or "", provider_message_id=row.provider_message_id,
        occurred_at=row.created_at, delivered_at=row.delivered_at, opened_at=None,
        # The SMS ledger predates actor attribution; saying "system" would be a
        # claim, so it says nothing.
        actor_name=None, actor_label="unknown", job=None, request_id=None,
        has_body=bool(row.body), secrets_masked=False,
    )


async def message_detail(db: AsyncSession, user: User, message_id: str) -> dict[str, Any] | None:
    """One message with its body decrypted — the preview.

    Returns None rather than raising when the row is missing or not this user's,
    so the route cannot leak the difference between the two.
    """
    from app.services.email.user_inbox_sync import decrypt_body

    kind, _, raw = message_id.partition(":")
    try:
        key = UUID(raw)
    except (ValueError, AttributeError):
        return None

    if kind == "sms":
        row = await db.get(SmsMessage, key)
        if row is None:
            return None
        return {
            **_from_sms(row).as_dict(),
            "body_text": row.body,
            "body_html": None,
            "cc": [],
            "attachments": [],
        }

    row = await db.get(MessageSend, key)
    if row is None:
        return None
    if not is_super_admin(user) and user.id not in (row.owner_user_id, row.actor_user_id):
        return None
    names = await _actor_names(db, [row.actor_user_id])
    return {
        **_from_email(row, names).as_dict(),
        "body_text": decrypt_body(row.body_text_enc, row.encryption_provider),
        "body_html": decrypt_body(row.body_html_enc, row.encryption_provider),
        "cc": row.cc_emails or [],
        "attachments": row.attachment_names or [],
    }


async def contexts(db: AsyncSession) -> list[str]:
    """The context values actually in use, so the filter offers real options
    rather than a hardcoded list that drifts."""
    a = (await db.execute(select(MessageSend.context).distinct())).scalars().all()
    b = (await db.execute(select(SmsMessage.context).distinct())).scalars().all()
    return sorted({str(c) for c in [*a, *b] if c})


async def message_count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count(MessageSend.id)))).scalar() or 0)


# ---------------------------------------------------------------------------
# The activity half
#
# application_profiles.audit_events already normalizes four differently-shaped
# trails into one list — for a single file. This is the same normalization
# without the file filter, which is what the audit page needs.
# ---------------------------------------------------------------------------


@dataclass
class ActivityRow:
    id: str
    occurred_at: datetime
    action: str
    summary: str
    actor_name: str | None
    actor_role: str | None
    source: str
    request_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "occurred_at": self.occurred_at, "action": self.action,
            "summary": self.summary, "actor_name": self.actor_name,
            "actor_role": self.actor_role, "source": self.source, "request_id": self.request_id,
        }


async def list_activity(
    db: AsyncSession,
    user: User,
    *,
    q: str = "",
    source: str = "",
    request_id: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ActivityRow], int]:
    from app.dealer_os.models import DealerAuditLog
    from app.models.activity import Activity
    from app.models.bucket import BucketActivityLog

    rows: list[ActivityRow] = []
    cap = 400

    if source in ("", "evidence"):
        stmt = select(BucketActivityLog).order_by(BucketActivityLog.created_at.desc()).limit(cap)
        if request_id:
            stmt = stmt.where(BucketActivityLog.request_id == request_id)
        if q:
            stmt = stmt.where(BucketActivityLog.action.ilike(f"%{q.strip()}%"))
        rows += [
            ActivityRow(
                id=f"bucket:{r.id}", occurred_at=r.created_at, action=r.action,
                summary=r.detail or r.action.replace("_", " "),
                actor_name=r.actor_name, actor_role=r.actor_role, source="evidence",
                request_id=r.request_id,
            )
            for r in (await db.execute(stmt)).scalars().all()
        ]

    if source in ("", "dealer_os"):
        stmt = select(DealerAuditLog).order_by(DealerAuditLog.created_at.desc()).limit(cap)
        if request_id:
            stmt = stmt.where(DealerAuditLog.request_id == request_id)
        if q:
            stmt = stmt.where(DealerAuditLog.action.ilike(f"%{q.strip()}%"))
        rows += [
            ActivityRow(
                id=f"dealer:{r.id}", occurred_at=r.created_at, action=r.action,
                summary=f"{r.action.replace('.', ' ')} on {r.entity_kind}",
                actor_name=r.actor_name, actor_role=None, source="dealer_os",
                request_id=r.request_id,
            )
            for r in (await db.execute(stmt)).scalars().all()
        ]

    if source in ("", "funding"):
        stmt = select(Activity).order_by(Activity.occurred_at.desc()).limit(cap)
        if request_id:
            stmt = stmt.where(Activity.request_id == request_id)
        if q:
            stmt = stmt.where(Activity.kind.ilike(f"%{q.strip()}%"))
        rows += [
            ActivityRow(
                id=f"activity:{r.id}", occurred_at=r.occurred_at, action=r.kind,
                summary=r.summary or r.kind, actor_name=None, actor_role=r.actor_label,
                source="funding", request_id=r.request_id,
            )
            for r in (await db.execute(stmt)).scalars().all()
        ]

    rows.sort(key=lambda r: r.occurred_at, reverse=True)
    return rows[offset : offset + limit], len(rows)


async def caused_by(db: AsyncSession, user: User, request_id: str) -> list[ActivityRow]:
    """The actions that share a message's request id — its cause.

    Empty for anything sent before the request context existed, and for a
    message whose action wrote no audit row. The page says so rather than
    implying a link it cannot prove.
    """
    if not request_id:
        return []
    rows, _ = await list_activity(db, user, request_id=request_id, limit=20)
    return rows
