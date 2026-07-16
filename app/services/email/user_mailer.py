"""Send-as-user email chokepoint.

`send_as_user` is the single decision point for outbound loan/lender/merged mail:
if the sender has a live per-user Gmail grant it sends from THEIR Gmail (message
lands in their Sent folder, replies come back to them); otherwise it falls back
to the firm SES transport. Returns a `SesSendResult` so callers don't branch on
transport.

Attachment shape is normalized to the SES tuple form `(filename, bytes, ctype)`;
this module converts to the Gmail dict form internally.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.email import gmail_client
from app.services.email.ses_client import SesSendResult, send_raw_email
from app.services.google import google_oauth_client
from app.services.google.google_oauth_client import GMAIL_SCOPES

log = logging.getLogger(__name__)


async def _try_gmail_send_as_user(
    db: AsyncSession,
    sender_user_id: uuid.UUID,
    *,
    to_emails: list[str],
    subject: str,
    body_text: str,
    cc_emails: list[str] | None,
    bcc_emails: list[str] | None,
    attachments: list[tuple[str, bytes, str]] | None,
) -> SesSendResult | None:
    """Send via the sender's connected Gmail. Returns a result on success/failure
    of the Gmail path, or None when the user simply isn't connected (so the caller
    falls back to SES)."""
    try:
        creds = await google_oauth_client.credentials_for_user(db, sender_user_id, GMAIL_SCOPES)
    except google_oauth_client.GoogleNotConnected:
        return None
    except google_oauth_client.GoogleScopeMissing:
        return None
    except google_oauth_client.GoogleTokenRevoked:
        return None
    except Exception:  # noqa: BLE001
        log.exception("send_as_user: credential resolution failed user=%s", sender_user_id)
        return None

    account = await google_oauth_client.get_account(db, sender_user_id)
    from_email = account.google_email if account else None

    gmail_attachments = [
        {"filename": fn, "mime_type": ct, "data": data} for (fn, data, ct) in (attachments or [])
    ]

    def _send() -> str | None:
        from googleapiclient.discovery import build

        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        built = gmail_client.build_message(
            to=", ".join(to_emails),
            subject=subject,
            body=body_text,
            from_email=from_email,
            cc=cc_emails,
            bcc=bcc_emails,
            attachments=gmail_attachments,
        )
        resp = svc.users().messages().send(userId="me", body={"raw": built.raw_base64}).execute()
        return resp.get("id")

    try:
        import asyncio

        msg_id = await asyncio.to_thread(_send)
        log.info("send_as_user: sent via Gmail user=%s to=%s id=%s", sender_user_id, to_emails, msg_id)
        return SesSendResult(True, msg_id, "sent_gmail")
    except Exception as exc:  # noqa: BLE001
        # Gmail send genuinely failed (not "not connected") — surface it rather
        # than silently falling back, so the failure is visible.
        log.warning("send_as_user: Gmail send failed user=%s: %s", sender_user_id, exc)
        return SesSendResult(False, None, f"gmail_send_failed: {exc}")


async def send_as_user(
    db: AsyncSession,
    sender_user_id: uuid.UUID | None,
    *,
    to_emails: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    cc_emails: list[str] | None = None,
    bcc_emails: list[str] | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> SesSendResult:
    """Send from the sender's connected Gmail when available, else firm SES.

    - sender_user_id None → straight to SES.
    - Gmail connected → send via their Gmail (bcc supported; body_html is not
      used on the Gmail path, which sends the plain-text body).
    - Not connected → SES send_raw_email (cc + attachments + html supported; no bcc).
    """
    if sender_user_id is not None:
        gmail_result = await _try_gmail_send_as_user(
            db,
            sender_user_id,
            to_emails=to_emails,
            subject=subject,
            body_text=body_text,
            cc_emails=cc_emails,
            bcc_emails=bcc_emails,
            attachments=attachments,
        )
        if gmail_result is not None:
            return gmail_result

    # SES fallback. send_raw_email honors BCC via the SMTP envelope (no Bcc
    # header), so the merged-email audit BCC survives even when the sender has no
    # connected Gmail. The transport is distinguishable by the result detail
    # ("sent" = SES vs "sent_gmail" = user Gmail) so callers attribute accurately.
    import asyncio

    return await asyncio.to_thread(
        send_raw_email,
        to_emails=to_emails,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        cc_emails=cc_emails,
        bcc_emails=bcc_emails,
        attachments=attachments,
    )
