"""Staff follow-ups and opt-in invitations. Only the recipient's POST grants consent."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from sqlalchemy import literal, select
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.models.activity import Activity
from app.models.dealer_prospect import DealerProspect
from app.models.email_marketing import (
    MarketingEnrollment,
    MarketingPermissionBatch,
    MarketingPermissionRequest,
    MarketingSubscriber,
)
from app.models.event import CalendarEvent
from app.schemas.email_marketing_audience import RecipientSelection
from app.services import email_marketing as marketing
from app.services import email_marketing_directory as directory
from app.services.prospect_outreach import (
    _lock_suppression_addresses,
    is_suppressed,
    set_suppression,
)

REVIEW_PATH = "/admin/email-marketing?eligibility=requestable"
OPEN_INVITES = ("queued", "sending", "sent", "uncertain", "failed")


class PermissionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    action: Literal["create_tasks", "send_requests"]
    expected_recipient_count: int = Field(gt=0, strict=True)
    confirmed: StrictBool
    expected_by_contacts: StrictBool = False
    filters: marketing.AudienceFilters = Field(default_factory=marketing.AudienceFilters)
    selection: RecipientSelection
    locale: Literal["en", "es"] = "en"

    @model_validator(mode="after")
    def require_confirmation(self):
        if not self.confirmed or (self.action == "send_requests" and not self.expected_by_contacts):
            raise ValueError("Confirm the action and that selected contacts expect an invitation before emailing.")
        return self


def permission_token(identifier: UUID):
    message = f"permission-invitation-v1:{identifier.hex}".encode()
    signature = hmac.new(marketing._secret(), message, hashlib.sha256).hexdigest()
    return f"{identifier.hex}.{signature}"


def checked_id(token: str) -> UUID:
    try:
        raw, _ = token.split(".", 1)
        identifier = UUID(hex=raw)
        if not hmac.compare_digest(token.encode(), permission_token(identifier).encode()):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(404, "Invalid invitation link.") from None
    return identifier


def request_link(token: str):
    return f"{get_settings().public_api_url.rstrip('/')}/api/v1/public/email-marketing/permission/{token}"


async def _lock_address(db, email):
    await db.execute(select(DealerProspect.id).where(DealerProspect.email_normalized == email)
                     .order_by(DealerProspect.id).with_for_update())
    await _lock_suppression_addresses(db, [email])


async def activate(db, actor_id, action: PermissionAction):
    from app.services.marketing_permission_tasks import ensure_task

    now = datetime.now(UTC)
    fingerprint = hashlib.sha256(json.dumps(action.model_dump(mode="json", exclude={"id", "expected_recipient_count"}),
                                 sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    batch = await db.scalar(insert(MarketingPermissionBatch).values(id=action.id, created_by=actor_id,
        action=action.action, fingerprint=fingerprint, task_count=0, requested_count=0).on_conflict_do_nothing(
        index_elements=["id"]).returning(MarketingPermissionBatch))
    if batch is None:
        batch = await db.get(MarketingPermissionBatch, action.id)
        if batch.fingerprint != fingerprint or batch.created_by != actor_id:
            raise HTTPException(409, "This review identifier has already been used.")
        return {"id": batch.id, "requested_count": batch.requested_count, "task_count": batch.task_count,
                "already_queued_count": batch.requested_count}
    contacts = directory.selected_contacts(action.filters, action.selection).subquery()
    rows = (await db.execute(select(contacts.c.email, contacts.c.locale).where(
        contacts.c.selected.is_(True), contacts.c.status == "requestable").order_by(contacts.c.email))).all()
    if len(rows) != action.expected_recipient_count:
        await db.rollback()
        raise HTTPException(409, "The requestable audience changed. Review the updated count.")
    sending = action.action == "send_requests"
    for address, locale in rows:
        language = locale if locale in {"en", "es"} else action.locale
        row = await db.scalar(insert(MarketingPermissionRequest).values(id=uuid4(), email=address,
            locale=language,
            created_by=actor_id, batch_id=batch.id, status="task_only", due_at=now)
            .on_conflict_do_nothing(index_elements=["email"]).returning(MarketingPermissionRequest))
        if row is None:
            row = await db.scalar(select(MarketingPermissionRequest).where(
                MarketingPermissionRequest.email == address).with_for_update())
        if row.status != "task_only" or row.invite_batch_id is not None:
            await db.rollback()
            raise HTTPException(409, "These contacts have already been reviewed. Refresh the directory.")
        # Recheck at the provider boundary as well. The bulk query above evaluates
        # presence, grant, suppression and eligibility without one query per address.
        await ensure_task(db, row, now, reopen=sending)
        if sending:
            row.locale = language
            row.status = "queued"
            row.invite_batch_id = batch.id
            row.expires_at = now + timedelta(days=30)
            row.due_at = now
    batch.task_count = len(rows)
    batch.requested_count = len(rows) if sending else 0
    db.add(Activity(actor_id=actor_id, actor_label="super_admin", kind="email_marketing.permission_action",
        summary="Queued expected permission invitations" if sending else "Created internal permission follow-up tasks",
        payload={"batch_id": str(batch.id), "action": batch.action, "expected_by_contacts": action.expected_by_contacts,
                 "task_count": batch.task_count, "requested_count": batch.requested_count}))
    await db.commit()
    return {"id": batch.id, "requested_count": batch.requested_count, "task_count": batch.task_count, "already_queued_count": 0}


async def close_request(db, row, status: str, now: datetime):
    from app.services.marketing_permission_tasks import resolve_task

    row.status = status
    if status in {"confirmed", "declined", "blocked", "expired"}:
        row.resolved_at = now
        await resolve_task(db, row, now)


async def has_consent(db, email: str):
    return bool(await db.scalar(select(MarketingEnrollment.id).join(MarketingSubscriber).where(
        MarketingSubscriber.email == email, MarketingSubscriber.unsubscribed_at.is_(None)).limit(1)))


async def active_consent(db, email: str):
    return await has_consent(db, email) and await db.scalar(select(marketing.contact_hold_reason(literal(email)))) is None


async def invitation(db, token, *, lock=False):
    query = select(MarketingPermissionRequest).where(MarketingPermissionRequest.id == checked_id(token),
                                                    MarketingPermissionRequest.invite_batch_id.is_not(None))
    row = await db.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if row is None:
        raise HTTPException(404, "Invalid invitation link.")
    return row


async def respond(db, token, *, agree):
    row = await invitation(db, token, lock=True)
    now = datetime.now(UTC)
    await _lock_address(db, row.email)
    if not agree:
        if row.status != "declined":
            if await is_suppressed(db, row.email) is None:
                await set_suppression(db, email=row.email, reason="unsubscribe", source="permission_invitation")
            subscriber = await db.scalar(select(MarketingSubscriber).where(MarketingSubscriber.email == row.email))
            if subscriber is not None:
                subscriber.unsubscribed_at = subscriber.unsubscribed_at or now
            await close_request(db, row, "declined", now)
        await db.commit()
        return "declined"
    if row.status == "confirmed":
        if await active_consent(db, row.email):
            return "confirmed"
        raise HTTPException(410, "This permission invitation is no longer available.")
    if row.status not in {"sent", "sending", "uncertain"} or not row.expires_at or marketing._utc_instant(row.expires_at) <= now:
        raise HTTPException(410, "This permission invitation is no longer available.")
    if not await directory.requestable_contact(db, row.email, ignore_prior_request=True):
        if await active_consent(db, row.email):
            await close_request(db, row, "confirmed", now)
            await db.commit()
            return "confirmed"
        await close_request(db, row, "blocked", now)
        await db.commit()
        raise HTTPException(410, "This permission invitation is no longer available.")
    await marketing.enroll(db, email=row.email, consent=True, source="permission_confirmation", locale=row.locale)
    if not await has_consent(db, row.email):
        await close_request(db, row, "blocked", now)
        await db.commit()
        raise HTTPException(410, "This permission invitation is no longer available.")
    await close_request(db, row, "confirmed", now)
    db.add(Activity(actor_label="email_recipient", kind="email_marketing.permission_confirmed",
                    summary="Recipient confirmed permission for educational emails and lending updates",
                    payload={"request_id": str(row.id), "disclosure_version": marketing.CONSENT_VERSION,
                             "locale": row.locale}))
    await db.commit()
    return "confirmed"


async def list_requests(db, limit):
    rows = (await db.execute(select(MarketingPermissionRequest, CalendarEvent).outerjoin(
        CalendarEvent, MarketingPermissionRequest.task_event_id == CalendarEvent.id).order_by(
        MarketingPermissionRequest.created_at.desc(), MarketingPermissionRequest.id).limit(limit))).all()
    return {"rows": [{"id": row.id, "email": row.email, "locale": row.locale, "status": row.status,
                      "created_at": row.created_at, "expires_at": row.expires_at, "task_event_id": row.task_event_id,
                      "reminder_at": event.starts_at if event else None, "task_status": event.status if event else None}
                     for row, event in rows]}
