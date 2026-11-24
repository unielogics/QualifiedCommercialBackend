"""Batch delivery for the consented social follow sequence and operator campaigns."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models.email_marketing import MarketingCampaign, MarketingDelivery, MarketingSubscriber
from app.models.message_send import MessageSend
from app.services import email_marketing, marketing_content
from app.services.email.ses_client import ses_configured
from app.services.messaging import outbox
from app.services.prospect_outreach import _lock_suppression_addresses

log = logging.getLogger(__name__)
DAYS = (0, 3, 7)
SEQUENCE = {
    "en": (
        ("Your funding updates from Qualified Commercial",
         "Thanks for signing up for educational emails and lending updates. Follow Qualified Commercial on Instagram or Facebook for short guides on business financing, application preparation and available programs."),
        ("What lenders look for in your bank statements",
         "Start by reviewing your beginning and ending balances, average daily balance, deposits and recurring payments. Understanding those figures can help you prepare for a financing conversation. Follow our pages for more underwriting explainers."),
        ("Keep up with new lending programs",
         "A working capital loan and a real estate refinance serve different needs. Our social pages share program updates and explain the documents that support each type of request. Follow us where you prefer to read lending updates."),
    ),
    "es": (
        ("Sus novedades de financiamiento de Qualified Commercial",
         "Gracias por suscribirse. Siga a Qualified Commercial en Instagram o Facebook para recibir guías sobre financiamiento comercial, preparación de solicitudes y programas disponibles."),
        ("Qué revisan los prestamistas en sus estados de cuenta",
         "Revise los saldos iniciales y finales, el saldo diario promedio, los depósitos y los pagos recurrentes. Entender esas cifras puede ayudarle a preparar una conversación sobre financiamiento. Siga nuestras páginas para más guías."),
        ("Manténgase al día con los programas de financiamiento",
         "Un préstamo de capital de trabajo y un refinanciamiento inmobiliario cubren necesidades distintas. En nuestras redes explicamos los documentos de cada programa y compartimos novedades. Síganos en la plataforma que prefiera."),
    ),
}


def delivery_readiness() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.email_marketing_enabled:
        return False, "Marketing delivery is paused."
    if not ses_configured():
        return False, "Set up the sending email address to deliver queued messages."
    if not settings.prospect_mailing_address.strip():
        return False, "Set a mailing address for marketing emails."
    if not settings.public_api_url.startswith("https://"):
        return False, "Configure the HTTPS unsubscribe address."
    try:
        email_marketing.unsubscribe_token(UUID(int=0))
    except HTTPException:
        return False, "Configure signing for unsubscribe links."
    return True, "Automatic email delivery is enabled."


def compose(subscriber, delivery, campaign, *, context=None):
    language = subscriber.locale
    subject, message = (campaign.subject, campaign.body) if campaign else SEQUENCE[language][delivery.step]
    settings = get_settings()
    token = email_marketing.unsubscribe_token(subscriber.id)
    url = f"{settings.public_api_url.rstrip('/')}/api/v1/public/email-marketing/unsubscribe?token={token}"
    address = settings.prospect_mailing_address.strip()
    rendered = marketing_content.render_message(
        subject=subject, body=message, body_html=campaign.body_html if campaign else None,
        values=context, locale=language, mailing_address=address, unsubscribe_url=url,
        personalize=campaign is not None and campaign.selection is not None,
    )
    return outbox.Draft(
        to=subscriber.email, subject=rendered.subject, body_text=rendered.body_text, body_html=rendered.body_html,
        artifact_locale=language, from_name="Qualified Commercial", secrets=(token,),
        headers={"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    )


async def finish(db, delivery, status, now):
    delivery.status = status
    if status == "sent" and delivery.campaign_id is None and delivery.step < len(DAYS) - 1:
        delay = DAYS[delivery.step + 1] - DAYS[delivery.step]
        await email_marketing.queue_step(db, delivery.subscriber_id, delivery.step + 1, now + timedelta(days=delay))
    await db.commit()


async def dispatch_one(db, identifier: int, now: datetime):
    delivery = await db.scalar(select(MarketingDelivery).where(
        MarketingDelivery.id == identifier, MarketingDelivery.status.in_(("queued", "sending")),
        MarketingDelivery.due_at <= now,
    ).with_for_update(skip_locked=True))
    if delivery is None:
        return
    if delivery.status == "sending":
        # Handle a worker dying after a durable claim. Only an affirmative
        # provider result advances the sequence; an ambiguous handoff stays closed.
        ledger = await db.get(MessageSend, delivery.message_send_id)
        if ledger and ledger.status in {"sent", "delivered"}:
            await finish(db, delivery, "sent", now)
        elif ledger and ledger.status in {"bounced", "complained", "blocked", "failed"}:
            await finish(db, delivery, "failed", now)
        else:
            await finish(db, delivery, "uncertain", now)
        return
    subscriber = await db.get(MarketingSubscriber, delivery.subscriber_id)
    campaign = await db.get(MarketingCampaign, delivery.campaign_id) if delivery.campaign_id else None
    filters = email_marketing.AudienceFilters.model_validate(campaign.filters if campaign else {})
    await _lock_suppression_addresses(db, [subscriber.email])
    eligible = await db.scalar(email_marketing.eligible_ids(filters).where(MarketingSubscriber.id == subscriber.id))
    if eligible is None:
        await finish(db, delivery, "blocked", now)
        return
    context = None
    if campaign is not None and campaign.selection is not None:
        from app.services.email_marketing_directory import recipient_context

        context = await recipient_context(db, subscriber.id, program_key=filters.program)
    draft = compose(subscriber, delivery, campaign, context=context)
    key = f"campaign_{campaign.id}" if campaign else f"social_follow_{delivery.step}"
    ledger = await outbox.record(db, channel="email", status="queued", context="email_marketing",
                                 draft=draft, template_key=key, subject=outbox.Subject(
                                     owner_user_id=campaign.created_by if campaign else None))
    if ledger is None:
        raise RuntimeError("Marketing delivery ledger unavailable.")
    delivery.message_send_id = ledger.id
    delivery.status = "sending"
    delivery.due_at = now + timedelta(hours=1)
    # outbox commits these fields alongside its 'sending' claim before calling SES.
    result = await outbox.deliver_email(db, draft, context="email_marketing", template_key=key,
                                        recorded_row=ledger, durable_handoff=True)
    if result.ok:
        await finish(db, delivery, "sent", datetime.now(UTC))
    else:
        # SES returns an explicit rejection code when it never accepted the
        # envelope. Connection timeouts have no such receipt and must not retry.
        rejection = re.match(r"^send_failed: An error occurred \(([A-Za-z0-9]+)\) when calling the SendRawEmail operation", result.detail)
        if result.detail == "not_configured" or (rejection and rejection[1] in {
            "Throttling", "ThrottlingException", "TooManyRequestsException", "LimitExceededException",
        }):
            delivery.status = "queued"
            delivery.due_at = now + timedelta(minutes=15)
            await db.commit()
        else:
            status = "uncertain" if not rejection and result.row and result.row.status == "uncertain" else "failed"
            await finish(db, delivery, status, now)


async def drain_pending():
    if not delivery_readiness()[0]:
        return
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        ids = list(await db.scalars(select(MarketingDelivery.id).where(
            MarketingDelivery.status.in_(("queued", "sending")), MarketingDelivery.due_at <= now,
        ).order_by(MarketingDelivery.due_at, MarketingDelivery.id).limit(100)))
    for identifier in ids:
        async with SessionLocal() as db:
            try:
                await dispatch_one(db, identifier, now)
            except Exception:
                await db.rollback()
                log.exception("Marketing delivery delayed id=%s", identifier)
                await db.execute(update(MarketingDelivery).where(
                    MarketingDelivery.id == identifier, MarketingDelivery.status == "queued",
                    MarketingDelivery.due_at <= now,
                ).values(due_at=now + timedelta(minutes=5)))
                await db.commit()
