"""AWS SES email transport — used by the AI re-engagement engine.

Distinct from `gmail_client.py` (operational lender mail, domain-wide
delegation). SES is the right tool for nurture-grade re-engagement
email: a dedicated sending subdomain, DKIM/SPF, bounce/complaint
handling, and an auto-send path that doesn't go through the
operator-approval EmailDraft queue.

Auth is the EC2 instance role — no keys in the env; the role needs
`ses:SendEmail` / `ses:SendRawEmail`.

Dormant by design: when `settings.ses_from_address` is empty, `send()`
returns a not-configured result and the caller logs + moves on. The
re-engagement engine keeps running; the email rung is just a no-op
until SES is provisioned. Same pattern as APNs / Gmail Pub/Sub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from app.config import get_settings

log = logging.getLogger(__name__)


@dataclass
class SesSendResult:
    ok: bool
    message_id: str | None
    detail: str

    @property
    def error(self) -> str | None:
        """The failure detail when the send did not succeed, else None.
        Callers persist result.error as the send's error column; without this
        property those reads raised AttributeError and 500'd the endpoint."""
        return None if self.ok else self.detail


def ses_configured() -> bool:
    """True when SES has a verified From address configured."""
    return bool(get_settings().ses_from_address.strip())


def send_email(
    *,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> SesSendResult:
    """Send one email via SES. Never raises — returns SesSendResult so
    the caller (the re-engagement engine) can record the outcome and
    continue the batch.

    Returns ok=False with detail='not_configured' when SES has no
    From address yet (dormant)."""
    settings = get_settings()
    from_addr = settings.ses_from_address.strip()
    if not from_addr:
        return SesSendResult(False, None, "not_configured")
    to = (to_email or "").strip()
    if not to or "@" not in to:
        return SesSendResult(False, None, f"bad recipient: {to_email!r}")

    try:
        import boto3  # local import — keeps module import cheap

        client = boto3.client("ses", region_name=settings.ses_region or "us-east-1")
        body: dict = {"Text": {"Data": body_text, "Charset": "UTF-8"}}
        if body_html:
            body["Html"] = {"Data": body_html, "Charset": "UTF-8"}
        kwargs: dict = {
            "Source": from_addr,
            "Destination": {"ToAddresses": [to]},
            "Message": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": body,
            },
        }
        cfg_set = settings.ses_configuration_set.strip()
        if cfg_set:
            kwargs["ConfigurationSetName"] = cfg_set
        resp = client.send_email(**kwargs)
        msg_id = resp.get("MessageId")
        log.info("ses_client: sent to=%s message_id=%s", to, msg_id)
        return SesSendResult(True, msg_id, "sent")
    except Exception as exc:  # noqa: BLE001
        log.warning("ses_client: send failed to=%s: %s", to, exc)
        return SesSendResult(False, None, f"send_failed: {exc}")


def send_raw_email(
    *,
    to_emails: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    cc_emails: list[str] | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> SesSendResult:
    """Send a MIME email through SES.

    Attachments are tuples of (filename, bytes, content_type). This keeps
    generated underwriting packets out of ad-hoc base64 code at call sites.
    """
    settings = get_settings()
    from_addr = settings.ses_from_address.strip()
    if not from_addr:
        return SesSendResult(False, None, "not_configured")
    recipients = [email.strip() for email in to_emails if email and "@" in email]
    cc = [email.strip() for email in (cc_emails or []) if email and "@" in email]
    if not recipients:
        return SesSendResult(False, None, "bad recipients")

    try:
        import boto3  # local import — keeps module import cheap

        msg = EmailMessage()
        msg["From"] = formataddr(("Qualified Commercial", from_addr))
        msg["To"] = ", ".join(recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        for filename, content, content_type in attachments or []:
            main_type, _, sub_type = (content_type or "application/octet-stream").partition("/")
            msg.add_attachment(
                content,
                maintype=main_type or "application",
                subtype=sub_type or "octet-stream",
                filename=filename,
            )

        client = boto3.client("ses", region_name=settings.ses_region or "us-east-1")
        kwargs: dict = {
            "Source": from_addr,
            "Destinations": recipients + cc,
            "RawMessage": {"Data": msg.as_bytes()},
        }
        cfg_set = settings.ses_configuration_set.strip()
        if cfg_set:
            kwargs["ConfigurationSetName"] = cfg_set
        resp = client.send_raw_email(**kwargs)
        msg_id = resp.get("MessageId")
        log.info("ses_client: raw sent to=%s cc=%s message_id=%s", recipients, cc, msg_id)
        return SesSendResult(True, msg_id, "sent")
    except Exception as exc:  # noqa: BLE001
        log.warning("ses_client: raw send failed to=%s cc=%s: %s", recipients, cc, exc)
        return SesSendResult(False, None, f"send_failed: {exc}")
