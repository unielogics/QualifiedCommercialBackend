"""The one door every outbound email goes through.

Before this, eleven send paths recorded nothing at all and nine more recorded
that something went without recording what it said. There was no equivalent of
the SMS ledger, so "what did we send them?" had no answer.

The contract is the one `send_sms_checked` has kept since 0169: **a row either
way**. A refused, failed or blocked send is a row with a reason, not an absence.
An audit page whose only evidence is the successes is not an audit page.

Order matters here. The body is masked before it is encrypted and encrypted
before it is stored, and the row is written before the transport is called —
so a send that crashes mid-flight still leaves a record that it was attempted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import request_context
from app.models.message_send import MessageSend
from app.services.email.user_inbox_sync import _encrypt_body
from app.services.messaging.redact import mask_all

log = logging.getLogger(__name__)


@dataclass
class SendOutcome:
    ok: bool
    detail: str
    message_id: str | None = None
    #: The ledger row. None only if the ledger write itself failed, which never
    #: blocks the send.
    row: Any = None

    @property
    def error(self) -> str | None:
        return None if self.ok else self.detail


@dataclass
class Subject:
    """Who the message is about, and who on the desk owns it.

    Ownership is what the audit page filters on: an operator sees their own,
    a super admin sees everything. A message with no owner belongs to nobody
    and is shown to super admins alone.
    """

    owner_user_id: Any = None
    client_id: Any = None
    profile_id: Any = None
    dealer_id: Any = None
    loan_id: Any = None
    intake_id: Any = None

    def as_columns(self) -> dict[str, Any]:
        return {
            "owner_user_id": self.owner_user_id,
            "client_id": self.client_id,
            "profile_id": self.profile_id,
            "dealer_id": self.dealer_id,
            "loan_id": self.loan_id,
            "intake_id": self.intake_id,
        }


@dataclass
class Draft:
    """What is about to be sent."""

    to: str
    subject: str
    body_text: str
    body_html: str | None = None
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    attachments: list[tuple] = field(default_factory=list)
    #: Credentials this message carries — the token or PIN the caller just
    #: minted. Declared secrets are removed by exact match, which is the only
    #: layer that cannot miss.
    secrets: tuple = ()


async def record(
    db: AsyncSession,
    *,
    channel: str,
    status: str,
    draft: Draft | None = None,
    to_phone: str | None = None,
    context: str,
    template_key: str | None = None,
    provider: str = "",
    provider_message_id: str | None = None,
    detail: str = "",
    subject: Subject | None = None,
) -> MessageSend | None:
    """Write one ledger row. Never raises — a logging failure must not be the
    reason a message fails to send."""
    ctx = request_context.current()
    subj = subject or Subject()
    try:
        text_enc = html_enc = None
        provider_name = "fernet"
        masked = False
        row_subject = None
        cc = None
        attachments = None
        if draft is not None:
            (text, html), hits = mask_all(draft.body_text, draft.body_html, known=draft.secrets)
            masked = bool(hits)
            text_enc, provider_name = _encrypt_body(text)
            html_enc, _ = _encrypt_body(html)
            row_subject = (draft.subject or "")[:512]
            cc = list(draft.cc) or None
            attachments = [a[0] for a in draft.attachments] or None

        row = MessageSend(
            channel=channel,
            direction="outbound",
            context=(context or "")[:48],
            template_key=(template_key or None),
            to_email=(draft.to[:320] if draft else None),
            to_phone=to_phone,
            cc_emails=cc,
            subject=row_subject,
            body_text_enc=text_enc,
            body_html_enc=html_enc,
            encryption_provider=provider_name,
            secrets_masked=masked,
            attachment_names=attachments,
            provider=provider[:24],
            provider_message_id=(provider_message_id or None),
            status=status,
            detail=(detail or "")[:500],
            actor_user_id=ctx.actor_user_id,
            actor_label=(ctx.actor_label or "system")[:24],
            request_id=ctx.request_id or None,
            job=(ctx.job or None),
            **_owned(subj, ctx),
        )
        db.add(row)
        await db.flush()
        return row
    except Exception:  # noqa: BLE001
        log.exception(
            "message ledger write failed channel=%s status=%s context=%s — send outcome unaffected",
            channel, status, context,
        )
        return None


def _owned(subject: Subject, ctx) -> dict[str, Any]:
    """Ownership falls to the person who sent it; failing that, to whoever the
    subject file belongs to. Neither means nobody, and nobody means super
    admins only."""
    columns = subject.as_columns()
    if columns["owner_user_id"] is None and ctx.actor_label not in ("cron", "system", "public"):
        columns["owner_user_id"] = ctx.actor_user_id
    return columns


async def deliver_email(
    db: AsyncSession,
    draft: Draft,
    *,
    context: str,
    template_key: str | None = None,
    subject: Subject | None = None,
    sender_user_id: Any = None,
) -> SendOutcome:
    """Send one email and record it, whatever happens.

    `sender_user_id` routes through that person's connected Gmail when they
    have one, falling back to firm SES — the existing `send_as_user` behaviour.
    Without it the message goes from the firm address.
    """
    to = (draft.to or "").strip()
    if not to or "@" not in to:
        row = await record(
            db, channel="email", status="blocked", draft=draft, context=context,
            template_key=template_key, detail=f"bad recipient: {draft.to!r}", subject=subject,
        )
        return SendOutcome(False, "bad recipient", None, row)

    # The row goes in first, so a transport that dies mid-call still leaves
    # evidence that we tried.
    row = await record(
        db, channel="email", status="queued", draft=draft, context=context,
        template_key=template_key, subject=subject,
    )

    provider, message_id, ok, detail = "ses", None, False, ""
    try:
        if sender_user_id is not None:
            from app.services.email.user_mailer import send_as_user

            result = await send_as_user(
                db,
                sender_user_id,
                to_emails=[to],
                subject=draft.subject,
                body_text=draft.body_text,
                body_html=draft.body_html,
                cc_emails=list(draft.cc),
                bcc_emails=list(draft.bcc),
                attachments=list(draft.attachments),
            )
            ok, message_id, detail = result.ok, result.message_id, result.detail
            provider = "gmail" if detail == "sent_gmail" else "ses"
        else:
            from app.services.email import ses_client

            if draft.cc or draft.bcc or draft.attachments or draft.body_html:
                result = ses_client.send_raw_email(
                    to_emails=[to],
                    subject=draft.subject,
                    body_text=draft.body_text,
                    body_html=draft.body_html,
                    cc_emails=list(draft.cc),
                    bcc_emails=list(draft.bcc),
                    attachments=list(draft.attachments),
                )
            else:
                result = ses_client.send_email(
                    to_email=to, subject=draft.subject, body_text=draft.body_text
                )
            ok, message_id, detail = result.ok, result.message_id, result.detail
    except Exception as exc:  # noqa: BLE001
        log.exception("email send raised context=%s", context)
        ok, detail = False, f"send_failed: {exc}"

    if row is not None:
        row.provider = provider
        row.provider_message_id = message_id
        row.status = "sent" if ok else "failed"
        row.detail = (detail or "")[:500]
        if not ok:
            row.failed_at = datetime.now(UTC)
        await db.flush()
    return SendOutcome(ok, detail, message_id, row)


#: SES event types mapped onto the ledger's vocabulary. `Send` and `Reject` are
#: about the API call, which the row already records, so they are ignored.
SES_EVENT_STATUS = {
    "Delivery": "delivered",
    "Bounce": "bounced",
    "Complaint": "complained",
    "Open": "opened",
}


async def mark_delivery(
    db: AsyncSession, *, provider_message_id: str, event: str, detail: str = ""
) -> bool:
    """Advance a row on an SES event.

    Mirrors the SMS ledger's rule and extends it: events arrive out of order, so
    a late `Delivery` must not undo a `Bounce`. A bounce or complaint is
    terminal — it can only be learned after the fact and it is the truer answer.
    An open is a hint, not a state, so it dates the row without changing it.
    """
    status = SES_EVENT_STATUS.get(event)
    if not provider_message_id or not status:
        return False
    row = (
        await db.execute(
            select(MessageSend).where(
                MessageSend.provider_message_id == provider_message_id,
                MessageSend.direction == "outbound",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        log.info("message ledger: %s event for unknown id %s", event, provider_message_id)
        return False

    now = datetime.now(UTC)
    if status == "opened":
        # Image-blocking makes an unopened message indistinguishable from an
        # unloaded pixel, so this never becomes the row's status.
        row.opened_at = row.opened_at or now
    elif status == "delivered":
        if row.status not in ("bounced", "complained"):
            row.status = "delivered"
            row.delivered_at = now
    else:
        row.status = status
        row.failed_at = now
        if detail:
            row.detail = detail[:500]
    await db.flush()
    return True
