"""Deliver one neutral permission invitation. Marketing enrollment happens only on confirmation."""
from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import literal, select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models.email_marketing import MarketingPermissionRequest
from app.models.message_send import MessageSend
from app.services import email_marketing, email_marketing_directory, marketing_permission_requests
from app.services.email_marketing_delivery import delivery_readiness
from app.services.messaging import outbox

log = logging.getLogger(__name__)
_COPY = {
    "en": (
        "Would you like emails from Qualified Commercial?",
        "Qualified Commercial is asking whether you would like occasional emails with financing guides and lending updates. "
        "The choice is yours and does not affect any financing application.",
        "Review my email preference",
        "You subscribe only if you confirm on the next page. You can also decline there. "
        "If you take no action, this invitation will not subscribe you.",
        "Decline future marketing emails",
    ),
    "es": (
        "¿Desea recibir correos de Qualified Commercial?",
        "Qualified Commercial le pregunta si desea recibir ocasionalmente guías de financiamiento y novedades sobre préstamos. "
        "Su decisión es voluntaria y no afecta ninguna solicitud de financiamiento.",
        "Revisar mi preferencia de correo",
        "Solo se suscribirá si lo confirma en la página siguiente. También puede rechazar la invitación allí. "
        "Si no hace nada, esta invitación no le suscribirá.",
        "Rechazar futuros correos de marketing",
    ),
}
_PENDING = ("queued", "sending")
_REJECTION = re.compile(r"^send_failed: An error occurred \(([A-Za-z0-9]+)\) when calling the SendRawEmail operation")


def compose(row) -> outbox.Draft:
    locale = "es" if row.locale == "es" else "en"
    subject, question, action, explanation, decline = _COPY[locale]
    token = marketing_permission_requests.permission_token(row.id)
    link = marketing_permission_requests.request_link(token)
    stop = f"{link}/decline"
    address = get_settings().prospect_mailing_address.strip()
    if not address or not link.startswith("https://"):
        raise ValueError("A physical mailing address and HTTPS permission link are required.")
    text = f"{question}\n\n{action}: {link}\n\n{explanation}\n\nQualified Commercial · {address}\n{decline}: {stop}"
    markup = (f'<div lang="{locale}"><p>{question}</p><p><a href="{html.escape(link, quote=True)}">{action}</a></p>'
              f'<p>{explanation}</p><hr><p>Qualified Commercial · {html.escape(address)}<br>'
              f'<a href="{html.escape(stop, quote=True)}">{decline}</a></p></div>')
    return outbox.Draft(to=row.email, subject=subject, body_text=text, body_html=markup, artifact_locale=locale,
                        headers={"List-Unsubscribe": f"<{stop}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
                        from_name="Qualified Commercial", secrets=(token,))


def _ready(now):
    table = MarketingPermissionRequest
    return table.status.in_(_PENDING) & table.invite_batch_id.is_not(None) & (table.due_at <= now)


async def _finish(db, row, status, now):
    await marketing_permission_requests.close_request(db, row, status, now)
    await db.commit()


def _outcome(result) -> str:
    if result.ok:
        return "sent"
    rejection = _REJECTION.match(result.detail)
    if result.detail == "not_configured" or (rejection and rejection[1] in {
        "Throttling", "ThrottlingException", "TooManyRequestsException", "LimitExceededException",
    }):
        return "queued"  # An affirmative refusal; the provider did not accept the email.
    return "failed" if rejection else "uncertain"


async def dispatch_one(db, identifier: UUID, now: datetime):
    table = MarketingPermissionRequest
    row = await db.scalar(select(table).where(table.id == identifier, _ready(now))
                          .with_for_update(skip_locked=True))
    if row is None:
        return
    expiry = row.expires_at
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry is None or expiry <= now:
        await _finish(db, row, "expired", now)
        return
    if row.status == "sending":
        ledger = await db.get(MessageSend, row.message_send_id) if row.message_send_id else None
        state = "uncertain"
        if ledger and ledger.status in {"sent", "delivered"}:
            state = "sent"
        elif ledger and ledger.status in {"failed", "blocked", "bounced", "complained"}:
            state = "failed"
        await _finish(db, row, state, now)
        return
    # Match confirmation and suppression writers' lock order before the final address-level check.
    await marketing_permission_requests._lock_address(db, row.email)
    if not await email_marketing_directory.requestable_contact(db, row.email, ignore_prior_request=True):
        state = "blocked"
        if await marketing_permission_requests.has_consent(db, row.email):
            hold = await db.scalar(select(email_marketing.contact_hold_reason(literal(row.email))))
            if hold is None:
                state = "confirmed"
        await _finish(db, row, state, now)
        return
    draft = compose(row)
    context = outbox.PERMISSION_REQUEST_CONTEXT
    key = "marketing_permission_invitation"
    ledger = await outbox.record(db, channel="email", status="queued", context=context, draft=draft,
                                 template_key=key, subject=outbox.Subject(owner_user_id=row.created_by))
    if ledger is None:
        raise RuntimeError("Permission invitation could not be recorded.")
    row.message_send_id = ledger.id
    row.status = "sending"
    row.due_at = now + timedelta(hours=1)
    result = await outbox.deliver_email(db, draft, context=context, template_key=key,
                                        recorded_row=ledger, durable_handoff=True)
    # A recipient can respond as soon as the provider accepts. Do not overwrite that choice.
    row = await db.scalar(select(table).where(table.id == identifier, table.status == "sending",
        table.message_send_id == ledger.id).with_for_update().execution_options(populate_existing=True))
    if row is not None:
        status = _outcome(result)
        if status == "queued":
            row.due_at = now + timedelta(minutes=15)
        await _finish(db, row, status, now)


async def drain_pending():
    now = datetime.now(UTC)
    if not delivery_readiness()[0]:
        return
    table = MarketingPermissionRequest
    async with SessionLocal() as db:
        ids = list(await db.scalars(select(table.id).where(_ready(now))
                                    .order_by(table.due_at, table.id).limit(100)))
    for identifier in ids:
        async with SessionLocal() as db:
            try:
                await dispatch_one(db, identifier, now)
            except Exception:
                await db.rollback()
                log.exception("Permission invitation delayed id=%s", identifier)
                await db.execute(update(table).where(table.id == identifier, table.status == "queued",
                    table.due_at <= now).values(due_at=now + timedelta(minutes=5)))
                await db.commit()
