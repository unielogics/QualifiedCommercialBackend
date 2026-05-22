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

from app.config import get_settings

log = logging.getLogger(__name__)


@dataclass
class SesSendResult:
    ok: bool
    message_id: str | None
    detail: str


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
